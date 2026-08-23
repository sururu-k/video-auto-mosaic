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


# ジョブごとの書き込み鍵。打点の保存は「読んで・足して・書く」なので、
# 鍵が無いと同時操作（別タブ・PC と携帯）で片方の打点が黙って消える。
# しかも消えたほうも 200 を返すので、画面からは気づけない。
_job_locks: dict[str, threading.Lock] = {}
_job_locks_guard = threading.Lock()


def _lock_for(job: Job) -> threading.Lock:
    with _job_locks_guard:
        lk = _job_locks.get(job.id)
        if lk is None:
            lk = _job_locks[job.id] = threading.Lock()
        return lk


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
    # 一時ファイルの名前は書き手ごとに分ける。同じ名前を使うと、同時に
    # 書いた片方がもう片方の書きかけを置き換えて中身が混ざる
    tmp = f"{job.annotations}.{os.getpid()}.{threading.get_ident()}.tmp"
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
    with _lock_for(job):
        items = [a for a in load(job) if int(a["frame"]) != int(frame)]
        entry: dict = {
            "frame": int(frame),
            "box": None if box is None else [float(v) for v in box],
        }
        if cls:
            entry["class"] = cls
        items.append(entry)
        save(job, items)
        return items


def delete(job: Job, frame: int) -> list[dict]:
    with _lock_for(job):
        items = [a for a in load(job) if int(a["frame"]) != int(frame)]
        save(job, items)
        return items


def expand(
    job: Job,
    max_interp: int = 20,
    hold: int = 4,
    default_class: str | None = None,
    merge: bool = True,
) -> dict:
    """打点を補間して corrections.json に展開する。

    既定は「既存の手修正に足す」。以前は置き換えを既定にしていたが、
    検査キューで積んだ修正（漏れを塞いだ矩形、狭めた矩形、誤検知の打ち消し）が
    手描きを一度使うだけで全部消えていた。1周ぶんの目視検査が無駄になる。

    しかも置き換えは corrections.json だけを書き換えて .progress.json の
    history を残すので、次の「ひとつ戻す」が存在しない修正を指して別物を削る。
    置き換えたい場合は、呼び出し側で履歴も一緒に捨てること。

    手描きの打点を編集し直すたびに追記されるのは merge の副作用だが、
    打点の編集は draw 画面の annotations 側で行うので、そちらを直せば済む。
    """
    tool = _tool()
    with _lock_for(job):
        return _expand_locked(job, tool, max_interp, hold, default_class, merge)


def _expand_locked(job, tool, max_interp, hold, default_class, merge) -> dict:
    """expand の本体。打点の書き込みと同じ鍵の内側で動かす。"""
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
