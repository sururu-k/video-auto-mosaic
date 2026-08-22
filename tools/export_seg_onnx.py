"""NSFW_Segmentation の YOLO11-seg (.pt) を ONNX にエクスポートする。

このスクリプトは ultralytics + torch を要求するので、本番の .venv ではなく
専用の .venv-export で実行すること。

    .venv-export/Scripts/python.exe tools/export_seg_onnx.py

出力される ONNX は入力が動的 (batch, 3, height, width)。素材の解像度に応じて
推論解像度を変えられるようにするため。ただし YOLO は stride 32 なので
実際に渡せる H/W は 32 の倍数に限られる。

エクスポート後の検証 (onnxruntime での読み込み確認) は .venv 側で行う:

    .venv/Scripts/python.exe tools/export_seg_onnx.py --verify-only
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# .pt のベース名。weights/ 直下に置いてある前提。
MODELS = [
    "nsfw-seg-penis-s",
    "nsfw-seg-penis-x",
    "nsfw-seg-vagina-s",
    "nsfw-seg-vagina-x",
]

WEIGHTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "weights")


def export_all(opset: int, imgsz: int, simplify: bool) -> list[str]:
    """ultralytics で .pt -> .onnx。要 .venv-export。"""
    from ultralytics import YOLO

    produced = []
    for name in MODELS:
        pt = os.path.join(WEIGHTS_DIR, f"{name}.pt")
        if not os.path.exists(pt):
            print(f"[skip] {pt} が無い")
            continue
        onnx_path = os.path.join(WEIGHTS_DIR, f"{name}.onnx")
        print(f"[export] {name}.pt -> {name}.onnx (opset={opset}, dynamic=True)")
        model = YOLO(pt)
        out = model.export(
            format="onnx",
            opset=opset,
            dynamic=True,   # (batch, 3, height, width) を可変にする
            simplify=simplify,
            imgsz=imgsz,    # dynamic でもトレース用に一度この解像度が使われる
            half=False,
            device="cpu",
        )
        # export() は生成パスを返す。名前が想定と違えばリネームしておく。
        out = str(out)
        if os.path.abspath(out) != os.path.abspath(onnx_path):
            os.replace(out, onnx_path)
        produced.append(onnx_path)
        print(f"  -> {onnx_path} ({os.path.getsize(onnx_path)/1e6:.1f} MB)")
    return produced


def verify(paths: list[str]) -> dict:
    """onnxruntime で読めるか、入出力 shape が取れるかを確認する。

    .venv 側 (onnxruntime-directml) で実行することを想定。
    """
    import numpy as np
    import onnxruntime

    print(f"onnxruntime {onnxruntime.__version__} / providers={onnxruntime.get_available_providers()}")
    report = {}
    for p in paths:
        if not os.path.exists(p):
            print(f"[skip] {p} が無い")
            continue
        sess = onnxruntime.InferenceSession(p, providers=["CPUExecutionProvider"])
        info = {
            "inputs": [
                {"name": i.name, "shape": list(i.shape), "type": i.type}
                for i in sess.get_inputs()
            ],
            "outputs": [
                {"name": o.name, "shape": list(o.shape), "type": o.type}
                for o in sess.get_outputs()
            ],
        }
        # 実際に 1 枚流して具体的な shape を見る
        iname = sess.get_inputs()[0].name
        for size in (640, 832):
            blob = np.zeros((1, 3, size, size), dtype=np.float32)
            outs = sess.run(None, {iname: blob})
            info[f"actual_{size}"] = [list(o.shape) for o in outs]
        report[os.path.basename(p)] = info
        print(json.dumps({os.path.basename(p): info}, indent=2, ensure_ascii=False))
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--opset", type=int, default=17,
                    help="ONNX opset。ORT 1.24 は 17 を問題なく扱える")
    ap.add_argument("--imgsz", type=int, default=640,
                    help="トレース時の解像度。dynamic なので実行時は変えられる")
    ap.add_argument("--no-simplify", action="store_true")
    ap.add_argument("--verify-only", action="store_true",
                    help="エクスポートせず onnxruntime での読み込み確認だけ行う")
    args = ap.parse_args()

    paths = [os.path.join(WEIGHTS_DIR, f"{n}.onnx") for n in MODELS]
    if not args.verify_only:
        export_all(args.opset, args.imgsz, not args.no_simplify)
    try:
        verify(paths)
    except ImportError as e:
        print(f"[verify skip] {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
