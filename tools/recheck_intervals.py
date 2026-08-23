"""指定した区間だけを全フレーム検出し直す。

間引き検出で沈黙した区間について、「間引きのせいで見逃したのか」「そもそも
検出器が反応しないのか」を切り分けるためのもの。短い区間ほど間引きの影響を
受けるので、これを分けないと検出器の性能を過小評価する。

区間ごとに ffmpeg で切り出して検出する。フレーム番号は元動画基準に戻す。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic.detector import DEFAULT_CLASSES, Detector  # noqa: E402
from automosaic import video as vid  # noqa: E402

import numpy as np  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("video")
    p.add_argument("--intervals", required=True)
    p.add_argument("--only-silent", help="突き合わせ結果の JSON。沈黙した区間だけ再検査する")
    p.add_argument("--model", default=os.path.join(ROOT, "weights", "640m.onnx"))
    p.add_argument("--infer-size", type=int, default=1280)
    p.add_argument("--conf", type=float, default=0.06)
    p.add_argument("--device-id", type=int, default=1)
    p.add_argument("--pad-sec", type=float, default=0.5, help="区間の前後に足す秒数")
    p.add_argument("--out", help="結果 JSON")
    args = p.parse_args()

    classes = set(DEFAULT_CLASSES)
    with open(args.intervals, encoding="utf-8") as f:
        rep = json.load(f)
    targets = rep["intervals"]

    if args.only_silent:
        with open(args.only_silent, encoding="utf-8") as f:
            silent = json.load(f)
        keys = {(s["start_sec"], s["end_sec"]) for s in silent}
        targets = [t for t in targets if (t["start_sec"], t["end_sec"]) in keys]

    info = vid.probe(args.video)
    det = Detector(
        model_path=args.model,
        infer_size=args.infer_size,
        conf=args.conf,
        provider="auto",
        device_id=args.device_id,
    )
    print(f"再検査 {len(targets)} 区間  推論 {args.infer_size}px  conf {args.conf}")
    print(f"プロバイダ {det.active_provider}\n")

    env = dict(os.environ)
    env["PATH"] = (
        os.path.join(env.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links")
        + os.pathsep + env.get("PATH", "")
    )

    results = []
    with tempfile.TemporaryDirectory() as td:
        for i, it in enumerate(targets):
            start = max(0.0, it["start_sec"] - args.pad_sec)
            dur = (it["end_sec"] - it["start_sec"]) + 1 + args.pad_sec * 2
            clip = os.path.join(td, f"c{i}.mp4")
            # -ss を -i の前に置くと速いが不正確。ここは短い区間なので後ろに置いて正確に取る
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", args.video,
                 "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                 "-an", "-c:v", "libx264", "-crf", "14", "-preset", "veryfast", clip],
                env=env, check=True,
            )

            cinfo = vid.probe(clip)
            dec_w, dec_h = vid.detection_frame_size(cinfo, args.infer_size, path=clip)
            fb = dec_w * dec_h * 3
            scale_back = cinfo.width / dec_w
            proc = vid.open_detection_reader(clip, args.infer_size, None)

            hits = []
            idx = 0
            try:
                while True:
                    raw = proc.stdout.read(fb)
                    if len(raw) < fb:
                        break
                    frame = np.frombuffer(raw, dtype=np.uint8).reshape(dec_h, dec_w, 3)
                    for d in det.detect_frame(frame):
                        if d.cls in classes:
                            hits.append(
                                {
                                    "offset_frame": idx,
                                    "sec": round(start + idx / cinfo.fps, 2),
                                    "class": d.cls,
                                    "score": round(d.score, 3),
                                    "box": [int(v * scale_back) for v in d.box],
                                }
                            )
                    idx += 1
            finally:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
                proc.wait()

            best = max((h["score"] for h in hits), default=0.0)
            n_hit_frames = len({h["offset_frame"] for h in hits})
            results.append(
                {
                    "start_sec": it["start_sec"],
                    "end_sec": it["end_sec"],
                    "desc": it["desc"],
                    "frames_scanned": idx,
                    "hit_frames": n_hit_frames,
                    "max_score": best,
                    "detected": n_hit_frames > 0,
                    "hits": hits[:10],
                }
            )
            mark = f"{n_hit_frames}/{idx}" if n_hit_frames else "なし"
            print(
                f"  {it['start_sec'] // 60}:{it['start_sec'] % 60:02d}"
                f"-{it['end_sec'] // 60}:{it['end_sec'] % 60:02d}  "
                f"{mark:>8s}  最大score {best:.2f}  {it['desc']}"
            )

    n_det = sum(1 for r in results if r["detected"])
    print(f"\n全フレーム検査で {n_det}/{len(results)} 区間が検出できた")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"results": results}, f, ensure_ascii=False, indent=1)
        print(f"{args.out} に保存")


if __name__ == "__main__":
    main()
