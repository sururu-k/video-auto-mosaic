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

    @property
    def active_provider(self) -> str:
        return self.session.get_providers()[0]

    def detect_square(
        self, square_bgr: np.ndarray, scale_back: float
    ) -> list[Detection]:
        """letterbox 済みの正方 BGR フレーム1枚を推論する。

        scale_back: 正方フレーム座標 → 元動画座標 の倍率。
        """
        blob = cv2.dnn.blobFromImage(
            square_bgr,
            1 / 255.0,
            (self.infer_size, self.infer_size),
            (0, 0, 0),
            swapRB=True,
            crop=False,
        )
        outputs = self.session.run(None, {self.input_name: blob})
        return self._postprocess(outputs, scale_back)

    def _postprocess(self, outputs, scale_back: float) -> list[Detection]:
        # (1, 4+num_classes, N) -> (N, 4+num_classes)
        preds = np.transpose(np.squeeze(outputs[0]))
        class_scores = preds[:, 4:]
        max_scores = class_scores.max(axis=1)

        keep = max_scores >= self.conf
        if not np.any(keep):
            return []

        preds = preds[keep]
        max_scores = max_scores[keep]
        class_ids = class_scores[keep].argmax(axis=1)

        # 中心座標 -> 左上座標。推論解像度から正方フレーム座標へ戻し、
        # さらに元動画座標へスケールする。
        ratio = scale_back / self.infer_size
        cx, cy, bw, bh = preds[:, 0], preds[:, 1], preds[:, 2], preds[:, 3]
        x = (cx - bw / 2) * ratio
        y = (cy - bh / 2) * ratio
        w = bw * ratio
        h = bh * ratio

        boxes = [[float(a), float(b), float(c), float(d)] for a, b, c, d in zip(x, y, w, h)]
        scores = [float(s) for s in max_scores]
        indices = cv2.dnn.NMSBoxes(boxes, scores, self.conf, self.nms_iou)
        if len(indices) == 0:
            return []

        results: list[Detection] = []
        for i in np.array(indices).flatten():
            bx, by, bw_, bh_ = boxes[int(i)]
            results.append(
                Detection(
                    cls=LABELS[int(class_ids[int(i)])],
                    score=scores[int(i)],
                    box=(int(round(bx)), int(round(by)), int(round(bw_)), int(round(bh_))),
                )
            )
        return results
