"""手描きモードの打点と、そこから corrections への展開。

検出を回さずに空の状態から矩形を置く使い方。全フレームに打つのは
現実的でないので、数フレームおきに打った点のあいだを補間で埋める。

補間の規則は `tools/annotations_to_corrections.py` の build() をそのまま
呼ぶ。同じ規則を書き直すと、コマンドラインで作った corrections と
画面で作った corrections が微妙に違う結果になり、どちらが正しいか
判断できなくなる。tools/ はパッケージではないのでファイル指定で読み込む。
"""

from __future__ import annotations

import importlib.util
import json
import os
import threading

from ..corrections import CorrectionSet
from .jobs import Job, REPO_ROOT

_TOOL_PATH = os.path.join(REPO_ROOT, "tools", "annotations_to_corrections.py")
_tool_lock = threading.Lock()
_tool_mod = None


def _tool():
    """tools/annotations_to_corrections.py を読み込む。

    tools/ は開発用スクリプト置き場でパッケージになっていない。
    sys.path をいじって import すると tools/ の他のスクリプト名まで
    トップレベルに出るので、ファイルを名前空間つきで直接読む。
    """
    global _tool_mod
    with _tool_lock:
        if _tool_mod is None:
            spec = importlib.util.spec_from_file_location(
                "_automosaic_annotations_tool", _TOOL_PATH
            )
            if spec is None or spec.loader is None:
                raise RuntimeError(f"読み込めません: {_TOOL_PATH}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _tool_mod = mod
    return _tool_mod


def load(job: Job) -> list[dict]:
    """打点の一覧。フレーム順。"""
    try:
        with open(job.annotations, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("annotations", [])
    out = [a for a in data if isinstance(a, dict) and "frame" in a]
    out.sort(key=lambda a: int(a["frame"]))
    return out


def save(job: Job, items: list[dict]) -> None:
    items = sorted(items, key=lambda a: int(a["frame"]))
    tmp = job.annotations + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"annotations": items}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, job.annotations)


def put(job: Job, frame: int, box, cls: str | None) -> list[dict]:
    """1フレームぶんの打点を置き換える。

    同じフレームに2つ以上の点を持たせない。補間は「このフレームでは
    ここに1つある」を前提にしており、複数あると隣のどちらへ繋ぐかが
    決まらない。置き直しは上書きにする。
    box が None なら「ここには無い」の意味で、そこで補間が止まる。
    """
    items = [a for a in load(job) if int(a["frame"]) != int(frame)]
    entry: dict = {"frame": int(frame), "box": None if box is None else [float(v) for v in box]}
    if cls:
        entry["class"] = cls
    items.append(entry)
    save(job, items)
    return items


def delete(job: Job, frame: int) -> list[dict]:
    items = [a for a in load(job) if int(a["frame"]) != int(frame)]
    save(job, items)
    return items


def expand(
    job: Job,
    max_interp: int = 20,
    hold: int = 4,
    default_class: str | None = None,
    merge: bool = False,
) -> dict:
    """打点を補間して corrections.json に展開する。

    既定は置き換え。手描きの打点を編集し直すたびに追記していくと、
    消したはずの位置の矩形が corrections に残り続けて取れなくなる。
    自動検出の結果に手描きを足したい場合だけ merge=True にする。
    """
    tool = _tool()
    items = load(job)
    cls = default_class or tool.DEFAULT_CLASS
    built = tool.build(items, max_interp, cls, hold)

    cs = CorrectionSet.load(job.corrections) if merge else CorrectionSet()
    cs.video = cs.video or os.path.basename(job.source)
    cs.width = cs.width or int(job.meta.get("width") or 0)
    cs.height = cs.height or int(job.meta.get("height") or 0)
    cs.items.extend(built)
    cs.save(job.corrections)

    frames = sorted({c.frame for c in built})
    job.meta["n_corrections"] = len(cs.items)
    job.meta["n_annotations"] = len([a for a in items if a.get("box")])
    job.save()
    return {
        "annotations": len(items),
        "points": len([a for a in items if a.get("box")]),
        "corrections": len(cs.items),
        "expanded": len(built),
        "frame_range": [frames[0], frames[-1]] if frames else None,
    }
