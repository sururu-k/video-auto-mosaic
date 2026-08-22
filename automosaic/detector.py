"""NudeNet の ONNX 重みを自前セッションで回す検出器。

nudenet パッケージの NudeDetector を使わない理由:
  - 信頼度しきい値 0.2 と NMS が _postprocess にハードコードされている。
    本用途は Recall 優先なのでもっと下げたい。
  - __init__ の providers 引数が実装側でコメントアウトされており、
    常に CPU 実行になる。DirectML を使えない。
前処理・後処理のロジック自体は nudenet.py と互換（レターボックスは右下パディング）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import cv2
import numpy as np
import onnxruntime

# nudenet.py の __labels と同順。ONNX 出力のクラス次元がこの順に対応する。
LABELS = [
    "FEMALE_GENITALIA_COVERED",
    "FACE_FEMALE",
    "BUTTOCKS_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_BREAST_EXPOSED",
    "ANUS_EXPOSED",
    "FEET_EXPOSED",
    "BELLY_COVERED",
    "FEET_COVERED",
    "ARMPITS_COVERED",
    "ARMPITS_EXPOSED",
    "FACE_MALE",
    "BELLY_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_COVERED",
    "FEMALE_BREAST_COVERED",
    "BUTTOCKS_COVERED",
]

# 刑法175条の対象範囲。露出している性器と肛門。
DEFAULT_CLASSES = (
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
)

# 参考: COVERED 系まで潰したい場合に指定する候補
CONSERVATIVE_CLASSES = DEFAULT_CLASSES + (
    "FEMALE_GENITALIA_COVERED",
    "ANUS_COVERED",
)


@dataclass
class Detection:
    """検出1件。box は元動画の座標系 (x, y, w, h)。"""

    cls: str
    score: float
    box: tuple[int, int, int, int]

    def as_dict(self) -> dict:
        return {"class": self.cls, "score": round(self.score, 4), "box": list(self.box)}

    @staticmethod
    def from_dict(d: dict) -> "Detection":
        return Detection(d["class"], float(d["score"]), tuple(int(v) for v in d["box"]))


def available_providers() -> list[str]:
    return list(onnxruntime.get_available_providers())


def resolve_providers(name: str | None) -> list[str]:
    """provider 名から ONNX Runtime の provider リストを組み立てる。

    'auto' は DirectML があればそれを、無ければ CPU を選ぶ。
    このマシンは AMD GPU なので CUDA は存在しない。
    """
    avail = available_providers()
    if name is None or name == "auto":
        for p in ("DmlExecutionProvider", "CUDAExecutionProvider"):
            if p in avail:
                return [p, "CPUExecutionProvider"]
        return ["CPUExecutionProvider"]
    if name == "cpu":
        return ["CPUExecutionProvider"]
    if name == "dml":
        if "DmlExecutionProvider" not in avail:
            raise RuntimeError(
                "DmlExecutionProvider が使えません。"
                "`pip install onnxruntime-directml` を入れてください。"
                f" 利用可能: {avail}"
            )
        return ["DmlExecutionProvider", "CPUExecutionProvider"]
    if name == "cuda":
        if "CUDAExecutionProvider" not in avail:
            raise RuntimeError(f"CUDAExecutionProvider が使えません。利用可能: {avail}")
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return [name]


class Detector:
    """ONNX の物体検出器。入力は letterbox 済み正方フレーム前提。"""

    def __init__(
        self,
        model_path: str,
        infer_size: int = 640,
        conf: float = 0.12,
        nms_iou: float = 0.45,
        provider: str | None = "auto",
        intra_threads: int = 0,
        device_id: int = 0,
        merge_mode: str = "union",
    ) -> None:
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"モデルが見つかりません: {model_path}")

        opts = onnxruntime.SessionOptions()
        opts.graph_optimization_level = (
            onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
        )
        if intra_threads > 0:
            opts.intra_op_num_threads = intra_threads

        self.providers = resolve_providers(provider)
        provider_options = [
            {"device_id": device_id} if p == "DmlExecutionProvider" else {}
            for p in self.providers
        ]
        self.session = onnxruntime.InferenceSession(
            model_path,
            sess_options=opts,
            providers=self.providers,
            provider_options=provider_options,
        )
        self.input_name = self.session.get_inputs()[0].name
        self.infer_size = infer_size
        self.conf = conf
        self.nms_iou = nms_iou
        self.merge_mode = merge_mode

    @property
    def active_provider(self) -> str:
        return self.session.get_providers()[0]

    def detect_square(
        self, square_bgr: np.ndarray, scale_back: float
    ) -> list[Detection]:
        """letterbox 済みの正方 BGR フレーム1枚を推論する。

        scale_back: 正方フレーム座標 -> 元動画座標 の倍率。
        """
        raw = self._infer(square_bgr)
        ratio = scale_back / self.infer_size
        raw = [(c, sc, (x * ratio, y * ratio, w * ratio, h * ratio)) for c, sc, (x, y, w, h) in raw]
        return self._nms(raw)

    def detect_frame(
        self,
        frame_bgr: np.ndarray,
        tta: bool = False,
        tiles: int = 1,
        tile_overlap: float = 0.25,
    ) -> list[Detection]:
        """デコード済みフレームを推論し、そのフレームの座標系で返す。

        再学習せずに Recall を上げるための2手を持つ:
          tta    水平反転した推論結果もマージする。左右非対称な学習の偏りを打ち消す
          tiles  フレームをタイルに割って各タイルを推論解像度いっぱいに拡大する。
                 小さく写る対象は入力縮小でサブピクセル化して消えるので、これが効く

        いずれも推論回数がそのまま増える。tta で2倍、tiles=2 で (1 + 4) 倍。
        """
        h, w = frame_bgr.shape[:2]
        collected: list[tuple[int, float, tuple[float, float, float, float]]] = []

        # 全体を1枚として見る（大きく写っている対象はこちらで取る）
        collected += self._detect_window(frame_bgr, 0, 0, tta)

        if tiles > 1:
            step_x = w / tiles
            step_y = h / tiles
            pad_x = step_x * tile_overlap
            pad_y = step_y * tile_overlap
            for ty in range(tiles):
                for tx in range(tiles):
                    x0 = int(max(0, tx * step_x - pad_x))
                    y0 = int(max(0, ty * step_y - pad_y))
                    x1 = int(min(w, (tx + 1) * step_x + pad_x))
                    y1 = int(min(h, (ty + 1) * step_y + pad_y))
                    if x1 - x0 < 16 or y1 - y0 < 16:
                        continue
                    collected += self._detect_window(
                        frame_bgr[y0:y1, x0:x1], x0, y0, tta
                    )

        return self._nms(collected)

    def _detect_window(
        self, window_bgr: np.ndarray, off_x: int, off_y: int, tta: bool
    ) -> list[tuple[int, float, tuple[float, float, float, float]]]:
        """1つの窓（全体またはタイル）を推論し、フレーム座標に戻す。"""
        wh, ww = window_bgr.shape[:2]
        side = max(wh, ww)
        square = np.zeros((side, side, 3), dtype=window_bgr.dtype)
        square[:wh, :ww] = window_bgr
        # 正方(side) -> 推論解像度 の逆変換倍率
        ratio = side / self.infer_size

        out: list[tuple[int, float, tuple[float, float, float, float]]] = []
        for det in self._infer(square):
            cls_id, score, (x, y, bw, bh) = det
            out.append(
                (cls_id, score, (x * ratio + off_x, y * ratio + off_y, bw * ratio, bh * ratio))
            )

        if tta:
            flipped = square[:, ::-1].copy()
            for cls_id, score, (x, y, bw, bh) in self._infer(flipped):
                # 反転を戻す。正方の辺は infer_size 換算なので x' = infer_size - x - w
                fx = self.infer_size - x - bw
                out.append(
                    (
                        cls_id,
                        score,
                        (fx * ratio + off_x, y * ratio + off_y, bw * ratio, bh * ratio),
                    )
                )
        return out

    def _infer(
        self, square_bgr: np.ndarray
    ) -> list[tuple[int, float, tuple[float, float, float, float]]]:
        """正方フレームを1回推論し、推論解像度の座標系で (クラスID, score, xywh) を返す。"""
        blob = cv2.dnn.blobFromImage(
            square_bgr,
            1 / 255.0,
            (self.infer_size, self.infer_size),
            (0, 0, 0),
            swapRB=True,
            crop=False,
        )
        outputs = self.session.run(None, {self.input_name: blob})

        # (1, 4+num_classes, N) -> (N, 4+num_classes)
        preds = np.transpose(np.squeeze(outputs[0]))
        class_scores = preds[:, 4:]
        max_scores = class_scores.max(axis=1)

        keep = max_scores >= self.conf
        if not np.any(keep):
            return []

        preds = preds[keep]
        scores = max_scores[keep]
        class_ids = class_scores[keep].argmax(axis=1)

        cx, cy, bw, bh = preds[:, 0], preds[:, 1], preds[:, 2], preds[:, 3]
        x = cx - bw / 2
        y = cy - bh / 2
        return [
            (int(ci), float(sc), (float(a), float(b), float(c), float(d)))
            for ci, sc, a, b, c, d in zip(class_ids, scores, x, y, bw, bh)
        ]

    def _nms(
        self, raw: list[tuple[int, float, tuple[float, float, float, float]]]
    ) -> list[Detection]:
        """重複検出をまとめて Detection に落とす。

        クラスをまたいだ抑制はしない。局部と臀部のように重なる部位が
        互いを消し合うと、片方を取りこぼすことになる。

        merge_mode:
          union  重なる検出を外接矩形に統合する（既定）。NMS は重なった候補を
                 「消す」ので、消された側がはみ出していた分の被覆が失われる。
                 過剰は許容・漏らすのは不可、という要件では統合が正しい
          nms    従来どおり最高スコアの1個を残す
        """
        if not raw:
            return []

        by_class: dict[int, list[tuple[float, tuple[float, float, float, float]]]] = {}
        for cls_id, score, box in raw:
            by_class.setdefault(cls_id, []).append((score, box))

        results: list[Detection] = []
        for cls_id, items in by_class.items():
            if self.merge_mode == "union":
                merged = _union_merge(items, self.nms_iou)
            else:
                merged = _nms_merge(items, self.conf, self.nms_iou)
            for score, (bx, by, bw, bh) in merged:
                results.append(
                    Detection(
                        cls=LABELS[cls_id],
                        score=score,
                        box=(
                            int(round(bx)),
                            int(round(by)),
                            int(round(bw)),
                            int(round(bh)),
                        ),
                    )
                )
        return results


def _iou_xywh(a, b) -> float:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax0 + aw, bx0 + bw), min(ay0 + ah, by0 + bh)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _union_merge(items, iou_threshold: float):
    """重なる検出を外接矩形に統合する。スコアはクラスタ内の最大値。

    スコア降順に見て、既存クラスタと重なれば取り込んで矩形を広げる。
    取り込みで矩形が広がるため、後続の判定は広がった矩形に対して行う。
    """
    items = sorted(items, key=lambda t: -t[0])
    clusters: list[list] = []  # [score, x, y, w, h]
    for score, box in items:
        placed = False
        for c in clusters:
            if _iou_xywh((c[1], c[2], c[3], c[4]), box) >= iou_threshold:
                x0 = min(c[1], box[0])
                y0 = min(c[2], box[1])
                x1 = max(c[1] + c[3], box[0] + box[2])
                y1 = max(c[2] + c[4], box[1] + box[3])
                c[1], c[2], c[3], c[4] = x0, y0, x1 - x0, y1 - y0
                c[0] = max(c[0], score)
                placed = True
                break
        if not placed:
            clusters.append([score, box[0], box[1], box[2], box[3]])
    return [(c[0], (c[1], c[2], c[3], c[4])) for c in clusters]


def _nms_merge(items, conf: float, iou_threshold: float):
    boxes = [list(b) for _, b in items]
    scores = [s for s, _ in items]
    indices = cv2.dnn.NMSBoxes(boxes, scores, conf, iou_threshold)
    if len(indices) == 0:
        return []
    return [
        (scores[int(i)], tuple(boxes[int(i)]))
        for i in np.array(indices).flatten()
    ]
