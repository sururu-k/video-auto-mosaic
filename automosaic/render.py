"""YUV 平面上で直接モザイクをかける。

RGB に変換して戻すと 8bit 往復で微小な色劣化が入るので、planar YUV のまま扱う。
4:2:0 の彩度平面は解像度が縦横とも半分なので、ブロックサイズと座標を //2 する。

ブロックの格子はフレーム座標に固定する。対象の bbox 基準にすると、
対象が動くたびに格子位置がずれてモザイクがチラつく。
"""

from __future__ import annotations

import cv2
import numpy as np

Box = tuple[float, float, float, float]


def default_block_size(long_edge: int) -> int:
    """FANZA同人 販売倫理規程 第6条の「長辺 x 1/100」「最小4px四方」を出発点にする。

    これは同人・静止画向けの基準であって実写動画を規律するものではない。
    法令に数値基準は存在しないので、あくまで下限の下限として扱うこと。
    彩度平面を //2 するため偶数に丸める。
    """
    b = max(4, int(round(long_edge / 100.0)))
    return b + (b % 2)


class FrameBuffer:
    """1フレーム分の planar YUV を numpy view として持つ。"""

    def __init__(self, width: int, height: int, ten_bit: bool = False) -> None:
        if width % 2 or height % 2:
            raise ValueError(f"4:2:0 は偶数解像度が必要です: {width}x{height}")
        self.width = width
        self.height = height
        self.dtype = np.uint16 if ten_bit else np.uint8
        self.itemsize = 2 if ten_bit else 1
        self.y_size = width * height
        self.c_w = width // 2
        self.c_h = height // 2
        self.c_size = self.c_w * self.c_h
        self.nbytes = (self.y_size + self.c_size * 2) * self.itemsize

    def wrap(self, raw: bytes) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        arr = np.frombuffer(raw, dtype=self.dtype)
        y = arr[: self.y_size].reshape(self.height, self.width)
        u = arr[self.y_size : self.y_size + self.c_size].reshape(self.c_h, self.c_w)
        v = arr[self.y_size + self.c_size :].reshape(self.c_h, self.c_w)
        # frombuffer は read-only view を返すのでコピーして書き込み可能にする
        return y.copy(), u.copy(), v.copy()

    @staticmethod
    def pack(y: np.ndarray, u: np.ndarray, v: np.ndarray) -> bytes:
        return y.tobytes() + u.tobytes() + v.tobytes()


def _snap(v: float, block: int, limit: int, ceil: bool) -> int:
    """フレーム座標系の格子にスナップする。"""
    if ceil:
        s = int(np.ceil(v / block) * block)
    else:
        s = int(np.floor(v / block) * block)
    return max(0, min(limit, s))


def pixelize_plane(plane: np.ndarray, box: Box, block: int) -> None:
    """平面の矩形領域をブロック平均色で潰す。in-place。

    平均色1色への量子化なので情報が実際に落ちる（不可逆）。
    ガウシアンブラーはデコンボリューションで復元可能なので使わない。
    """
    h, w = plane.shape
    x, y, bw, bh = box
    x0 = _snap(x, block, w, ceil=False)
    y0 = _snap(y, block, h, ceil=False)
    x1 = _snap(x + bw, block, w, ceil=True)
    y1 = _snap(y + bh, block, h, ceil=True)
    if x1 <= x0 or y1 <= y0:
        return

    roi = plane[y0:y1, x0:x1]
    rh, rw = roi.shape
    nw = max(1, rw // block)
    nh = max(1, rh // block)

    small = cv2.resize(roi, (nw, nh), interpolation=cv2.INTER_AREA)
    plane[y0:y1, x0:x1] = cv2.resize(small, (rw, rh), interpolation=cv2.INTER_NEAREST)


def fill_plane(plane: np.ndarray, box: Box, value: int) -> None:
    h, w = plane.shape
    x0 = max(0, int(box[0]))
    y0 = max(0, int(box[1]))
    x1 = min(w, int(np.ceil(box[0] + box[2])))
    y1 = min(h, int(np.ceil(box[1] + box[3])))
    if x1 <= x0 or y1 <= y0:
        return
    plane[y0:y1, x0:x1] = value


def apply_regions(
    y: np.ndarray,
    u: np.ndarray,
    v: np.ndarray,
    boxes: list[Box],
    block: int,
    mode: str = "pixelize",
    ten_bit: bool = False,
) -> None:
    """フレームの全領域にモザイクをかける。in-place。"""
    if not boxes:
        return

    c_block = max(2, block // 2)
    for box in boxes:
        if mode == "pixelize":
            pixelize_plane(y, box, block)
            cbox = (box[0] / 2, box[1] / 2, box[2] / 2, box[3] / 2)
            pixelize_plane(u, cbox, c_block)
            pixelize_plane(v, cbox, c_block)
        elif mode == "black":
            neutral = 512 if ten_bit else 128
            luma = 64 if ten_bit else 16
            fill_plane(y, box, luma)
            cbox = (box[0] / 2, box[1] / 2, box[2] / 2, box[3] / 2)
            fill_plane(u, cbox, neutral)
            fill_plane(v, cbox, neutral)
        else:
            raise ValueError(f"不明なモード: {mode}")
