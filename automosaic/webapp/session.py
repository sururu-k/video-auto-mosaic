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
from . import runner
from .jobs import Job, count_corrections

# 同時に開いておくセッション数。1人で使う道具なので数本で足りる。
# 開きっぱなしにすると VideoCapture のぶんだけメモリとハンドルを持つ。
#
# 実測（issue #25。4000フレーム、間欠検出、1920x1080相当の座標）:
# regions 612KB + per_frame 374KB + coverage(str) 4KB ≈ 1MB/セッション
# （Python オブジェクトぶんのみ。VideoCapture 自体が確保する OS 側の
# デコーダバッファ/ハンドルは未測定）。issue に書かれた実測 32,000フレーム
# 規模でも Python オブジェクト側は数MB止まりで、3という本数を決めている
# 実質のボトルネックは VideoCapture の本数（未測定）とセッション構築の
# 所要時間（構築は #25 でロックの外に出したので、本数を絞る理由としては
# 以前より薄くなっている）と見られる。VideoCapture 側を測っていないので、
# この数字だけでは MAX_OPEN を変える根拠にならない。変えるなら測ってから。
MAX_OPEN = 3


def _review_flag_names() -> set[str]:
    """review.py の argparse が実際に受け付けるオプション名。

    「job.meta の settings のうち、どのキーがレビューの絵に効くか」を
    ここから決める。review.py 側にも許可リストを持つと、review.py が
    オプションを増減したときに session.py が追随せず食い違う。
    """
    p = review.build_parser()
    return {opt for a in p._actions for opt in a.option_strings}


def session_overrides(settings: dict) -> dict:
    """ジョブの meta.settings から、レビューの絵（領域計算・描画）に効く
    項目だけを session_for_job() の overrides として取り出す。

    キー・CLI 引数名・cast は `runner.SETTINGS_OPTS` / `runner.SETTINGS_FLAGS`
    （`build_argv()` と同じ場所）を見る。2箇所に書き写さない（#15）。

    `infer_size` / `conf` / `crf` / `provider` / `tta` / `detect_only` /
    `limit_frames` は検出や最終エンコードにしか効かず、レビューは既存の
    `detections.json` を読むだけなので関係ない。review.py の argparse が
    そもそもこれらの名前を知らないので、`_review_flag_names()` との
    突き合わせで自然に外れる。

    値は画面から自由に打ち込める文字列（またはブラウザ側の不具合で
    型が違うもの）なので、壊れた値・型違いが入りうる。黙って無視すると
    「設定したのに反映されない」がまた起きるだけで、そのまま渡すと
    原因の分からない例外になる。ここで cast し、失敗したキーは理由付きで
    ValueError にする（呼び出し側が 400 として利用者に見せる。素通しの絵を
    「合ってる」と誤認させるより、レビューを止めるほうが安全 -- RULES.md 0）。
    """
    if not isinstance(settings, dict):
        # meta.json 自体が壊れているケース。個別キーの話ではないので、
        # 「設定なし」として扱う（review.py の既定値に落ちる）
        return {}

    flags = _review_flag_names()
    out: dict[str, object] = {}
    bad: list[str] = []

    for key, flag, cast in runner.SETTINGS_OPTS:
        if flag not in flags:
            continue
        v = settings.get(key)
        if v is None or v == "":
            continue
        try:
            out[key] = cast(v)
        except (TypeError, ValueError):
            bad.append(f"{key}={v!r}")

    for key in runner.SETTINGS_FLAGS:
        flag = "--" + key.replace("_", "-")
        if flag not in flags:
            continue
        v = settings.get(key)
        if v is None or v == "":
            continue
        out[key] = runner.parse_setting_bool(key, v)

    if bad:
        raise ValueError(
            "ジョブの設定に壊れた値があります（レビューを開けません）: "
            + ", ".join(bad)
        )
    return out


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
        # そのセッションを組んだときの overrides。ジョブに settings が
        # 入っている限り、毎回の /state /queue /mark 呼び出しで overrides が
        # 空でなくなる（#15）。「overrides が空か」だけで判定すると、
        # 呼ぶたびに同じ設定で作り直すことになり、det.json の再パースと
        # temporal.process() の全フレーム再計算（実測 5.2秒/32,000フレーム）
        # を1リクエストごとに払うことになる。前回と同じ overrides なら使い回す
        self._overrides: dict[str, dict] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        # 構築中のジョブID -> 完了通知。get() の中で組む
        # (実測 5.2秒/32,000フレーム。det.json のパース + temporal.process()
        # の全フレーム再計算) をこのクラスの _lock の下でやると、無関係な
        # 他ジョブの get() まで待たされる（issue #25）。構築はロックの外で
        # 行い、同じジョブへ同時に来た要求は自分では作らず先行者を待つ
        # （二重に 5 秒級の処理を走らせない・後発が先発の動画ハンドルを
        # 即座に閉じて捨てる事故を避ける）。
        self._building: dict[str, threading.Event] = {}
        self.limit = limit

    def get(self, job: Job, **overrides) -> review.ReviewSession:
        while True:
            with self._lock:
                s = self._items.get(job.id)
                # overrides が前回と同じ（空も含む）なら使い回す。#15 で
                # ジョブに settings が入ると overrides が常に非空になったので、
                # 「overrides が空か」だけの判定では毎回作り直しになってしまう。
                # 前回の overrides と比べて、同じなら使い回す
                if s is not None and self._overrides.get(job.id) == overrides:
                    self._touch(job.id)
                    return s
                ev = self._building.get(job.id)
                if ev is None:
                    ev = threading.Event()
                    self._building[job.id] = ev
                    mine = True
                else:
                    mine = False
            if mine:
                break
            ev.wait()
            # 出来上がったはず。キャッシュを見直すところからやり直す
            # （待っていた側の overrides が先行者と違えば、今度は自分が作る番になる）

        try:
            new_s = session_for_job(job, **overrides)
        except BaseException:
            # 構築に失敗しても、待っているスレッドは解放する。ここでは
            # self._items を触らない（new_s が無い）ので、_items への反映と
            # _building の解除を分けて2回ロックを取り直すと、その間隙で
            # 「_items にまだ無い・_building ももう無い」を見た待機スレッドが
            # 自分も構築側に回ってしまう（TOCTOU。二重構築の実測あり）。
            # 正常系はこの隙間を作らないよう、下の1回のロックで両方を済ませる
            with self._lock:
                self._building.pop(job.id, None)
                ev.set()
            raise

        with self._lock:
            # 設定違いで作り直された場合、古い方の動画ハンドルは必ず閉じる。
            # 構築が終わるまで古いセッションを差し替えないので、構築中も
            # 他の要求は（overrides が前回と同じである限り）引き続き古い方を使える。
            #
            # self._items への反映と _building の解除・ev.set() を同じロック
            # 保持区間でやる。分けると、解除後・反映前の隙間で目覚めた待機
            # スレッドが「_items にまだ無い」「_building ももう無い」を見て
            # 自分も構築側に回り、二重構築になる（実測: setswitchinterval
            # 1e-6 下で500試行中320件、最大6重）。
            old = self._items.get(job.id)
            self._items[job.id] = new_s
            self._overrides[job.id] = overrides
            self._touch(job.id)
            while len(self._order) > self.limit:
                self._close(self._order[0])
            self._building.pop(job.id, None)
            ev.set()
        if old is not None and old is not new_s:
            try:
                old.reader.close()
            except Exception:  # noqa: BLE001
                pass
        return new_s

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
        self._overrides.pop(job_id, None)
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
