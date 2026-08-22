"""評価用フレームを等間隔で切り出す。

フレーム番号はパス1の検出と同じ採番（先頭を0とする連番）にそろえる必要があるので、
ffmpeg の select フィルタではなく、全フレームを順に読んで数えながら抜く。
select は VFR やタイムスタンプの扱いで番号がずれることがある。
"""

import argparse
import json
import os
import subprocess
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic import video as vid  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("video")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--count", type=int, default=80, help="切り出す枚数")
    args = p.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    info = vid.probe(args.video)
    total = info.estimated_frames()
    if not total:
        print("フレーム数が取得できませんでした", file=sys.stderr)
        return

    step = max(1, total // args.count)
    wanted = set(range(0, total, step))
    print(f"総フレーム {total} から {len(wanted)} 枚（{step} フレームおき）を抜きます")

    cmd = [
        "ffmpeg", "-v", "error", "-i", args.video,
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]
    env = dict(os.environ)
    env["PATH"] = (
        os.path.join(env.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links")
        + os.pathsep + env.get("PATH", "")
    )
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)

    frame_bytes = info.width * info.height * 3
    saved = []
    idx = 0
    try:
        while True:
            raw = proc.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            if idx in wanted:
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                    info.height, info.width, 3
                )
                name = f"{idx:06d}.png"
                cv2.imwrite(os.path.join(args.out_dir, name), frame)
                saved.append(idx)
            idx += 1
    finally:
        proc.stdout.close()
        proc.wait()

    index = {
        "video": os.path.abspath(args.video),
        "total_frames": idx,
        "fps": info.fps,
        "width": info.width,
        "height": info.height,
        "frames": saved,
    }
    with open(os.path.join(args.out_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    print(f"{len(saved)} 枚を {args.out_dir} に保存しました")


if __name__ == "__main__":
    main()
