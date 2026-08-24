"""区間の両端に置いた矩形から、あいだを補間して Correction の列を作る。

#22 の第一歩。**区間を保存する層そのものではない。** いまの `review.py` の
`mark()` は span 判定を「同じ矩形をフレーム数ぶん複製する」方法で展開しており
（`review.py:1260-1278`）、対象が動いていると端で外れる（issue #22 の問題1）。

ここでは検査キューからの呼び出しはまだ配線しない。展開そのものを
`tools/annotations_to_corrections.py` の `build()` に委ねる関数だけを切り出す。
手描き画面（`handdraw.py`）と同じ規則で補間されることを保証するため、
別の補間式を書かない。

保存層・undo の単位・API・フロントエンドは #22 の残りとして issue に返す。
"""

from __future__ import annotations

import importlib.util
import math
import os
import threading
from typing import Callable

from ..corrections import Correction

Box = tuple[float, float, float, float]

_TOOL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tools",
    "annotations_to_corrections.py",
)
_tool_lock = threading.Lock()
_tool_mod = None


def _tool():
    """tools/annotations_to_corrections.py を読み込む。handdraw.py と同じやり方。

    tools/ はパッケージではないので、ファイルを名前空間つきで直接読む。
    handdraw.py の `_tool()` とキャッシュを分けているのは、この2つが将来
    別プロセス/別スレッドの都合で分離されても壊れないようにするため
    （現状は同じ関数を指すが、依存を明示的にしておく）。
    """
    global _tool_mod
    with _tool_lock:
        if _tool_mod is None:
            spec = importlib.util.spec_from_file_location(
                "_automosaic_annotations_tool_spans", _TOOL_PATH
            )
            if spec is None or spec.loader is None:
                raise RuntimeError(f"読み込めません: {_TOOL_PATH}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _tool_mod = mod
    return _tool_mod


def interval_records(
    lo_frame: int,
    lo_box: Box,
    hi_frame: int,
    hi_box: Box,
    cls: str,
    kind: str = "add",
) -> list[Correction]:
    """区間 [lo_frame, hi_frame] の両端に置いた矩形のあいだを補間する。

    `lo_box` と `hi_box` が同じ座標なら、いまの `mark()` と同じ「複製」になる。
    違う座標を渡せば、対象が動いた分だけ線形に追従する。

    `kind` は補間後にまとめて適用する。`tools/annotations_to_corrections.py`
    の `build()` は Correction を作るが kind の概念を持たない（打点は
    「対象がある/ない」の二値で、remove の概念が無い）ので、ここで上書きする。
    `remove` を渡すこと自体は禁止しない（狭めた領域を区間で持たせる用途は
    ありうる）が、`review.py` の `toobig` が使う「そのフレームの自動領域を
    包む」remove とは意味が違う点に注意。あちらはフレームごとに実際の自動
    検出矩形を包むので、対象追従はすでにできている。

    2点だけの区間の補間なので、対象が区間の中で片道以上折り返す動きには
    追従できない（直線補間の限界。issue #22 の懸念にある「区間内の1フレーム
    だけ漏れる」問題はここでは解決しない）。

    戻り値は昇順ソート済みで、`lo_frame` と `hi_frame` を必ず含む。
    """
    lo_frame = int(lo_frame)
    hi_frame = int(hi_frame)
    if hi_frame < lo_frame:
        raise ValueError(f"hi_frame({hi_frame}) が lo_frame({lo_frame}) より前です")

    tool = _tool()
    if hi_frame == lo_frame:
        annotations = [{"frame": lo_frame, "box": list(lo_box), "class": cls}]
    else:
        annotations = [
            {"frame": lo_frame, "box": list(lo_box), "class": cls},
            {"frame": hi_frame, "box": list(hi_box), "class": cls},
        ]

    built = tool.build(
        annotations,
        max_interp=max(1, hi_frame - lo_frame),
        default_class=cls,
        hold=0,
    )
    return [
        Correction(frame=c.frame, box=c.box, cls=c.cls, kind=kind) for c in built
    ]


def _envelope(boxes: list[Box], width: int, height: int) -> Box:
    """複数の矩形を包む最小の矩形。`review.py` の `cover_box()` と同じ規則。

    `review.py` を import すると `review -> webapp.spans -> review` の循環
    import になるので、ここでは bbox の外接だけを4行複製してある。複製して
    いるのは矩形の外接という単純な幾何演算だけで、**補間そのもの
    （`_lerp` / `build()`）は複製しない。** そこは `interval_records()` が
    `tools/annotations_to_corrections.py` の `build()` を呼ぶ一本道のまま。
    """
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    x0 = max(0.0, float(math.floor(x0)))
    y0 = max(0.0, float(math.floor(y0)))
    x1 = min(float(width), float(math.ceil(x1)))
    y1 = min(float(height), float(math.ceil(y1)))
    return (x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))


def interval_add_records(
    lo_frame: int,
    lo_box: Box,
    hi_frame: int,
    hi_box: Box,
    cls: str,
    width: int,
    height: int,
    existing_cover: Callable[[int], Box | None] | None = None,
) -> list[Correction]:
    """`interval_records()` の "add" 専用ラッパー（issue #46 / #22 の配線先）。

    `review.py` の `ReviewSession.mark_interval()` から呼ぶ。**kind は "add" に
    固定する。** "toobig" の remove 側にこの補間を使うと、対象が直線から
    外れたフレームで、実際の自動検出領域より狭い範囲しか残らない
    （issue #22 の実測: 150条件中26条件で mean_iou 悪化、9条件で完全に外れる
    フレームが発生）。add は無条件で足されるだけなので、この関数の出力を
    そのまま足しても被覆が減ることは無い。

    `existing_cover(frame)` を渡すと、区間内の各フレームで既存の自動検出
    領域を包む矩形を取得し、補間結果より大きければそちらの envelope を採る。
    RULES.md 0「補間で作った矩形が既存の検出矩形より小さくなる場合は、
    大きいほうを採る」をそのまま実装したもの。add は元々 union されるだけ
    なので必須ではないが、区間補間の申告自体を検出より狭く見せないための
    追加の安全側寄せ。
    """
    recs = interval_records(lo_frame, lo_box, hi_frame, hi_box, cls, kind="add")
    if existing_cover is None:
        return recs
    out: list[Correction] = []
    for rec in recs:
        cov = existing_cover(rec.frame)
        box = rec.box
        if cov is not None:
            box = _envelope([box, cov], width, height)
        out.append(Correction(frame=rec.frame, box=box, cls=rec.cls, kind=rec.kind))
    return out
