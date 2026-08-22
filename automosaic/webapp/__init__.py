"""アップロードから完成品の受け取りまでを1画面で扱う Web アプリ。

既存の `automosaic.review` は「手元にある動画と検出結果を見る」ための道具で、
素材を渡すところと完成品を受け取るところが外にある。案件を1件ずつこなす
使い方だと、そこが毎回コマンドラインの操作になって続かない。

このパッケージは `data/library/<ジョブID>/` を1件の作業単位として持ち、
アップロード・処理の起動・検査・手修正・書き出しを同じ場所に集める。
処理そのものは既存の CLI をサブプロセスで呼ぶだけで、検出も描画も
再実装しない。領域計算と検査キューは `automosaic.review` を import して使う。

`automosaic.review` は利用者が今も使っているので、こちらからは触らない。
"""

from __future__ import annotations

__all__ = ["create_app", "jobs", "runner"]


def create_app(*args, **kwargs):
    """FastAPI アプリを組む。import を遅らせて fastapi 未導入でも
    `automosaic.webapp.jobs` だけを使えるようにしておく。"""
    from .app import create_app as _create

    return _create(*args, **kwargs)
