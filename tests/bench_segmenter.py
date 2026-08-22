"""NSFW_Segmentation (YOLO11-seg) を実素材で計測する。

やること:
  scan     動画を全フレーム走査し、検出スコア / bbox 面積 / マスク面積を記録する。
           conf を振った集計は記録済みスコアから後段で導出するので、走査は1回で済む。
  speed    (モデル × provider) のスループットを測る。60分@30fps 換算も出す。
  overlay  代表フレームにマスクを重ねた PNG を出す（目視確認用）。
  report   scan の結果を集計して表示する。

実行例:
    .venv/Scripts/python.exe tests/bench_segmenter.py all
    .venv/Scripts/python.exe tests/bench_segmenter.py report
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from automosaic.segmenter import Segmenter  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIDEO = os.path.join(ROOT, "data", "myvideo5", "マイビデオ-5.mp4")
NUDENET_JSON = os.path.join(ROOT, "data", "myvideo5", "compare", "det_tta_size1280.json")
OUT_DIR = os.path.join(ROOT, "data", "myvideo5", "segcheck")
SCAN_JSON = os.path.join(OUT_DIR, "seg_scan.json")
SPEED_JSON = os.path.join(OUT_DIR, "seg_speed.json")

MODELS = {
    "penis-s": "weights/nsfw-seg-penis-s.onnx",
    "penis-x": "weights/nsfw-seg-penis-x.onnx",
    "vagina-s": "weights/nsfw-seg-vagina-s.onnx",
    "vagina-x": "weights/nsfw-seg-vagina-x.onnx",
}

# 走査時はこの下限で拾い、集計時に閾値を上げて数え直す
SCAN_CONF = 0.02
CONF_LEVELS = (0.05, 0.1, 0.25)

# NudeNet 側で刑法175条の対象としているクラス
NUDENET_TARGET = ("FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED")


# ----------------------------------------------------------------------
# scan


def scan(models: list[str], infer_size: int, provider: str, device_id: int) -> dict:
    os.makedirs(OUT_DIR, exist_ok=True)
    result = {
        "video": VIDEO,
        "infer_size": infer_size,
        "provider": provider,
        "scan_conf": SCAN_CONF,
        "models": {},
    }
    if os.path.exists(SCAN_JSON):
        try:
            result = json.load(open(SCAN_JSON, encoding="utf-8"))
        except Exception:
            pass
    result.setdefault("models", {})

    for key in models:
        path = os.path.join(ROOT, MODELS[key])
        seg = Segmenter(
            path,
            infer_size=infer_size,
            conf=SCAN_CONF,
            provider=provider,
            device_id=device_id,
        )
        print(f"[scan] {key} provider={seg.active_provider} size={infer_size}")

        cap = cv2.VideoCapture(VIDEO)
        frames: dict[str, list] = {}
        i = 0
        t0 = time.perf_counter()
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            dets = seg.segment_detections(frame)
            if dets:
                frames[str(i)] = [
                    {
                        "score": round(d.score, 4),
                        "box": list(d.box),
                        "mask_area": d.mask_area,
                        "box_area": d.box_area,
                    }
                    for d in dets
                ]
            i += 1
            if i % 200 == 0:
                el = time.perf_counter() - t0
                print(f"  {i} frames  {i/el:.2f} fps  hits={len(frames)}", flush=True)
        cap.release()
        elapsed = time.perf_counter() - t0
        result["models"][key] = {
            "n_frames": i,
            "elapsed_sec": round(elapsed, 2),
            "fps": round(i / elapsed, 3),
            "provider": seg.active_provider,
            "infer_size": infer_size,
            "frames": frames,
        }
        print(f"[scan] {key} done: {i} frames, {i/elapsed:.2f} fps, hit={len(frames)}")
        json.dump(result, open(SCAN_JSON, "w", encoding="utf-8"), ensure_ascii=False)
    return result


# ----------------------------------------------------------------------
# speed


def speed(infer_size: int, n: int, device_id: int) -> dict:
    os.makedirs(OUT_DIR, exist_ok=True)
    cap = cv2.VideoCapture(VIDEO)
    buf = []
    while len(buf) < n:
        ok, f = cap.read()
        if not ok:
            break
        buf.append(f)
    cap.release()
    print(f"[speed] {len(buf)} frames をメモリに載せた（デコード時間を除外するため）")

    out = {}
    for key, rel in MODELS.items():
        for prov in ("cpu", "dml"):
            path = os.path.join(ROOT, rel)
            try:
                seg = Segmenter(
                    path, infer_size=infer_size, conf=0.25,
                    provider=prov, device_id=device_id,
                )
                for f in buf[:3]:
                    seg.segment_detections(f)   # ウォームアップ
                t = time.perf_counter()
                for f in buf:
                    seg.segment_detections(f)
                dt = time.perf_counter() - t
                fps = len(buf) / dt
                # 60分 @ 30fps = 108000 フレーム
                hours = 108000 / fps / 3600
                out[f"{key}/{prov}"] = {
                    "provider": seg.active_provider,
                    "ms_per_frame": round(dt / len(buf) * 1000, 2),
                    "fps": round(fps, 3),
                    "hours_for_60min_30fps": round(hours, 2),
                }
                print(f"  {key:10s} {prov:4s} {dt/len(buf)*1000:8.1f} ms  "
                      f"{fps:6.2f} fps  60分尺 {hours:.2f} 時間", flush=True)
            except Exception as e:
                out[f"{key}/{prov}"] = {"error": repr(e)[:200]}
                print(f"  {key} {prov} ERROR {e!r}"[:200])
    json.dump(out, open(SPEED_JSON, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    return out


# ----------------------------------------------------------------------
# overlay


def overlay(models: list[str], infer_size: int, provider: str, device_id: int,
            count: int = 10) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    scan_data = json.load(open(SCAN_JSON, encoding="utf-8")) if os.path.exists(SCAN_JSON) else {"models": {}}

    # 検出があったフレームを優先して、無ければ等間隔で選ぶ
    hits: dict[int, float] = {}
    for key in models:
        for fi, ds in scan_data.get("models", {}).get(key, {}).get("frames", {}).items():
            s = max(d["score"] for d in ds)
            hits[int(fi)] = max(hits.get(int(fi), 0.0), s)
    picked = [f for f, _ in sorted(hits.items(), key=lambda kv: -kv[1])[:count]]
    if len(picked) < count:
        cap = cv2.VideoCapture(VIDEO)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        step = max(1, total // (count - len(picked) + 1))
        picked += [i for i in range(0, total, step) if i not in picked][: count - len(picked)]
    picked = sorted(set(picked))
    print(f"[overlay] frames = {picked}")

    segs = [
        Segmenter(os.path.join(ROOT, MODELS[k]), infer_size=infer_size,
                  conf=SCAN_CONF, provider=provider, device_id=device_id)
        for k in models
    ]
    colors = {"PENIS": (0, 0, 255), "VAGINA": (255, 0, 0)}

    cap = cv2.VideoCapture(VIDEO)
    i = 0
    wanted = set(picked)
    while wanted:
        ok, frame = cap.read()
        if not ok:
            break
        if i in wanted:
            wanted.discard(i)
            vis = frame.copy()
            lines = []
            for seg in segs:
                for d in seg.segment_detections(frame):
                    col = colors.get(d.cls, (0, 255, 0))
                    tint = np.zeros_like(vis)
                    tint[:] = col
                    vis[d.mask] = cv2.addWeighted(
                        vis, 0.45, tint, 0.55, 0)[d.mask]
                    x, y, w, h = d.box
                    cv2.rectangle(vis, (x, y), (x + w, y + h), col, 1)
                    ratio = d.mask_area / d.box_area if d.box_area else 0
                    lines.append(f"{d.cls} {d.score:.3f} mask/box={ratio:.2f}")
            for j, t in enumerate(lines[:6]):
                cv2.putText(vis, t, (6, 16 + j * 15), cv2.FONT_HERSHEY_SIMPLEX,
                            0.45, (255, 255, 255), 1, cv2.LINE_AA)
            side = np.hstack([frame, vis])
            cv2.imwrite(os.path.join(OUT_DIR, f"frame{i:05d}.png"), side)
            print(f"  frame{i:05d}.png  {lines}")
        i += 1
    cap.release()


# ----------------------------------------------------------------------
# report


def _nudenet_hits() -> tuple[int, set[int], int]:
    d = json.load(open(NUDENET_JSON, encoding="utf-8"))
    hits = set()
    for fi, ds in d["detections"].items():
        if any(x["class"] in NUDENET_TARGET for x in ds):
            hits.add(int(fi))
    return d["n_frames"], hits, len(hits)


def report() -> None:
    n_frames, nn_hits, nn_count = _nudenet_hits()
    print(f"NudeNet 640m (TTA+1280) 対象クラス検出: {nn_count} / {n_frames} フレーム\n")

    if not os.path.exists(SCAN_JSON):
        print("scan がまだ。`bench_segmenter.py scan` を先に。")
        return
    data = json.load(open(SCAN_JSON, encoding="utf-8"))

    print(f"{'model':10s} {'conf':>5s} {'frames':>7s} {'/1768':>7s} "
          f"{'vs NudeNet':>11s} {'mask/box':>9s} {'mask%frame':>11s} {'dets':>6s}")
    print("-" * 78)
    union_by_conf: dict[float, dict[str, set[int]]] = {c: {} for c in CONF_LEVELS}
    for key, m in data["models"].items():
        for conf in CONF_LEVELS:
            fs = {
                int(fi): [d for d in ds if d["score"] >= conf]
                for fi, ds in m["frames"].items()
            }
            fs = {k: v for k, v in fs.items() if v}
            ratios = [d["mask_area"] / d["box_area"] for ds in fs.values()
                      for d in ds if d["box_area"] > 0]
            areas = [d["mask_area"] / (640 * 480) for ds in fs.values() for d in ds]
            ndet = sum(len(v) for v in fs.values())
            group = "penis" if "penis" in key else "vagina"
            union_by_conf[conf].setdefault(key.split("-")[1], set()).update(fs.keys())
            print(f"{key:10s} {conf:5.2f} {len(fs):7d} {len(fs)/n_frames*100:6.1f}% "
                  f"{len(fs)/nn_count*100:10.1f}% "
                  f"{(np.mean(ratios) if ratios else 0):9.3f} "
                  f"{(np.mean(areas)*100 if areas else 0):10.2f}% {ndet:6d}")

    print("\npenis + vagina を統合したフレーム数（同一サイズ同士）")
    for conf in CONF_LEVELS:
        for size, s in sorted(union_by_conf[conf].items()):
            inter = len(s & nn_hits)
            print(f"  conf={conf:4.2f} size=-{size}: {len(s):5d} / {n_frames} "
                  f"({len(s)/nn_count*100:.1f}% of NudeNet {nn_count})  "
                  f"NudeNet と重なるフレーム {inter}  "
                  f"NudeNet が見逃した所での検出 {len(s - nn_hits)}")

    print("\nスループット（scan 実測、デコード込み）")
    for key, m in data["models"].items():
        fps = m["fps"]
        print(f"  {key:10s} {m['provider']:22s} {fps:6.2f} fps  "
              f"60分@30fps -> {108000/fps/3600:.2f} 時間")

    if os.path.exists(SPEED_JSON):
        print("\nスループット（推論のみ、デコード除外）")
        for k, v in json.load(open(SPEED_JSON, encoding="utf-8")).items():
            if "error" in v:
                print(f"  {k:16s} ERROR {v['error'][:60]}")
            else:
                print(f"  {k:16s} {v['ms_per_frame']:8.1f} ms  {v['fps']:6.2f} fps  "
                      f"60分@30fps -> {v['hours_for_60min_30fps']:.2f} 時間")


# ----------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["scan", "speed", "overlay", "report", "all"])
    ap.add_argument("--models", default="penis-x,vagina-x,penis-s,vagina-s")
    ap.add_argument("--infer-size", type=int, default=832)
    ap.add_argument("--provider", default="dml")
    ap.add_argument("--device-id", type=int, default=1)
    ap.add_argument("--speed-frames", type=int, default=40)
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if args.mode in ("scan", "all"):
        scan(models, args.infer_size, args.provider, args.device_id)
    if args.mode in ("speed", "all"):
        speed(args.infer_size, args.speed_frames, args.device_id)
    if args.mode in ("overlay", "all"):
        overlay([m for m in models if m.endswith("-x")] or models,
                args.infer_size, args.provider, args.device_id)
    if args.mode in ("report", "all"):
        report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
