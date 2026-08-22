"""レポートの「推定のみ区間」からフレームを抜き出す。

検出器が1フレームも当たっていない区間だけを対象にする。全フレームを見る必要はなく、
区間内を数フレームおきに見て座標を決めれば、あいだは既存の補間機構が埋める。

現在の矩形も併せて書き出しておくと、既存の推定がどれだけずれているかが分かる。
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
from automosaic.detector import DEFAULT_CLASSES, Detection  # noqa: E402
from automosaic.temporal import TemporalConfig, process  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("video")
    p.add_argument("--report", required=True)
    p.add_argument("--detections", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--stride", type=int, default=12, help="区間内で何フレームおきに抜くか")
    p.add_argument("--edge", type=int, default=3, help="区間の前後にこのフレーム数だけ余分に抜く")
    args = p.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    info = vid.probe(args.video)

    with open(args.report, encoding="utf-8") as f:
        rep = json.load(f)
    ranges = rep.get("estimated_only_ranges", [])
    uncovered = rep.get("uncovered_ranges", [])

    with open(args.detections, encoding="utf-8") as f:
        det = json.load(f)
    per_frame = {
        int(k): [Detection.from_dict(d) for d in v] for k, v in det["detections"].items()
    }
    n_frames = det["n_frames"]
    regions, _ = process(
        per_frame,
        n_frames,
        info.width,
        info.height,
        set(DEFAULT_CLASSES),
        TemporalConfig(
            margin_scale=0.35, margin_cap_px=16, memory_before=20,
            stitch_max_gap=90, motion_weight=2.0,
        ),
    )

    wanted: dict[int, str] = {}
    for r in ranges:
        s, e = r["start_frame"], r["end_frame"]
        for f in range(max(0, s - args.edge), min(n_frames, e + args.edge + 1), args.stride):
            wanted[f] = "est"
        wanted[s] = "est"
        wanted[e] = "est"
    for r in uncovered:
        s, e = r["start_frame"], r["end_frame"]
        for f in range(s, e + 1, args.stride):
            wanted[f] = "unc"
        wanted[s] = "unc"

    print(f"{len(ranges)} 区間 + 未処理 {len(uncovered)} 区間 から {len(wanted)} フレームを抜きます")

    env = dict(os.environ)
    env["PATH"] = (
        os.path.join(env.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links")
        + os.pathsep + env.get("PATH", "")
    )
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", args.video, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
    )

    frame_bytes = info.width * info.height * 3
    manifest = []
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
                cv2.imwrite(os.path.join(args.out_dir, f"{idx:06d}.png"), frame)
                cur = [
                    {
                        "box": [round(v) for v in box],
                        "source": reg.source,
                        "class": reg.cls,
                    }
                    for box, reg in regions.get(idx, [])
                ]
                manifest.append({"frame": idx, "kind": wanted[idx], "current": cur})
            idx += 1
    finally:
        proc.stdout.close()
        proc.wait()

    with open(os.path.join(args.out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "video": os.path.abspath(args.video),
                "width": info.width,
                "height": info.height,
                "fps": info.fps,
                "total_frames": idx,
                "ranges": ranges,
                "uncovered": uncovered,
                "frames": manifest,
            },
            f,
            ensure_ascii=False,
            indent=1,
        )
    print(f"{len(manifest)} 枚を {args.out_dir} に保存しました")


if __name__ == "__main__":
    main()
