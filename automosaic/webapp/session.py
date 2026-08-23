"""ジョブに対する検査セッション。

領域計算・検査キュー・フレーム画像・学習データ書き出しは、すべて
`automosaic.review` の実装をそのまま呼ぶ。ここで別実装を持つと
「Web で見た絵」と「既存レビュー UI で見た絵」と「実際に焼かれる絵」の
3つがずれる。ずれた瞬間にレビューの意味が消えるので、入口だけを変える。

セッションは動画を1本開いたままにするので、ジョブごとに1つだけ持ち、
使わなくなったものは閉じる。
"""

from __future__ import annotations

import os
import threading

from .. import review
from .jobs import Job, count_corrections

# 同時に開いておくセッション数。1人で使う道具なので数本で足りる。
# 開きっぱなしにすると VideoCapture のぶんだけメモリとハンドルを持つ。
MAX_OPEN = 3


def session_for_job(job: Job, **overrides) -> review.ReviewSession:
    """ジョブから ReviewSession を組む。

    review.session_from_args() をそのまま使う。あちらは argparse の
    Namespace を受けるので、review.build_parser() に引数を渡して
    既定値ごと作らせる。既定値を書き写すと review 側の変更に追随できない。
    """
    if not os.path.exists(job.source):
        raise FileNotFoundError(f"素材がありません: {job.source}")

    argv = [job.source, "--corrections", job.corrections]
    # 検出をまだ回していないジョブ（手描きモード）は --detections を渡さない。
    # 渡すと session_from_args が「見つかりません」で止まる
    if os.path.exists(job.detections):
        argv += ["--detections", job.detections]
    if os.path.exists(job.output):
        argv += ["--rendered", job.output]
    for k, v in overrides.items():
        if v is None:
            continue
        if isinstance(v, bool):
            if v:
                argv.append(f"--{k.replace('_', '-')}")
        else:
            argv += [f"--{k.replace('_', '-')}", str(v)]

    args = review.build_parser().parse_args(argv)
    return review.session_from_args(args, argv)


class SessionCache:
    """ジョブID -> ReviewSession。使い回しつつ本数を抑える。"""

    def __init__(self, limit: int = MAX_OPEN) -> None:
        self._items: dict[str, review.ReviewSession] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self.limit = limit

    def get(self, job: Job, **overrides) -> review.ReviewSession:
        with self._lock:
            s = self._items.get(job.id)
            if s is not None and not overrides:
                self._touch(job.id)
                return s
            if s is not None:
                # 設定違いで作り直す。古い方の動画ハンドルは必ず閉じる
                self._close(job.id)
            s = session_for_job(job, **overrides)
            self._items[job.id] = s
            self._order.append(job.id)
            while len(self._order) > self.limit:
                self._close(self._order[0])
            return s

    def drop(self, job_id: str) -> None:
        with self._lock:
            self._close(job_id)

    def close_all(self) -> None:
        with self._lock:
            for jid in list(self._order):
                self._close(jid)

    # -- 内部 -----------------------------------------------------------
    def _touch(self, job_id: str) -> None:
        if job_id in self._order:
            self._order.remove(job_id)
        self._order.append(job_id)

    def _close(self, job_id: str) -> None:
        s = self._items.pop(job_id, None)
        if job_id in self._order:
            self._order.remove(job_id)
        if s is not None:
            try:
                s.reader.close()
            except Exception:  # noqa: BLE001
                pass


def sync_meta(job: Job, session: review.ReviewSession) -> None:
    """検査で分かった数字を meta.json に戻す。

    案件をこなすほど「どの設定で何件手直しが要ったか」が溜まる形にしたい。
    検査画面を閉じたあとでも一覧から読めるように、セッションではなく
    meta に書く。
    """
    prog = session.progress_payload()
    job.meta["n_corrections"] = count_corrections(job)
    job.meta["review"] = {
        "queue_total": prog["total"],
        "done": prog["done"],
        "counts": prog["counts"],
    }
    # 「漏れ」件数 = 検査で塞いだ枚数。改善の効果を測る主指標にする
    job.meta["leak_count"] = prog["counts"].get("fixed", 0)
    job.save()
