"""DirectML のアダプタごとに 640m のスループットを測る。

このマシンには Parsec 仮想アダプタ・Radeon 760M(iGPU)・RX 560 が刺さっている。
どれが割り当てられるかは device_id 次第なので実測して選ぶ。
"""

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic.detector import Detector  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = os.path.join(ROOT, "weights", "640m.onnx")


def main() -> None:
    n = 25
    rng = np.random.default_rng(0)
    frames = [rng.integers(0, 256, size=(640, 640, 3), dtype=np.uint8) for _ in range(n + 3)]

    for dev in range(4):
        try:
            det = Detector(MODEL, infer_size=640, provider="dml", device_id=dev)
        except Exception as e:  # noqa: BLE001
            print(f"  device_id={dev}  初期化失敗: {type(e).__name__}")
            continue
        try:
            for f in frames[:3]:
                det.detect_square(f, 1920.0)
            t0 = time.perf_counter()
            for f in frames[3:]:
                det.detect_square(f, 1920.0)
            dt = time.perf_counter() - t0
        except Exception as e:  # noqa: BLE001
            print(f"  device_id={dev}  推論失敗: {type(e).__name__}")
            continue
        fps = n / dt
        print(f"  device_id={dev}  {fps:6.2f} fps   60分@30fps = {108000 / fps / 3600:5.2f} 時間")
        del det


if __name__ == "__main__":
    main()
