"""別条件で検出をやり直し、その結果がモザイクで覆われているかを調べる。

入力側でどれだけ頑張っても「漏れていない」ことの保証にはならない。検出器が
見落とした対象は、その検出器の出力を眺めていても永遠に見つからない。
第二の意見が要る。

## 2つのモード

**source（既定・推奨）**
  元素材に対して、本番と違う条件でもう一度検出する。その検出がモザイクの外に
  あれば警告する。元素材では対象がはっきり写っているので、検出器が本来の性能を
  出せる。

**output**
  出力動画そのものに検出をかける。一見こちらが本筋に見えるが、**実測では
  使い物にならなかった**（再現率43%、警告262件のうち59件が正常フレームへの誤報）。
  理由は構造的で、検証対象が既にモザイクで潰れた絵だから。検出器は潰れていない
  画像で学習しているので、部分的に潰れた対象には反応しない。
  実測の記録として残してあるが、出荷ゲートには使えない。

## 判定の考え方

第二の検出のうち、モザイク領域に覆われている割合を見る。大部分が外にあれば、
本番の検出がその対象を取りこぼしたか、矩形が足りていないということになる。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic.detector import DEFAULT_CLASSES, Detection, Detector  # noqa: E402
from automosaic.temporal import TemporalConfig, process  # noqa: E402
from automosaic import corrections as corr  # noqa: E402
from automosaic import video as vid  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# automosaic/cli.py の既定 --model / --conf と同じ値。値を変えたら両方直す。
PROD_MODEL_BASENAME = "640m.onnx"
PROD_CONF = 0.06


def structural_warning(mode: str, model_path: str) -> str | None:
    """この実行が「独立した第二の意見」になっていないなら警告文を返す。None なら該当なし。

    source モードで本番と同じ重みを使う限り、検出器自身が盲目な場所は
    もう一度検出しても同じく盲目になる。conf・infer-size・TTA をどう変えても、
    重みが同じなら盲点の本質は動かない（docs/05: 実測 source モード 0/2 件、
    「検出器を自分自身では検証できない」という循環）。
    """
    if mode != "source":
        return None
    if os.path.basename(model_path) != PROD_MODEL_BASENAME:
        return None
    return (
        f"[警告] 検証モデルが本番既定と同じ重み ({PROD_MODEL_BASENAME}) です。\n"
        "  conf/infer-size/TTA をどう変えても、同じ重みが盲目な場所は\n"
        "  再検出でも同じく盲目になります（docs/05: source モード実測 0/2 件、\n"
        "  「検出器を自分自身では検証できない」という循環）。\n"
        "  ここで 0 件が出ても「漏れが無い証拠」にはなりません。「この検証が\n"
        "  対象を捕捉できない構造になっている」可能性のほうが高いです。\n"
        "  独立した出荷判定には、別アーキテクチャ・別学習データの検出器\n"
        "  （issue #7 の画素被覆指標 / issue #12 の segmenter 併走）が要ります。"
    )


def _covered_fraction(box, regions) -> float:
    """box のうち、regions のどれかに覆われている面積の割合。

    矩形同士の重なりをそのまま足すと重複を二重に数えるので、
    box を細かい格子に切って「覆われた格子の割合」で近似する。
    格子は 16x16 なので誤差は数%に収まる。実用上これで足りる。
    """
    x, y, w, h = box
    if w <= 0 or h <= 0:
        return 1.0
    n = 16
    covered = 0
    for i in range(n):
        px = x + (i + 0.5) * w / n
        for j in range(n):
            py = y + (j + 0.5) * h / n
            for rx, ry, rw, rh in regions:
                if rx <= px <= rx + rw and ry <= py <= ry + rh:
                    covered += 1
                    break
    return covered / (n * n)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="出力動画に別の検出器をかけて漏れを探す（出荷ゲート）"
    )
    p.add_argument("video", help="mode=source なら元素材、mode=output なら処理済み動画")
    p.add_argument(
        "--mode",
        default="source",
        choices=["source", "output"],
        help="source は元素材を別条件で検出し直す（推奨）。"
        "output は処理済み動画を検証する（実測では使い物にならなかった）",
    )
    p.add_argument("--detections", required=True, help="本番で使った検出結果 JSON")
    p.add_argument("--corrections", help="手修正 JSON")
    p.add_argument(
        "--model",
        default=os.path.join(ROOT, "weights", "640m.onnx"),
        help="検証に使う重み。本番と違う条件にすること（重み・解像度・TTAのいずれか）",
    )
    p.add_argument("--infer-size", type=int, default=960)
    p.add_argument("--tta", action="store_true", help="検証側でTTAを使う")
    p.add_argument("--conf", type=float, default=0.10)
    p.add_argument("--provider", default="auto")
    p.add_argument("--device-id", type=int, default=1)
    p.add_argument("--classes", default=",".join(DEFAULT_CLASSES))
    p.add_argument(
        "--min-outside",
        type=float,
        default=0.05,
        help="検出のこの割合以上がモザイクの外にあれば漏れとみなす。"
        "0.5 だと検出矩形の49%がモザイクの外でも報告しない（issue #13）",
    )
    p.add_argument("--frame-step", type=int, default=1, help="Nフレームおきに検証")
    p.add_argument("--limit-frames", type=int)
    p.add_argument("--report", help="結果の JSON 出力先")
    # 本番の描画設定。領域を再現するために必要
    p.add_argument("--margin-scale", type=float, default=0.35)
    p.add_argument("--margin-cap", type=float, default=16.0)
    p.add_argument("--estimate-gaps", action="store_true")
    return p


def main() -> None:
    args = build_parser().parse_args()

    classes = {c.strip() for c in args.classes.split(",") if c.strip()}

    # 本番で塗った領域を再現する。出力を見ただけではどこを塗ったか分からないので、
    # 検出結果と手修正から同じ計算をやり直す
    with open(args.detections, encoding="utf-8") as f:
        det_data = json.load(f)
    per_frame = {
        int(k): [Detection.from_dict(x) for x in v]
        for k, v in det_data["detections"].items()
    }
    n_frames = det_data["n_frames"]
    src_w = det_data.get("width", 0)
    src_h = det_data.get("height", 0)

    cfg_kw = dict(margin_scale=args.margin_scale, margin_cap_px=args.margin_cap)
    if not args.estimate_gaps:
        cfg_kw.update(
            memory=2, memory_before=2, bridge_max=0, hold_growth=0.0, motion_weight=1.0
        )
    regions, _ = process(per_frame, n_frames, src_w, src_h, classes, TemporalConfig(**cfg_kw))

    if args.corrections:
        regions = corr.apply(regions, corr.CorrectionSet.load(args.corrections))

    boxes_per_frame = {f: [b for b, _ in v] for f, v in regions.items()}

    info = vid.probe(args.video)
    det = Detector(
        model_path=args.model,
        infer_size=args.infer_size,
        conf=args.conf,
        provider=args.provider,
        device_id=args.device_id,
    )
    print(f"検証モデル {os.path.basename(args.model)}  プロバイダ {det.active_provider}")
    print(f"モード {args.mode}")
    print(f"対象 {args.video}  {info.width}x{info.height}  {n_frames} フレーム")

    warning = structural_warning(args.mode, args.model)
    if warning:
        print(f"\n{warning}\n", file=sys.stderr)

    dec_w, dec_h = vid.detection_frame_size(info, args.infer_size, path=args.video)
    frame_bytes = dec_w * dec_h * 3
    scale_back = info.width / dec_w

    proc = vid.open_detection_reader(args.video, args.infer_size, args.limit_frames)

    findings = []
    idx = 0
    checked = 0
    try:
        while True:
            raw = proc.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            if args.frame_step <= 1 or idx % args.frame_step == 0:
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(dec_h, dec_w, 3)
                dets = det.detect_frame(frame, tta=args.tta)
                mosaics = boxes_per_frame.get(idx, [])
                for d in dets:
                    if d.cls not in classes:
                        continue
                    box = tuple(v * scale_back for v in d.box)
                    outside = 1.0 - _covered_fraction(box, mosaics)
                    if outside >= args.min_outside:
                        findings.append(
                            {
                                "frame": idx,
                                "sec": round(idx / info.fps, 2),
                                "class": d.cls,
                                "score": round(d.score, 3),
                                "box": [int(v) for v in box],
                                "outside": round(outside, 3),
                                "n_mosaic": len(mosaics),
                            }
                        )
                checked += 1
            idx += 1
            if idx % 300 == 0:
                sys.stderr.write(f"\r検証 {idx} フレーム  疑い {len(findings)} 件   ")
                sys.stderr.flush()
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()
    sys.stderr.write("\n")

    print(f"\n検証 {checked} フレーム / 疑い {len(findings)} 件")
    if not findings and warning:
        print(
            "[判定] 0件ですが、上の警告のとおりこの実行は独立検証になっていません。"
            "「漏れが無い」の根拠にしないこと。",
            file=sys.stderr,
        )
    if findings:
        print("\n[モザイクの外に検出が出たフレーム]")
        for fd in findings[:40]:
            print(
                f"  frame {fd['frame']:>6} ({fd['sec']:7.2f}s) "
                f"{fd['class']:<24s} score {fd['score']:.2f} "
                f"外側 {fd['outside'] * 100:5.1f}%  {fd['box']}"
            )
        if len(findings) > 40:
            print(f"  ... 他 {len(findings) - 40} 件")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "mode": args.mode,
                    "video": os.path.abspath(args.video),
                    "verify_model": os.path.basename(args.model),
                    "conf": args.conf,
                    "min_outside": args.min_outside,
                    "frames_checked": checked,
                    "findings": findings,
                    "structural_warning": warning,
                },
                f,
                ensure_ascii=False,
                indent=1,
            )
        print(f"\nレポートを保存: {args.report}")

    # 出荷ゲートとして使えるよう、疑いがあれば終了コードを立てる
    sys.exit(1 if findings else 0)


if __name__ == "__main__":
    main()
