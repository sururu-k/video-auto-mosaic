"""issue #13: 検出器を使わない出荷ゲート（第二の意見・検出非依存版）。

## なぜこれが要るか

`tools/verify_output.py` は本番と同じ検出器・同じ重みで再検出する。同じ重みが
見落とした対象は、条件（conf/infer-size/TTA）をどう変えても再検出でも見落とす
（`verify_output.structural_warning` が警告している循環そのもの）。**0件が出て
も「漏れが無い」証拠にはならない。**

`tools/pixel_coverage.py`（issue #7）は「GT矩形が実際に画素として塗られている
か」を測る道具だが、GT自体が本番の検出結果（det.json+corrections）から作られる。
検出器が見落とした場所は GT にも現れないので、GT の外側は測っていない。

ここは GT も検出器も使わない。画面全体を格子に切り、「モザイクの構造（セル内
分散がほぼゼロに潰れた領域）がフレームのどこかにあるか」だけを見る。検出器が
その対象を一度も検出していなくても、GT に矩形が無くても判定は変わらない。
検出器・GTの弱点とは独立な経路なので、同じ見落としを繰り返さない
（issue #13 コメントの案1）。

## 原理は `tools/pixel_coverage.py` と同じ。判定基準は自分で導き直した

`render.pixelize_plane` はブロック格子にスナップして各セルを平均色1色に潰す
（不可逆）ので、モザイク領域はセル内分散がほぼゼロになる。セルの判定関数
（`cell_analysis` / `frame_is_painted`）は `pixel_coverage.py` のものをそのまま
再利用する（テスト済みの同じ物差しを二重に実装しない）。

**ただし `std_min` / `ratio_max` / `min_cells` の既定値は docs/11 の数字を
そのまま借りていない。** `RULES.md` 2.1「既知の基準を借りてこない」に従い、
この実素材（`data/library/20260823-234604-9be9`）で自分で測り直した
（測定の生ログはこの変更の PR 本文にある）。

- ブロックサイズ 20px は推測ではなく `run.log` の実際の焼き付けログ
  （`ブロック   20 px`）から確定させた。`default_block_size(1920) == 20` と
  一致することも確認済みだが、一致を根拠にはしていない
- `std_min=6.0` / `ratio_max=0.35` は docs/11 と同じ値に**落ち着いた**が、
  これは「元セルに明確なテクスチャがある(std>=6)のに出力で分散が1/3未満に
  潰れている」という物理的な意味を先に決めてから、この素材で
  `eligible`（測定可能面積）が過小にならないことを確認した上で採用した
  （測定可能面積の実測は PR 本文参照。同じ値に落ち着くことは
  「同じ crf16・同じ1080p 系列の素材では妥当」という証拠であって、
  「常に妥当」という意味ではない）
- `min_cells`（フレームが「塗装あり」と判定される閾値）と `min_run_frames`
  （この閾値未満の穴は無視する連続フレーム数）は、本番自身が計算した領域
  （det.json+corrections、`n_corrections=0` なのでdet.jsonのみ）を GT として
  この動画の全 55,303 フレームを密に走査し、分布を測ってから決めた
  （校正ログは PR 本文参照）。**この GT は「検出器が対象を見つけたか」では
  なく「render.py が実際にそこを塗ったか」なので、検出器の盲点とは無関係**
  （`render.pixelize_plane` は決定的に動くので、regionが1つでもあればそこは
  必ず塗られている）
- **`min_cells=2`（docs/11 の値）は全数走査すると危険側に外れた。** GT が
  region無しと計算した 16,660 フレームのうち、`min_cells=2` では 6 件
  (0.036%) が潰れたセル2個以上になり「塗装あり」と誤判定された(危険側)。
  60窓x100フレームの標本校正(docs/11)ではこの6件を踏んでいなかった
  （RULES.md 2.1「標本ではなく密に見る」がここで実際に効いた）。
  `min_cells=3` まで上げると危険側は 0/16,660 になる
  （代償として GT region ありのフレームの見逃し[安全側]が 0.259%->0.329%
  に増える）。そのため既定値を 3 にした

## これが保証しないこと（必ず読むこと。出力にも毎回書く）

- **モザイクが対象の上に乗っているか。** フレームに"どこかに"モザイクがある
  ことしか見ない。対象を外れた場所に塗った矩形と区別できない
- **2人以上映る場面でどちらが塗られたか**
- **誤検出率・precision。** 「見ていない場面」を数える基準がここには無い
- **対象がそもそも画面内に存在するか。** source 側に何が映っているかは
  一切判定しない。「対象が写っているのに塗られていない」と「そもそも何も
  写っていない」を区別できない
- **`--min-run-frames` 未満の短い穴は「見つからない」。** 閾値の選び方は
  この素材での校正に基づくが、他の素材でそのまま妥当という保証はない
- **元がもともと平坦な場所に塗った場合は判定できない（測定の盲点）。**
  `std_min` 未満のセルは `eligible` から除外される。真っ暗な場面などで
  モザイクを検出し損ねる方向に誤る（危険側ではなく安全側の誤り。詳細は
  PR 本文の「report塗装なのに画素で見えない」の実測）

**このツールが 0 件（フラグされた区間なし）で終わっても「漏れが無い」の証拠
にはならない。** 「この基準で見える範囲に、この長さ以上の構造的な沈黙が
無かった」としか言えない。実行結果は常に人間のレビュー対象であって、
出荷の自動承認シグナルではない。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pixel_coverage import cell_analysis, iter_gray_frames  # noqa: E402
from automosaic import video as vid  # noqa: E402

# この素材（1920x1080, crf16, 20px ブロック）で校正した既定値。校正ログは
# この変更の PR 本文にある。他の素材では、本番が計算した領域（det.json+
# corrections）を GT に `--report` の collapsed_counts を測り直し、分布を
# 見てから min_cells / min_run_frames を選び直すこと（RULES.md 2.1。
# `tools/eval_structural_silence_gate.py` が分布と分離幅を出す）。
DEFAULT_CELL = 20
DEFAULT_STD_MIN = 6.0
DEFAULT_RATIO_MAX = 0.35
DEFAULT_MIN_CELLS = 3
DEFAULT_MIN_RUN_FRAMES = 8  # 校正根拠は PR 本文参照

DISCLAIMER = """\
[このゲートが保証しないこと]
  - モザイクが対象の上に乗っているか（フレームに"どこかに"あるかしか見ない）
  - 2人以上映る場面でどちらが塗られたか
  - 誤検出率・precision
  - 対象がそもそも画面内に存在するか（source側の内容は判定しない）
  - --min-run-frames 未満の短い穴（この長さ未満は「見つからない」）
  - 元がもともと平坦な場所に塗った場合（測定の盲点。安全側に誤る）
  0件で終わっても「漏れが無い」証拠にはならない。人間のレビュー対象として扱うこと。
"""


# ---------------------------------------------------------------------------
# コアロジック
# ---------------------------------------------------------------------------


def painted_flags(collapsed_counts: Sequence[int], min_cells: int) -> list[bool]:
    """フレームごとの潰れたセル数から「そのフレームにモザイクがあるか」を判定する。"""
    return [c >= min_cells for c in collapsed_counts]


def find_silent_runs(
    painted: Sequence[bool], min_run_frames: int, frame_offset: int = 0
) -> list[dict]:
    """`painted` が False (モザイク無し) の連続区間のうち、min_run_frames 以上のものを返す。

    1フレームだけの穴は圧縮ノイズやテクスチャの揺らぎで普通に起こる
    （PR本文の校正ログ参照）。閾値未満の穴を無視するのはこの理由による。
    ただし「無視する」は「安全」の意味ではない。DISCLAIMER の通り、短い穴も
    見つからないだけで存在しうる。
    """
    runs: list[dict] = []
    start: int | None = None
    n = len(painted)
    for i in range(n + 1):
        is_silent = i < n and not painted[i]
        if is_silent:
            if start is None:
                start = i
        else:
            if start is not None:
                length = i - start
                if length >= min_run_frames:
                    runs.append(
                        {
                            "start_frame": start + frame_offset,
                            "end_frame": i - 1 + frame_offset,
                            "length": length,
                        }
                    )
                start = None
    return runs


NOT_MEASURED = [
    "モザイクが対象の上に乗っているか",
    "2人以上映る場面でどちらが塗られたか",
    "誤検出率・precision",
    "対象がそもそも画面内に存在するか",
    "min_run_frames 未満の短い穴",
    "元がもともと平坦な場所に塗った場合（測定の盲点、安全側の誤り）",
]


def build_report(
    source: str,
    output: str,
    cell: int,
    std_min: float,
    ratio_max: float,
    min_cells: int,
    min_run_frames: int,
    n_scanned: int,
    runs: list[dict],
    collapsed_counts: Sequence[int],
) -> dict:
    """CLI と切り離してテストできるように、レポート組み立てを独立させた。

    `not_measured` を必ず含める。0件で通っても「漏れが無い」証拠にならない
    ことを、この関数を経由する限りレポートJSONから省略できないようにする。
    """
    return {
        "source": source,
        "output": output,
        "cell": cell,
        "std_min": std_min,
        "ratio_max": ratio_max,
        "min_cells": min_cells,
        "min_run_frames": min_run_frames,
        "n_frames_scanned": n_scanned,
        "runs": runs,
        "n_silent_frames": sum(r["length"] for r in runs),
        "collapsed_counts": list(collapsed_counts),
        "not_measured": list(NOT_MEASURED),
    }


def decide_exit_code(runs: list[dict], no_fail_on_silence: bool) -> int:
    """`RULES.md` 0「判断がつかないときは塞ぐ・止める」の適用箇所。

    構造的な沈黙が1件でも見つかったら、既定では終了コード1で止める。
    `--no-fail-on-silence` で無効化できるが、それは「安全と分かった」
    ではなく「人間が別の方法で確認した上で握りつぶす」を意味する。
    """
    if runs and not no_fail_on_silence:
        return 1
    return 0


def scan_video_pair(source_path: str, output_path: str, cell: int, std_min: float, ratio_max: float, limit_frames: int | None = None):
    """フレーム0から順に (frame_idx, collapsed_count) を生成する。

    `iter_gray_frames`（pixel_coverage.py）を再利用する。フレーム番号がずれたら
    即座に例外を出す（無音の一致ミスが素通し側の誤判定を生むため、`RULES.md 0`
    に従い黙って進めない）。
    """
    src_iter = iter_gray_frames(source_path, limit_frames)
    out_iter = iter_gray_frames(output_path, limit_frames)
    for (si, sframe), (oi, oframe) in zip(src_iter, out_iter):
        if si != oi:
            raise RuntimeError(f"元動画と出力動画のフレーム番号がずれています: {si} != {oi}")
        _, collapsed = cell_analysis(sframe, oframe, cell, std_min, ratio_max)
        yield si, int(collapsed.sum())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="検出器を使わず、画素の構造だけからモザイクの構造的な沈黙を探す"
        "（出荷ゲート・issue #13）。0件でも漏れが無い証拠にはならない。"
    )
    p.add_argument("--source", required=True, help="元動画")
    p.add_argument("--output", required=True, help="モザイクを焼いた出力動画")
    p.add_argument("--cell", type=int, default=DEFAULT_CELL, help=f"診断格子のセルサイズpx（既定 {DEFAULT_CELL}、この素材の実測ブロックサイズ）")
    p.add_argument("--std-min", type=float, default=DEFAULT_STD_MIN)
    p.add_argument("--ratio-max", type=float, default=DEFAULT_RATIO_MAX)
    p.add_argument("--min-cells", type=int, default=DEFAULT_MIN_CELLS, help="このセル数以上潰れていればそのフレームは「塗装あり」")
    p.add_argument("--min-run-frames", type=int, default=DEFAULT_MIN_RUN_FRAMES, help="この長さ以上「塗装なし」が連続したらフラグする")
    p.add_argument("--limit-frames", type=int)
    p.add_argument("--report", help="結果JSON（潰れたセル数の生列を含む）の出力先")
    p.add_argument(
        "--no-fail-on-silence",
        action="store_true",
        help="フラグされた区間があっても終了コード0のまま終える"
        "（既定は判断がつかないときは止める。RULES.md 0）",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()

    info = vid.probe(args.source)
    print(f"元動画 {args.source}  {info.width}x{info.height}  {info.nb_frames} フレーム")
    print(f"出力動画 {args.output}")
    print(f"格子 {args.cell}px  std_min={args.std_min}  ratio_max={args.ratio_max}  min_cells={args.min_cells}  min_run_frames={args.min_run_frames}")
    print(DISCLAIMER)

    collapsed_counts: list[int] = []
    idx = -1
    for idx, count in scan_video_pair(
        args.source, args.output, args.cell, args.std_min, args.ratio_max, args.limit_frames
    ):
        collapsed_counts.append(count)
        if (idx + 1) % 2000 == 0:
            sys.stderr.write(f"\r走査 {idx + 1} フレーム   ")
            sys.stderr.flush()
    sys.stderr.write("\n")

    n_scanned = len(collapsed_counts)
    painted = painted_flags(collapsed_counts, args.min_cells)
    runs = find_silent_runs(painted, args.min_run_frames)

    n_silent_frames = sum(r["length"] for r in runs)
    print(f"\n走査 {n_scanned} フレーム")
    print(f"構造的な沈黙区間（{args.min_run_frames}フレーム以上連続で塗装無し）: {len(runs)} 件 / 計 {n_silent_frames} フレーム ({n_silent_frames / info.fps:.1f}秒)")
    if runs:
        print("\n[区間]")
        for r in runs[:50]:
            print(
                f"  frame {r['start_frame']:>6}-{r['end_frame']:<6} "
                f"({r['start_frame'] / info.fps:7.2f}s-{r['end_frame'] / info.fps:7.2f}s)  "
                f"{r['length']} フレーム"
            )
        if len(runs) > 50:
            print(f"  ... 他 {len(runs) - 50} 件")

    print(f"\n{DISCLAIMER}")

    if args.report:
        report = build_report(
            args.source, args.output, args.cell, args.std_min, args.ratio_max,
            args.min_cells, args.min_run_frames, n_scanned, runs, collapsed_counts,
        )
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        print(f"{args.report} に保存")

    code = decide_exit_code(runs, args.no_fail_on_silence)
    if code:
        print(
            "\n[判定] 構造的な沈黙が見つかったため、判断がつかない状態として"
            "終了コード1で止めます（RULES.md 0）。人間が目視で確認すること。"
            "--no-fail-on-silence で抑止できるが、抑止しても漏れが無いことには"
            "ならない。",
            file=sys.stderr,
        )
        sys.exit(code)


if __name__ == "__main__":
    main()
