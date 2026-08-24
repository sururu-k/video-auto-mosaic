"""確認用プロキシ動画の生成と状態管理（issue #18）。

生成はバックグラウンドスレッドで1本だけ走らせる。呼び出し口は2つ:

- パス2完了直後（runner.py の JobRunner._wait）。最速で見えるようにする。
- ジョブを開いたとき（app.py の get_job）。パス2完了より前にこの機能を
  入れた古いジョブや、サーバ再起動をまたいで完了したジョブ（runner の
  _wait が走らなかったジョブ）を遅延生成で拾う。

どちらの経路で呼んでも ensure_started() は同じ判定をする。二重に生成が
走らないよう、生成中のジョブID をプロセス内の集合で覚えておく。

meta.json の "proxy" キーだけを読み書きする。呼び出し時点の Job オブジェクトを
そのまま書き換えて保存すると、生成に数分かかる間に他の経路（検査の判定など）が
書いた他のキーを上書きしてしまう。保存の直前に必ずジョブを読み直す。
"""

from __future__ import annotations

import os
import threading
import time

from .. import video
from . import jobs as jobs_mod

STATUS_GENERATING = "generating"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

_lock = threading.Lock()
_active: set[str] = set()  # 生成中のジョブID


def _library_for(job: jobs_mod.Job) -> jobs_mod.Library:
    # job.dir は "<library_root>/<job.id>"。ライブラリを別途持ち回らずに
    # 済ませるため、ここから作り直す。
    return jobs_mod.Library(os.path.dirname(job.dir))


def is_generating(job_id: str) -> bool:
    """このプロセスがそのジョブのプロキシを生成中かどうか。

    ジョブ削除の直前に確認する用。生成中に消すと、Windows では書き込み中の
    ファイルを rmtree が消しきれずに残骸が残ることがある（未検証）。
    """
    with _lock:
        return job_id in _active


def ensure_started(job: jobs_mod.Job) -> bool:
    """未生成なら生成を始める。戻り値は「今回始めたか」。

    - ジョブが完了（done）していなければ何もしない。焼き直し中の output.mp4
      は ffmpeg（パス2）が今まさに書いている最中で、途中の中身を読むと
      壊れたプロキシができる。get_job() は状態を問わず毎回これを呼ぶので、
      ここで status を見て弾く。
    - output.mp4 が無ければ何もしない（作りようがない）。
    - 既に成功していて実体もあるなら何もしない。
    - 生成中なら何もしない（多重起動しない）。
    - 失敗の記録が残っているだけなら、もう一度試す
      （前回は一時的な失敗だったかもしれない。手動の再試行導線が無いので
      ここで自然に拾う）。
    """
    if job.status != jobs_mod.STATUS_DONE:
        return False
    if not os.path.exists(job.output):
        return False
    p = job.meta.get("proxy") or {}
    if p.get("status") == STATUS_DONE and os.path.exists(job.proxy):
        return False
    with _lock:
        if job.id in _active:
            return False
        _active.add(job.id)
    lib = _library_for(job)
    threading.Thread(target=_run, args=(lib, job.id), daemon=True).start()
    return True


def _save_status(lib: jobs_mod.Library, job_id: str, **fields) -> None:
    """保存の直前にジョブを読み直してから、"proxy" キーだけ書き換える。

    ジョブが消えている（削除された）場合は静かに諦める。生成の完了報告先が
    無いだけで、エラーにする理由が無い。
    """
    try:
        job = lib.get(job_id)
    except KeyError:
        return
    job.update(proxy=fields)


def _run(lib: jobs_mod.Library, job_id: str) -> None:
    t0 = time.time()
    _save_status(lib, job_id, status=STATUS_GENERATING, error=None, started_at=t0)
    try:
        job = lib.get(job_id)
    except KeyError:
        with _lock:
            _active.discard(job_id)
        return

    try:
        video.generate_proxy(job.output, job.proxy)
        n_out = video.nb_frames(job.output)
        n_proxy = video.nb_frames(job.proxy)
        if n_out is None or n_proxy is None or n_out != n_proxy:
            raise RuntimeError(
                "プロキシのフレーム数が output.mp4 と一致しません"
                f"（output={n_out} / proxy={n_proxy}）。"
                "1フレームでもずれたプロキシは、別のフレームを見て"
                "OKを出す道具になるので使わせない。"
            )
    except Exception as e:  # noqa: BLE001
        try:
            if os.path.exists(job.proxy):
                os.remove(job.proxy)
        except OSError:
            pass
        _save_status(
            lib, job_id,
            status=STATUS_FAILED, error=str(e),
            started_at=t0, finished_at=time.time(),
        )
    else:
        _save_status(
            lib, job_id,
            status=STATUS_DONE, error=None,
            started_at=t0, finished_at=time.time(),
            elapsed_sec=round(time.time() - t0, 1),
            size_bytes=os.path.getsize(job.proxy) if os.path.exists(job.proxy) else 0,
            n_frames=n_proxy,
        )
    finally:
        with _lock:
            _active.discard(job_id)
