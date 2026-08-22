"""検出のスループットを測る。

長尺の処理時間はここで決まる。60分尺の見積もりまで出す。
"""

import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic.detector import Detector, available_providers  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bench(model: str, size: int, provider: str, n: int, warmup: int = 3) -> float | None:
    path = os.path.join(ROOT, "weights", model)
    if not os.path.exists(path):
        print(f"  {model:12s} {provider:5s}  モデルなし: {path}")
        return None
    try:
        det = Detector(path, infer_size=size, provider=provider)
    except Exception as e:  # noqa: BLE001
        print(f"  {model:12s} {provider:5s}  初期化失敗: {e}")
        return None

    rng = np.random.default_rng(0)
    frames = [
        rng.integers(0, 256, size=(size, size, 3), dtype=np.uint8) for _ in range(warmup + n)
    ]

    for f in frames[:warmup]:
        det.detect_square(f, 1920.0)

    t0 = time.perf_counter()
    for f in frames[warmup:]:
        det.detect_square(f, 1920.0)
    dt = time.perf_counter() - t0

    fps = n / dt
    h30 = 108000 / fps / 3600  # 60分@30fps
    print(
        f"  {model:12s} {det.active_provider.replace('ExecutionProvider',''):5s} "
        f"{fps:7.2f} fps   60分@30fps = {h30:5.2f} 時間"
    )
    return fps


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("-n", type=int, default=30, help="計測フレーム数")
    args = p.parse_args()

    print("利用可能なプロバイダ:", ", ".join(available_providers()))
    print(f"\n{args.n} フレームで計測（合成フレーム、デコード時間は含まない）\n")

    combos = [
        ("320n.onnx", 320, "cpu"),
        ("640m.onnx", 640, "cpu"),
    ]
    if "DmlExecutionProvider" in available_providers():
        combos += [
            ("320n.onnx", 320, "dml"),
            ("640m.onnx", 640, "dml"),
        ]

    for model, size, provider in combos:
        bench(model, size, provider, args.n)


if __name__ == "__main__":
    main()
