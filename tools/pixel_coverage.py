"""GT 矩形が実際に画素として塗られているかを測る（issue #7）。

これまでの「被覆」は `covered = sum(1 for f in range(n_frames) if out[f])`
（`temporal.py`）のように、**そのフレームに矩形が1個でもあるか**しか見ていなかった。
矩形が対象の上に乗っているかは判定に入っていない。

ここでは `docs/11-coverage-vs-leak-report.md` で実測・校正した「モザイクの構造
そのものを見る基準」を、任意の GT（矩形つきフレーム）に対して再利用できる形にする。

## 原理

`render.pixelize_plane` はブロック格子にスナップして各ブロックを平均色1色に
潰す（不可逆）ので、**モザイク領域はブロック内分散がほぼゼロになる**。
元動画と出力動画を同じ格子で比較し、「元セルの std が高いのに出力セルの std が
大きく下がっている」セルを「潰れた（＝塗られた）セル」と判定する。

差分ではなく構造を見るのは、`RULES.md` 2.1 に実測が書いてある通り、
crf 再エンコードの粒状ノイズで平均絶対差が塗装/未塗装で重なってしまうため。

## 校正は素材ごとにやり直すこと

`RULES.md` 2.1: 「既知の基準を借りてこない。素材が変われば分離も変わる」。
docs/11 は 1920x1080 素材で 20px グリッドを使ったが、これは
`render.default_block_size(1920) == 20` にほぼ一致していたための選択だった。

このツールを実際に 640x480 素材（`data/myvideo5`）に適用したところ、
`default_block_size(640) == 6` は実際にその動画を焼いたブロックサイズ
（実測 12px）と一致しなかった。**動画が実際にどの `--block` で焼かれたかは
呼び出し側の記録が無ければ分からない。** そのため `--cell` を省略した場合、
理論値には頼らず出力動画の画素から実測する（`detect_block_size`）。

`--std-min` / `--ratio-max` / `--min-cells` は動画ごとに `--calibrate` で
分布を測ってから決めること。既定値は docs/11 の 1080p 校正をそのまま使って
いるが、640x480 素材ではこの既定値のままだと測定可能面積が大きく下がる
ことを実測で確認している（`docs/12-pixel-coverage-tool.md` 参照）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic import corrections as corr  # noqa: E402
from automosaic import video as vid  # noqa: E402
from automosaic.render import default_block_size  # noqa: E402

Box = tuple[float, float, float, float]


# ---------------------------------------------------------------------------
# 画素構造の判定（コアロジック）
# ---------------------------------------------------------------------------


def cell_std_grid(frame: np.ndarray, cell: int) -> np.ndarray:
    """フレームを cell x cell の格子に切り、各セルの標準偏差を返す。

    格子はフレーム座標 (0, 0) を起点に固定する。`render._snap` が使う格子と
    同じ起点なので、`cell == 実際のブロックサイズ` を渡せばセル境界が
    モザイクのブロック境界と一致する。
    """
    h, w = frame.shape
    nh, nw = h // cell, w // cell
    if nh == 0 or nw == 0:
        return np.zeros((0, 0), dtype=np.float64)
    cropped = frame[: nh * cell, : nw * cell].astype(np.float64)
    blocks = cropped.reshape(nh, cell, nw, cell).swapaxes(1, 2)
    return blocks.std(axis=(2, 3))


def detect_block_size(
    frame: np.ndarray, min_gap: int = 4, max_gap: int = 64, edge_thresh: float = 2.0
) -> int | None:
    """出力フレームの画素から、実際のモザイクブロックサイズを実測する。

    `render.default_block_size(long_edge)` は「その動画がどう焼かれたか」を
    知らないと信用できない（このツールの開発中、実際にこれで踏んだ:
    `default_block_size(640) == 6` を信じたところ、実際にこの素材を焼いた
    ブロックは 12px だった。原因はコードの計算式ではなく、**その動画が
    実際にどの `--block` で焼かれたかは呼び出し側の記録に無ければ分からない**
    こと）。焼いた本人に聞く代わりに、出力画素の色が変わる境界の間隔を数える。

    横方向の隣接画素差が閾値を超える位置（＝ブロック境界）を全行で拾い、
    境界間の間隔の最頻値をブロックサイズとする。

    **「モザイクが無いフレームを渡すと検出できない（None を返す）」は誤り。**
    実素材（1920x1080、crf16）のモザイク無しコントロールで実測したところ、
    edges が全く出ないことは無く、**むしろ min_gap=4 の下限に張り付いた 4px を
    自信満々に返した**（実測: 300/300 フレームで 4px、None は 0 件）。自然な
    肌・布のテクスチャや圧縮ブロックノイズが、4px 間隔のエッジを大量に作る
    ため。呼び出し側は戻り値が None かどうかでは「モザイクの有無」を判定でき
    ない。`detect_block_size_over_frames` / このモジュールの CLI が行っている
    ように、**`render.default_block_size` の理論値との食い違いと、サンプル
    フレーム間のばらつきを見て警告する**しかない。
    """
    if frame.ndim != 2:
        raise ValueError("gray フレームのみ対応")
    diffs = np.abs(np.diff(frame.astype(np.int16), axis=1))
    gaps: list[int] = []
    for row in diffs:
        edges = np.where(row > edge_thresh)[0]
        if len(edges) > 1:
            gaps.extend(np.diff(edges).tolist())
    if not gaps:
        return None
    arr = np.array(gaps)
    arr = arr[(arr >= min_gap) & (arr <= max_gap)]
    if len(arr) == 0:
        return None
    vals, counts = np.unique(arr, return_counts=True)
    return int(vals[np.argmax(counts)])


def detect_block_size_per_frame(frames: list[np.ndarray]) -> list[int | None]:
    """各フレームに `detect_block_size` を適用した結果を集約せずそのまま返す。

    `detect_block_size_over_frames` の中央値だけを見ると、フレーム間で値が
    大きく割れていること（構図次第で外れが混ざっていること）が隠れる。
    呼び出し側で割れを検出して警告するために、生の値を返す関数を分けた。
    """
    return [detect_block_size(f) for f in frames]


def detect_block_size_over_frames(frames: list[np.ndarray]) -> int | None:
    """複数フレームで `detect_block_size` を実行し、中央値を返す。

    1フレームだけだと構図次第で外れることがあるので、複数フレームの
    中央値を取ってロバストにする。**ただし中央値を取っても外れ値の影響が
    消えるとは限らない**（実測: 1920x1080 の実素材で真のブロックサイズ 20px
    に対し4サンプル点中3点で中央値が 4px になった。詳細は `detect_block_size`
    のdocstring）。呼び出し側は返り値を鵜呑みにせず、
    `detect_block_size_per_frame` でフレームごとの値のばらつきと
    `render.default_block_size` の理論値との食い違いを確認すること。
    """
    detected = [b for b in detect_block_size_per_frame(frames) if b is not None]
    if not detected:
        return None
    return int(np.median(detected))


def cell_analysis(
    source_frame: np.ndarray,
    output_frame: np.ndarray,
    cell: int,
    std_min: float,
    ratio_max: float,
) -> tuple[np.ndarray, np.ndarray]:
    """(eligible, collapsed) の2つの格子を返す。

    eligible: 元セルに std_min 以上の分散があり、そもそも判定可能なセル。
    collapsed: そのうち出力セルの分散が元の ratio_max 倍未満に潰れているセル
      （＝実際に塗られたセル）。

    元がもともと平坦な場所（std_min 未満）は eligible に入らない。そこは
    モザイクを掛けても掛けなくても分散が変わらないので判定できない
    （docs/11 が「測定の盲点」として記録した唯一の外れ方）。
    """
    s_std = cell_std_grid(source_frame, cell)
    o_std = cell_std_grid(output_frame, cell)
    eligible = s_std >= std_min
    collapsed = eligible & (o_std < ratio_max * s_std)
    return eligible, collapsed


def collapsed_cells(
    source_frame: np.ndarray,
    output_frame: np.ndarray,
    cell: int,
    std_min: float,
    ratio_max: float,
) -> np.ndarray:
    """`cell_analysis` の collapsed だけを返す薄いラッパー（後方互換用）。"""
    _, collapsed = cell_analysis(source_frame, output_frame, cell, std_min, ratio_max)
    return collapsed


def frame_is_painted(collapsed: np.ndarray, min_cells: int) -> bool:
    return int(collapsed.sum()) >= min_cells


def box_mask_fraction(box: Box, mask: np.ndarray, cell: int) -> float:
    """box のうち、mask が True のセルに覆われている面積の割合。

    `tools/verify_output.py` の `_covered_fraction` は矩形同士の幾何重なりを
    16x16 の標本点で近似する。ここは実際の出力画素の構造（潰れているか）を
    見るので、"矩形が計算上そこにある" ではなく "実際に塗り潰されている" を測る。
    セルと box の重なり面積そのもので重み付けするので標本誤差は無い。
    """
    nh, nw = mask.shape
    x, y, bw, bh = box
    if bw <= 0 or bh <= 0 or nh == 0 or nw == 0:
        return 1.0
    x0, y0, x1, y1 = x, y, x + bw, y + bh
    total_area = bw * bh
    i0 = max(0, int(x0 // cell))
    i1 = min(nw - 1, int((x1 - 1e-9) // cell))
    j0 = max(0, int(y0 // cell))
    j1 = min(nh - 1, int((y1 - 1e-9) // cell))
    if i1 < i0 or j1 < j0:
        return 0.0
    covered_area = 0.0
    for j in range(j0, j1 + 1):
        for i in range(i0, i1 + 1):
            if not mask[j, i]:
                continue
            cx0, cy0 = i * cell, j * cell
            cx1, cy1 = cx0 + cell, cy0 + cell
            ox0, oy0 = max(x0, cx0), max(y0, cy0)
            ox1, oy1 = min(x1, cx1), min(y1, cy1)
            ow, oh = max(0.0, ox1 - ox0), max(0.0, oy1 - oy0)
            covered_area += ow * oh
    return covered_area / total_area


def box_pixel_coverage(box: Box, collapsed: np.ndarray, cell: int) -> float:
    """`box_mask_fraction` の薄いラッパー（後方互換用）。"""
    return box_mask_fraction(box, collapsed, cell)


# ---------------------------------------------------------------------------
# GT の読み込み
# ---------------------------------------------------------------------------


@dataclass
class GtItem:
    frame: int
    box: Box
    cls: str


def load_gt_corrections(path: str, kinds: tuple[str, ...] = ("add",)) -> list[GtItem]:
    """`corrections.json`（人手修正）から GT 矩形を読む。

    `kind == "add"` は「検出が見逃したので人間が確実に対象があると確認して
    足した」矩形なので、そのまま GT として使える。`remove` は誤検出の否定
    領域であって GT ではないので既定では含めない。
    """
    cs = corr.CorrectionSet.load(path)
    return [
        GtItem(frame=c.frame, box=c.box, cls=c.cls)
        for c in cs.items
        if c.kind in kinds
    ]


def load_gt_yolo(dataset_dir: str) -> list[GtItem]:
    """YOLO 形式のデータセット（`images/`, `labels/`, `classes.txt`）から GT を読む。

    画像ファイル名の数字部分をフレーム番号として扱う（このデータセットの
    命名規則: `000020.png` = フレーム20）。
    """
    classes_path = os.path.join(dataset_dir, "classes.txt")
    with open(classes_path, encoding="utf-8") as f:
        classes = [line.strip() for line in f if line.strip()]

    images_dir = os.path.join(dataset_dir, "images")
    labels_dir = os.path.join(dataset_dir, "labels")
    items: list[GtItem] = []
    for name in sorted(os.listdir(images_dir)):
        stem, _ = os.path.splitext(name)
        try:
            frame = int(stem)
        except ValueError:
            continue
        label_path = os.path.join(labels_dir, stem + ".txt")
        if not os.path.exists(label_path):
            continue
        # 画像1枚ずつ開くのは遅いので、YOLO データセット共通の1本のサイズを使う。
        # dataset.yaml があればそこから読むが、無ければ呼び出し側が渡す。
        with open(label_path, encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) != 5:
                    continue
                ci, cx, cy, cw, ch = parts
                cls_idx = int(ci)
                cls = classes[cls_idx] if cls_idx < len(classes) else str(cls_idx)
                items.append(
                    GtItem(
                        frame=frame,
                        box=(float(cx), float(cy), float(cw), float(ch)),
                        cls=cls,
                    )
                )
    return items


def denormalize_yolo(items: list[GtItem], width: int, height: int) -> list[GtItem]:
    """YOLO の正規化中心座標 (cx, cy, w, h) を左上原点の px 矩形に変換する。"""
    out = []
    for it in items:
        cx, cy, w, h = it.box
        bw, bh = w * width, h * height
        x, y = cx * width - bw / 2, cy * height - bh / 2
        out.append(GtItem(frame=it.frame, box=(x, y, bw, bh), cls=it.cls))
    return out


# ---------------------------------------------------------------------------
# 動画からのフレーム読み出し（原寸・輝度のみ）
# ---------------------------------------------------------------------------


def iter_gray_frames(path: str, limit_frames: int | None = None):
    """原寸の輝度平面だけを1フレームずつ返す。ffmpeg の生パイプなので色劣化が無い。"""
    info = vid.probe(path)
    proc = vid.open_full_reader(path, "gray", limit_frames)
    frame_bytes = info.width * info.height
    idx = 0
    try:
        while True:
            raw = proc.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            yield idx, np.frombuffer(raw, dtype=np.uint8).reshape(info.height, info.width)
            idx += 1
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        proc.wait()
        if proc.returncode not in (0, None) and err:
            print(f"ffmpeg 警告: {err.strip()[:500]}", file=sys.stderr)


def collect_needed_frames(
    source_path: str,
    output_path: str,
    frames_needed: set[int],
) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    """必要なフレーム番号だけ、元動画と出力動画を並行デコードして拾う。

    ffmpeg のパイプは前から順にしか読めないので、シークし直すより1回で
    最後まで流して必要な番号だけ保持するほうが単純で確実。

    **戻り値の辞書に必要なフレーム全部を溜め込む。** 呼び出し側が必要フレーム数の
    少ない用途（`--cell` 自動実測でのサンプル5枚など）に限って使うこと。GT が
    多い動画全体の評価には使わない（1920x1080 の gray 平面1枚は 2,073,600 バイト。
    元+出力の2枚を 21,030 フレーム分貯めると 2,073,600 x 2 x 21,030 ≒ 87GB になる
    計算）。そちらは `iter_needed_frame_pairs` でフレームを溜めずに1枚ずつ
    処理すること。
    """
    if not frames_needed:
        return {}
    result: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    for si, sframe, oframe in iter_needed_frame_pairs(source_path, output_path, frames_needed):
        result[si] = (sframe, oframe)
    return result


def iter_needed_frame_pairs(
    source_path: str,
    output_path: str,
    frames_needed: set[int],
):
    """必要なフレーム番号の (元, 出力) ペアを1枚ずつ生成する。溜め込まない。

    `collect_needed_frames` は全部を辞書に貯めるので、GT が多い動画では
    メモリを使い切る（1080p 21,030フレームで 87GB になる計算。上記参照）。
    こちらは呼び出し側が1枚処理したら手放せるようにジェネレータにしてある。
    """
    if not frames_needed:
        return
    max_needed = max(frames_needed)
    src_iter = iter_gray_frames(source_path, limit_frames=max_needed + 1)
    out_iter = iter_gray_frames(output_path, limit_frames=max_needed + 1)
    for (si, sframe), (oi, oframe) in zip(src_iter, out_iter):
        if si != oi:
            raise RuntimeError(f"元動画と出力動画のフレーム番号がずれています: {si} != {oi}")
        if si in frames_needed:
            yield si, sframe, oframe
        if si >= max_needed:
            break


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------


def evaluate(
    source_path: str,
    output_path: str,
    gt: list[GtItem],
    cell: int,
    std_min: float,
    ratio_max: float,
    min_cells: int,
) -> dict:
    by_frame: dict[int, list[GtItem]] = {}
    for it in gt:
        by_frame.setdefault(it.frame, []).append(it)

    # フレームを辞書に溜めず、1枚読んだら評価してすぐ手放す（collect_needed_frames
    # は全部を辞書に貯めるので、GT が多い動画では 1080p 21,030フレームで 87GB になる
    # 計算（詳細は collect_needed_frames / iter_needed_frame_pairs 参照）。
    per_gt = []
    seen_frames: set[int] = set()
    for frame, sframe, oframe in iter_needed_frame_pairs(source_path, output_path, set(by_frame)):
        seen_frames.add(frame)
        items = by_frame[frame]
        eligible, collapsed = cell_analysis(sframe, oframe, cell, std_min, ratio_max)
        painted_anywhere = frame_is_painted(collapsed, min_cells)
        for it in items:
            frac = box_mask_fraction(it.box, collapsed, cell)
            measurable = box_mask_fraction(it.box, eligible, cell)
            # 測定可能な面積のうち塗られている割合。measurable が低い box は
            # 「元がもともと平坦で判定できない」割合が高いので、frac を
            # 過小評価している可能性がある（docs/11 の測定の盲点）。
            frac_among_measurable = frac / measurable if measurable > 1e-6 else None
            per_gt.append(
                {
                    "frame": frame,
                    "box": [round(v, 1) for v in it.box],
                    "class": it.cls,
                    "pixel_coverage": round(frac, 4),
                    "measurable_fraction": round(measurable, 4),
                    "pixel_coverage_among_measurable": (
                        round(frac_among_measurable, 4)
                        if frac_among_measurable is not None
                        else None
                    ),
                    "old_existence_covered": painted_anywhere,
                }
            )

    missing_frames = sorted(set(by_frame) - seen_frames)
    n = len(per_gt)
    fully_covered = sum(1 for r in per_gt if r["pixel_coverage"] >= 0.999)
    zero_covered = sum(1 for r in per_gt if r["pixel_coverage"] <= 0.001)
    mean_cov = sum(r["pixel_coverage"] for r in per_gt) / n if n else 0.0
    mean_measurable = sum(r["measurable_fraction"] for r in per_gt) / n if n else 0.0
    old_existence_true = sum(1 for r in per_gt if r["old_existence_covered"])

    return {
        "cell": cell,
        "std_min": std_min,
        "ratio_max": ratio_max,
        "min_cells": min_cells,
        "n_gt": n,
        "n_gt_frames": len(by_frame),
        "missing_frames": missing_frames,
        "mean_pixel_coverage": round(mean_cov, 4),
        "mean_measurable_fraction": round(mean_measurable, 4),
        "gt_fully_covered": fully_covered,
        "gt_not_fully_covered": n - fully_covered,
        "gt_zero_covered": zero_covered,
        "old_existence_metric_true": old_existence_true,
        "old_existence_metric_pct": round(100 * old_existence_true / n, 2) if n else 0.0,
        "new_pixel_metric_fully_covered_pct": round(100 * fully_covered / n, 2) if n else 0.0,
        "per_gt": per_gt,
    }


# ---------------------------------------------------------------------------
# 校正（この素材での分布を測ってから閾値を決めるため）
# ---------------------------------------------------------------------------


def calibrate(
    source_path: str,
    output_path: str,
    gt_frames: set[int],
    cell: int,
    std_min: float,
    ratio_max: float,
    sample_step: int,
    limit_frames: int | None,
) -> dict:
    """GT がある区間と無い区間で「潰れたセル数」の分布がどれだけ分離するか測る。

    docs/11 と同じ考え方: GT フレーム（対象が確実に映っている）で潰れたセル数が
    高く、GT の無いフレーム（サンプル）で低ければ、`min_cells` の閾値に意味がある。
    重なっていれば、この cell/std_min/ratio_max はこの素材に合っていない。
    """
    info = vid.probe(source_path)
    limit = limit_frames or info.nb_frames
    gt_counts = []
    other_counts = []
    idx = 0
    src_iter = iter_gray_frames(source_path, limit_frames=limit)
    out_iter = iter_gray_frames(output_path, limit_frames=limit)
    for (si, sframe), (oi, oframe) in zip(src_iter, out_iter):
        if si != oi:
            raise RuntimeError(f"フレーム番号がずれています: {si} != {oi}")
        if si in gt_frames:
            collapsed = collapsed_cells(sframe, oframe, cell, std_min, ratio_max)
            gt_counts.append(int(collapsed.sum()))
        elif sample_step > 0 and si % sample_step == 0:
            collapsed = collapsed_cells(sframe, oframe, cell, std_min, ratio_max)
            other_counts.append(int(collapsed.sum()))
        idx += 1

    def summarize(vals: list[int]) -> dict:
        if not vals:
            return {"n": 0}
        arr = np.array(vals)
        return {
            "n": len(arr),
            "min": int(arr.min()),
            "p1": float(np.percentile(arr, 1)),
            "median": float(np.median(arr)),
            "p99": float(np.percentile(arr, 99)),
            "max": int(arr.max()),
        }

    return {
        "cell": cell,
        "std_min": std_min,
        "ratio_max": ratio_max,
        "gt_frame_collapsed_cells": summarize(gt_counts),
        "other_frame_collapsed_cells": summarize(other_counts),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(
        description="GT 矩形が出力動画で実際に画素として塗られているかを測る（issue #7）"
    )
    p.add_argument("--source", required=True, help="元動画")
    p.add_argument("--output", required=True, help="モザイクを焼いた出力動画")
    p.add_argument("--corrections", help="GT として使う corrections.json")
    p.add_argument("--yolo-dataset", help="GT として使う YOLO データセットディレクトリ")
    p.add_argument("--gt-width", type=int, help="YOLO GT の正規化を戻す元の幅（省略時は --source から probe）")
    p.add_argument("--gt-height", type=int, help="YOLO GT の正規化を戻す元の高さ")
    p.add_argument(
        "--cell",
        type=int,
        help="診断格子のセルサイズ px。省略時は出力動画のGTフレームを実測して決める"
        "（`render.default_block_size` は動画がどう焼かれたか知らないと信用できない）",
    )
    p.add_argument(
        "--std-min",
        type=float,
        default=6.0,
        help="docs/11 の 1080p 素材での校正値。RULES.md 2.1 の通り、素材が変われば"
        "分離も変わるので既定値を鵜呑みにせず --calibrate で確かめること",
    )
    p.add_argument("--ratio-max", type=float, default=0.35)
    p.add_argument("--min-cells", type=int, default=2)
    p.add_argument("--calibrate", action="store_true", help="閾値を決める前の分布測定を行う")
    p.add_argument("--calibrate-sample-step", type=int, default=30, help="GT外フレームの間引き間隔")
    p.add_argument("--calibrate-limit-frames", type=int, help="校正に使う先頭フレーム数の上限")
    p.add_argument("--report", help="結果 JSON の出力先")
    args = p.parse_args()

    if not args.corrections and not args.yolo_dataset:
        p.error("--corrections か --yolo-dataset のどちらかが必要です")

    info = vid.probe(args.source)
    print(f"元動画 {args.source}  {info.width}x{info.height}  {info.nb_frames} フレーム")
    print(f"出力動画 {args.output}")

    gt: list[GtItem] = []
    if args.corrections:
        gt += load_gt_corrections(args.corrections)
        print(f"corrections.json から GT {len(gt)} 件")
    if args.yolo_dataset:
        yolo_items = load_gt_yolo(args.yolo_dataset)
        gw = args.gt_width or info.width
        gh = args.gt_height or info.height
        yolo_items = denormalize_yolo(yolo_items, gw, gh)
        print(f"YOLO データセットから GT {len(yolo_items)} 件（{gw}x{gh} で正規化解除）")
        gt += yolo_items

    if args.cell:
        cell = args.cell
    else:
        sample_frames = sorted({it.frame for it in gt})[:5]
        _, out_samples = zip(
            *collect_needed_frames(args.source, args.output, set(sample_frames)).values()
        ) if sample_frames else ((), ())
        per_frame = detect_block_size_per_frame(list(out_samples)) if out_samples else []
        detected_values = [v for v in per_frame if v is not None]
        detected = int(np.median(detected_values)) if detected_values else None
        fallback = default_block_size(max(info.width, info.height))
        if detected is not None:
            cell = detected
            print(
                f"ブロックサイズを出力動画の{len(out_samples)}フレームから実測: {cell}px"
                f"（フレームごと: {per_frame}）"
            )
            # 実測 4px を実際の 1920x1080 素材(真のブロック20px)で確認すると、4点中3点で
            # サンプル内の値が割れ、割れたケースでは中央値が真値と異なる方に寄っていた
            # （detect_block_size のdocstring参照）。ここで気付けるようにする。
            if len(set(detected_values)) > 1:
                print(
                    f"警告: サンプルフレーム間で実測ブロックサイズが割れています: {per_frame}。"
                    f"中央値 {cell}px を採用しましたが、この中央値が真のブロックサイズと"
                    f"異なる場合がある（実測済み、detect_block_size のdocstring参照）。"
                    f"--cell で明示指定するか、複数フレームをオーバーレイで目視確認すること。",
                    file=sys.stderr,
                )
            if cell != fallback:
                print(
                    f"警告: 実測ブロックサイズ {cell}px が理論値 default_block_size="
                    f"{fallback}px と一致しません。どちらか一方が誤っている可能性がある"
                    f"（640x480素材では理論値が誤りだったが、1920x1080素材では実測が"
                    f"4px に外れ理論値20pxが正しかった実例がある）。--cell で明示指定するか、"
                    f"目視で確認すること。",
                    file=sys.stderr,
                )
        else:
            cell = fallback
            print(
                f"警告: 出力動画からブロックサイズを実測できませんでした。"
                f"default_block_size の理論値 {fallback}px にフォールバックします。"
                f"この値がこの動画の実際のブロックサイズと一致する保証はありません。"
                f"--cell で明示指定するか、--calibrate で確認してください。",
                file=sys.stderr,
            )
    print(f"診断格子セルサイズ {cell}px  std_min={args.std_min}  ratio_max={args.ratio_max}  min_cells={args.min_cells}")

    if args.calibrate:
        gt_frames = {it.frame for it in gt}
        cal = calibrate(
            args.source,
            args.output,
            gt_frames,
            cell,
            args.std_min,
            args.ratio_max,
            args.calibrate_sample_step,
            args.calibrate_limit_frames,
        )
        print("\n[校正] 潰れたセル数の分布")
        print(f"  GT フレーム    : {cal['gt_frame_collapsed_cells']}")
        print(f"  その他(間引き) : {cal['other_frame_collapsed_cells']}")
        print(
            "  注意: この分布は cell が実際のブロックサイズと一致しているときだけ意味を持つ。"
            "cell を外すと非GTフレーム側の潰れたセル数が実際より多く出て、素通し側に誤る"
            "（docs/12「旧指標・--calibrate・box指標は cell の誤りに対して同じ強さで壊れない」"
            "参照）。GT/非GT の分離が良く見えても、それが cell の正しさを保証しない。",
            file=sys.stderr,
        )
        if args.report:
            with open(args.report, "w", encoding="utf-8") as f:
                json.dump(cal, f, ensure_ascii=False, indent=1)
            print(f"\n{args.report} に保存")
        return

    result = evaluate(args.source, args.output, gt, cell, args.std_min, args.ratio_max, args.min_cells)

    print(f"\nGT {result['n_gt']} 件（{result['n_gt_frames']} フレーム）")
    if result["missing_frames"]:
        print(f"  警告: 動画から読めなかった GT フレーム {len(result['missing_frames'])} 件: {result['missing_frames'][:10]}")
    print(f"\n[旧指標] フレームに矩形/塗りが存在するか（有無のみ）")
    print(f"  {result['old_existence_metric_true']} / {result['n_gt']} ({result['old_existence_metric_pct']}%)")
    print(f"\n[新指標] GT矩形のうち実際に画素で塗られている割合")
    print(f"  平均被覆率            {result['mean_pixel_coverage'] * 100:.1f}%")
    print(f"  100%被覆              {result['gt_fully_covered']} / {result['n_gt']} ({result['new_pixel_metric_fully_covered_pct']}%)")
    print(f"  被覆が100%未満        {result['gt_not_fully_covered']} 件")
    print(f"  完全に塗られていない  {result['gt_zero_covered']} 件")
    print(f"  平均測定可能面積      {result['mean_measurable_fraction'] * 100:.1f}%"
          f"（残りは元がもともと平坦で判定できないセル。docs/11 の「測定の盲点」）")
    if result["mean_measurable_fraction"] < 0.5:
        print(
            "  警告: 測定可能面積が半分未満です。std_min がこの素材には高すぎる可能性が"
            "あります。--calibrate で分布を確認し、閾値を下げることを検討してください。",
            file=sys.stderr,
        )

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        print(f"\n{args.report} に保存")


if __name__ == "__main__":
    main()
