"""マージン設定を変えたときに、実際の領域が元の検出からどれだけ膨らむかを数値で出す。

動画を焼く前に「潰しすぎ」を数字で確認するためのもの。
面積比の中央値が3〜4倍を超えているようなら、まず間違いなく過剰。
"""

import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic.detector import DEFAULT_CLASSES, Detection  # noqa: E402
from automosaic.temporal import TemporalConfig, process  # noqa: E402


def run(per_frame, n_frames, w, h, classes, **kwargs) -> dict:
    cfg = TemporalConfig(**kwargs)
    regions, stats = process(per_frame, n_frames, w, h, classes, cfg)

    ratios = []
    areas = []
    frame_area = float(w * h)
    for f in range(n_frames):
        for box, reg in regions.get(f, []):
            src_area = max(1.0, reg.box[2] * reg.box[3])
            ratios.append((box[2] * box[3]) / src_area)
            areas.append((box[2] * box[3]) / frame_area)
    return {
        "median_expand": statistics.median(ratios) if ratios else 0.0,
        "p90_expand": (sorted(ratios)[int(len(ratios) * 0.9)] if ratios else 0.0),
        "median_frame_ratio": statistics.median(areas) if areas else 0.0,
        "max_frame_ratio": max(areas) if areas else 0.0,
        "coverage": 100.0 * stats["frames_with_mosaic"] / n_frames,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("detections")
    p.add_argument("--classes", default=",".join(DEFAULT_CLASSES))
    args = p.parse_args()

    with open(args.detections, encoding="utf-8") as f:
        data = json.load(f)
    n_frames = data["n_frames"]
    w, h = data.get("width", 640), data.get("height", 480)
    per_frame = {
        int(k): [Detection.from_dict(d) for d in v]
        for k, v in data["detections"].items()
    }
    classes = {c.strip() for c in args.classes.split(",") if c.strip()}

    settings = [
        ("margin 1.0 (既定)", dict(margin_scale=1.0)),
        ("margin 0.7", dict(margin_scale=0.7)),
        ("margin 0.5", dict(margin_scale=0.5)),
        ("margin 0.5 + cap24", dict(margin_scale=0.5, margin_cap_px=24)),
        ("margin 0.35 + cap16", dict(margin_scale=0.35, margin_cap_px=16)),
    ]

    print(f"{w}x{h}  {n_frames} フレーム\n")
    print(
        f"{'設定':<22s}{'面積倍率(中央)':>14s}{'面積倍率(p90)':>14s}"
        f"{'画面占有(中央)':>14s}{'画面占有(最大)':>14s}{'適用率':>8s}"
    )
    for name, kw in settings:
        r = run(per_frame, n_frames, w, h, classes, **kw)
        print(
            f"{name:<22s}{r['median_expand']:>13.2f}x{r['p90_expand']:>13.2f}x"
            f"{r['median_frame_ratio'] * 100:>13.1f}%{r['max_frame_ratio'] * 100:>13.1f}%"
            f"{r['coverage']:>7.1f}%"
        )


if __name__ == "__main__":
    main()
