"""YOLO11-seg (NSFW_Segmentation) の ONNX をピクセルマスク推論として回すラッパ。

detector.py が bbox しか返せないのに対し、こちらは検出ごとに bool のマスクを返す。
モザイクを矩形でなく輪郭に沿ってかけられる。

ultralytics には依存しない（本番の .venv には入れない方針）。
必要なのは onnxruntime / numpy / opencv-python だけ。

ONNX の入出力（tools/export_seg_onnx.py で dynamic=True エクスポートしたもの）:
    images  : (batch, 3, height, width)            float32, RGB, 0..1
    output0 : (batch, 37, anchors)                 4(cx,cy,w,h) + 1(class) + 32(mask係数)
    output1 : (batch, 32, height/4, width/4)       プロトタイプマスク

マスクの復元は
    mask = sigmoid(mask係数[32] @ プロトタイプ[32, mh*mw]) -> (mh, mw)
を bbox でクロップし、元解像度へリサイズして 0.5 で二値化する。

レターボックスの規約は detector.py の _detect_window と同じ。
辺 = max(h, w) の正方に左上詰めで貼り、右下をゼロ埋めする。
ONNX は dynamic エクスポートしてあるので、infer_size に (w, h) のタプルを渡すと
パディング無しで直接その解像度へリサイズする経路も選べる。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import cv2
import numpy as np
import onnxruntime

# provider の解決規則は detector.py と共通にしておく（挙動が食い違うと事故る）
from .detector import resolve_providers, available_providers  # noqa: F401

# モデルは 1 クラス ('item') なので、ファイル名から人が読める名前を当てる。
_NAME_HINTS = (
    ("penis", "PENIS"),
    ("vagina", "VAGINA"),
    ("anus", "ANUS"),
)


@dataclass
class SegDetection:
    """検出1件。box は元フレーム座標系 (x, y, w, h)、mask は元フレームと同じ H×W の bool。"""

    cls: str
    score: float
    mask: np.ndarray
    box: tuple[int, int, int, int]

    @property
    def mask_area(self) -> int:
        return int(self.mask.sum())

    @property
    def box_area(self) -> int:
        return int(self.box[2]) * int(self.box[3])


def _guess_class_name(model_path: str) -> str:
    base = os.path.basename(model_path).lower()
    for key, label in _NAME_HINTS:
        if key in base:
            return label
    return os.path.splitext(os.path.basename(model_path))[0].upper()


class Segmenter:
    """ONNX のインスタンスセグメンテーション器。

    使い方:
        seg = Segmenter("weights/nsfw-seg-penis-x.onnx", infer_size=832, provider="dml", device_id=1)
        for cls, score, mask, box in seg.segment(frame_bgr):
            frame_bgr[mask] = 0
    """

    def __init__(
        self,
        model_path: str,
        infer_size: int | tuple[int, int] = 832,
        conf: float = 0.1,
        nms_iou: float = 0.45,
        mask_thresh: float = 0.5,
        provider: str | None = "auto",
        intra_threads: int = 0,
        device_id: int = 1,
        class_name: str | None = None,
        max_det: int = 50,
    ) -> None:
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"モデルが見つかりません: {model_path}")

        # infer_size は int なら「正方レターボックス（右下パディング）」、
        # (w, h) のタプルなら「パディング無しでそのサイズへ直接リサイズ」。
        # ONNX を dynamic エクスポートしてあるので後者が使える。素材が 4:3 なら
        # 正方に詰めると 25% が黒帯になって実効解像度を捨てることになるため、
        # アスペクト比を保った非正方入力の方が有利なことがある。
        if isinstance(infer_size, (tuple, list)):
            self.net_w, self.net_h = int(infer_size[0]), int(infer_size[1])
            self.letterbox = False
        else:
            self.net_w = self.net_h = int(infer_size)
            self.letterbox = True
        # YOLO の stride は 32。推論解像度が 32 の倍数でないと出力側の形が壊れる。
        if self.net_w % 32 or self.net_h % 32:
            raise ValueError(
                f"推論解像度は縦横とも 32 の倍数にしてください: {self.net_w}x{self.net_h}"
            )

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
        self.output_names = [o.name for o in self.session.get_outputs()]
        if len(self.output_names) != 2:
            raise RuntimeError(
                "セグメンテーション用の ONNX は出力が2つ (output0/output1) のはずです。"
                f" 実際: {self.output_names}"
            )

        self.model_path = model_path
        self.infer_size = infer_size
        self.conf = conf
        self.nms_iou = nms_iou
        self.mask_thresh = mask_thresh
        self.max_det = max_det
        self.class_name = class_name or _guess_class_name(model_path)

    @property
    def active_provider(self) -> str:
        return self.session.get_providers()[0]

    # ------------------------------------------------------------------

    def segment(self, frame_bgr: np.ndarray) -> list[tuple[str, float, np.ndarray, tuple[int, int, int, int]]]:
        """フレーム1枚を推論して (class_name, score, mask_bool, bbox) のリストを返す。

        mask は frame_bgr と同じ (H, W) の bool ndarray。bbox は (x, y, w, h) の int。
        """
        return [(d.cls, d.score, d.mask, d.box) for d in self.segment_detections(frame_bgr)]

    def segment_detections(self, frame_bgr: np.ndarray) -> list[SegDetection]:
        """segment() と同じ推論だが SegDetection の dataclass で返す。"""
        h, w = frame_bgr.shape[:2]

        if self.letterbox:
            # detector.py と同じレターボックス。左上詰め・右下ゼロ埋め。
            side = max(h, w)
            padded = np.zeros((side, side, 3), dtype=frame_bgr.dtype)
            padded[:h, :w] = frame_bgr
            pad_w = pad_h = side
        else:
            # パディングせずネットワーク入力へ直接リサイズする
            padded = frame_bgr
            pad_w, pad_h = w, h

        blob = cv2.dnn.blobFromImage(
            padded,
            1 / 255.0,
            (self.net_w, self.net_h),
            (0, 0, 0),
            swapRB=True,
            crop=False,
        )
        out0, protos = self.session.run(self.output_names, {self.input_name: blob})

        # (1, 37, N) -> (N, 37)
        preds = np.transpose(np.squeeze(out0, axis=0))
        num_mask = protos.shape[1]              # 32
        num_cls = preds.shape[1] - 4 - num_mask  # このモデルでは 1

        cls_scores = preds[:, 4 : 4 + num_cls]
        scores = cls_scores.max(axis=1)
        keep = scores >= self.conf
        if not np.any(keep):
            return []

        preds = preds[keep]
        scores = scores[keep]

        # 候補が多いと後段のマスク計算が重い。スコア上位だけ残す。
        if len(scores) > self.max_det * 8:
            top = np.argpartition(-scores, self.max_det * 8)[: self.max_det * 8]
            preds, scores = preds[top], scores[top]

        # 推論解像度の座標系 (cx, cy, bw, bh) -> (x0, y0, x1, y1)
        cx, cy, bw, bh = preds[:, 0], preds[:, 1], preds[:, 2], preds[:, 3]
        boxes_i = np.stack(
            [cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1
        )

        idx = _nms(boxes_i, scores, self.nms_iou)[: self.max_det]
        if len(idx) == 0:
            return []
        boxes_i = boxes_i[idx]
        scores = scores[idx]
        coeffs = preds[idx, 4 + num_cls :]  # (n, 32)

        masks = self._decode_masks(coeffs, protos, boxes_i, h, w, pad_w, pad_h)

        # 推論解像度 -> パディング後の画像 -> 元フレーム。左上詰めなのでオフセットは無い。
        rx, ry = pad_w / self.net_w, pad_h / self.net_h
        results: list[SegDetection] = []
        for i in range(len(scores)):
            x0, y0, x1, y1 = boxes_i[i] * np.array([rx, ry, rx, ry])
            x0 = float(np.clip(x0, 0, w))
            y0 = float(np.clip(y0, 0, h))
            x1 = float(np.clip(x1, 0, w))
            y1 = float(np.clip(y1, 0, h))
            if x1 - x0 < 1 or y1 - y0 < 1:
                continue
            results.append(
                SegDetection(
                    cls=self.class_name,
                    score=float(scores[i]),
                    mask=masks[i],
                    box=(
                        int(round(x0)),
                        int(round(y0)),
                        int(round(x1 - x0)),
                        int(round(y1 - y0)),
                    ),
                )
            )
        return results

    # ------------------------------------------------------------------

    def _decode_masks(
        self,
        coeffs: np.ndarray,
        protos: np.ndarray,
        boxes_i: np.ndarray,
        frame_h: int,
        frame_w: int,
        pad_w: int,
        pad_h: int,
    ) -> list[np.ndarray]:
        """マスク係数 × プロトタイプ -> sigmoid -> bbox クロップ -> 元解像度。

        coeffs  (n, 32)              検出ごとのマスク係数
        protos  (1, 32, mh, mw)      プロトタイプマスク。mh = net_h / 4
        boxes_i (n, 4)               推論解像度座標の xyxy
        pad_w/pad_h                  パディング後の画像サイズ（レターボックス無しなら元サイズ）
        戻り値は各要素が (frame_h, frame_w) の bool ndarray。
        """
        c, mh, mw = protos.shape[1], protos.shape[2], protos.shape[3]
        flat = protos.reshape(c, mh * mw)                    # (32, mh*mw)
        m = coeffs @ flat                                     # (n, mh*mw)
        m = 1.0 / (1.0 + np.exp(-m))                          # sigmoid
        m = m.reshape(-1, mh, mw)

        # レターボックスの有効領域（右下パディングを除いた部分）をプロト座標で求める。
        # パディング後の pad_w/pad_h のうち frame_w/frame_h だけが実画像。
        vx1 = max(1, int(round(frame_w / pad_w * mw)))
        vy1 = max(1, int(round(frame_h / pad_h * mh)))

        # 推論解像度 -> 元フレーム座標 の倍率
        rx, ry = pad_w / self.net_w, pad_h / self.net_h

        out: list[np.ndarray] = []
        for i in range(m.shape[0]):
            # パディングを落としてから元フレームサイズへ引き伸ばす。
            # ultralytics は プロト解像度のままクロップしてから拡大するが、
            # それだと細い bbox が 1/4 解像度に丸められて精度が落ちる。
            # 拡大してからクロップすれば bbox の縁がフレーム解像度で効く。
            full = cv2.resize(
                m[i, :vy1, :vx1], (frame_w, frame_h), interpolation=cv2.INTER_LINEAR
            )
            # bbox の外を落とす。輪郭が bbox 外へ滲むのを防ぐ（crop_mask 相当）
            bx0 = int(np.clip(np.floor(boxes_i[i, 0] * rx), 0, frame_w))
            by0 = int(np.clip(np.floor(boxes_i[i, 1] * ry), 0, frame_h))
            bx1 = int(np.clip(np.ceil(boxes_i[i, 2] * rx), 0, frame_w))
            by1 = int(np.clip(np.ceil(boxes_i[i, 3] * ry), 0, frame_h))
            keep = np.zeros((frame_h, frame_w), dtype=bool)
            if bx1 > bx0 and by1 > by0:
                keep[by0:by1, bx0:bx1] = True
            out.append((full >= self.mask_thresh) & keep)
        return out


class MultiSegmenter:
    """複数モデル（penis / vagina など）をまとめて回す薄いラッパ。"""

    def __init__(self, model_paths: list[str], **kwargs) -> None:
        self.segmenters = [Segmenter(p, **kwargs) for p in model_paths]

    @property
    def active_provider(self) -> str:
        return self.segmenters[0].active_provider

    def segment(self, frame_bgr: np.ndarray):
        out = []
        for s in self.segmenters:
            out += s.segment(frame_bgr)
        return out

    def union_mask(self, frame_bgr: np.ndarray) -> np.ndarray:
        """全検出のマスクを OR したもの。モザイク適用にはこれだけあれば足りる。"""
        h, w = frame_bgr.shape[:2]
        acc = np.zeros((h, w), dtype=bool)
        for _, _, mask, _ in self.segment(frame_bgr):
            acc |= mask
        return acc


def _nms(boxes_xyxy: np.ndarray, scores: np.ndarray, iou_thresh: float) -> np.ndarray:
    """スコア降順の greedy NMS。返すのは残ったインデックス。

    マスクを持つ検出は矩形の union 統合ができない（形が壊れる）ので、
    detector.py の union_merge ではなく素直な NMS を使う。
    重なりが残っても呼び出し側でマスクを OR すればよい。
    """
    if len(scores) == 0:
        return np.empty(0, dtype=int)
    order = scores.argsort()[::-1]
    x0, y0, x1, y1 = (boxes_xyxy[:, i] for i in range(4))
    areas = np.maximum(0.0, x1 - x0) * np.maximum(0.0, y1 - y0)

    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        ix0 = np.maximum(x0[i], x0[rest])
        iy0 = np.maximum(y0[i], y0[rest])
        ix1 = np.minimum(x1[i], x1[rest])
        iy1 = np.minimum(y1[i], y1[rest])
        inter = np.maximum(0.0, ix1 - ix0) * np.maximum(0.0, iy1 - iy0)
        union = areas[i] + areas[rest] - inter
        iou = np.where(union > 0, inter / np.maximum(union, 1e-9), 0.0)
        order = rest[iou < iou_thresh]
    return np.array(keep, dtype=int)
