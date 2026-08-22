"""正解ラベルと検出結果を突き合わせて、設定ごとの Recall / Precision を出す。

本件で本当に知りたいのは mAP ではなく「モザイクが要るのに何も塗られなかった
フレームが何枚あるか」なので、フレーム単位の二値で評価する。

  見逃し (miss)      正解=あり なのにモザイク領域が1つも無い ... 法的に致命的
  過剰 (over)        正解=なし なのにモザイクがかかっている ... 許容だが少ない方がよい

Recall = 1 - 見逃し率。これが最優先の指標。

「検出」段階と「時間方向の処理後」の両方で出す。後者が実際に出力される絵に対応する。
"""

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic.detector import DEFAULT_CLASSES, Detection  # noqa: E402
from automosaic.temporal import TemporalConfig, process  # noqa: E402


def evaluate(labels: dict, covered: set[int]) -> dict:
    yes = {int(k) for k, v in labels.items() if v == "yes"}
    no = {int(k) for k, v in labels.items() if v == "no"}

    miss = sorted(yes - covered)
    hit = yes & covered
    over = sorted(no & covered)

    recall = len(hit) / len(yes) if yes else float("nan")
    over_rate = len(over) / len(no) if no else float("nan")
    return {
        "n_yes": len(yes),
        "n_no": len(no),
        "hit": len(hit),
        "miss": len(miss),
        "miss_frames": miss,
        "over": len(over),
        "recall": recall,
        "over_rate": over_rate,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("labels", help="annotate_eval.py が吐いた labels.json")
    p.add_argument("--compare-dir", help="compare_configs.py の出力ディレクトリ")
    p.add_argument("--detections", help="単一の検出結果 JSON を評価する")
    p.add_argument("--classes", default=",".join(DEFAULT_CLASSES))
    p.add_argument("--conf", type=float, default=0.0, help="この値未満の検出を無視して再評価")
    args = p.parse_args()

    with open(args.labels, encoding="utf-8") as f:
        lab = json.load(f)
    labels = lab["labels"]
    n_frames = lab["total_frames"]
    classes = {c.strip() for c in args.classes.split(",") if c.strip()}

    targets = []
    if args.compare_dir:
        for path in sorted(glob.glob(os.path.join(args.compare_dir, "det_*.json"))):
            name = os.path.basename(path)[len("det_") : -len(".json")]
            targets.append((name, path))
    if args.detections:
        targets.append((os.path.basename(args.detections), args.detections))
    if not targets:
        print("評価対象がありません（--compare-dir か --detections を指定）")
        return

    n_yes = sum(1 for v in labels.values() if v == "yes")
    n_no = sum(1 for v in labels.values() if v == "no")
    n_skip = sum(1 for v in labels.values() if v == "skip")
    print(f"正解ラベル: あり {n_yes} / なし {n_no} / 保留 {n_skip}\n")

    print(
        f"{'設定':<14s}{'段階':<10s}{'Recall':>8s}{'見逃し':>7s}"
        f"{'過剰':>7s}{'過剰率':>8s}"
    )
    rows = []
    for name, path in targets:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        per_frame = {
            int(k): [Detection.from_dict(d) for d in v]
            for k, v in data["detections"].items()
        }
        if args.conf > 0:
            per_frame = {
                k: [d for d in v if d.score >= args.conf] for k, v in per_frame.items()
            }
        width = data.get("width", 640)
        height = data.get("height", 480)

        # 段階1: 生の検出だけ（時間方向の処理なし）
        raw_cov = {f for f, ds in per_frame.items() if any(d.cls in classes for d in ds)}
        r1 = evaluate(labels, raw_cov)

        # 段階2: 時間方向の処理を通した後（実際に出力される絵）
        regions, _ = process(
            per_frame, n_frames, width, height, classes, TemporalConfig()
        )
        final_cov = {f for f in range(n_frames) if regions.get(f)}
        r2 = evaluate(labels, final_cov)

        for stage, r in (("検出のみ", r1), ("時間処理後", r2)):
            print(
                f"{name:<14s}{stage:<10s}{r['recall'] * 100:7.1f}%{r['miss']:>7d}"
                f"{r['over']:>7d}{r['over_rate'] * 100:7.1f}%"
            )
        rows.append((name, r1, r2))
        print()

    print("\n[見逃したフレーム（時間処理後）]")
    for name, _, r2 in rows:
        if r2["miss_frames"]:
            fps = lab["fps"]
            secs = ", ".join(f"{f}({f / fps:.1f}s)" for f in r2["miss_frames"][:15])
            more = "" if len(r2["miss_frames"]) <= 15 else f" ... 他{len(r2['miss_frames']) - 15}件"
            print(f"  {name}: {secs}{more}")
        else:
            print(f"  {name}: なし")


if __name__ == "__main__":
    main()
