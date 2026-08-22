"""人手レビュー UI。ローカルに HTTP サーバを立てて、ブラウザから修正する。

設計目標は「全フレームを目視しない」こと。実素材で全体の 28% が
「推定のみで覆っている区間」（実観測が1つも無く、補間と memory だけで
塗っている）だと分かっている。そこだけを提示して、そこだけ直す。

外部依存を増やさないために標準ライブラリの http.server を使う。単一利用者の
ローカルツールなので性能要件は無く、素の ThreadingHTTPServer で足りる。
ただし動画配信中に API が止まると操作不能になるのでスレッド化は必須。

領域の計算は temporal.process() をそのまま呼ぶ。ここで独自に領域を作ると
「レビュー画面で見た絵」と「実際に焼かれる絵」がずれ、レビューの意味が消える。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import cv2
import numpy as np

from .corrections import Correction, CorrectionSet
from .detector import CONSERVATIVE_CLASSES, DEFAULT_CLASSES, Detection
from .render import apply_regions, default_block_size
from .temporal import TemporalConfig, process

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

    reader: FrameReader = field(init=False)
    lock: threading.Lock = field(init=False)
    regions: dict = field(init=False, default_factory=dict)
    stats: dict = field(init=False, default_factory=dict)
    coverage: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self.reader = FrameReader(self.video)
        self.lock = threading.Lock()
        self.recompute()

    # -- 領域の再計算 ----------------------------------------------------
    def recompute(self) -> None:
        """検出 + 手修正から最終領域を作り直す。

        手修正のたびに全フレーム回すのは一見無駄だが、process() は座標だけの
        処理なので数千フレームでも一瞬で終わる。差分更新を書くと
        「焼き込みと同じ計算」という保証が崩れるので、素直に全部やり直す。
        """
        from .corrections import apply as apply_corrections

        regions, stats = process(
            self.per_frame, self.n_frames, self.width, self.height, self.classes, self.cfg
        )
        stats.pop("_left_open", None)
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

    # -- 送出用の整形 ----------------------------------------------------
    def regions_payload(self) -> dict:
        """フレーム -> 矩形リスト。矩形は [x, y, w, h, 由来, スコア]。

        描画に使うのは膨張後の矩形。実際にモザイクが乗る範囲そのものを
        見せないと「隠れているつもりで隠れていない」を見落とす。
        """
        out: dict[str, list] = {}
        for f in range(self.n_frames):
            regs = self.regions.get(f, [])
            if not regs:
                continue
            out[str(f)] = [
                [
                    int(round(b[0])),
                    int(round(b[1])),
                    int(round(b[2])),
                    int(round(b[3])),
                    SOURCE_CODE.get(r.source, "?"),
                    round(r.score, 3),
                ]
                for b, r in regs
            ]
        return out

    def ranges_payload(self) -> dict:
        est = runs_of(self.coverage, COV_ESTIMATED, min_len=5)
        unc = runs_of(self.coverage, COV_NONE, min_len=1)
        return {
            "estimated_only_ranges": [
                {"start": s, "end": e, "frames": e - s + 1} for s, e in est
            ],
            "uncovered_ranges": [
                {"start": s, "end": e, "frames": e - s + 1} for s, e in unc
            ],
        }

    def state_payload(self) -> dict:
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
            "coverage": self.coverage,
            "regions": self.regions_payload(),
            "stats": self.stats,
            "n_corrections": len(self.corrections.items),
        }
        d.update(self.ranges_payload())
        return d

    def update_payload(self) -> dict:
        """修正を保存したあとに返す差分。state 全体より軽い。"""
        d = {
            "ok": True,
            "coverage": self.coverage,
            "regions": self.regions_payload(),
            "n_corrections": len(self.corrections.items),
        }
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
        self.recompute()

    # -- プレビュー画像 --------------------------------------------------
    def frame_png(self, n: int, raw: bool) -> bytes | None:
        frame = self.reader.read(n)
        if frame is None:
            return None
        if not raw:
            boxes = [b for b, _ in self.regions.get(n, [])]
            frame = mosaic_bgr(frame, boxes, self.block, self.mode)
        ok, buf = cv2.imencode(".png", frame)
        return buf.tobytes() if ok else None


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class ReviewHandler(BaseHTTPRequestHandler):
    session: ReviewSession
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
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 手修正のたびに絵が変わるので、フレーム画像も API もキャッシュさせない
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self._write(body)

    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status)

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

    # -- 静的ファイル ----------------------------------------------------
    def _serve_static(self, rel: str) -> None:
        rel = rel.lstrip("/")
        path = os.path.normpath(os.path.join(WEB_DIR, rel))
        # 127.0.0.1 限定とはいえ、web/ の外を読ませる筋は塞いでおく
        if not path.startswith(WEB_DIR) or not os.path.isfile(path):
            self._error(404, "not found")
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        with open(path, "rb") as f:
            self._send_bytes(f.read(), ctype)

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

    # -- ルーティング ----------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        s = self.session

        if u.path == "/":
            self._serve_static("index.html")
        elif u.path.startswith("/static/"):
            self._serve_static(u.path[len("/static/") :])
        elif u.path == "/video":
            self._serve_video()
        elif u.path == "/frame":
            try:
                n = int(q.get("n", ["0"])[0])
            except ValueError:
                self._error(400, "n が数値ではありません")
                return
            n = max(0, min(s.n_frames - 1, n))
            raw = q.get("raw", ["0"])[0] not in ("0", "", "false")
            png = s.frame_png(n, raw)
            if png is None:
                self._error(404, f"フレーム {n} を読めません")
                return
            self._send_bytes(png, "image/png")
        elif u.path == "/api/state":
            with s.lock:
                self._send_json(s.state_payload())
        elif u.path == "/api/corrections":
            with s.lock:
                self._send_json(
                    {
                        "video": s.corrections.video,
                        "width": s.corrections.width,
                        "height": s.corrections.height,
                        "corrections": [c.as_dict() for c in s.corrections.items],
                    }
                )
        else:
            self._error(404, "not found")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        u = urlsplit(self.path)
        if u.path != "/api/corrections":
            self._error(404, "not found")
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        body = self.rfile.read(n) if n > 0 else b"{}"
        try:
            data = json.loads(body.decode("utf-8"))
            items = data.get("corrections", [])
        except Exception as e:  # noqa: BLE001
            self._error(400, f"JSON を読めません: {e}")
            return
        s = self.session
        try:
            with s.lock:
                s.set_corrections(items)
                payload = s.update_payload()
        except Exception as e:  # noqa: BLE001
            self._error(400, f"修正を適用できません: {e}")
            return
        self._send_json(payload)


# --------------------------------------------------------------------------
# 学習データ書き出し
# --------------------------------------------------------------------------


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
    """
    names = sorted(session.classes)
    cls_id = {c: i for i, c in enumerate(names)}
    img_dir = os.path.join(out_dir, "images")
    lbl_dir = os.path.join(out_dir, "labels")
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(lbl_dir, exist_ok=True)

    by_frame = session.corrections.by_frame()
    frames = sorted(f for f, cs in by_frame.items() if any(c.kind == "add" for c in cs))

    written = 0
    for f in frames:
        frame = session.reader.read(f)
        if frame is None:
            if not quiet:
                print(f"  フレーム {f} を読めないので飛ばします", file=sys.stderr)
            continue

        boxes: list[tuple[str, tuple[float, float, float, float]]] = []
        for c in by_frame[f]:
            if c.kind == "add" and c.cls in cls_id:
                boxes.append((c.cls, c.box))

        # 同じフレームの自動検出も一緒に入れる。手修正だけを教師にすると
        # 「他には何も写っていない」という誤った負例を教えることになる。
        # ただし補間・memory・橋渡し由来は実観測ではないので入れない。
        for _, r in session.regions.get(f, []):
            if r.source != "detected" or r.cls not in cls_id:
                continue
            if any(_iou(r.box, b) > 0.5 for _, b in boxes):
                continue  # 手修正と同じ対象。二重ラベルを避ける
            boxes.append((r.cls, r.box))

        if not boxes:
            continue

        cv2.imwrite(os.path.join(img_dir, f"{f:06d}.png"), frame)
        lines = []
        for cls, box in boxes:
            cx, cy, bw, bh = to_yolo(box, session.width, session.height)
            if bw <= 0 or bh <= 0:
                continue
            lines.append(f"{cls_id[cls]} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
        with open(os.path.join(lbl_dir, f"{f:06d}.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        written += 1

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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m automosaic.review",
        description="人手レビュー UI。推定のみの区間だけを見て直す",
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
    p.add_argument("--host", default="127.0.0.1", help="待ち受けアドレス")
    p.add_argument("--port", type=int, default=8765, help="待ち受けポート")
    p.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    p.add_argument("--verbose", action="store_true", help="アクセスログを出す")
    p.add_argument(
        "--default-size",
        help="手で置く矩形の既定サイズ。'80' か '80x60'（既定: 検出サイズの中央値）",
    )
    p.add_argument("--block", type=int, default=0, help="モザイクのブロックサイズ px（0で自動）")
    p.add_argument("--mode", default="pixelize", choices=["pixelize", "black"])
    p.add_argument(
        "--export-dataset",
        help="UI を起動せず、手修正を YOLO 形式の学習データとして書き出す",
    )

    # 帯とプレビューは焼き込みと同じ領域計算でなければ意味がないので、
    # cli.py の時間方向オプションと同じ既定値を持たせる。
    t = p.add_argument_group("時間方向（cli と同じ既定値）")
    t.add_argument("--classes", default="default")
    t.add_argument("--max-gap", type=int, default=12)
    t.add_argument("--memory", type=int, default=6)
    t.add_argument("--memory-before", type=int, default=0)
    t.add_argument("--stitch-gap", type=int, default=90)
    t.add_argument("--stitch-dist", type=float, default=0.12)
    t.add_argument("--margin-scale", type=float, default=1.0)
    t.add_argument("--margin-cap", type=float, default=0.0)
    t.add_argument("--motion-weight", type=float, default=2.0)
    t.add_argument("--motion-cap", type=float, default=60.0)
    t.add_argument("--hold-growth", type=float, default=0.5)
    t.add_argument("--estimated-factor", type=float, default=1.3)
    t.add_argument("--max-area-ratio", type=float, default=0.35)
    t.add_argument("--track-min-peak", type=float, default=0.0)
    t.add_argument("--bridge-max", type=int, default=150)
    t.add_argument("--no-bridge", action="store_true")
    t.add_argument("--no-despike", action="store_true")
    t.add_argument("--frame-step", type=int, default=1)
    return p


def resolve_classes(spec: str) -> set[str]:
    if spec == "default":
        return set(DEFAULT_CLASSES)
    if spec == "conservative":
        return set(CONSERVATIVE_CLASSES)
    return {c.strip() for c in spec.split(",") if c.strip()}


def session_from_args(args) -> ReviewSession:
    src = args.input
    if not src or not os.path.exists(src):
        raise SystemExit(f"元動画が見つかりません: {src}")

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

    classes = resolve_classes(args.classes)
    cfg = TemporalConfig(
        max_gap=args.max_gap,
        memory=args.memory,
        margin_scale=args.margin_scale,
        max_area_ratio=args.max_area_ratio,
        min_track_len=0 if args.no_despike else 2,
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
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session = session_from_args(args)

    if args.export_dataset:
        export_dataset(session, args.export_dataset)
        session.reader.close()
        return 0

    ReviewHandler.session = session
    ReviewHandler.verbose = args.verbose

    # 127.0.0.1 以外に晒すと、素材そのものが LAN に出る。既定で塞いでおく
    host = args.host
    try:
        httpd = ThreadingHTTPServer((host, args.port), ReviewHandler)
    except OSError as e:
        print(f"ポート {args.port} を開けません: {e}", file=sys.stderr)
        return 1
    httpd.daemon_threads = True

    url = f"http://{host}:{httpd.server_address[1]}/"
    est = len(session.ranges_payload()["estimated_only_ranges"])
    unc = len(session.ranges_payload()["uncovered_ranges"])
    n_est_frames = session.coverage.count(COV_ESTIMATED)
    print(f"元動画      {session.video}")
    print(f"            {session.width}x{session.height}  {session.fps:.3f} fps  "
          f"{session.n_frames} フレーム")
    print(f"再生用      {session.rendered or '（未指定。コマ送りのみ）'}")
    print(f"手修正      {session.corrections_path}（{len(session.corrections.items)} 件）")
    print(f"既定の矩形  {session.default_size[0]}x{session.default_size[1]} px")
    print(
        f"推定のみ    {n_est_frames} フレーム "
        f"({100.0 * n_est_frames / max(1, session.n_frames):.1f}%) / 区間 {est} 件"
    )
    print(f"未処理      {unc} 区間")
    print(f"\n{url} を開いてください（Ctrl-C で終了）")
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
