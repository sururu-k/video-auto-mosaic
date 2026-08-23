"""人手で足した/消した領域の保存と適用。

自動処理と人手レビューをプロセスとして分離するための層。
検出結果 JSON はそのまま残し、修正だけを別ファイルに持つ。こうしておくと
検出をやり直しても修正が生き残るし、修正だけを学習データに書き出せる。
"""

from __future__ import annotations

import json
import os
import sys
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


def apply(
    regions_per_frame: dict,
    corrections: CorrectionSet,
    stats: dict | None = None,
) -> dict:
    """時間方向の処理を通した領域に、手修正を反映する。

    add は無条件で足す。remove はその矩形と重なる自動領域を落とす
    （手で足した領域は落とさない。消したいなら UI 側で消す）。

    描画対象に存在しないフレーム（`n_frames` の外、負のフレーム番号）を指す修正は
    反映しようがない。**手修正は最後の砦なので、黙って落とすことだけはしない。**
    落とした件数は必ず stderr に出し、`stats` を渡せば呼び出し側でも読める。

    以前は直近の結果を関数属性 `apply.last_stats` にも残していたが、これは
    全呼び出しで共有される1個のグローバル変数であり、複数スレッドから同時に
    呼ばれると値が混ざる（実測: 3スレッド×200回、呼び出しと読み取りの間に
    0.2ms 置くと 600 回の読み取り中 512 回が他スレッドの値を返した）。
    review.py は ThreadingHTTPServer で recompute() が複数箇所から呼ばれるため、
    将来この属性を読むコードが増えると壊れる。関数属性という設計そのものをやめ、
    呼び出しごとに閉じたローカル変数である `stats` 引数だけを正とする。

    stats に入る値:
      total                件数（渡された修正の総数）
      applied              反映した修正の件数
      applied_add          そのうち add
      applied_remove       そのうち remove
      dropped_out_of_range 範囲外フレームを指していて反映できなかった件数
      dropped_frames       その修正が指していたフレーム番号（昇順）

    戻り値は従来どおり領域の辞書だけ。既存の呼び出しは変えなくてよい。
    """
    from .temporal import Region  # 循環importを避けるためここで

    by_frame = corrections.by_frame()
    info = {
        "total": len(corrections.items),
        "applied": 0,
        "applied_add": 0,
        "applied_remove": 0,
        "dropped_out_of_range": 0,
        "dropped_frames": [],
    }

    out = dict(regions_per_frame)
    for frame in sorted(by_frame):
        cs = by_frame[frame]
        if frame not in out:
            info["dropped_out_of_range"] += len(cs)
            info["dropped_frames"].append(frame)
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
            info["applied_remove"] += len(removes)

        for c in cs:
            if c.kind != "add":
                continue
            current.append((c.box, Region(c.box, c.cls, 1.0, "manual")))
            info["applied_add"] += 1

        out[frame] = current

    info["applied"] = info["applied_add"] + info["applied_remove"]
    if stats is not None:
        stats.update(info)

    if info["dropped_out_of_range"]:
        frames = info["dropped_frames"]
        shown = ", ".join(str(f) for f in frames[:10])
        if len(frames) > 10:
            shown += f", ... 他 {len(frames) - 10} フレーム"
        print(
            f"手修正 {info['dropped_out_of_range']} 件が範囲外のフレームを指しているため"
            f"反映されませんでした（frame {shown}）。"
            "検出JSONと修正ファイルの組み合わせが正しいか確かめてください。",
            file=sys.stderr,
        )

    return out
