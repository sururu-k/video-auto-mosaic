"""人手レビュー UI。ローカルに HTTP サーバを立てて、手元の端末から修正する。

設計目標は「全フレームを目視しない」こと。実素材で全体の 28% が
「推定のみで覆っている区間」（実観測が1つも無く、補間と memory だけで
塗っている）だと分かっている。そこだけを提示して、そこだけ直す。

主画面は検査キュー方式にしてある。タイムラインを行き来させると、
「どこまで見たか」を利用者が覚えていなければならず、片手持ちの端末では
それが破綻する。見るべきフレームをサーバ側で並べ、1枚ずつ出して、
1タップで次に送る。判定の記録はサーバが持つので、途中で閉じても続きから戻れる。

外部依存を増やさないために標準ライブラリの http.server を使う。単一利用者の
ローカルツールなので性能要件は無く、素の ThreadingHTTPServer で足りる。
ただし画像取得中に API が止まると操作不能になるのでスレッド化は必須。

領域の計算は temporal.process() をそのまま呼ぶ。ここで独自に領域を作ると
「レビュー画面で見た絵」と「実際に焼かれる絵」がずれ、レビューの意味が消える。

LAN に出す前提があるので、アクセストークンを必須にした。扱うのが成人向けの
素材である以上、同じネットワークに繋がっているだけで見られる状態は作らない。
"""

from __future__ import annotations

import argparse
import hmac
import json
import math
import mimetypes
import os
import re
import secrets
import socket
import sys
import threading
import webbrowser
from dataclasses import astuple, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import cv2
import numpy as np

from .corrections import Correction, CorrectionSet
from .detector import Detection
from .render import apply_regions, default_block_size
from .webapp import spans
from .temporal import (
    TemporalConfig,
    effective_settings,
    effective_settings_sha256,
    estimated_only_ranges,
    narrow_without_estimate_gaps,
    process,
    resolve_classes,
)

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# Region.source を1文字に潰す。フレーム数ぶん送るので JSON を短くしたい。
SOURCE_CODE = {
    "detected": "d",
    "interpolated": "i",
    "memory": "m",
    "bridged": "b",
    "manual": "x",
}

# 被覆状況の1フレーム1文字表現。タイムライン描画はこれだけで済む。
COV_NONE = "0"       # 素通し
COV_REAL = "1"       # 実観測（検出 or 手修正）を含む
COV_ESTIMATED = "2"  # 推定だけで覆っている

# 検査キューに載せる理由。優先度が小さいほど先に見る。
#
# 「検出破棄」が最優先なのは、despike が実観測を丸ごと捨てた帯だから。捨てた
# フレームに他の根拠が残っていれば「被覆あり」に見えてキューの他の基準
# （uncovered 等）に一切引っかからない。#23 の実測では despike が捨てた
# 125件のトラックのうち 40件が実際にそのフレームを素通しにしていたが、
# 残り85件は「別の根拠でフレームは被覆扱いだが、捨てた対象の実在は未確認」
# のまま埋もれていた。ここを最優先にしないと、いちばん確認が要る帯が
# 検査キューに一度も現れない。
#
# 「未処理」を「推定のみ」より上にしたのは実測による（issue #21、
# docs/13-queue-priority-2026-08-25.md）。実素材（55,303フレーム、他社が
# 漏らした78区間の人間検証つき）で、この動画の「確実に未塗装」5,075フレーム
# （78区間 ∩ 未処理）を、despiked と uncovered の2理由だけで**100%捕捉した**
# のに対し、estimated / area_jump / low_conf はこの5,075フレームを
# **1件も追加で捕捉しなかった**（0/5075）。以前の並びは「推定のみ」を
# 「未処理」より上に置いていたが、それは処理量の重さの直感で決めた並びで、
# 実測してみたら未処理のほうが漏れ捕捉に直結していた
# （RULES.md 2.1「既知の基準を借りてこない。分布を測ってから決める」）。
# estimated / area_jump / low_conf を消していない理由: 78区間データセットは
# 「他社ツールが完全に沈黙した場所」しか測れておらず、「検出はしたが位置が
# ズレている」「そもそも塗り忘れ以外の壊れ方」を測る物差しではない。
# 0/5075 は「この物差しでは効かなかった」であって「無価値」ではない。
# 落とさず優先度を下げるだけにした（RULES.md 0）。
QUEUE_REASONS = {
    "despiked": (1, "検出破棄"),
    "uncovered": (2, "未処理"),
    "estimated": (3, "推定のみ"),
    "area_jump": (4, "面積の急変"),
    "low_conf": (5, "低信頼"),
    "sampled": (6, "定期確認"),
}

# 既定のキュー（all_frames=False）に載せる理由。despiked と uncovered は
# 実測で「確実に未塗装」を100%捕捉した2理由（上のコメント参照）。それ以外
# （estimated / area_jump / low_conf）は既定のキューから外し、all_frames=True
# （フロントには既に「全部見る」トグルとして配線済み。frontend/src/review/app.tsx
# の setAllFrames）でだけ出す。理由を削除するわけではない――計算はそのまま
# 続ける。既定表示から外すだけで、build_queue() の呼び出し側が拾えば
# いつでも全件に戻せる。
DEFAULT_QUEUE_REASONS = frozenset({"despiked", "uncovered"})

# 1つの区間（despiked/estimated/uncovered の各レンジ）から拾う代表フレーム数の
# 上限。以前は区間の長さに関係なく queue_step おきに全部拾っており、実素材の
# 最長未処理区間（644フレーム = 21.5秒）1本だけで129件のキュー項目を生んでいた。
# 始点・中点・終点の3枚あれば区間の存在とおおよその範囲は伝わり、キューの
# 「区間追従」操作（#81）で始点と終点を指定すればあいだは埋まる。実測
# （docs/13）では despiked+uncovered の全区間をこの上限で間引いても、
# 「確実に未塗装」5,075フレームの捕捉率は変わらなかった（区間が1件でも
# キューに現れれば、その区間の存在自体は見逃さないため）。
DEFAULT_MAX_PER_RANGE = 3

# 判定の種類。
#   ok              問題なし
#   fixed           漏れていたので塗る範囲を足した
#   unsure          判断できない
#   toobig          塗り過ぎていたので範囲を狭めた（remove + add の組で記録する）
#   false_positive  そもそも局部ではないところに乗っていたので消した（remove だけ）
#
# .progress.json の読み込みはこの並びに無い値を捨てる。増やすぶんには
# 古い記録がそのまま読めるし、古い版で新しい記録を開いても未知の判定が
# 落ちるだけで壊れない。順番には意味が無いので末尾に足す。
VERDICTS = ("ok", "fixed", "unsure", "toobig", "false_positive")

# 矩形を置かせる判定。押しただけでは終わらず、位置指定モードに入る
BOX_VERDICTS = ("fixed", "toobig")

# 自動領域を選ばせる判定。位置ではなく「どれを消すか」を指定させる。
# BOX_VERDICTS と分けてあるのは、こちらは add を一切置かないため。
# 同じ扱いにすると「消したのに塗り足す」という逆向きの修正が混ざる。
PICK_VERDICTS = ("false_positive",)

COOKIE_NAME = "automosaic_t"


# --------------------------------------------------------------------------
# Range ヘッダ
# --------------------------------------------------------------------------

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Range ヘッダを (start, end) の閉区間に直す。扱えないものは None。

    ここを手抜きすると <video> がシークできない。ブラウザは末尾指定
    (bytes=-N) と開区間 (bytes=N-) の両方を投げてくるので両方通す。
    複数レンジ (bytes=0-9,20-29) は 206 multipart が要るが、実際の
    ブラウザは動画再生で使ってこないので未対応にして全体を返す。
    """
    if not header or size <= 0:
        return None
    m = _RANGE_RE.match(header.strip())
    if not m:
        return None
    first, last = m.group(1), m.group(2)
    if first == "":
        if last == "":
            return None
        # 末尾 N バイト
        length = int(last)
        if length <= 0:
            return None
        start = max(0, size - length)
        return start, size - 1
    start = int(first)
    if start >= size:
        return None  # 416 相当。呼び出し側で扱う
    end = int(last) if last else size - 1
    end = min(end, size - 1)
    if end < start:
        return None
    return start, end


# --------------------------------------------------------------------------
# アクセストークン
# --------------------------------------------------------------------------


def make_token() -> str:
    """URL に載せるアクセストークン。

    URL に手で打つ可能性があるので token_urlsafe より短く、記号の無い
    英数字にする。12 文字あれば LAN 内の総当たりで当たる長さではない。
    """
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"  # 紛らわしい l, o, 0, 1 は外す
    return "".join(secrets.choice(alphabet) for _ in range(12))


def _inside(base: str, path: str) -> bool:
    """path が base の中にあるか。

    文字列の先頭一致で判定すると兄弟ディレクトリが通ってしまう。
    base が automosaic/web のとき automosaic/webapp/ が startswith を満たすため、
    LAN に公開した状態で webapp 配下のソースが読めていた。
    """
    base_abs = os.path.abspath(base)
    try:
        return os.path.commonpath([base_abs, os.path.abspath(path)]) == base_abs
    except ValueError:  # ドライブが違う等、共通の親が取れない場合
        return False


def token_matches(expected: str, given: str | None) -> bool:
    """トークンの照合。長さの違いで早期に返らないよう compare_digest を使う。

    非 ASCII が来ると compare_digest が TypeError を投げるので、先に弾く。
    """
    if not expected:
        return True  # トークン無効化時（テスト用）
    if not given:
        return False
    if not given.isascii():
        # compare_digest は str 同士だと両方 ASCII を要求する。
        # 非 ASCII をそのまま渡すと認証の判定ではなく例外で 500 になる
        return False
    return hmac.compare_digest(expected, given)


def cookie_token(header: str | None) -> str | None:
    """Cookie ヘッダから自分のトークンを拾う。

    画像やスクリプトの取得まで毎回 URL にトークンを付けさせると、
    どこか1本でも付け忘れた瞬間に画面が壊れる。最初の1回で Cookie に
    移して、以降はブラウザ任せにする。
    """
    if not header:
        return None
    for part in header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE_NAME:
            return v
    return None


def lan_addresses() -> list[str]:
    """この機械が LAN 上で名乗れる IPv4 アドレス。

    端末から開くための URL を出すのが目的なので、取り方は複数試して
    拾えたものを全部出す。既定経路の出口アドレスが本命だが、無線と有線が
    両方生きている環境ではそれ以外からも繋がる。
    """
    addrs: set[str] = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 実際には送らない。経路表を引かせて出口アドレスを得るだけ
        s.connect(("8.8.8.8", 80))
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith("127."))


# --------------------------------------------------------------------------
# QR コード（バイトモード / 誤り訂正 L / 型番 1-9）
# --------------------------------------------------------------------------
#
# 端末で開くのに URL を手打ちさせたくない。外部ライブラリは入れられないので
# 自前で組む。用途は「短い URL を1枚だけ出す」ことに限られるので、
# 型番 9（232 バイト）まで・誤り訂正 L・バイトモードだけに絞ってある。
# 割り当ての表が型番ごとに1組で済み、ブロック長も揃うので実装が短くなる。

# 型番 -> (全コードワード数, ブロックあたりの誤り訂正コードワード数, ブロック数)
_QR_L = {
    1: (26, 7, 1),
    2: (44, 10, 1),
    3: (70, 15, 1),
    4: (100, 20, 1),
    5: (134, 26, 1),
    6: (172, 18, 2),
    7: (196, 20, 2),
    8: (242, 24, 2),
    9: (292, 30, 2),
}

# 位置合わせパターンの中心座標
_QR_ALIGN = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
}

_QR_MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _gf_tables() -> tuple[list[int], list[int]]:
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D  # QR の既約多項式
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_GF_EXP, _GF_LOG = _gf_tables()


def _rs_generator(n: int) -> list[int]:
    """次数 n の生成多項式。係数は次数の高い順。"""
    g = [1]
    for i in range(n):
        # g(x) * (x + a^i) を展開する
        new = [0] * (len(g) + 1)
        for j, coef in enumerate(g):
            new[j] ^= coef
            if coef:
                new[j + 1] ^= _GF_EXP[(_GF_LOG[coef] + i) % 255]
        g = new
    return g


def _rs_encode(data: list[int], n_ec: int) -> list[int]:
    """Reed-Solomon の剰余を求める。多項式除算をそのまま回す。"""
    gen = _rs_generator(n_ec)
    rem = list(data) + [0] * n_ec
    for i in range(len(data)):
        coef = rem[i]
        if coef == 0:
            continue
        lead = _GF_LOG[coef]
        for j in range(len(gen)):
            rem[i + j] ^= _GF_EXP[(_GF_LOG[gen[j]] + lead) % 255] if gen[j] else 0
    return rem[len(data):]


def _bch_format(bits5: int) -> int:
    """形式情報 15 ビット。BCH(15,5) を付けて既定のマスクで XOR する。"""
    v = bits5 << 10
    for i in range(4, -1, -1):
        if v & (1 << (i + 10)):
            v ^= 0b10100110111 << i
    return ((bits5 << 10) | v) ^ 0b101010000010010


def _bch_version(ver: int) -> int:
    """型番情報 18 ビット。型番 7 以上でのみ書き込む。"""
    v = ver << 12
    for i in range(5, -1, -1):
        if v & (1 << (i + 12)):
            v ^= 0b1111100100101 << i
    return (ver << 12) | v


def _qr_penalty(m: list[list[int]], size: int) -> int:
    """マスク選択のための減点。仕様の4規則をそのまま数える。"""
    score = 0
    # 規則1: 同色の連続
    for line in list(m) + [list(col) for col in zip(*m)]:
        run, prev = 1, line[0]
        for v in line[1:]:
            if v == prev:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run, prev = 1, v
        if run >= 5:
            score += 3 + (run - 5)
    # 規則2: 2x2 の同色ブロック
    for r in range(size - 1):
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    # 規則3: 位置検出パターンに似た並び
    pat_a = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat_b = list(reversed(pat_a))
    for line in list(m) + [list(col) for col in zip(*m)]:
        for i in range(size - 10):
            seg = line[i:i + 11]
            if seg == pat_a or seg == pat_b:
                score += 40
    # 規則4: 全体の暗モジュール比率の偏り
    dark = sum(sum(row) for row in m)
    ratio = dark * 100 // (size * size)
    lo = (ratio // 5) * 5
    score += 10 * min(abs(lo - 50) // 5, abs(lo + 5 - 50) // 5)
    return score


def qr_matrix(text: str) -> list[list[int]]:
    """文字列を QR のモジュール行列（1=暗）にする。長すぎるなら ValueError。"""
    data = text.encode("utf-8")
    need_bits = 4 + 8 + 8 * len(data)  # モード + 文字数（型番1-9は8ビット） + 本体
    version = None
    for v in range(1, 10):
        total, ec_per, blocks = _QR_L[v]
        if need_bits <= (total - ec_per * blocks) * 8:
            version = v
            break
    if version is None:
        raise ValueError("QR に収まらない長さです")

    total, ec_per, blocks = _QR_L[version]
    n_data = total - ec_per * blocks

    bits: list[int] = []

    def put(value: int, width: int) -> None:
        for i in range(width - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)
    put(len(data), 8)
    for b in data:
        put(b, 8)
    put(0, min(4, n_data * 8 - len(bits)))  # 終端子
    while len(bits) % 8:
        bits.append(0)
    codewords = [int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits), 8)]
    # 余りは 0xEC / 0x11 の交互で埋める（仕様どおり）
    pad = (0xEC, 0x11)
    k = 0
    while len(codewords) < n_data:
        codewords.append(pad[k % 2])
        k += 1

    # 型番 1-9 の L はブロック長が揃うので、単純な等分でよい
    per = n_data // blocks
    dblocks = [codewords[i * per:(i + 1) * per] for i in range(blocks)]
    eblocks = [_rs_encode(b, ec_per) for b in dblocks]

    stream: list[int] = []
    for i in range(per):
        for b in dblocks:
            stream.append(b[i])
    for i in range(ec_per):
        for b in eblocks:
            stream.append(b[i])

    bitstream: list[int] = []
    for cw in stream:
        for i in range(7, -1, -1):
            bitstream.append((cw >> i) & 1)

    size = 17 + 4 * version
    m = [[0] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def rect(r0: int, c0: int, h: int, w: int, val: int | None = None) -> None:
        for r in range(r0, r0 + h):
            for c in range(c0, c0 + w):
                if 0 <= r < size and 0 <= c < size:
                    fixed[r][c] = True
                    if val is not None:
                        m[r][c] = val

    def finder(r0: int, c0: int) -> None:
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = r0 + r, c0 + c
                if not (0 <= rr < size and 0 <= cc < size):
                    continue
                inside = 0 <= r < 7 and 0 <= c < 7
                dark = inside and (
                    r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4)
                )
                m[rr][cc] = 1 if dark else 0
                fixed[rr][cc] = True

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    for i in range(size):  # タイミングパターン
        if not fixed[6][i]:
            m[6][i] = 1 - (i % 2)
            fixed[6][i] = True
        if not fixed[i][6]:
            m[i][6] = 1 - (i % 2)
            fixed[i][6] = True

    centers = _QR_ALIGN[version]
    for r in centers:
        for c in centers:
            if (r < 9 and c < 9) or (r < 9 and c > size - 10) or (r > size - 10 and c < 9):
                continue  # 位置検出パターンと重なるところには置かない
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    m[r + dr][c + dc] = 1 if max(abs(dr), abs(dc)) != 1 else 0
                    fixed[r + dr][c + dc] = True

    m[size - 8][8] = 1  # 常に暗いモジュール
    fixed[size - 8][8] = True
    rect(8, 0, 1, 9)          # 形式情報（横）
    rect(0, 8, 9, 1)          # 形式情報（縦）
    rect(size - 7, 8, 7, 1)
    rect(8, size - 8, 1, 8)
    if version >= 7:
        rect(size - 11, 0, 3, 6)
        rect(0, size - 11, 6, 3)

    # データ配置。右下から2列ずつ、縦蛇行。6列目はタイミングなので飛ばす
    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        for i in range(size):
            row = (size - 1 - i) if upward else i
            for c in (col, col - 1):
                if fixed[row][c]:
                    continue
                m[row][c] = bitstream[idx] if idx < len(bitstream) else 0
                idx += 1
        upward = not upward
        col -= 2

    best = None
    for mask_no, cond in enumerate(_QR_MASKS):
        cand = [row[:] for row in m]
        for r in range(size):
            for c in range(size):
                if not fixed[r][c] and cond(r, c):
                    cand[r][c] ^= 1
        # 形式情報。ビット0が最下位で、左上は縦→横、右上と左下は横→縦に並ぶ
        fmt = _bch_format(0b01000 | mask_no)  # 誤り訂正 L = 01
        for i in range(6):
            cand[i][8] = (fmt >> i) & 1
        cand[7][8] = (fmt >> 6) & 1
        cand[8][8] = (fmt >> 7) & 1
        cand[8][7] = (fmt >> 8) & 1
        for i in range(9, 15):
            cand[8][14 - i] = (fmt >> i) & 1
        for i in range(8):
            cand[8][size - 1 - i] = (fmt >> i) & 1
        for i in range(8, 15):
            cand[size - 15 + i][8] = (fmt >> i) & 1
        cand[size - 8][8] = 1
        if version >= 7:
            vinfo = _bch_version(version)
            for i in range(18):
                bit = (vinfo >> i) & 1
                cand[i // 3][size - 11 + i % 3] = bit
                cand[size - 11 + i % 3][i // 3] = bit
        p = _qr_penalty(cand, size)
        if best is None or p < best[0]:
            best = (p, cand)
    return best[1]


def qr_terminal(matrix: list[list[int]], quiet: int = 4) -> str:
    """モジュール行列を端末に出せる文字列にする。

    上下2行を半ブロック文字1つにまとめる。端末の文字は縦長なので、
    こうしないとモジュールが縦に伸びて読み取り精度が落ちる。
    色は明示指定にする。端末の配色が明るいか暗いかに依存させると、
    背景と同化して読めない事故が起きる。
    """
    size = len(matrix)
    pad = [0] * (size + quiet * 2)
    rows = [pad[:] for _ in range(quiet)]
    for row in matrix:
        rows.append([0] * quiet + list(row) + [0] * quiet)
    rows.extend([pad[:] for _ in range(quiet)])
    if len(rows) % 2:
        rows.append(pad[:])

    out = []
    for r in range(0, len(rows), 2):
        line = []
        for top, bottom in zip(rows[r], rows[r + 1]):
            # 暗モジュール=黒。前景色を上半分、背景色を下半分に割り当てる
            fg = 30 if top else 37
            bg = 40 if bottom else 47
            line.append(f"\x1b[{fg};{bg}m▀")
        out.append("".join(line) + "\x1b[0m")
    return "\n".join(out)


def qr_terminal_blocks(matrix: list[list[int]], quiet: int = 2) -> str:
    """半ブロック文字が出せない端末（cp932 など）向けの代替。

    1モジュールを空白2つで塗る。縦に間延びするが、文字集合が ASCII だけで
    済むので、どの端末でも化けない。
    """
    size = len(matrix)
    out = []
    for r in range(-quiet, size + quiet):
        line = []
        for c in range(-quiet, size + quiet):
            dark = 0 <= r < size and 0 <= c < size and matrix[r][c]
            line.append("\x1b[40m  " if dark else "\x1b[47m  ")
        out.append("".join(line) + "\x1b[0m")
    return "\n".join(out)


def print_qr(text: str, stream=None) -> bool:
    """URL を QR として出す。出せなければ False（呼び側は URL 表示で済ませる）。"""
    stream = stream or sys.stdout
    try:
        matrix = qr_matrix(text)
    except ValueError:
        return False
    art = qr_terminal(matrix)
    enc = getattr(stream, "encoding", None) or "utf-8"
    try:
        art.encode(enc)
    except (UnicodeEncodeError, LookupError):
        # 半ブロック文字を持たない端末。ASCII だけの版に落とす
        art = qr_terminal_blocks(matrix)
    try:
        stream.write(art + "\n")
    except UnicodeEncodeError:
        return False
    return True


# --------------------------------------------------------------------------
# 元動画のフレーム取り出し
# --------------------------------------------------------------------------


class FrameReader:
    """元動画から任意フレームを取り出す。VideoCapture を使い回す。

    set(CAP_PROP_POS_FRAMES) は毎回キーフレームまで戻るので、コマ送りの
    たびに呼ぶと目に見えて重い。前方への小さいジャンプは grab() で進める。
    """

    SEQ_LIMIT = 30  # これ以内の前方移動なら grab で詰める

    def __init__(self, path: str) -> None:
        self.path = path
        self._cap: cv2.VideoCapture | None = None
        self._next = -1  # 次に read() が返すフレーム番号
        self._lock = threading.Lock()

    def _open(self) -> cv2.VideoCapture:
        if self._cap is None:
            cap = cv2.VideoCapture(self.path)
            if not cap.isOpened():
                raise RuntimeError(f"動画を開けません: {self.path}")
            self._cap = cap
            self._next = 0
        return self._cap

    def read(self, n: int) -> np.ndarray | None:
        with self._lock:
            cap = self._open()
            if n != self._next:
                if self._next <= n <= self._next + self.SEQ_LIMIT:
                    for _ in range(n - self._next):
                        if not cap.grab():
                            return None
                else:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, n)
                self._next = n
            ok, frame = cap.read()
            self._next = n + 1 if ok else -1
            return frame if ok else None

    def close(self) -> None:
        with self._lock:
            if self._cap is not None:
                self._cap.release()
                self._cap = None


def mosaic_bgr(
    frame: np.ndarray, boxes: list, block: int, mode: str = "pixelize"
) -> np.ndarray:
    """BGR フレームに、焼き込みと同じ経路でモザイクをかける。

    プレビュー用に別実装を書くと「UI では隠れているのに出力では出ている」
    という最悪の食い違いが起きうる。I420 に変換して render.apply_regions を
    そのまま通し、見た目を出力と一致させる。
    """
    if not boxes:
        return frame
    h, w = frame.shape[:2]
    if h % 2 or w % 2:
        # 4:2:0 に落とせないので、偶数に切り詰めてから処理する
        h, w = h - (h % 2), w - (w % 2)
        frame = frame[:h, :w]
    yuv = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420)
    c_size = (h // 2) * (w // 2)
    y = yuv[:h]
    flat = yuv[h:].reshape(-1)
    u = flat[:c_size].reshape(h // 2, w // 2)
    v = flat[c_size : c_size * 2].reshape(h // 2, w // 2)
    apply_regions(y, u, v, boxes, block, mode=mode)
    return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)


# --------------------------------------------------------------------------
# セッション状態
# --------------------------------------------------------------------------


def median_box_size(
    per_frame: dict[int, list[Detection]], classes: set[str], fallback: int
) -> tuple[int, int]:
    """対象クラスの検出サイズの中央値。手で置く矩形の既定サイズに使う。

    平均だと単発の巨大な誤検出に引っ張られる。中央値なら「この動画で
    対象がだいたいこのくらいに写る」を素直に取れる。
    """
    ws = [d.box[2] for v in per_frame.values() for d in v if d.cls in classes]
    hs = [d.box[3] for v in per_frame.values() for d in v if d.cls in classes]
    if not ws:
        return fallback, fallback
    ws.sort()
    hs.sort()
    return max(8, int(ws[len(ws) // 2])), max(8, int(hs[len(hs) // 2]))


def dominant_class(
    per_frame: dict[int, list[Detection]], classes: set[str]
) -> str:
    """対象クラスのうち、その動画でいちばん多く出ているもの。

    手で足す矩形の既定クラスに使う。クラス名の辞書順で決めると、
    その動画に映っていないクラスのラベルが学習データに混ざる。
    """
    counts: dict[str, int] = {}
    for v in per_frame.values():
        for d in v:
            if d.cls in classes:
                counts[d.cls] = counts.get(d.cls, 0) + 1
    if not counts:
        return sorted(classes)[0] if classes else "FEMALE_GENITALIA_EXPOSED"
    return max(counts.items(), key=lambda kv: kv[1])[0]


def runs_of(cov: str, ch: str, min_len: int = 1) -> list[tuple[int, int]]:
    """被覆文字列から、指定文字が連続する閉区間を拾う。"""
    out: list[tuple[int, int]] = []
    start = None
    for i, c in enumerate(cov):
        if c == ch:
            if start is None:
                start = i
        elif start is not None:
            if i - start >= min_len:
                out.append((start, i - 1))
            start = None
    if start is not None and len(cov) - start >= min_len:
        out.append((start, len(cov) - 1))
    return out


def tap_to_box(
    nx: float, ny: float, size: tuple[float, float], width: int, height: int
) -> tuple[float, float, float, float]:
    """正規化タップ座標 (0..1) を、そこを中心とする矩形に直す。

    座標変換をサーバ側に寄せてあるのは、端末の画面幅・回転・拡大率に
    左右されない値だけを受け取るため。画面ピクセルを送らせると、
    端末ごとの devicePixelRatio の扱いで静かにずれる。

    端に寄ったタップでは矩形を切り詰めずに内側へ押し戻す。切り詰めると
    画面端の対象を覆いきれず、「押したのに漏れたまま」になる。
    """
    w = max(4.0, min(float(width), float(size[0])))
    h = max(4.0, min(float(height), float(size[1])))
    cx = min(max(float(nx), 0.0), 1.0) * width
    cy = min(max(float(ny), 0.0), 1.0) * height
    x = min(max(cx - w / 2.0, 0.0), width - w)
    y = min(max(cy - h / 2.0, 0.0), height - h)
    return (round(x, 1), round(y, 1), round(w, 1), round(h, 1))


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix0, iy0 = max(ax, bx), max(ay, by)
    ix1, iy1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def best_overlap(box, boxes: list) -> int | None:
    """box といちばん重なる矩形の位置。1つも重ならなければ None。

    「誤検知」を前後のフレームへ広げるときの対応付けに使う。対象が動くと
    フレームごとに座標が違うので、同じ番号の領域が同じ対象だとは限らない。
    重なりで選べば、少なくとも「別物を消す」ことは起きない。

    重なりが 0 のものは選ばない。無関係な領域まで消すくらいなら、その
    フレームには何も置かないほうが安全側になる。
    """
    best, best_iou = None, 0.0
    for i, b in enumerate(boxes):
        v = _iou(box, b)
        if v > best_iou:
            best, best_iou = i, v
    return best


def cover_box(
    boxes: list, width: int, height: int
) -> tuple[float, float, float, float] | None:
    """渡した矩形をすべて含む最小の矩形。1つも無ければ None。

    「でかすぎる」で置く remove の範囲に使う。corrections.apply() は
    remove と重なる自動領域を落とす実装なので、打ち消したい領域を確実に
    含む1枚でなければ、狭めたはずの縁が残る。

    外側に丸めるのは、保存時に小数第1位へ丸められて境界がわずかに内側へ
    寄っても、接しているだけの領域を取り逃がさないため。
    """
    if not boxes:
        return None
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    x0 = max(0.0, float(math.floor(x0)))
    y0 = max(0.0, float(math.floor(y0)))
    x1 = min(float(width), float(math.ceil(x1)))
    y1 = min(float(height), float(math.ceil(y1)))
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def _thin(frames: list[int], step: int) -> list[int]:
    """近すぎるフレームを間引く。同じ現象で連続して足踏みさせないため。"""
    out: list[int] = []
    for f in frames:
        if not out or f - out[-1] >= step:
            out.append(f)
    return out


def _sample_range(start: int, end: int, step: int, max_items: int | None = None) -> list[int]:
    """区間から見るフレームを選ぶ。

    step より短い区間は中央の1枚だけにする。端を取ると隣の区間との
    境目が写り、その区間の実態が分からない絵になる。

    `max_items` を指定すると、step 刻みで拾った候補数がそれを超える場合に
    始点と終点を必ず含む均等間隔に切り替える。長い区間ほど step 刻みの
    候補数が線形に増え、実素材の最長未処理区間（644フレーム）1本だけで
    129件になっていた（issue #21）。始点・終点を必ず含むのは、区間の
    存在とおおよその範囲（どこから どこまで）を、間引いたあとも
    確実に伝えるため。
    """
    if end - start + 1 <= step:
        return [(start + end) // 2]
    length = end - start + 1
    n_by_step = (length + step - 1) // step
    if max_items and max_items >= 1 and n_by_step > max_items:
        if max_items == 1:
            return [(start + end) // 2]
        picked: list[int] = []
        for i in range(max_items):
            f = start + round(i * (end - start) / (max_items - 1))
            if not picked or picked[-1] != f:
                picked.append(f)
        return picked
    return list(range(start, end + 1, step))


def anomaly_frames(regions: dict, n_frames: int) -> tuple[list[int], list[int]]:
    """面積が急変したフレームと、低信頼のフレーム。

    temporal.review_flags と同じ基準。あちらは全区間の一覧を作る用途で、
    こちらはキューに載せる候補が要るだけなのでフレーム番号だけ返す。
    """
    area_jump: list[int] = []
    low_conf: list[int] = []
    prev_area = None
    for f in range(n_frames):
        regs = regions.get(f, [])
        if not regs:
            continue
        area = sum(b[2] * b[3] for b, _ in regs)
        if prev_area is not None and prev_area > 0:
            if area > prev_area * 2 or area < prev_area / 2:
                area_jump.append(f)
        prev_area = area
        if max(r.score for _, r in regs) < 0.30:
            low_conf.append(f)
    return area_jump, low_conf


def build_queue(
    coverage: str,
    regions: dict,
    n_frames: int,
    step: int = 5,
    all_frames: bool = False,
    despiked_ranges: list | None = None,
    max_per_range: int | None = DEFAULT_MAX_PER_RANGE,
) -> list[dict]:
    """見るべきフレームを、見るべき順に並べる。

    キューは起動時に1回だけ組む。修正のたびに組み直すと、直した瞬間に
    並びが変わって「いま何枚目を見ているのか」が消える。手が止まる。

    同じフレームが複数の理由に当たることはあるので、いちばん重い理由に
    寄せて1回だけ出す。同じ絵を理由違いで2回見せるのは時間の無駄。

    `all_frames=False`（既定）では DEFAULT_QUEUE_REASONS（despiked・
    uncovered）だけを載せる。この2理由だけで、実素材の「確実に未塗装」
    5,075フレームを100%捕捉できることを実測している
    （docs/13-queue-priority-2026-08-25.md、issue #21）。estimated・
    area_jump・low_conf は計算はするが既定のキューには積まない――
    削除ではなく降格で、`all_frames=True` で全理由が出る
    （frontend の「全部見る」トグルが既にこの引数に配線されている。
    frontend/src/review/app.tsx setAllFrames 参照）。

    `max_per_range` は despiked/estimated/uncovered の1区間あたりに積む
    代表フレーム数の上限（既定 DEFAULT_MAX_PER_RANGE=3）。None にすると
    従来どおり step 刻みで無制限に積む（後方互換）。長い区間1本が
    キューの大半を占める事態を防ぐためで、区間の存在自体（始点付近・
    終点付近が残る）は間引いても失われない。
    """
    step = max(1, int(step))
    picked: dict[int, str] = {}

    def add(frame: int, reason: str) -> None:
        if not (0 <= frame < n_frames):
            return
        cur = picked.get(frame)
        if cur is None or QUEUE_REASONS[reason][0] < QUEUE_REASONS[cur][0]:
            picked[frame] = reason

    def want(reason: str) -> bool:
        return all_frames or reason in DEFAULT_QUEUE_REASONS

    # despike が捨てた帯。temporal.despike() の契約により、呼び出し側は
    # これを必ず報告すること（temporal.py の despike() docstring）。捨てた
    # フレームに他の根拠（別トラックの補間・memory）が残っていることがあり、
    # その場合は coverage 上「被覆あり」に見えて他のどの基準にも引っかからない。
    # 区間の長さで足切りしない理由は uncovered と同じ: 1フレームでも
    # 実観測を丸ごと捨てているなら、それだけで確認に値する。
    if want("despiked"):
        for s, e, _cls, _score in despiked_ranges or []:
            for f in _sample_range(s, e, step, max_per_range):
                add(f, "despiked")
    # 未処理は1フレームでも素通しなので、長さで足切りしない
    if want("uncovered"):
        for s, e in runs_of(coverage, COV_NONE, min_len=1):
            for f in _sample_range(s, e, step, max_per_range):
                add(f, "uncovered")
    # 推定のみ。temporal.estimated_only_ranges() をそのまま使う。以前はここで
    # runs_of(coverage, COV_ESTIMATED, min_len=5) を自前で書いており、
    # サンプリング刻みを考慮しない固定 min_len=5 のせいで1〜4フレームの
    # 推定のみ区間がまるごとキューから消えていた。あちらは実観測間隔から
    # 間引き刻みを推定したうえで min_len=1 を効かせるので、短い区間も拾える
    if want("estimated"):
        for s, e, _peak in estimated_only_ranges(regions, n_frames):
            for f in _sample_range(s, e, step, max_per_range):
                add(f, "estimated")

    if want("area_jump") or want("low_conf"):
        jump, low = anomaly_frames(regions, n_frames)
        if want("area_jump"):
            for f in _thin(jump, step):
                add(f, "area_jump")
        if want("low_conf"):
            for f in _thin(low, step):
                add(f, "low_conf")

    if all_frames:
        for f in range(0, n_frames, step):
            add(f, "sampled")

    items = [
        {
            "frame": f,
            "reason": r,
            "priority": QUEUE_REASONS[r][0],
            "label": QUEUE_REASONS[r][1],
        }
        for f, r in picked.items()
    ]
    items.sort(key=lambda d: (d["priority"], d["frame"]))
    return items


@dataclass
class ReviewSession:
    """1本の動画に対するレビュー状態。ハンドラから共有される。"""

    video: str
    rendered: str | None
    corrections_path: str
    width: int
    height: int
    fps: float
    n_frames: int
    classes: set[str]
    cfg: TemporalConfig
    per_frame: dict[int, list[Detection]]
    corrections: CorrectionSet
    block: int
    default_size: tuple[int, int]
    default_class: str
    mode: str = "pixelize"
    queue_step: int = 5
    queue_all: bool = False
    queue_max_per_range: int | None = DEFAULT_MAX_PER_RANGE
    progress_path: str | None = None

    reader: FrameReader = field(init=False)
    # 状態変更（recompute / set_corrections / mark / undo / rebuild_queue）を
    # 直列化するロック。フレーム画像の取得（frame_image）はこれを取らない
    # (issue #25)。理由:
    #   - self.regions / self.stats / self.coverage / self.version は
    #     recompute() が新しい値を作ってから丸ごと代入で差し替える
    #     （インプレースで書き換えない）。CPython の属性代入は GIL の下で
    #     1バイトコードなので、途中の値が読めることはない。読めるのは
    #     「差し替え前の一貫した値」か「差し替え後の一貫した値」のどちらか。
    #   - self.block / self.mode はセッション構築後に変わらない。
    #   - 実際のフレーム読み出しは FrameReader._lock がすでに直列化している
    #     （同じ VideoCapture を2スレッドから同時に read() させないため）。
    # 複数フィールドをまたいで一貫性が要る state_payload 等はこのロックの下で読む。
    lock: threading.Lock = field(init=False)
    regions: dict = field(init=False, default_factory=dict)
    stats: dict = field(init=False, default_factory=dict)
    # despike が捨てたトラックの (start, end, class, max_score)。既定では
    # despike 自体が無効（min_track_len=0）なので通常は空。temporal.despike()
    # の契約上、呼び出し側はこれを必ず検査キューに反映すること
    despiked_ranges: list = field(init=False, default_factory=list)
    coverage: str = field(init=False, default="")
    queue: list = field(init=False, default_factory=list)
    verdicts: dict = field(init=False, default_factory=dict)
    history: list = field(init=False, default_factory=list)
    version: int = field(init=False, default=0)
    # 「誤検知」と判定された自動領域。{"frame", "box", "class"} の並び。
    # corrections.json の remove からは「狭めたのか、そもそも違ったのか」を
    # 区別できないので、否定の例としてここに別建てで持つ。
    false_positives: list = field(init=False, default_factory=list)

    # temporal.process() の結果のキャッシュ（issue #77）。
    # (per_frame の id, n_frames, width, height, classes, cfg, regions, stats,
    # despiked_ranges) を持つ。per_frame/classes/cfg/width/height/n_frames は
    # __post_init__ 以降どのメソッドも書き換えない（手修正は self.corrections
    # だけを動かす）ので、これらが変わっていない限り process() の出力は
    # 前回と1バイトも違わない。フィンガープリントが変わっていたら（将来
    # どこかの変更で書き換えられるようになったら）キャッシュを捨てて律儀に
    # 計算し直す。「たぶん変わっていない」で済ませない。
    _process_cache: tuple | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self.reader = FrameReader(self.video)
        self.lock = threading.Lock()
        if self.progress_path is None:
            # 修正ファイルの隣に置く。動画と修正の組ごとに進捗が分かれてほしい
            self.progress_path = os.path.splitext(self.corrections_path)[0] + ".progress.json"
        self.recompute()
        self.load_progress()
        self.rebuild_queue()

    # -- 実効設定のフィンガープリント（issue #16） ------------------------
    def effective_settings(self) -> dict:
        """このセッションが領域計算に使っている実効設定。

        cli.py が report.json に書くものと同じ形。ここが cli.py 側と
        1フィールドでも食い違うと、レビューは焼き込みと違う絵を見せている。
        """
        return effective_settings(self.cfg, self.classes, self.block, self.mode)

    def effective_sha256(self) -> str:
        return effective_settings_sha256(self.effective_settings())

    # -- 領域の再計算 ----------------------------------------------------
    def recompute(self) -> None:
        """検出 + 手修正から最終領域を作り直す。

        `process()`（トラッキング/結合/デスパイク/補間/橋渡し/膨張）は
        `self.per_frame` / `self.classes` / `self.cfg` /
        `self.width` / `self.height` / `self.n_frames` だけで決まり、
        手修正は `self.corrections` にしか触らない（`_save_corrections()` /
        `set_corrections()` / `undo()` を見ればわかる。process() の入力側の
        フィールドを書き換えるメソッドはこのクラスに無い）。つまり手修正の
        前後で process() の出力は1バイトも変わらない。以前は「差分更新は
        焼き込みと同じ計算という保証が崩れる」という理由で毎回全部やり直して
        いたが、それは正しい心配ではなかった —— 差分更新をするのではなく、
        変わらない入力から出た**同一の出力を使い回すだけ**なので、保証は
        1ミリも緩まない（issue #77）。
        手修正の適用（apply_corrections）は process() の後段でしか起きない
        処理なので、ここは毎回やる。
        """
        from .corrections import apply as apply_corrections

        # `id(self.per_frame)` と `self.cfg` をそのまま入れてはいけない。
        # id は解放後に別のオブジェクトへ再利用されるので、per_frame を
        # 差し替えたときに偶然一致しうる。cfg は frozen でない dataclass
        # なので、同じオブジェクトを入れると「保存した値」と「今の値」が
        # 同じものを指し、その場で書き換えられても比較が必ず一致してしまう。
        # per_frame は参照そのものを持って `is` で見る（参照を持つので
        # id の再利用も起きない）。cfg は astuple() で値を取り出して比べる。
        fingerprint = (
            self.n_frames,
            self.width,
            self.height,
            frozenset(self.classes),
            astuple(self.cfg),
        )
        cached = self._process_cache
        if cached is not None and cached[0] == fingerprint and cached[1] is self.per_frame:
            regions, stats, despiked_ranges = cached[2], dict(cached[3]), cached[4]
        else:
            regions, stats = process(
                self.per_frame, self.n_frames, self.width, self.height, self.classes, self.cfg
            )
            stats.pop("_left_open", None)
            despiked_ranges = stats.pop("_despiked_ranges", [])
            # apply_corrections() は regions を書き換えない（新しい dict/list を
            # 返す）ので、process() の生出力をキャッシュへそのまま持っていて安全。
            self._process_cache = (fingerprint, self.per_frame, regions, dict(stats), despiked_ranges)

        self.despiked_ranges = despiked_ranges
        self.regions = apply_corrections(regions, self.corrections)
        self.stats = stats

        cov = []
        for f in range(self.n_frames):
            regs = self.regions.get(f, [])
            if not regs:
                cov.append(COV_NONE)
            elif any(r.source in ("detected", "manual") for _, r in regs):
                cov.append(COV_REAL)
            else:
                cov.append(COV_ESTIMATED)
        self.coverage = "".join(cov)
        # フレーム画像はブラウザにキャッシュさせる。修正で絵が変わったことを
        # URL に載せて伝えるための世代番号
        self.version += 1

    def rebuild_queue(self) -> None:
        self.queue = build_queue(
            self.coverage,
            self.regions,
            self.n_frames,
            step=self.queue_step,
            all_frames=self.queue_all,
            despiked_ranges=self.despiked_ranges,
            max_per_range=self.queue_max_per_range,
        )

    # -- 進捗の保存 ------------------------------------------------------
    def load_progress(self) -> None:
        """判定の記録を読み戻す。壊れていたら黙って捨てる。

        進捗は失っても修正そのものは corrections.json に残る。読めない
        ファイルのために起動を止める価値はない。
        """
        if not self.progress_path or not os.path.exists(self.progress_path):
            return
        try:
            with open(self.progress_path, encoding="utf-8") as f:
                d = json.load(f)
            self.verdicts = {int(k): v for k, v in d.get("verdicts", {}).items() if v in VERDICTS}
            # fp は「誤検知」を足したときに増えた鍵。古い記録には無いので既定 0。
            # 無いものを 0 として読めば、旧版が書いた履歴もそのまま戻せる
            self.history = [
                {
                    "frame": int(h["frame"]),
                    "prev": h.get("prev"),
                    "added": int(h.get("added", 0)),
                    "fp": int(h.get("fp", 0)),
                }
                for h in d.get("history", [])
            ]
            self.false_positives = [
                {
                    "frame": int(p["frame"]),
                    "box": [float(v) for v in p["box"]],
                    "class": p.get("class", self.default_class),
                }
                for p in d.get("false_positives", [])
            ]
        except Exception:  # noqa: BLE001
            self.verdicts, self.history, self.false_positives = {}, [], []

    def save_progress(self) -> None:
        if not self.progress_path:
            return
        tmp = self.progress_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "video": os.path.basename(self.video),
                    "verdicts": {str(k): v for k, v in self.verdicts.items()},
                    "history": self.history,
                    "false_positives": self.false_positives,
                },
                f,
                ensure_ascii=False,
            )
        os.replace(tmp, self.progress_path)

    # -- 送出用の整形 ----------------------------------------------------
    def frame_regions(self, n: int) -> list:
        """1フレームぶんの矩形。[x, y, w, h, 由来, スコア]。

        描画に使うのは膨張後の矩形。実際にモザイクが乗る範囲そのものを
        見せないと「隠れているつもりで隠れていない」を見落とす。
        """
        return [
            [
                int(round(b[0])),
                int(round(b[1])),
                int(round(b[2])),
                int(round(b[3])),
                SOURCE_CODE.get(r.source, "?"),
                round(r.score, 3),
            ]
            for b, r in self.regions.get(n, [])
        ]

    def auto_regions(self, frame: int) -> list:
        """そのフレームの自動領域。(膨張後の矩形, Region) の組で返す。

        手で足した領域は含めない。remove は自動領域だけを落とす実装なので、
        手修正まで対象にしても効果が無く、無駄な否定領域が残るだけになる。
        """
        return [(b, r) for b, r in self.regions.get(frame, []) if r.source != "manual"]

    def auto_cover_box(self, frame: int) -> tuple[float, float, float, float] | None:
        """そのフレームの自動領域をまとめて包む矩形。無ければ None。

        包むのは膨張後の矩形（実際にモザイクが乗る範囲）。検出そのものの
        矩形で包むと、膨張したぶんの縁が remove から外れて残る。
        """
        return cover_box([b for b, _ in self.auto_regions(frame)], self.width, self.height)

    def regions_payload(self) -> dict:
        out: dict[str, list] = {}
        for f in range(self.n_frames):
            if self.regions.get(f):
                out[str(f)] = self.frame_regions(f)
        return out

    def frame_regions_payload(self, n: int) -> dict:
        """1コマぶんだけの矩形（#24）。

        タイムライン画面（frontend/src/timeline/timeline.tsx）は同時に
        1コマぶんの絵しか描かない（shared/canvas-draw.ts の
        drawRegionOverlay 参照）。regions_payload() は動画全体を積むので
        1時間の動画で10MBを超える。表示中のコマぶんだけ返せば足りる。
        """
        n = max(0, min(self.n_frames - 1, int(n)))
        return {"frame": n, "version": self.version, "regions": self.frame_regions(n)}

    def ranges_payload(self) -> dict:
        # ブラウザの「推定のみの区間」リストと G（次の推定のみ区間）キーが
        # 見るのはここ。build_queue と同じ理由で、自前の min_len=5 固定をやめ
        # temporal.estimated_only_ranges() を使う（サンプリング刻みを考慮する）
        est = estimated_only_ranges(self.regions, self.n_frames)
        unc = runs_of(self.coverage, COV_NONE, min_len=1)
        return {
            "estimated_only_ranges": [
                {"start": s, "end": e, "frames": e - s + 1} for s, e, _peak in est
            ],
            "uncovered_ranges": [
                {"start": s, "end": e, "frames": e - s + 1} for s, e in unc
            ],
            # despike が捨てた帯。他の根拠で被覆済みに見えることがあるので、
            # uncovered_ranges とは別に必ず出す（temporal.despike() の契約）
            "despiked_ranges": [
                {"start": s, "end": e, "class": c, "max_score": sc, "frames": e - s + 1}
                for s, e, c, sc in self.despiked_ranges
            ],
        }

    def progress_payload(self) -> dict:
        # 判定の種類から作る。判定を増やしたときに集計だけ落ちるのを防ぐ
        counts = {v: 0 for v in VERDICTS}
        done = 0
        for it in self.queue:
            v = self.verdicts.get(it["frame"])
            if v:
                counts[v] = counts.get(v, 0) + 1
                done += 1
        return {
            "total": len(self.queue),
            "done": done,
            "remaining": len(self.queue) - done,
            "counts": counts,
            "can_undo": bool(self.history),
        }

    def omitted_reason_counts(self) -> dict:
        """既定のキュー（all_frames=False）から外れている理由の区間・件数。

        削除ではなく降格であることを、往復するたびに payload 自身が示す
        （RULES.md 0「落としたものを黙って消さない」）。件数は区間・フレーム数
        そのもの（1区間あたりの間引き前）で、`all_frames=True` にすれば
        すべて同じキューに戻る。all_frames=True のときは何も外していないので
        空の辞書を返す。
        """
        if self.queue_all:
            return {}
        est = estimated_only_ranges(self.regions, self.n_frames)
        jump, low = anomaly_frames(self.regions, self.n_frames)
        return {
            "estimated": len(est),
            "area_jump": len(jump),
            "low_conf": len(low),
        }

    def queue_payload(self) -> dict:
        """キューの中身。各枚に、その場で重ねる矩形も同梱する。

        全フレームぶんの矩形（/api/state の regions）は1時間の動画で 10MB を
        超える。端末の回線でそれを起動時に落とさせるのは論外なので、
        実際に見るフレームのぶんだけ載せる。長さは動画ではなくキューに比例する。
        """
        return {
            "step": self.queue_step,
            "all_frames": self.queue_all,
            "max_per_range": self.queue_max_per_range,
            "version": self.version,
            "n_corrections": len(self.corrections.items),
            "items": [
                dict(
                    it,
                    verdict=self.verdicts.get(it["frame"]),
                    boxes=self.frame_regions(it["frame"]),
                )
                for it in self.queue
            ],
            "progress": self.progress_payload(),
            # 既定のキューから外れている理由と件数（区間ベース）。
            # all_frames=True で見れば全部戻る。0件ではなく「無い」を返す
            # (queue_all=True時は{})のと「降格して見えていない」({}以外)を
            # 区別できるようにしてある
            "omitted_by_default": self.omitted_reason_counts(),
        }

    def state_payload(self, light: bool = False) -> dict:
        """動画の基本情報。light では重い配列を落とす。

        検査キュー画面が要るのは解像度・クラス・既定サイズだけで、
        全フレームの矩形も被覆文字列も使わない。
        """
        d = {
            "video": os.path.basename(self.video),
            "rendered": os.path.basename(self.rendered) if self.rendered else None,
            "has_video": bool(self.rendered and os.path.exists(self.rendered)),
            "corrections_path": self.corrections_path,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "n_frames": self.n_frames,
            "block": self.block,
            "classes": sorted(self.classes),
            "default_size": list(self.default_size),
            "default_class": self.default_class,
            "stats": self.stats,
            "version": self.version,
            "queue_step": self.queue_step,
            "n_corrections": len(self.corrections.items),
        }
        if not light:
            d["coverage"] = self.coverage
            d["regions"] = self.regions_payload()
            d.update(self.ranges_payload())
        return d

    def update_payload(self, frame: int | None = None) -> dict:
        """修正を保存したあとに返す差分。state 全体より軽い。

        frame を渡すと、regions はそのコマぶんだけになる（#24）。
        タイムライン画面は矩形を1個置く・消す・戻すたびにここへ来るが、
        描き直すのは表示中の1コマだけなので、動画全体の領域マップを
        往復させる理由が無い（1時間の動画で10MB超。queue_payload() の
        コメントと同じ実測）。frame を渡さない呼び出し元には、これまでどおり
        全フレームぶんを返す（後方互換。webapp 側の /corrections など）。
        """
        d = {
            "ok": True,
            "coverage": self.coverage,
            "version": self.version,
            "n_corrections": len(self.corrections.items),
        }
        if frame is None:
            d["regions"] = self.regions_payload()
        else:
            frame = max(0, min(self.n_frames - 1, int(frame)))
            d["regions"] = {str(frame): self.frame_regions(frame)}
        d.update(self.ranges_payload())
        return d

    # -- 修正の受け取り --------------------------------------------------
    def set_corrections(self, items: list[dict]) -> None:
        self.corrections = CorrectionSet(
            video=os.path.basename(self.video),
            width=self.width,
            height=self.height,
            items=[Correction.from_dict(c) for c in items],
        )
        self.corrections.save(self.corrections_path)
        # 履歴を捨てる。undo は「末尾から件数ぶん落とす」実装なので、一覧を丸ごと
        # 差し替えたあとの履歴は別物を指す。件数が一致しても同一性の保証にならず、
        # 直前の判定ではなく無関係な修正が消える。消えるのは add（漏れを塞いだ矩形）
        # なので、そのフレームは素通しに戻る。
        self.history.clear()
        self.recompute()

    def mark(
        self,
        frame: int,
        verdict: str,
        tap: tuple[float, float] | None = None,
        size: tuple[float, float] | None = None,
        span: int = 0,
        cls: str | None = None,
        pick: list | None = None,
    ) -> int:
        """1フレームの判定を記録する。矩形を伴う判定なら修正も足す。

        fixed          指定範囲を add する。漏れを塞ぐ
        toobig         自動領域を包む remove と、指定範囲の add を組で置く。
                       remove だけだとそのフレームが素通しになるので add を必ず伴う。
                       「でかすぎる」は塗る範囲を狭める操作であって、塗らない操作ではない
        false_positive 選ばれた自動領域を包む remove だけを置く。add は置かない。
                       そこは局部ではなかったのだから、代わりに塗るものが無い

        span は前後に何フレーム広げるか。キューは間引いて出しているので、
        1コマだけ直しても隣のコマは元のまま。既定で間引き幅ぶん広げる。
        remove は「そのフレームの」自動領域から作る。対象が動いていると
        フレームごとに位置も大きさも違うので、1枚を使い回すと端が残る。

        戻り値は足した修正の件数（ひとつ戻すで消す件数でもある）。remove と
        add を両方置いたフレームは 2 件と数える。まとめて末尾に積むので、
        undo は件数ぶん切り落とすだけで remove と add が同時に取り消される。
        """
        if verdict not in VERDICTS:
            raise ValueError(f"不明な判定: {verdict}")
        frame = int(frame)
        # 範囲外を端へ寄せない。寄せると、応答は送った番号を返すのに記録は
        # 最終フレーム（や 0）に付く。「判定したはずのコマが未判定のまま」
        # 「触っていないコマに判定が付く」の両方が黙って起きる（webapp と揃える）
        if not 0 <= frame < self.n_frames:
            raise ValueError(
                f"フレーム番号が範囲外です: {frame}（0〜{self.n_frames - 1}）"
            )

        added = 0
        n_fp = 0
        if verdict in BOX_VERDICTS:
            if tap is None:
                # toobig で remove だけが残ると、そのフレームは素通しになる。
                # 位置が無いなら判定ごと拒否して、記録も進捗も動かさない
                raise ValueError("位置が指定されていません")
            box = tap_to_box(tap[0], tap[1], size or self.default_size, self.width, self.height)
            lo = max(0, frame - max(0, int(span)))
            hi = min(self.n_frames - 1, frame + max(0, int(span)))
            for f in range(lo, hi + 1):
                if verdict == "toobig":
                    cover = self.auto_cover_box(f)
                    # 自動領域が無いフレームには remove を置かない。効果が無い
                    # うえ、あとから検出をやり直したときに効いてしまう
                    if cover is not None:
                        self.corrections.items.append(
                            Correction(
                                frame=f,
                                box=cover,
                                cls=cls or self.default_class,
                                kind="remove",
                            )
                        )
                        added += 1
                self.corrections.items.append(
                    Correction(frame=f, box=box, cls=cls or self.default_class, kind="add")
                )
                added += 1
            self._save_corrections()

        elif verdict in PICK_VERDICTS:
            added, n_fp = self._mark_false_positive(frame, pick, span, cls)

        self.history.append(
            {"frame": frame, "prev": self.verdicts.get(frame), "added": added, "fp": n_fp}
        )
        self.verdicts[frame] = verdict
        self.save_progress()
        return added

    def mark_interval(
        self,
        frame: int,
        tap: tuple[float, float],
        start_frame: int,
        start_tap: tuple[float, float],
        size: tuple[float, float] | None = None,
        start_size: tuple[float, float] | None = None,
        cls: str | None = None,
    ) -> int:
        """区間の両端（打った2点）のあいだを補間して埋める（issue #46）。

        「漏れている」（fixed = add のみ）専用。issue #22 が問題視した
        「複製で展開する」`mark()` の経路とは別に、`spans.interval_add_records()`
        （= `spans.interval_records()`。中身は
        `tools/annotations_to_corrections.py` の `build()`）を呼んで、対象が
        動いた分だけ両端のあいだを線形補間する。

        **「でかすぎる」（toobig）の remove 側はここでは対象にしない。**
        remove は自動検出領域を打ち消す操作なので、区間補間が対象の実際の
        動き（往復・加減速）から外れたフレームでは、実際の検出領域より
        狭い範囲しか残らず、漏れる方向に壊れる（`tests/test_spans.py` の実測:
        150条件中26条件で mean_iou が複製より悪化、9条件で完全に外れる
        フレームが発生）。add はもともと無条件で足すだけなので、この危険が
        構造的に無い。安全な範囲（add のみ）から通す。

        `spans.interval_add_records()` に `self.auto_cover_box` を渡し、
        区間内の各フレームで補間した矩形と既存の自動検出領域の envelope を
        採る（RULES.md 0「大きいほうを採る」）。

        両端の前後関係は問わない。`start_frame` が `frame` より後ろでもよい
        （キーボードで先に置いた点と、後で置いた点のどちらが時間的に先かは
        利用者の操作順に依らない）。
        """
        lo_frame, lo_tap = int(start_frame), start_tap
        lo_size = start_size or self.default_size
        hi_frame, hi_tap = int(frame), tap
        hi_size = size or self.default_size
        if hi_frame < lo_frame:
            lo_frame, hi_frame = hi_frame, lo_frame
            lo_tap, hi_tap = hi_tap, lo_tap
            lo_size, hi_size = hi_size, lo_size
        if not 0 <= lo_frame < self.n_frames or not 0 <= hi_frame < self.n_frames:
            raise ValueError(
                f"フレーム番号が範囲外です: {lo_frame}〜{hi_frame}"
                f"（0〜{self.n_frames - 1}）"
            )

        lo_box = tap_to_box(lo_tap[0], lo_tap[1], lo_size, self.width, self.height)
        hi_box = tap_to_box(hi_tap[0], hi_tap[1], hi_size, self.width, self.height)
        use_cls = cls or self.default_class

        records = spans.interval_add_records(
            lo_frame,
            lo_box,
            hi_frame,
            hi_box,
            use_cls,
            self.width,
            self.height,
            existing_cover=self.auto_cover_box,
        )
        for rec in records:
            self.corrections.items.append(
                Correction(frame=rec.frame, box=rec.box, cls=rec.cls, kind="add")
            )
        added = len(records)
        self._save_corrections()

        # verdict は両端のフレームにだけ付ける。span 判定（mark()）が
        # 「渡された frame」だけに付けているのと同じ扱い。中間フレームは
        # キューに出ていれば別途判定される
        self.history.append(
            {
                "frame": hi_frame,
                "prev": self.verdicts.get(hi_frame),
                "added": added,
                "fp": 0,
                "extra_verdicts": [[lo_frame, self.verdicts.get(lo_frame)]],
            }
        )
        self.verdicts[hi_frame] = "fixed"
        self.verdicts[lo_frame] = "fixed"
        self.save_progress()
        return added

    def _save_corrections(self) -> None:
        """修正一覧をファイルへ書いて、領域を作り直す。

        video/width/height は空のまま保存すると、あとで別の動画の修正と
        取り違えられる。書き出す直前に埋める。
        """
        self.corrections.video = self.corrections.video or os.path.basename(self.video)
        self.corrections.width = self.corrections.width or self.width
        self.corrections.height = self.corrections.height or self.height
        self.corrections.save(self.corrections_path)
        self.recompute()

    def _mark_false_positive(
        self, frame: int, pick: list | None, span: int, cls: str | None
    ) -> tuple[int, int]:
        """「誤検知」の修正を置く。戻り値は (修正の件数, 否定例の件数)。

        モザイクを消す方向の操作なので、あいまいなら通さない。どの領域を
        消すのかを利用者が選んだうえでなければ確定させない。全部消すかどうかは
        画面側で見せるが、サーバでも「選ばれた領域が実在するか」は必ず確かめる。

        span で前後へ広げるときは、選んだ領域と最も重なる領域を各フレームで
        選び直す。番号で対応付けると、対象が増減した瞬間に無関係な領域を
        消してしまう。重なる領域が無いフレームには何も置かない。
        """
        if not pick:
            raise ValueError("消す領域が指定されていません")

        here = self.auto_regions(frame)
        if not here:
            raise ValueError("このフレームには自動で塗った領域がありません")

        # 画面から届くのは丸めた座標なので、実体の矩形に寄せ直してから使う
        seeds: list[tuple] = []
        for p in pick:
            i = best_overlap(tuple(float(v) for v in p[:4]), [b for b, _ in here])
            if i is not None and here[i] not in seeds:
                seeds.append(here[i])
        if not seeds:
            raise ValueError("指定された領域が見つかりません")

        added = 0
        n_fp = 0
        lo = max(0, frame - max(0, int(span)))
        hi = min(self.n_frames - 1, frame + max(0, int(span)))
        for f in range(lo, hi + 1):
            cands = self.auto_regions(f)
            boxes = [b for b, _ in cands]
            taken: list[int] = []
            for seed_box, _ in seeds:
                i = best_overlap(seed_box, boxes)
                if i is not None and i not in taken:
                    taken.append(i)
            for i in taken:
                box, reg = cands[i]
                cover = cover_box([box], self.width, self.height)
                if cover is None:
                    continue
                # クラスは消される側のものを使う。remove の判定はクラスを見ないが、
                # 記録としては「何を消したか」が残っていないと後から追えない
                self.corrections.items.append(
                    Correction(
                        frame=f, box=cover, cls=reg.cls or cls or self.default_class, kind="remove"
                    )
                )
                added += 1
                # 否定例として残すのは検出そのものの矩形。膨張後の矩形で
                # 覚えると、hard negative に使うときに実際より広い「局部でない
                # 場所」を教えることになる
                self.false_positives.append(
                    {
                        "frame": f,
                        "box": [round(float(v), 1) for v in reg.box],
                        "class": reg.cls or self.default_class,
                    }
                )
                n_fp += 1

        if added:
            self._save_corrections()
        return added, n_fp

    def undo(self) -> dict | None:
        """直前の判定を取り消す。誤タップを1手で戻せないと画面が信用されない。

        足した修正は末尾に積んであるので、末尾から件数ぶん落とせば戻せる。
        別画面から修正一覧を丸ごと差し替えられた場合に備えて件数だけ確認する。
        """
        if not self.history:
            return None
        h = self.history.pop()
        n = int(h.get("added", 0))
        if n and len(self.corrections.items) >= n:
            del self.corrections.items[-n:]
            self.corrections.save(self.corrections_path)
            self.recompute()
        # 否定例も同時に巻き戻す。残したままだと、取り消したはずの矩形が
        # 学習データ側にだけ「局部ではない」として残る
        n_fp = int(h.get("fp", 0))
        if n_fp and len(self.false_positives) >= n_fp:
            del self.false_positives[-n_fp:]
        if h.get("prev") is None:
            self.verdicts.pop(h["frame"], None)
        else:
            self.verdicts[h["frame"]] = h["prev"]
        # mark_interval() は両端2フレームぶんの判定を付けるが、history の
        # "frame" キーは1つしか持てない。もう一方（区間の始点）の判定は
        # ここで戻す（無ければ何もしない。古い history には無いキー）
        for f, prev in h.get("extra_verdicts", []):
            if prev is None:
                self.verdicts.pop(f, None)
            else:
                self.verdicts[f] = prev
        self.save_progress()
        return h

    # -- プレビュー画像 --------------------------------------------------
    def frame_image(
        self, n: int, raw: bool = False, max_w: int = 0, fmt: str = "png"
    ) -> tuple[bytes, str] | None:
        """1フレームを画像にする。既定はモザイク済み。

        端末で見るときに原寸 PNG を投げると、1枚に数百 KB〜数 MB かかって
        タップの手応えが消える。max_w で縮めて JPEG に落とせるようにした。
        モザイクは原寸で焼いてから縮める。縮めてから焼くと、実際に焼かれる
        矩形と1〜2px ずれた絵を見せることになる。
        """
        frame = self.reader.read(n)
        if frame is None:
            return None
        if not raw:
            boxes = [b for b, _ in self.regions.get(n, [])]
            frame = mosaic_bgr(frame, boxes, self.block, self.mode)
        h, w = frame.shape[:2]
        if max_w and w > max_w:
            scale = max_w / float(w)
            frame = cv2.resize(
                frame, (max_w, max(1, int(round(h * scale)))), interpolation=cv2.INTER_AREA
            )
        if fmt == "jpg":
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
            return (buf.tobytes(), "image/jpeg") if ok else None
        ok, buf = cv2.imencode(".png", frame)
        return (buf.tobytes(), "image/png") if ok else None


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class ReviewHandler(BaseHTTPRequestHandler):
    session: ReviewSession
    token: str = ""
    verbose = False

    # Range と keep-alive のために 1.1 を名乗る。Content-Length は必ず付ける。
    protocol_version = "HTTP/1.1"
    server_version = "automosaic-review"

    def log_message(self, fmt: str, *args) -> None:
        if self.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- 送信ヘルパ ------------------------------------------------------
    def _send_bytes(
        self, body: bytes, ctype: str, status: int = 200, extra: dict | None = None
    ) -> None:
        extra = extra or {}
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 既定はキャッシュさせない。フレーム画像だけは世代番号付きの URL で
        # 取りに来るので、そちらで個別に上書きする（二重に送らない）
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self._write(body)

    def _send_json(self, obj, status: int = 200, extra: dict | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status, extra)

    def _error(self, status: int, msg: str) -> None:
        self._send_json({"error": msg}, status)

    def _write(self, data: bytes) -> bool:
        """書けたら True。切られていたら False。

        close_connection は Connection: close ヘッダでも立つので、送出中断の
        判定には使えない（使うと1チャンクで止まって本体が欠ける）。
        """
        try:
            self.wfile.write(data)
            return True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # ブラウザはシークのたびに前のレンジ要求を切る。正常系なので黙る
            self.close_connection = True
            return False

    # -- トークン --------------------------------------------------------
    def _authorized(self, q: dict) -> bool:
        """URL・Cookie・ヘッダのどれかにトークンがあれば通す。

        端末では URL 付きのリンクを1回開くだけにしたいので、通ったら
        Cookie に移す。以降の画像取得や API はブラウザが自動で付けてくれる。
        """
        # keep-alive では同じハンドラで何度も呼ばれる。前回の判断を持ち越さない
        self._issue_cookie = False
        given = (q.get("t") or [None])[0]
        if token_matches(self.token, given):
            self._issue_cookie = bool(given)
            return True
        if token_matches(self.token, cookie_token(self.headers.get("Cookie"))):
            return True
        if token_matches(self.token, self.headers.get("X-Review-Token")):
            return True
        return False

    def _cookie_header(self) -> dict:
        if not getattr(self, "_issue_cookie", False) or not self.token:
            return {}
        self._issue_cookie = False
        # HttpOnly は付けない。画面側からトークンを読んで API に付け直すため
        return {"Set-Cookie": f"{COOKIE_NAME}={self.token}; Path=/; SameSite=Lax"}

    # -- 静的ファイル ----------------------------------------------------
    def _serve_static(self, rel: str) -> None:
        rel = rel.lstrip("/")
        path = os.path.normpath(os.path.join(WEB_DIR, rel))
        # LAN に出す以上、web/ の外を読ませる筋は必ず塞ぐ。
        # 文字列の先頭一致では兄弟ディレクトリが通ってしまう。WEB_DIR が
        # automosaic/web のとき automosaic/webapp/ が startswith を満たす。
        if not _inside(WEB_DIR, path) or not os.path.isfile(path):
            self._error(404, "not found")
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        # 明示で no-store を付ける。_send_bytes の既定も no-store だが、ここで
        # 明示しておかないと「静的ファイルにキャッシュ制御が無い」と読める。
        # ブラウザのヒューリスティックキャッシュで旧 timeline.js が残ると、
        # 組を割る旧ロジックが残ったクライアントから素通しを作れてしまう。
        # ETag は付けない: no-store の下では再検証（If-None-Match）自体が
        # 起きないので、ETag を足しても効果が無い（実測で no-store のみが
        # 送られていることを確認済み: curl -I /static/timeline.js）
        extra = dict(self._cookie_header())
        extra["Cache-Control"] = "no-store"
        with open(path, "rb") as f:
            self._send_bytes(f.read(), ctype, extra=extra)

    # -- 動画（Range 対応） ----------------------------------------------
    def _serve_video(self) -> None:
        path = self.session.rendered
        if not path or not os.path.exists(path):
            self._error(404, "モザイク済み動画が指定されていません")
            return
        size = os.path.getsize(path)
        rng = parse_range(self.headers.get("Range"), size)

        if rng is None and self.headers.get("Range"):
            # 範囲が不正・範囲外。416 を返して Content-Range で正解を教える
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        start, end = rng if rng else (0, size - 1)
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            remain = length
            while remain > 0:
                chunk = f.read(min(256 * 1024, remain))
                if not chunk:
                    break
                if not self._write(chunk):
                    return
                remain -= len(chunk)

    # -- フレーム画像 ----------------------------------------------------
    def _serve_frame(self, q: dict) -> None:
        s = self.session
        try:
            n = int((q.get("n") or ["0"])[0])
        except ValueError:
            self._error(400, "n が数値ではありません")
            return
        n = max(0, min(s.n_frames - 1, n))
        raw = (q.get("raw") or ["0"])[0] not in ("0", "", "false")
        try:
            max_w = int((q.get("w") or ["0"])[0])
        except ValueError:
            max_w = 0
        fmt = "jpg" if (q.get("fmt") or ["png"])[0] in ("jpg", "jpeg") else "png"

        # s.lock は取らない。recompute() 中でもフレーム画像は出す
        # （issue #25。ReviewSession.lock のコメントを参照）。デコード自体は
        # FrameReader._lock がすでに直列化している。
        got = s.frame_image(n, raw=raw, max_w=max(0, max_w), fmt=fmt)
        if got is None:
            self._error(404, f"フレーム {n} を読めません")
            return
        body, ctype = got
        # 世代番号付きで取りに来ているなら、先読みが効くようにキャッシュを許す。
        # 修正が入れば version が変わり URL も変わるので、古い絵は残らない。
        # ただし原画は絶対にキャッシュさせない。「サーバを閉じたら見えない」という
        # 前提が崩れ、モザイク前のフレームが端末のディスクに残る。
        # version はモザイク領域の更新を伝える番号なので、原画には意味も無い。
        extra = dict(self._cookie_header())
        if (q.get("v") or [None])[0] and not raw:
            extra["Cache-Control"] = "private, max-age=600"
        self._send_bytes(body, ctype, extra=extra)

    # -- ルーティング ----------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        s = self.session

        if not self._authorized(q):
            # 素材そのものが漏れる経路なので、何があるかも含めて一切答えない
            self._error(403, "アクセストークンが違います")
            return

        if u.path == "/":
            self._serve_static("index.html")
        elif u.path in ("/timeline", "/timeline/"):
            self._serve_static("timeline.html")
        elif u.path.startswith("/static/"):
            self._serve_static(u.path[len("/static/") :])
        elif u.path == "/video":
            self._serve_video()
        elif u.path == "/frame":
            self._serve_frame(q)
        elif u.path == "/api/state":
            light = (q.get("light") or ["0"])[0] not in ("0", "", "false")
            with s.lock:
                self._send_json(s.state_payload(light), extra=self._cookie_header())
        elif u.path == "/api/regions":
            # タイムライン画面が表示中の1コマぶんだけ取りに来る経路（#24）。
            # 修正のたびに動画全体の regions を積む代わりに、画面はここで
            # 1コマぶんだけ取り直す
            try:
                n = int((q.get("n") or ["0"])[0])
            except ValueError:
                self._error(400, "n が数値ではありません")
                return
            with s.lock:
                self._send_json(s.frame_regions_payload(n), extra=self._cookie_header())
        elif u.path == "/api/queue":
            rebuild = (q.get("rebuild") or ["0"])[0] not in ("0", "", "false")
            with s.lock:
                if "step" in q:
                    try:
                        s.queue_step = max(1, int(q["step"][0]))
                        rebuild = True
                    except ValueError:
                        pass
                if "all" in q:
                    s.queue_all = q["all"][0] not in ("0", "", "false")
                    rebuild = True
                if "max_per_range" in q:
                    raw = q["max_per_range"][0]
                    try:
                        s.queue_max_per_range = None if raw in ("0", "", "none") else max(1, int(raw))
                        rebuild = True
                    except ValueError:
                        pass
                if rebuild or not s.queue:
                    s.rebuild_queue()
                self._send_json(s.queue_payload(), extra=self._cookie_header())
        elif u.path == "/api/corrections":
            with s.lock:
                self._send_json(
                    {
                        "video": s.corrections.video,
                        "width": s.corrections.width,
                        "height": s.corrections.height,
                        "corrections": [c.as_dict() for c in s.corrections.items],
                    },
                    extra=self._cookie_header(),
                )
        else:
            self._error(404, "not found")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        body = self.rfile.read(n) if n > 0 else b"{}"
        return json.loads(body.decode("utf-8"))

    def do_POST(self) -> None:  # noqa: N802
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        if not self._authorized(q):
            self._error(403, "アクセストークンが違います")
            return

        s = self.session
        try:
            data = self._read_json()
        except Exception as e:  # noqa: BLE001
            self._error(400, f"JSON を読めません: {e}")
            return

        if u.path == "/api/corrections":
            # キーが無い本文を「空の一覧で置き換えろ」と読まない。読むと、
            # 壊れた本文ひとつで手修正が全部消える。判定（.progress.json）は
            # 残るので、「塞いだ」と表示されたまま塞いだ矩形だけが無い状態になる
            items = data.get("corrections")
            if not isinstance(items, list):
                self._error(400, "corrections（配列）が本文にありません")
                return
            # frame は任意（#24）。画面が「いま表示しているコマ」を教えてくれた
            # ときだけ、そのコマぶんの regions に絞って返す。省略時は従来どおり
            # 全フレームぶん（後方互換）
            frame_arg: int | None = None
            if "frame" in data:
                try:
                    frame_arg = int(data["frame"])
                except (TypeError, ValueError):
                    frame_arg = None
            try:
                with s.lock:
                    s.set_corrections(items)
                    # set_corrections は履歴を捨てるが、捨てたことを保存しないと
                    # セッションを開き直した瞬間に .progress.json から古い履歴が
                    # 生き返り、次の undo が無関係な修正を末尾から削る（W-1）
                    s.save_progress()
                    payload = s.update_payload(frame_arg)
            except Exception as e:  # noqa: BLE001
                self._error(400, f"修正を適用できません: {e}")
                return
            self._send_json(payload, extra=self._cookie_header())

        elif u.path == "/api/mark":
            try:
                frame = int(data["frame"])
                verdict = str(data["verdict"])
                tap = None
                if "x" in data and "y" in data:
                    tap = (float(data["x"]), float(data["y"]))
                size = None
                if data.get("w") and data.get("h"):
                    size = (float(data["w"]), float(data["h"]))
                # 「誤検知」で消す自動領域。画面が見て選んだ矩形をそのまま送る。
                # 番号ではなく座標で送らせるのは、送っている間にキューが
                # 組み直されても指すものが変わらないようにするため
                pick = None
                if data.get("pick"):
                    pick = [[float(v) for v in b[:4]] for b in data["pick"]]
                with s.lock:
                    added = s.mark(
                        frame,
                        verdict,
                        tap=tap,
                        size=size,
                        span=int(data.get("span", 0)),
                        cls=data.get("class"),
                        pick=pick,
                    )
                    payload = {
                        "ok": True,
                        "frame": frame,
                        "verdict": verdict,
                        "added": added,
                        "version": s.version,
                        "n_corrections": len(s.corrections.items),
                        "regions": s.frame_regions(frame),
                        "progress": s.progress_payload(),
                    }
            except (KeyError, TypeError, ValueError) as e:
                self._error(400, f"判定を記録できません: {e}")
                return
            self._send_json(payload, extra=self._cookie_header())

        elif u.path == "/api/undo":
            with s.lock:
                h = s.undo()
                if h is None:
                    self._send_json({"ok": False, "error": "戻せる操作がありません"}, 200)
                    return
                payload = {
                    "ok": True,
                    "frame": h["frame"],
                    "removed": h.get("added", 0),
                    "version": s.version,
                    "n_corrections": len(s.corrections.items),
                    "regions": s.frame_regions(h["frame"]),
                    "progress": s.progress_payload(),
                }
            self._send_json(payload, extra=self._cookie_header())

        else:
            self._error(404, "not found")


class ReviewServer(ThreadingHTTPServer):
    """待ち受けサーバ。ポートの重複を握りつぶさない。

    Windows の SO_REUSEADDR は POSIX と意味が違い、既に誰かが待ち受けている
    ポートにもう一度 bind できてしまう。既定のままだと、前回の
    レビューサーバが生きたまま新しい方を起動したとき、両方が同じポートを
    名乗り、ブラウザは古い方に繋がる。「直したはずの画面が出ない」という
    追いにくい症状になるので、Windows では重複を素直に失敗させる。
    """

    daemon_threads = True
    allow_reuse_address = os.name != "nt"


# --------------------------------------------------------------------------
# 学習データ書き出し
# --------------------------------------------------------------------------


def to_yolo(box, width: int, height: int) -> tuple[float, float, float, float]:
    """(x, y, w, h) ピクセル -> YOLO の (cx, cy, w, h) 正規化。

    はみ出した矩形はクリップしてから中心を取る。クリップせずに中心だけ
    正規化すると、画面外にはみ出した分だけ中心がずれた教師になる。
    """
    x, y, w, h = box
    x0 = max(0.0, min(float(width), x))
    y0 = max(0.0, min(float(height), y))
    x1 = max(0.0, min(float(width), x + w))
    y1 = max(0.0, min(float(height), y + h))
    cx = (x0 + x1) / 2 / width
    cy = (y0 + y1) / 2 / height
    bw = (x1 - x0) / width
    bh = (y1 - y0) / height
    return cx, cy, bw, bh


def export_dataset(session: ReviewSession, out_dir: str, quiet: bool = False) -> int:
    """手修正した箇所を YOLO 形式の学習データとして書き出す。

    書き出すのは手修正のあったフレームだけ。全フレーム出すと、検出できて
    いる大多数の絵ばかりが集まって「取りこぼす絵」が薄まる。

    「誤検知」と判定したフレームも書き出す。そこは局部ではないと人が言った
    絵なので、ラベルからは外したうえで画像だけ残す。ラベルが空のまま残った
    フレームは、そのフレーム全体が負例になる。誤検知を減らす学習にはこれが
    いちばん直接効くので、捨てずに出す。
    """
    names = sorted(session.classes)
    cls_id = {c: i for i, c in enumerate(names)}
    img_dir = os.path.join(out_dir, "images")
    lbl_dir = os.path.join(out_dir, "labels")
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(lbl_dir, exist_ok=True)

    by_frame = session.corrections.by_frame()
    fp_by_frame: dict[int, list[dict]] = {}
    for p in session.false_positives:
        fp_by_frame.setdefault(int(p["frame"]), []).append(p)

    frames = sorted(
        {f for f, cs in by_frame.items() if any(c.kind == "add" for c in cs)}
        | set(fp_by_frame)
    )

    written = 0
    has_image: set[int] = set()
    for f in frames:
        frame = session.reader.read(f)
        if frame is None:
            if not quiet:
                print(f"  フレーム {f} を読めないので飛ばします", file=sys.stderr)
            continue

        boxes: list[tuple[str, tuple[float, float, float, float]]] = []
        for c in by_frame.get(f, []):
            if c.kind == "add" and c.cls in cls_id:
                boxes.append((c.cls, c.box))

        fp_boxes = [tuple(float(v) for v in p["box"]) for p in fp_by_frame.get(f, [])]

        # 同じフレームの自動検出も一緒に入れる。手修正だけを教師にすると
        # 「他には何も写っていない」という誤った負例を教えることになる。
        # ただし補間・memory・橋渡し由来は実観測ではないので入れない。
        for _, r in session.regions.get(f, []):
            if r.source != "detected" or r.cls not in cls_id:
                continue
            if any(_iou(r.box, b) > 0.5 for _, b in boxes):
                continue  # 手修正と同じ対象。二重ラベルを避ける
            if any(_iou(r.box, b) > 0.5 for b in fp_boxes):
                # 誤検知と判定された矩形。remove が効いていればここには
                # 残らないが、修正ファイルを差し替えたり検出をやり直したりすると
                # 復活しうる。「これは局部だ」と教えてしまうと元も子もないので、
                # ラベルを組む場所でも必ず落とす
                continue
            boxes.append((r.cls, r.box))

        if not boxes and f not in fp_by_frame:
            continue

        cv2.imwrite(os.path.join(img_dir, f"{f:06d}.png"), frame)
        has_image.add(f)
        lines = []
        for cls, box in boxes:
            cx, cy, bw, bh = to_yolo(box, session.width, session.height)
            if bw <= 0 or bh <= 0:
                continue
            lines.append(f"{cls_id[cls]} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
        with open(os.path.join(lbl_dir, f"{f:06d}.txt"), "w", encoding="utf-8") as fh:
            # 空のラベルは空ファイルにする。改行だけの行を置くと、読み込み側で
            # 壊れた注釈として弾かれ、せっかくの負例が使われない
            fh.write("".join(line + "\n" for line in lines))
        written += 1

    # 否定の例。ラベルには入れられないが、hard negative として使えるように
    # 「どの絵のどこが局部ではなかったか」を別ファイルに残す
    fp_items = []
    for f in sorted(fp_by_frame):
        img = f"images/{f:06d}.png" if f in has_image else None
        for p in fp_by_frame[f]:
            fp_items.append(
                {
                    "frame": f,
                    "box": [round(float(v), 1) for v in p["box"]],
                    "class": p.get("class", ""),
                    "note": f"誤検知（{img}）" if img else "誤検知（画像なし）",
                }
            )
    with open(os.path.join(out_dir, "false_positives.json"), "w", encoding="utf-8") as fh:
        json.dump(fp_items, fh, ensure_ascii=False, indent=1)

    with open(os.path.join(out_dir, "classes.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(names) + "\n")

    # ultralytics 形式。train/val は分けずに同じディレクトリを指す
    # （枚数が少ないうちに分割すると評価が偏る。分割は学習側で決める）
    with open(os.path.join(out_dir, "dataset.yaml"), "w", encoding="utf-8") as fh:
        fh.write(f"path: {os.path.abspath(out_dir)}\n")
        fh.write("train: images\n")
        fh.write("val: images\n")
        fh.write(f"nc: {len(names)}\n")
        fh.write("names:\n")
        for i, n in enumerate(names):
            fh.write(f"  {i}: {n}\n")

    if not quiet:
        print(f"学習データを書き出しました: {out_dir}（{written} フレーム）")
        if fp_items:
            print(f"  誤検知 {len(fp_items)} 件を false_positives.json に出しました")
    return written


# --------------------------------------------------------------------------
# 起動
# --------------------------------------------------------------------------


def probe_with_cv2(path: str) -> tuple[int, int, float, int]:
    """cv2 だけで解像度・fps・フレーム数を取る。

    video.probe() は ffprobe を要求するが、レビューは検出済み JSON を
    見るだけなので ffmpeg が無い環境でも開けるようにしておく。
    """
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError(f"動画を開けません: {path}")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return w, h, float(fps), n


def parse_size(text: str | None, fallback: tuple[int, int]) -> tuple[int, int]:
    if not text:
        return fallback
    t = text.lower().replace("×", "x")
    if "x" in t:
        a, b = t.split("x", 1)
        return max(2, int(a)), max(2, int(b))
    v = max(2, int(t))
    return v, v


def _cli_defaults() -> dict[str, object]:
    """cli.py の argparse 既定値を、フラグ名をキーにして返す。

    値をここに書き写すと、cli.py 側の既定が変わったときに追随できず、レビュー画面が
    焼き込みと違う設定で領域を計算する（#14）。cli.py の build_parser() を直接呼んで
    値を借りることで、既定値の唯一の正を cli.py 側に保つ。

    リスク（PR #34 の検証が指摘）: この関数自体は辞書を作るだけだが、呼び出し側
    （build_parser() の cd["--block"] のようなフラグ名引き）は .get() ではなく [] を
    使っている。cli.py 側でそのオプションが改名・削除されると、この辞書に該当キーが
    無くなり、呼び出し側で KeyError が飛ぶ。Web アプリは起動時に review.build_parser()
    を呼ぶため、cli.py 側の改名・削除と review.py 側の追随が同一コミットでないと
    Web アプリごと起動不能になる。#56 で --block / --mode をこの仕組みに乗せたことで、
    この KeyError リスクを持つフラグは --classes と合わせて3つに増えた
    （リスクの種類自体は元からある。増えたのは対象フラグの数）。
    """
    from . import cli as _cli

    out: dict[str, object] = {}
    for action in _cli.build_parser()._actions:
        for opt in action.option_strings:
            if opt.startswith("--"):
                out[opt] = action.default
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m automosaic.review",
        description="人手レビュー UI。見るべきフレームを1枚ずつ出して判定する",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("input", nargs="?", help="元動画")
    p.add_argument("--rendered", help="モザイク済み動画（再生用）")
    p.add_argument("--detections", help="検出結果 JSON")
    p.add_argument(
        "--corrections",
        default="corrections.json",
        help="手修正の保存先。既存があれば読み込む",
    )
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="待ち受けアドレス。端末から見るなら 0.0.0.0",
    )
    p.add_argument("--port", type=int, default=8765, help="待ち受けポート")
    p.add_argument("--token", help="アクセストークンを固定する（既定は毎回ランダム）")
    p.add_argument(
        "--no-token",
        action="store_true",
        help="トークン検証を切る。127.0.0.1 でのみ使うこと",
    )
    p.add_argument("--no-qr", action="store_true", help="QR コードを出さない")
    p.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    p.add_argument("--verbose", action="store_true", help="アクセスログを出す")
    p.add_argument(
        "--queue-step", type=int, default=5, help="検査キューに載せるフレーム間隔"
    )
    p.add_argument(
        "--queue-all",
        action="store_true",
        help="問題のある区間だけでなく、全フレームを間隔ごとにキューへ載せる"
        "（estimated / area_jump / low_conf も既定のキューに含める）",
    )
    p.add_argument(
        "--queue-max-per-range",
        type=int,
        default=DEFAULT_MAX_PER_RANGE,
        help="1区間あたりキューに積む代表フレーム数の上限（既定 %(default)s。"
        "0 で無制限＝従来どおり step 刻みで全部積む）",
    )
    p.add_argument(
        "--default-size",
        help="手で置く矩形の既定サイズ。'80' か '80x60'（既定: 検出サイズの中央値）",
    )
    p.add_argument(
        "--export-dataset",
        help="UI を起動せず、手修正を YOLO 形式の学習データとして書き出す",
    )

    # 帯とプレビューは焼き込みと同じ領域計算でなければ意味がないので、
    # cli.py の時間方向オプションと同じ既定値を持たせる。既定値は cli.py の
    # build_parser() から借りる（_cli_defaults）。数値をここに書き写さないのは、
    # 過去に書き写した2つ（margin-scale/margin-cap）が実際に食い違って、レビュー画面
    # が焼き込みより広く塗って見せていたため（#14: 実測で5,950フレームぶん）。
    cd = _cli_defaults()

    # --block / --mode も焼き込みの見た目に直結するので同じ理由でここに数値を
    # 書き写さない（issue #33 完了条件）。以前は 0 / "pixelize" を直書きしており、
    # cli.py 側の既定が変わっても追随しない構造だった（値は今のところ食い違って
    # いなかったが、margin-scale/margin-cap と同じ形の潜在的リスク）。
    p.add_argument(
        "--block", type=int, default=cd["--block"], help="モザイクのブロックサイズ px（0で自動）"
    )
    p.add_argument("--mode", default=cd["--mode"], choices=["pixelize", "black"])
    t = p.add_argument_group("時間方向（cli と同じ既定値）")
    t.add_argument("--classes", default=cd["--classes"])
    t.add_argument("--max-gap", type=int, default=cd["--max-gap"])
    t.add_argument("--memory", type=int, default=cd["--memory"])
    t.add_argument("--memory-before", type=int, default=cd["--memory-before"])
    t.add_argument("--stitch-gap", type=int, default=cd["--stitch-gap"])
    t.add_argument("--stitch-dist", type=float, default=cd["--stitch-dist"])
    t.add_argument("--margin-scale", type=float, default=cd["--margin-scale"])
    t.add_argument("--margin-cap", type=float, default=cd["--margin-cap"])
    t.add_argument("--motion-weight", type=float, default=cd["--motion-weight"])
    t.add_argument("--motion-cap", type=float, default=cd["--motion-cap"])
    t.add_argument("--hold-growth", type=float, default=cd["--hold-growth"])
    t.add_argument("--estimated-factor", type=float, default=cd["--estimated-factor"])
    t.add_argument("--max-area-ratio", type=float, default=cd["--max-area-ratio"])
    t.add_argument("--track-min-peak", type=float, default=cd["--track-min-peak"])
    t.add_argument("--bridge-max", type=int, default=cd["--bridge-max"])
    t.add_argument("--no-bridge", action="store_true", default=cd["--no-bridge"])
    t.add_argument("--despike", action="store_true", default=cd["--despike"])
    t.add_argument("--no-despike", action="store_true", default=cd["--no-despike"])
    t.add_argument("--frame-step", type=int, default=cd["--frame-step"])
    t.add_argument(
        "--estimate-gaps",
        action="store_true",
        default=cd["--estimate-gaps"],
        help="cli.py と同じ意味。検出が途切れた区間を推定で埋める"
        "（memory・橋渡し・不確かさ膨張が有効になる）。既定は無効で、cli.py が"
        "既定ジョブで絞り込む memory / memory-before / bridge-max / hold-growth /"
        " motion-weight を、レビューも同じ式で絞り込む",
    )
    return p


def explicit_options(argv: list[str] | None) -> set[str]:
    """レビューのコマンドラインで実際に指定されたオプション名を返す。

    cli.py の同名関数と同じ手口。既定値と同じ値を明示指定することもあるので、
    値の比較では判別できない。既定値を全部 None にしたパーサでもう一度読み、
    埋まったものだけを拾う。
    """
    p = build_parser()
    for action in p._actions:
        action.default = None
    try:
        parsed = p.parse_args(argv)
    except SystemExit:
        return set()
    return {k for k, v in vars(parsed).items() if v is not None}


def session_from_args(args, argv: list[str] | None = None) -> ReviewSession:
    """引数から ReviewSession を組む。

    `argv` は `--estimate-gaps` が明示指定されたかどうかの判別にだけ使う
    （cli.py と同じ絞り込みを、明示指定を上書きせずに行うため）。省略した場合は
    「何も明示指定されていない」として扱う。
    """
    src = args.input
    if not src or not os.path.exists(src):
        raise SystemExit(f"元動画が見つかりません: {src}")

    # cli.py と同じ矛盾検査。--despike は既定オフの明示的な opt-in、--no-despike は
    # 後方互換のためだけに残っている（cli.py の意味と同じ）。黙ってどちらかに倒さない
    if args.despike and args.no_despike:
        raise SystemExit(
            "--despike と --no-despike は同時に指定できません（矛盾する指定を黙って"
            "どちらかに倒さない）"
        )

    width, height, fps, n_frames = probe_with_cv2(src)

    per_frame: dict[int, list[Detection]] = {}
    if args.detections:
        if not os.path.exists(args.detections):
            raise SystemExit(f"検出結果が見つかりません: {args.detections}")
        with open(args.detections, encoding="utf-8") as f:
            data = json.load(f)
        n_frames = int(data.get("n_frames") or n_frames)
        # 検出座標は検出時の解像度基準。ずれていたらレビューの意味がないので止める
        dw, dh = int(data.get("width", width)), int(data.get("height", height))
        if (dw, dh) != (width, height):
            raise SystemExit(
                f"検出結果の解像度 {dw}x{dh} が元動画 {width}x{height} と違います"
            )
        per_frame = {
            int(k): [Detection.from_dict(d) for d in v]
            for k, v in data["detections"].items()
        }
    per_frame = {f: per_frame.get(f, []) for f in range(n_frames)}

    # --estimate-gaps 無しが既定で、これは「実際に検出できた箇所だけ」を塗る方針
    # （塗り過ぎを避ける）。この絞り込みが無いと、レビューは常に memory/橋渡し/
    # 不確かさ膨張ありで領域を計算し、焼き込みでは塗られない橋渡し・memory 領域を
    # 「塗られている」と見せてしまう（#14: 実測で bench3 素材 9,344 フレームぶん。
    # issue #14 記載の実素材では 5,950フレーム）。絞り込みの式そのものは
    # temporal.narrow_without_estimate_gaps() に一本化してある（cli.py の main()
    # と共有。issue #33。以前は同じ式が2箇所に別々に書かれていた）。
    if not args.estimate_gaps:
        given = explicit_options(argv)
        narrow_without_estimate_gaps(args, given)

    classes = resolve_classes(args.classes)
    cfg = TemporalConfig(
        max_gap=args.max_gap,
        memory=args.memory,
        margin_scale=args.margin_scale,
        max_area_ratio=args.max_area_ratio,
        min_track_len=2 if args.despike else 0,
        bridge_max=0 if args.no_bridge else args.bridge_max,
        frame_step=max(1, args.frame_step),
        track_min_peak=args.track_min_peak,
        memory_before=args.memory_before,
        stitch_max_gap=args.stitch_gap,
        stitch_dist_ratio=args.stitch_dist,
        margin_cap_px=args.margin_cap,
        motion_weight=args.motion_weight,
        hold_growth=args.hold_growth,
        motion_cap=args.motion_cap,
        estimated_factor=args.estimated_factor,
    )

    corrections = CorrectionSet.load(args.corrections)
    corrections.video = corrections.video or os.path.basename(src)
    corrections.width = corrections.width or width
    corrections.height = corrections.height or height

    fallback = max(16, int(max(width, height) * 0.12))
    default_size = parse_size(
        args.default_size, median_box_size(per_frame, classes, fallback)
    )

    return ReviewSession(
        video=src,
        rendered=args.rendered,
        corrections_path=args.corrections,
        width=width,
        height=height,
        fps=fps,
        n_frames=n_frames,
        classes=classes,
        cfg=cfg,
        per_frame=per_frame,
        corrections=corrections,
        block=args.block or default_block_size(max(width, height)),
        default_size=default_size,
        default_class=dominant_class(per_frame, classes),
        mode=args.mode,
        queue_step=max(1, args.queue_step),
        queue_all=args.queue_all,
        queue_max_per_range=None if args.queue_max_per_range in (0, None) else max(1, args.queue_max_per_range),
    )


def print_banner(session: ReviewSession, host: str, port: int, token: str, no_qr: bool) -> str:
    """起動時の案内。端末から開くための URL をここで全部出す。

    戻り値はブラウザに渡す URL（loopback 側）。
    """
    q = f"?t={token}" if token else ""
    local_url = f"http://127.0.0.1:{port}/{q}"

    est = session.coverage.count(COV_ESTIMATED)
    unc = session.coverage.count(COV_NONE)
    print(f"元動画      {session.video}")
    print(
        f"            {session.width}x{session.height}  {session.fps:.3f} fps  "
        f"{session.n_frames} フレーム"
    )
    print(f"再生用      {session.rendered or '（未指定。コマ送りのみ）'}")
    print(f"手修正      {session.corrections_path}（{len(session.corrections.items)} 件）")
    print(f"既定の矩形  {session.default_size[0]}x{session.default_size[1]} px")
    print(
        f"推定のみ    {est} フレーム "
        f"({100.0 * est / max(1, session.n_frames):.1f}%) / 未処理 {unc} フレーム"
    )
    prog = session.progress_payload()
    print(
        f"検査キュー  {prog['total']} 枚（{session.queue_step} フレームおき"
        f"{'・全フレーム対象' if session.queue_all else ''}）"
        f" / 判定済み {prog['done']} 枚"
    )

    exposed = host not in ("127.0.0.1", "localhost", "::1")
    if exposed:
        print("\n注意: LAN に公開しています。同じネットワークの端末から見えます")
        if not token:
            print("注意: トークン検証が切れています。誰でも開けます")

    print("\n手元の PC:")
    print(f"  {local_url}")
    urls = [local_url]
    if exposed:
        addrs = lan_addresses()
        if addrs:
            print("\n同じ LAN の端末:")
            for a in addrs:
                u = f"http://{a}:{port}/{q}"
                print(f"  {u}")
                urls.append(u)
        else:
            print("\nLAN の IP アドレスを取得できませんでした")

    # QR は端末で開く URL に対して出す。手元 PC 用の 127.0.0.1 では意味がない
    qr_url = urls[1] if len(urls) > 1 else local_url
    if not no_qr:
        print(f"\n{qr_url}")
        if not print_qr(qr_url):
            print("（QR を出せませんでした。上の URL を開いてください）")

    print("\nCtrl-C で終了")
    return local_url


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session = session_from_args(args, argv)

    if args.export_dataset:
        export_dataset(session, args.export_dataset)
        session.reader.close()
        return 0

    token = "" if args.no_token else (args.token or make_token())

    ReviewHandler.session = session
    ReviewHandler.token = token
    ReviewHandler.verbose = args.verbose

    try:
        httpd = ReviewServer((args.host, args.port), ReviewHandler)
    except OSError as e:
        print(f"{args.host}:{args.port} を開けません: {e}", file=sys.stderr)
        print("（前回のレビューサーバが残っていないか確かめてください）", file=sys.stderr)
        return 1

    port = httpd.server_address[1]
    url = print_banner(session, args.host, port, token, args.no_qr)
    # ログにリダイレクトすると serve_forever 中は押し出されないので、ここで流す
    sys.stdout.flush()

    if not args.no_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します")
    finally:
        httpd.server_close()
        session.reader.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
