"""目視で出した座標を corrections.json に変換する。

全フレームに座標を打つのは現実的でないので、数フレームおきに打った点のあいだを
線形補間で埋める。抜き出したフレームの間隔と合わせて使う。

入力の形（annotations.json）:
  [
    {"frame": 577, "box": [485, 230, 100, 110], "class": "MALE_GENITALIA_EXPOSED"},
    {"frame": 586, "box": [490, 240, 100, 110]},
    {"frame": 599, "box": null}          <- ここで対象が消える（補間を打ち切る）
  ]

box が null のフレームは「ここには無い」の意味で、直前の点からの補間をそこで止める。
これが無いと、対象が画面から消えた後もモザイクが伸び続ける。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic.corrections import Correction, CorrectionSet  # noqa: E402

DEFAULT_CLASS = "MALE_GENITALIA_EXPOSED"


def _lerp(a, b, w):
    return [a[i] + (b[i] - a[i]) * w for i in range(4)]


def build(
    annotations: list[dict],
    max_interp: int,
    default_class: str,
    hold: int,
) -> list[Correction]:
    """打った点のあいだを補間して、フレームごとの矩形に展開する。"""
    pts = sorted(
        (a for a in annotations if "frame" in a), key=lambda a: int(a["frame"])
    )
    out: list[Correction] = []
    seen: set[tuple[int, tuple]] = set()

    def emit(frame: int, box, cls: str) -> None:
        key = (frame, tuple(round(v) for v in box))
        if key in seen:
            return
        seen.add(key)
        out.append(Correction(frame=frame, box=tuple(float(v) for v in box), cls=cls))

    for i, a in enumerate(pts):
        f = int(a["frame"])
        box = a.get("box")
        cls = a.get("class") or default_class
        if box is None:
            continue

        emit(f, box, cls)

        nxt = pts[i + 1] if i + 1 < len(pts) else None
        if nxt is None:
            # 最後の点。少しだけ持続させる
            for d in range(1, hold + 1):
                emit(f + d, box, cls)
            continue

        nf = int(nxt["frame"])
        nbox = nxt.get("box")
        gap = nf - f

        if nbox is None:
            # 次の点で「無い」と判定されている。そこまでは持たせず、途中で切る
            for d in range(1, min(hold, max(1, gap // 2)) + 1):
                emit(f + d, box, cls)
            continue

        if gap <= 1:
            continue
        if gap > max_interp:
            # 間隔が空きすぎ。両端の近傍だけ塞いで、あいだは触らない
            for d in range(1, hold + 1):
                emit(f + d, box, cls)
            continue

        ncls = nxt.get("class") or default_class
        if ncls != cls:
            # クラスが変わる＝別対象。補間しない
            for d in range(1, hold + 1):
                emit(f + d, box, cls)
            continue

        for d in range(1, gap):
            emit(f + d, _lerp(box, nbox, d / gap), cls)

    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("annotations", help="目視で出した座標の JSON")
    p.add_argument("-o", "--out", required=True, help="corrections.json の出力先")
    p.add_argument("--video", default="")
    p.add_argument("--width", type=int, default=0)
    p.add_argument("--height", type=int, default=0)
    p.add_argument(
        "--max-interp",
        type=int,
        default=20,
        help="この間隔までは点と点のあいだを補間する",
    )
    p.add_argument(
        "--hold", type=int, default=4, help="打った点の後ろに何フレーム持続させるか"
    )
    p.add_argument("--class", dest="cls", default=DEFAULT_CLASS)
    p.add_argument("--merge", action="store_true", help="既存の出力先に追記する")
    args = p.parse_args()

    with open(args.annotations, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("annotations", [])

    items = build(data, args.max_interp, args.cls, args.hold)

    cs = CorrectionSet.load(args.out) if args.merge else CorrectionSet()
    if args.video:
        cs.video = args.video
    if args.width:
        cs.width = args.width
    if args.height:
        cs.height = args.height
    cs.items.extend(items)
    cs.save(args.out)

    frames = sorted({c.frame for c in items})
    print(f"打点 {len([a for a in data if a.get('box')])} 個 -> 矩形 {len(items)} 件")
    if frames:
        print(f"対象フレーム {frames[0]}〜{frames[-1]}（{len(frames)} フレーム）")
    print(f"{args.out} に保存（合計 {len(cs.items)} 件）")


if __name__ == "__main__":
    main()
