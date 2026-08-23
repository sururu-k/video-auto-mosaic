"""Cascade Mask R-CNN (MMDetection -> MMDeploy ONNX) をこのマシンで実測する。

`.venv-mmdet` で動かす。メインの `.venv` は触らない。

    .venv-mmdet\\Scripts\\python.exe tests\\bench_mmdet.py speed
    .venv-mmdet\\Scripts\\python.exe tests\\bench_mmdet.py check

speed  実素材のフレームで (解像度 x provider) のスループットを測る。
check  実素材1枚に推論して、検出とマスクが妥当かを PNG に焼く。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import cv2
import numpy as np
import onnxruntime as ort

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MODEL = os.path.join(ROOT, "weights", "cascade_mask_rcnn_r101_fpn.onnx")
VIDEO = os.path.join(ROOT, "data", "bench3", "clips", "src_0501.mp4")
OUT_DIR = os.path.join(ROOT, "data", "mmdet_bench")

# mmdet の data_preprocessor。系列でまったく違うので取り違えないこと
#   torchvision 系 (Mask R-CNN / Cascade Mask R-CNN): RGB 変換あり
#   rtmdet 系: BGR のまま、mean/std が逆順
NORMS = {
    "torchvision": (np.array([123.675, 116.28, 103.53], dtype=np.float32),
                    np.array([58.395, 57.12, 57.375], dtype=np.float32), True),
    "rtmdet": (np.array([103.53, 116.28, 123.675], dtype=np.float32),
               np.array([57.375, 57.12, 58.395], dtype=np.float32), False),
}
PAD_DIVISOR = 32

COCO = (
    "person bicycle car motorcycle airplane bus train truck boat traffic_light "
    "fire_hydrant stop_sign parking_meter bench bird cat dog horse sheep cow "
    "elephant bear zebra giraffe backpack umbrella handbag tie suitcase frisbee "
    "skis snowboard sports_ball kite baseball_bat baseball_glove skateboard "
    "surfboard tennis_racket bottle wine_glass cup fork knife spoon bowl banana "
    "apple sandwich orange broccoli carrot hot_dog pizza donut cake chair couch "
    "potted_plant bed dining_table toilet tv laptop mouse remote keyboard "
    "cell_phone microwave oven toaster sink refrigerator book clock vase "
    "scissors teddy_bear hair_drier toothbrush"
).split()


def preprocess(bgr: np.ndarray, dst_w: int, dst_h: int,
               norm: str = "torchvision") -> tuple[np.ndarray, float]:
    """mmdet の Resize(keep_ratio) + Normalize + Pad 相当。"""
    mean, std, to_rgb = NORMS[norm]
    h, w = bgr.shape[:2]
    scale = min(dst_w / w, dst_h / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    img = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
    if to_rgb:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    rgb = (img.astype(np.float32) - mean) / std
    ph = (nh + PAD_DIVISOR - 1) // PAD_DIVISOR * PAD_DIVISOR
    pw = (nw + PAD_DIVISOR - 1) // PAD_DIVISOR * PAD_DIVISOR
    out = np.zeros((ph, pw, 3), dtype=np.float32)
    out[:nh, :nw] = rgb
    return out.transpose(2, 0, 1)[None], scale


def make_session(model: str, provider: str, device_id: int) -> ort.InferenceSession:
    so = ort.SessionOptions()
    so.log_severity_level = 3
    if provider == "dml":
        providers = [("DmlExecutionProvider", {"device_id": device_id}),
                     "CPUExecutionProvider"]
    else:
        providers = ["CPUExecutionProvider"]
    return ort.InferenceSession(model, so, providers=providers)


def load_frames(video: str, n: int) -> list[np.ndarray]:
    cap = cv2.VideoCapture(video)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or n * 10
    step = max(1, total // n)
    frames, i = [], 0
    while len(frames) < n:
        ok, f = cap.read()
        if not ok:
            break
        if i % step == 0:
            frames.append(f)
        i += 1
    cap.release()
    if not frames:
        raise SystemExit(f"フレームが読めない: {video}")
    return frames


# ----------------------------------------------------------------------


def speed(args) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    raw = load_frames(args.video, args.frames)
    print(f"[speed] {len(raw)} フレーム  素材 {raw[0].shape[1]}x{raw[0].shape[0]}  "
          f"model={os.path.basename(args.model)}")

    sizes = []
    for spec in args.sizes.split(","):
        w, h = (int(v) for v in spec.lower().split("x"))
        sizes.append((w, h))

    results = {}
    for provider in args.providers.split(","):
        try:
            sess = make_session(args.model, provider, args.device_id)
        except Exception as e:  # noqa: BLE001
            print(f"  provider={provider} 初期化失敗: {e}")
            continue
        active = sess.get_providers()[0]
        for w, h in sizes:
            batch = [preprocess(f, w, h, args.norm)[0] for f in raw]
            ndets = []
            try:
                for x in batch[:2]:
                    sess.run(None, {"input": x})       # ウォームアップ
                t0 = time.perf_counter()
                for x in batch:
                    o = sess.run(None, {"input": x})
                    ndets.append(int(o[0].shape[1]))
                dt = time.perf_counter() - t0
            except Exception as e:  # noqa: BLE001
                print(f"  {provider} {w}x{h}  実行失敗: {str(e)[:200]}")
                continue
            fps = len(batch) / dt
            hours = 108000 / fps / 3600      # 60分 @ 30fps
            key = f"{provider}/{w}x{h}"
            results[key] = {
                "active_provider": active,
                "input_shape": list(batch[0].shape),
                "ms_per_frame": round(dt / len(batch) * 1000, 1),
                "fps": round(fps, 3),
                "hours_for_60min_30fps": round(hours, 2),
                "mean_dets": round(float(np.mean(ndets)), 1),
            }
            print(f"  {provider:4s} {w}x{h:<5d} 実入力{tuple(batch[0].shape[2:])} "
                  f"{dt/len(batch)*1000:8.1f} ms  {fps:6.3f} fps  "
                  f"60分@30fps -> {hours:6.2f} 時間  平均det {np.mean(ndets):.1f}")
    path = os.path.join(OUT_DIR, "mmdet_speed.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"[speed] -> {path}")


def check(args) -> None:
    """マスクが実際に出ているか、COCO クラスが妥当かを目視できる形にする。"""
    os.makedirs(OUT_DIR, exist_ok=True)
    sess = make_session(args.model, args.providers.split(",")[0], args.device_id)
    frames = load_frames(args.video, args.check_frames)
    w, h = (int(v) for v in args.sizes.split(",")[0].lower().split("x"))

    for idx, bgr in enumerate(frames):
        x, scale = preprocess(bgr, w, h, args.norm)
        dets, labels, masks = sess.run(None, {"input": x})
        dets, labels, masks = dets[0], labels[0], masks[0]
        vis = bgr.copy()
        lines = []
        for d, lb, mk in zip(dets, labels, masks):
            if d[4] < args.conf:
                continue
            x1, y1, x2, y2 = (d[:4] / scale).astype(int)
            x1, y1 = max(x1, 0), max(y1, 0)
            x2, y2 = min(x2, bgr.shape[1]), min(y2, bgr.shape[0])
            if x2 <= x1 or y2 <= y1:
                continue
            if mk.shape[:2] == x.shape[2:]:
                # RTMDet-Ins は入力サイズのマスクをそのまま出す
                full = cv2.resize(mk, (bgr.shape[1], bgr.shape[0]))
                m = full[y1:y2, x1:x2] > 0.5
            else:
                # Mask R-CNN 系は RoI 内 28x28
                m = cv2.resize(mk, (x2 - x1, y2 - y1)) > 0.5
            sub = vis[y1:y2, x1:x2]
            tint = np.zeros_like(sub)
            tint[:] = (0, 0, 255)
            sub[m] = cv2.addWeighted(sub, 0.5, tint, 0.5, 0)[m]
            cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 255, 0), 1)
            name = COCO[int(lb)] if 0 <= int(lb) < len(COCO) else str(int(lb))
            lines.append(f"{name} {d[4]:.2f} mask/box={m.mean():.2f}")
        for j, t in enumerate(lines[:8]):
            cv2.putText(vis, t, (6, 16 + j * 15), cv2.FONT_HERSHEY_SIMPLEX,
                        0.45, (255, 255, 255), 1, cv2.LINE_AA)
        out = os.path.join(OUT_DIR, f"check_{idx:03d}.png")
        cv2.imwrite(out, np.hstack([bgr, vis]))
        print(f"  {out}  dets>{args.conf}: {len(lines)}  {lines[:4]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["speed", "check"])
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--video", default=VIDEO)
    ap.add_argument("--sizes", default="1333x800,1280x720,960x960,640x480")
    ap.add_argument("--providers", default="dml,cpu")
    ap.add_argument("--device-id", type=int, default=0)
    ap.add_argument("--frames", type=int, default=12)
    ap.add_argument("--check-frames", type=int, default=4)
    ap.add_argument("--conf", type=float, default=0.3)
    ap.add_argument("--norm", default="torchvision", choices=list(NORMS))
    args = ap.parse_args()

    if args.mode == "speed":
        speed(args)
    else:
        check(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
