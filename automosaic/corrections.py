"""人手で足した/消した領域の保存と適用。

自動処理と人手レビューをプロセスとして分離するための層。
検出結果 JSON はそのまま残し、修正だけを別ファイルに持つ。こうしておくと
検出をやり直しても修正が生き残るし、修正だけを学習データに書き出せる。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

Box = tuple[float, float, float, float]


@dataclass
class Correction:
    """1フレームぶんの手修正。

    kind:
      add     見逃しを埋めるために足した領域
      remove  過剰なモザイクを消すための否定領域（この矩形と重なる自動領域を落とす）
    """

    frame: int
    box: Box
    cls: str = "FEMALE_GENITALIA_EXPOSED"
    kind: str = "add"

    def as_dict(self) -> dict:
        return {
            "frame": self.frame,
            "box": [round(v, 1) for v in self.box],
            "class": self.cls,
            "kind": self.kind,
        }

    @staticmethod
    def from_dict(d: dict) -> "Correction":
        return Correction(
            frame=int(d["frame"]),
            box=tuple(float(v) for v in d["box"]),
            cls=d.get("class", "FEMALE_GENITALIA_EXPOSED"),
            kind=d.get("kind", "add"),
        )


@dataclass
class CorrectionSet:
    video: str = ""
    width: int = 0
    height: int = 0
    items: list[Correction] = field(default_factory=list)

    def by_frame(self) -> dict[int, list[Correction]]:
        out: dict[int, list[Correction]] = {}
        for c in self.items:
            out.setdefault(c.frame, []).append(c)
        return out

    def add(self, c: Correction) -> None:
        self.items.append(c)

    def remove_at(self, frame: int, x: float, y: float) -> bool:
        """指定座標を含む手修正を1つ消す。消せたら True。"""
        for i in range(len(self.items) - 1, -1, -1):
            c = self.items[i]
            if c.frame != frame:
                continue
            bx, by, bw, bh = c.box
            if bx <= x <= bx + bw and by <= y <= by + bh:
                del self.items[i]
                return True
        return False

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "video": self.video,
                    "width": self.width,
                    "height": self.height,
                    "corrections": [c.as_dict() for c in self.items],
                },
                f,
                ensure_ascii=False,
                indent=1,
            )

    @staticmethod
    def load(path: str) -> "CorrectionSet":
        if not os.path.exists(path):
            return CorrectionSet()
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return CorrectionSet(
            video=d.get("video", ""),
            width=int(d.get("width", 0)),
            height=int(d.get("height", 0)),
            items=[Correction.from_dict(c) for c in d.get("corrections", [])],
        )


def _overlaps(a: Box, b: Box) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)


def apply(regions_per_frame: dict, corrections: CorrectionSet) -> dict:
    """時間方向の処理を通した領域に、手修正を反映する。

    add は無条件で足す。remove はその矩形と重なる自動領域を落とす
    （手で足した領域は落とさない。消したいなら UI 側で消す）。
    """
    from .temporal import Region  # 循環importを避けるためここで

    by_frame = corrections.by_frame()
    if not by_frame:
        return regions_per_frame

    out = dict(regions_per_frame)
    for frame, cs in by_frame.items():
        if frame not in out:
            continue
        current = list(out[frame])

        removes = [c for c in cs if c.kind == "remove"]
        if removes:
            current = [
                (box, reg)
                for box, reg in current
                if reg.source == "manual"
                or not any(_overlaps(box, r.box) for r in removes)
            ]

        for c in cs:
            if c.kind != "add":
                continue
            current.append((c.box, Region(c.box, c.cls, 1.0, "manual")))

        out[frame] = current
    return out
