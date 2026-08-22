"""描画パスを実動画で確認するための、手書き検出結果を吐く。

検出器を通さずに --reuse-detections でパス2だけを回せるので、
モザイクの見た目・格子の固定・彩度の潰れを実素材で確認できる。
"""

import argparse
import json


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("out")
    p.add_argument("--frames", type=int, default=120)
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--gap-from", type=int, help="この区間の検出を落として補間を試す")
    p.add_argument("--gap-to", type=int)
    args = p.parse_args()

    dets = {}
    for f in range(args.frames):
        if args.gap_from is not None and args.gap_from <= f < (args.gap_to or 0):
            continue  # 検出漏れを再現
        x = 200 + f * 6
        dets[str(f)] = [
            {
                "class": "FEMALE_GENITALIA_EXPOSED",
                "score": 0.85,
                "box": [x, 300, 120, 100],
            }
        ]

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "n_frames": args.frames,
                "width": args.width,
                "height": args.height,
                "detections": dets,
            },
            fh,
        )
    print(f"{args.out} に {len(dets)}/{args.frames} フレーム分を書き出しました")


if __name__ == "__main__":
    main()
