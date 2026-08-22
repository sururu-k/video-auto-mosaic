"""マージン設定を並べて、被覆と大きさのトレードオフを数値で見る。

「潰しすぎ」と「漏れ」は逆方向なので、片方だけ見ても判断できない。
特定の区間の素通しフレーム数と、全体の面積倍率を同時に出す。
"""

import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic.detector import DEFAULT_CLASSES, Detection  # noqa: E402
from automosaic.temporal import TemporalConfig, process  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("detections")
    p.add_argument("--range", default="", help="注目する区間 例 480-606")
    p.add_argument("--classes", default=",".join(DEFAULT_CLASSES))
    args = p.parse_args()

    with open(args.detections, encoding="utf-8") as f:
        d = json.load(f)
    pf = {
        int(k): [Detection.from_dict(x) for x in v] for k, v in d["detections"].items()
    }
    n, W, H = d["n_frames"], d.get("width", 640), d.get("height", 480)
    frame_area = float(W * H)
    classes = {c.strip() for c in args.classes.split(",") if c.strip()}

    lo, hi = (0, n)
    if args.range:
        a, b = args.range.split("-")
        lo, hi = int(a), int(b)

    settings = [
        ("margin 0.25 cap12", 0.25, 12.0),
        ("margin 0.35 cap16", 0.35, 16.0),
        ("margin 0.5  cap20", 0.5, 20.0),
        ("margin 0.6  cap24", 0.6, 24.0),
    ]

    print(f"{W}x{H}  {n} フレーム   注目区間 {lo}-{hi}\n")
    print(
        f"{'設定':<20s}{'区間の素通し':>12s}{'全体の素通し':>12s}"
        f"{'面積倍率':>10s}{'画面占有(中央)':>14s}{'適用率':>8s}"
    )
    for label, ms, cap in settings:
        cfg = TemporalConfig(
            margin_scale=ms,
            margin_cap_px=cap,
            memory=2,
            memory_before=2,
            bridge_max=0,
            hold_growth=0.0,
            motion_weight=1.0,
        )
        r, st = process(pf, n, W, H, classes, cfg)
        bare_range = sum(1 for f in range(lo, hi) if not r.get(f))
        bare_all = n - st["frames_with_mosaic"]
        ratios = [
            (b[2] * b[3]) / max(1.0, g.box[2] * g.box[3])
            for f in range(n)
            for b, g in r.get(f, [])
        ]
        occ = [b[2] * b[3] / frame_area for f in range(n) for b, g in r.get(f, [])]
        print(
            f"{label:<20s}{bare_range:>10d}F{bare_all:>11d}F"
            f"{statistics.median(ratios):>9.2f}x"
            f"{100 * statistics.median(occ):>13.1f}%"
            f"{100 * st['frames_with_mosaic'] / n:>7.1f}%"
        )


if __name__ == "__main__":
    main()
