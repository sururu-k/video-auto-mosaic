"""FastAPI アプリ本体。

画面は3つ。ジョブ一覧（アップロードと起動）、検査キュー（1枚出して3択）、
手描き（検出なしで矩形を置く）。どれも既存のロジックを呼ぶだけにしてある。

トークンは全経路で必須。扱うのが成人向けの素材である以上、同じ LAN に
繋がっているだけで見られる状態は作らない。`automosaic.review` と同じ規則
（URL・Cookie・ヘッダのどれか）にしてあるので、1回リンクを開けば以降は
Cookie で通る。
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import time
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)

from .. import review
from . import handdraw
from . import jobs as jobs_mod
from . import proxy as proxy_mod
from .jobs import Job, Library
from .runner import RunnerRegistry
from .session import SessionCache, session_overrides, sync_meta

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# 配信範囲の判定は review.py と同じものを使う。文字列の先頭一致で判定すると
# 兄弟ディレクトリが通るので、実装を2箇所に持たない
_inside = review._inside

# 素材のアップロードはディスクへ直に流す。数 GB を受けるので、
# 一度メモリやテンポラリに溜めてから移す実装にはしない。
UPLOAD_CHUNK = 4 * 1024 * 1024

# SSE の送出間隔と心拍。心拍が無いと、進捗が動かない区間（デコード待ち）で
# 経路上のプロキシや端末のスリープに黙って切られる。
SSE_POLL = 0.5
SSE_HEARTBEAT = 15.0


def _err(status: int, msg: str) -> HTTPException:
    return HTTPException(status_code=status, detail=msg)


def create_app(
    library_dir: str | None = None,
    token: str = "",
    require_token: bool = True,
) -> FastAPI:
    lib = Library(library_dir)
    runners = RunnerRegistry()
    sessions = SessionCache()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 起動時に、実行中のまま残っているジョブを現実に合わせる
        jobs_mod.reconcile(lib)
        yield
        # 終了時。走っているサブプロセスは殺さない（runners.shutdown を見よ）
        runners.shutdown()
        sessions.close_all()

    app = FastAPI(
        title="automosaic web", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.library = lib
    app.state.runners = runners
    app.state.sessions = sessions
    app.state.token = token
    app.state.require_token = bool(require_token and token)

    # ------------------------------------------------------------------
    # トークン
    # ------------------------------------------------------------------
    @app.middleware("http")
    async def auth(request: Request, call_next):
        if not app.state.require_token:
            return await call_next(request)
        given = request.query_params.get("t")
        ok = review.token_matches(app.state.token, given)
        issue = ok and bool(given)
        if not ok:
            ok = review.token_matches(
                app.state.token, review.cookie_token(request.headers.get("cookie"))
            )
        if not ok:
            ok = review.token_matches(
                app.state.token, request.headers.get("x-review-token")
            )
        if not ok:
            # 素材が漏れる経路なので、何があるかも含めて一切答えない
            return JSONResponse({"detail": "アクセストークンが違います"}, status_code=403)
        resp = await call_next(request)
        if issue:
            # 1回リンクを開けば以降は Cookie で通る。画面側からも読めるよう
            # HttpOnly は付けない（fetch にヘッダで付け直すため）
            resp.set_cookie(
                review.COOKIE_NAME, app.state.token, path="/", samesite="lax"
            )
        return resp

    # ------------------------------------------------------------------
    # 補助
    # ------------------------------------------------------------------
    def get_job(job_id: str) -> Job:
        try:
            job = lib.get(job_id)
        except KeyError:
            raise _err(404, f"ジョブがありません: {job_id}")
        _reconcile_one(job)
        # 遅延生成。パス2完了直後（runner.py）に始め損ねたジョブ（この機能を
        # 入れる前に完了していた・サーバ再起動をまたいで完了した）を、
        # 開かれたタイミングで拾う。output.mp4 が無い・既に完了/生成中なら
        # ensure_started 側が何もしないので、呼ぶこと自体は毎回のコストが低い
        proxy_mod.ensure_started(job)
        return job

    def _reconcile_one(job: Job) -> None:
        """見るたびに meta と現実を突き合わせる。

        このサーバが起動したランナーを持っているあいだは触らない。終了処理
        （_wait）が meta を書く途中なので、横から書くと状態が競合する。
        """
        if runners.get(job.id) is not None:
            return
        jobs_mod.settle(job)

    def job_state(job: Job) -> dict:
        d = job.detail()
        r = runners.get(job.id)
        if r is not None:
            snap = r.snapshot()
            d["progress"] = snap["progress"] or d["progress"]
            d["alive"] = snap["alive"]
            d["seq"] = snap["seq"]
        else:
            d["alive"] = jobs_mod.pid_alive(job.meta.get("pid"))
            d["seq"] = 0
        d["detached"] = bool(job.meta.get("detached"))
        return d

    def page(name: str) -> HTMLResponse:
        with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as f:
            return HTMLResponse(f.read())

    # ------------------------------------------------------------------
    # 画面
    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index():
        return page("index.html")

    @app.get("/job/{job_id}", response_class=HTMLResponse)
    def job_page(job_id: str):
        get_job(job_id)
        return page("job.html")

    @app.get("/review/{job_id}", response_class=HTMLResponse)
    def review_page(job_id: str):
        get_job(job_id)
        return page("review.html")

    @app.get("/draw/{job_id}", response_class=HTMLResponse)
    def draw_page(job_id: str):
        get_job(job_id)
        return page("draw.html")

    @app.get("/framestep", response_class=HTMLResponse)
    def framestep_page():
        # ジョブに紐付かない独立した確認ビュー（issue #19）。?src= で動画 URL を渡す。
        return page("framestep.html")

    @app.get("/static/{name:path}")
    def static_file(name: str):
        path = os.path.normpath(os.path.join(STATIC_DIR, name))
        # LAN に出す以上、static/ の外を読ませる筋は必ず塞ぐ。
        # 文字列の先頭一致だと兄弟ディレクトリが通る（review.py の _inside と同趣旨）
        if not _inside(STATIC_DIR, path) or not os.path.isfile(path):
            raise _err(404, "not found")
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return FileResponse(path, media_type=ctype)

    # ------------------------------------------------------------------
    # 1. ジョブの受け皿
    # ------------------------------------------------------------------
    @app.get("/api/jobs")
    def api_jobs():
        out = []
        for job in lib.list():
            _reconcile_one(job)
            s = job.summary()
            r = runners.get(job.id)
            if r is not None and r.alive():
                s["progress"] = r.snapshot()["progress"]
            out.append(s)
        return {"jobs": out, "library": lib.root, "active": runners.active()}

    @app.put("/api/upload")
    async def api_upload(request: Request, name: str = ""):
        """素材をディスクへ直に流し込む。

        受信した塊をそのまま書く。UploadFile 経由だと一度テンポラリに
        書いてから本番の場所へ複製することになり、2GB の素材で書き込みが
        倍になる。ここは画面側から fetch(PUT, File) で呼ぶ。
        """
        job = lib.create(name or "upload.mp4")
        total = 0
        try:
            with open(job.source, "wb", buffering=0) as f:
                buf = bytearray()
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    buf += chunk
                    total += len(chunk)
                    if len(buf) >= UPLOAD_CHUNK:
                        await anyio.to_thread.run_sync(f.write, bytes(buf))
                        buf.clear()
                if buf:
                    await anyio.to_thread.run_sync(f.write, bytes(buf))
        except Exception as e:  # noqa: BLE001
            lib.delete(job.id)
            raise _err(400, f"アップロードに失敗しました: {e}")
        if total == 0:
            lib.delete(job.id)
            raise _err(400, "中身が空です")
        job.update(size_bytes=total, uploaded_at=time.time())
        await anyio.to_thread.run_sync(jobs_mod.probe_source, job)
        return job.detail()

    @app.post("/api/jobs")
    async def api_create(file: UploadFile, name: str = Form("")):
        """multipart 版。JS を使わない <form> からの投稿用。

        こちらも塊で読んで書く。file.read() を引数なしで呼ぶと全体を
        メモリに載せるので、必ずサイズを指定して読む。
        """
        job = lib.create(name or file.filename or "upload.mp4")
        total = 0
        try:
            with open(job.source, "wb") as f:
                while True:
                    chunk = await file.read(UPLOAD_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    await anyio.to_thread.run_sync(f.write, chunk)
        except Exception as e:  # noqa: BLE001
            lib.delete(job.id)
            raise _err(400, f"アップロードに失敗しました: {e}")
        if total == 0:
            lib.delete(job.id)
            raise _err(400, "中身が空です")
        job.update(size_bytes=total, uploaded_at=time.time())
        await anyio.to_thread.run_sync(jobs_mod.probe_source, job)
        return job.detail()

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: str):
        return job_state(get_job(job_id))

    @app.delete("/api/jobs/{job_id}")
    def api_delete(job_id: str):
        job = get_job(job_id)
        if job.status == jobs_mod.STATUS_RUNNING:
            raise _err(409, "実行中のジョブは消せません。先に中断してください")
        if proxy_mod.is_generating(job_id):
            # 生成中の proxy.mp4 は ffmpeg が書き込み中。Windows では
            # 開いたままのファイルを rmtree が消しきれず残骸が残りうる
            raise _err(409, "プロキシ生成中のジョブは消せません。生成が終わるまで待ってください")
        sessions.drop(job_id)
        lib.delete(job_id)
        return {"ok": True, "id": job_id}

    # ------------------------------------------------------------------
    # 2. 処理の起動と進捗配信
    # ------------------------------------------------------------------
    @app.post("/api/jobs/{job_id}/start")
    async def api_start(job_id: str, payload: dict | None = None):
        job = get_job(job_id)
        if not os.path.exists(job.source):
            raise _err(400, "素材がありません")
        payload = payload or {}
        settings = dict(job.meta.get("settings") or {})
        settings.update(payload.get("settings") or {})
        # null と空文字は「この項目は指定しない」。落とさないと、保存済み設定に
        # 混ぜるだけの実装では項目を消せない。試写で入れた --limit-frames が
        # meta に残り続け、素材の一部しか処理していない完成品が
        # 「完了」として出てくる（--block も一度小さくすると自動に戻せない）
        settings = {k: v for k, v in settings.items() if v is not None and v != ""}
        reuse = bool(payload.get("reuse"))
        # 領域計算のキャッシュは焼き直しで無効になる。開いたままにしない
        sessions.drop(job_id)
        try:
            r = runners.start(job, settings, reuse=reuse)
        except RuntimeError as e:
            raise _err(409, str(e))
        return {"ok": True, "id": job_id, "argv": r.argv, "status": job.status}

    @app.post("/api/jobs/{job_id}/cancel")
    def api_cancel(job_id: str):
        job = get_job(job_id)
        if not runners.cancel(job_id):
            raise _err(409, "実行中ではありません")
        return {"ok": True, "id": job.id}

    @app.get("/api/jobs/{job_id}/progress")
    def api_progress(job_id: str, since: int = 0):
        """SSE が使えない経路のための取り直し口。

        進捗は積み上げでなく現在値なので、これを1回叩けば追いつく。
        """
        job = get_job(job_id)
        r = runners.get(job_id)
        if r is None:
            d = job_state(job)
            d["log"] = []
            return d
        snap = r.snapshot(since=since)
        snap["status"] = job.status
        snap["detail"] = job.detail()
        return snap

    @app.get("/api/jobs/{job_id}/events")
    async def api_events(job_id: str, request: Request, since: int = 0):
        """進捗を SSE で流す。

        3〜4時間かかる処理なので、途中で切れることを前提にする。繋ぎ直しの
        たびに現状を1件目として送るので、取りこぼしても追いつける。ログ行は
        連番付きで、`Last-Event-ID`（または since）から続きだけ送る。
        """
        job = get_job(job_id)
        last = since
        hdr = request.headers.get("last-event-id")
        if hdr and hdr.isdigit():
            last = int(hdr)

        async def gen():
            nonlocal last
            beat = time.time()
            sent = None
            while True:
                if await request.is_disconnected():
                    return
                cur = lib.get(job_id)
                r = runners.get(job_id)
                if r is not None:
                    snap = r.snapshot(since=last)
                    last = snap["seq"]
                else:
                    snap = {
                        "job": job_id,
                        "progress": cur.meta.get("progress") or {},
                        "log": [],
                        "seq": last,
                        "alive": jobs_mod.pid_alive(cur.meta.get("pid")),
                    }
                snap["status"] = cur.status
                snap["error"] = cur.meta.get("error")
                snap["has_output"] = os.path.exists(cur.output)
                body = json.dumps(snap, ensure_ascii=False)
                if body != sent or time.time() - beat > SSE_HEARTBEAT:
                    yield f"id: {last}\nevent: progress\ndata: {body}\n\n"
                    sent = body
                    beat = time.time()
                if cur.status in (
                    jobs_mod.STATUS_DONE,
                    jobs_mod.STATUS_FAILED,
                    jobs_mod.STATUS_CANCELED,
                    jobs_mod.STATUS_INTERRUPTED,
                ) and not snap.get("alive"):
                    yield f"event: end\ndata: {body}\n\n"
                    return
                await asyncio.sleep(SSE_POLL)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # 逆プロキシに溜め込ませない。溜まると進捗が塊で届く
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/jobs/{job_id}/log")
    def api_log(job_id: str, tail: int = 200):
        job = get_job(job_id)
        if not os.path.exists(job.log):
            return {"lines": []}
        with open(job.log, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return {"lines": lines[-max(1, tail):]}

    # ------------------------------------------------------------------
    # 3. 検査キュー
    # ------------------------------------------------------------------
    def _report_effective(job: Job) -> dict | None:
        """report.json から実効設定のフィンガープリントを取り出す。

        無い・壊れている・古い形式（effective / effective_sha256 を持たない）
        場合は None を返す。呼び出し側はこれを「一致」として扱わないこと。
        """
        if not os.path.exists(job.report):
            return None
        try:
            with open(job.report, encoding="utf-8") as f:
                rep = json.load(f)
        except (OSError, ValueError):
            return None
        eff = rep.get("effective")
        sha = rep.get("effective_sha256")
        if not eff or not sha:
            return None
        return {"effective": eff, "sha256": sha}

    def _check_effective_settings(job: Job, s) -> None:
        """レビューの実効設定を、実際に焼いた report.json と突き合わせる。

        issue #16: どちらも警告ゼロ・エラーゼロで食い違っていた（#14, #15）。
        黙って通さず、食い違いを検出したら絵を返さずに 409 で止める
        （RULES.md 0: 判断がつかないときは塞ぐ・止める）。

        完成品（job.output）がまだ無いジョブは比較対象が無い。検出だけ済ませて
        描画前にレビュー/手描きをする通常の使い方を塞がないよう、ここでは
        何もしない。
        """
        if not os.path.exists(job.output):
            return
        rep = _report_effective(job)
        session_eff = s.effective_settings()
        session_sha = s.effective_sha256()
        if rep is None:
            raise _err(
                409,
                "完成品はありますが report.json に実効設定の記録がありません"
                "（古い形式か破損しています）。レビューが焼き込みと同じ設定で"
                "絵を作っている保証がないため表示できません。"
                "焼き直すと記録されます。"
                f" レビュー側の実効設定: {session_eff}",
            )
        if rep["sha256"] != session_sha:
            raise _err(
                409,
                "この動画は表示中の設定と違う設定で焼かれています。"
                "焼き直すか、レビューの設定を焼き込みに合わせてください。"
                f" 焼き込み: {rep['effective']} / レビュー: {session_eff}",
            )

    def get_session(job: Job):
        # ジョブの settings（焼き込みに使った mode/block/classes/estimate_gaps
        # など）を、そのままレビューにも渡す。ここを空のまま呼ぶと、レビューは
        # review.py の argparse 既定（pixelize / 自動ブロック / default 3クラス）
        # で見せることになり、実際に焼いた絵と食い違う（#15）。
        try:
            overrides = session_overrides(job.meta.get("settings") or {})
        except ValueError as e:
            # 壊れた設定値。黙って無視して既定値で見せると「合ってる」と
            # 誤認させるので、レビューを止めて利用者に直させる
            raise _err(400, str(e))
        try:
            s = sessions.get(job, **overrides)
        except FileNotFoundError as e:
            raise _err(400, str(e))
        except SystemExit as e:  # session_from_args は SystemExit で止める
            raise _err(400, str(e))
        except RuntimeError as e:
            raise _err(400, str(e))
        _check_effective_settings(job, s)
        return s

    @app.get("/api/jobs/{job_id}/state")
    def api_state(job_id: str, light: int = 1):
        job = get_job(job_id)
        s = get_session(job)
        with s.lock:
            d = s.state_payload(bool(light))
        d["job"] = job.summary()
        return d

    @app.get("/api/jobs/{job_id}/queue")
    def api_queue(
        job_id: str, rebuild: int = 0, step: int | None = None, all: int | None = None
    ):
        job = get_job(job_id)
        s = get_session(job)
        with s.lock:
            if step is not None:
                s.queue_step = max(1, int(step))
                rebuild = 1
            if all is not None:
                s.queue_all = bool(all)
                rebuild = 1
            if rebuild or not s.queue:
                s.rebuild_queue()
            return s.queue_payload()

    @app.post("/api/jobs/{job_id}/mark")
    def api_mark(job_id: str, payload: dict):
        job = get_job(job_id)
        s = get_session(job)
        try:
            frame = int(payload["frame"])
            # 範囲外を端へ寄せない。寄せると、応答は送った番号を返すのに
            # 記録は最終フレームに付き、画面と .progress.json が食い違う。
            # 「判定したはずのコマが未判定のまま」「触っていないコマに
            # 判定が付く」の両方が黙って起きる
            if not 0 <= frame < s.n_frames:
                raise ValueError(
                    f"フレーム番号が範囲外です: {frame}（0〜{s.n_frames - 1}）"
                )
            verdict = str(payload["verdict"])
            tap = None
            if "x" in payload and "y" in payload:
                tap = (float(payload["x"]), float(payload["y"]))
            size = None
            if payload.get("w") and payload.get("h"):
                size = (float(payload["w"]), float(payload["h"]))
            # 「誤検知」で消す自動領域。画面が見て選んだ矩形をそのまま送る。
            # 番号ではなく座標で送らせるのは、送っている間にキューが
            # 組み直されても指すものが変わらないようにするため（review.py と同じ）。
            # これが無いと false_positive は常に「消す領域が指定されていません」で
            # 400 になり、旧レビュー UI にしか誤検知を通す経路が無いことになる。
            pick = None
            if payload.get("pick"):
                pick = [[float(v) for v in b[:4]] for b in payload["pick"]]
            # 区間の始点（issue #46）。end 側は既存のフィールド（frame/x/y/w/h）を
            # そのまま使う。始点だけ別名にしてあるのは、フィールドが1組しか
            # 無い既存の呼び出し（span 判定）とサーバ側で区別するため
            start_frame = None
            start_tap = None
            start_size = None
            if "start_frame" in payload and "start_x" in payload and "start_y" in payload:
                start_frame = int(payload["start_frame"])
                start_tap = (float(payload["start_x"]), float(payload["start_y"]))
                if payload.get("start_w") and payload.get("start_h"):
                    start_size = (float(payload["start_w"]), float(payload["start_h"]))
            with s.lock:
                if start_frame is not None:
                    # 「でかすぎる」「誤検知」の remove を区間補間で動かすのは
                    # 危険（review.mark_interval のドキュストリング参照）。
                    # add だけの「漏れている」に限定する
                    if verdict != "fixed":
                        raise ValueError(
                            "区間指定は「漏れている」判定だけに対応しています"
                        )
                    if tap is None:
                        raise ValueError("終点の位置が指定されていません")
                    added = s.mark_interval(
                        frame,
                        tap,
                        start_frame,
                        start_tap,
                        size=size,
                        start_size=start_size,
                        cls=payload.get("class"),
                    )
                else:
                    added = s.mark(
                        frame,
                        verdict,
                        tap=tap,
                        size=size,
                        span=int(payload.get("span", 0)),
                        cls=payload.get("class"),
                        pick=pick,
                    )
                out = {
                    "ok": True,
                    "frame": frame,
                    "verdict": verdict,
                    "added": added,
                    "version": s.version,
                    "n_corrections": len(s.corrections.items),
                    "regions": s.frame_regions(frame),
                    "progress": s.progress_payload(),
                }
                sync_meta(job, s)
        except (KeyError, TypeError, ValueError) as e:
            raise _err(400, f"判定を記録できません: {e}")
        return out

    @app.post("/api/jobs/{job_id}/undo")
    def api_undo(job_id: str):
        job = get_job(job_id)
        s = get_session(job)
        with s.lock:
            h = s.undo()
            if h is None:
                return {"ok": False, "error": "戻せる操作がありません"}
            out = {
                "ok": True,
                "frame": h["frame"],
                "removed": h.get("added", 0),
                "version": s.version,
                "n_corrections": len(s.corrections.items),
                "regions": s.frame_regions(h["frame"]),
                "progress": s.progress_payload(),
            }
            sync_meta(job, s)
        return out

    @app.get("/api/jobs/{job_id}/frame")
    def api_frame(
        job_id: str, n: int = 0, raw: int = 0, w: int = 0, fmt: str = "jpg", v: int = 0
    ):
        job = get_job(job_id)
        s = get_session(job)
        # 範囲外に最終フレームの絵を返さない。同じ番号を /mark に出すと 400 なので、
        # 絵は出るのに判定は付かない状態になる
        if not 0 <= n < s.n_frames:
            raise _err(404, f"フレーム番号が範囲外です: {n}（0〜{s.n_frames - 1}）")
        fmt = "jpg" if fmt in ("jpg", "jpeg") else "png"
        # s.lock は取らない。recompute() 中でもフレーム画像は出す
        # （issue #25。review.ReviewSession.lock のコメントを参照）。
        got = s.frame_image(n, raw=bool(raw), max_w=max(0, int(w)), fmt=fmt)
        if got is None:
            raise _err(404, f"フレーム {n} を読めません")
        body, ctype = got
        headers = {}
        # 世代番号付きで取りに来ているなら先読みが効くようにする。
        # 修正が入れば version が変わり URL も変わるので古い絵は残らない。
        # ただし原画はキャッシュさせない。モザイク前のフレームが端末のディスクに
        # 残り、「サーバを閉じたら見えない」という前提が崩れる
        headers["Cache-Control"] = (
            "private, max-age=600" if (v and not raw) else "no-store"
        )
        return Response(content=body, media_type=ctype, headers=headers)

    @app.get("/api/jobs/{job_id}/corrections")
    def api_get_corrections(job_id: str):
        job = get_job(job_id)
        s = get_session(job)
        with s.lock:
            return {
                "video": s.corrections.video,
                "width": s.corrections.width,
                "height": s.corrections.height,
                "corrections": [c.as_dict() for c in s.corrections.items],
            }

    @app.post("/api/jobs/{job_id}/corrections")
    def api_set_corrections(job_id: str, payload: dict):
        job = get_job(job_id)
        s = get_session(job)
        # キーが無い本文を「空の一覧で置き換えろ」と読まない。読むと、
        # 壊れた本文ひとつで手修正が全部消える。判定（.progress.json）は
        # 残るので、「塞いだ」と表示されたまま塞いだ矩形だけが無い状態になる
        items = payload.get("corrections")
        if not isinstance(items, list):
            raise _err(400, "corrections（配列）が本文にありません")
        # frame は任意（#24）。呼び出し側が「いま表示しているコマ」を教えて
        # くれたときだけ、そのコマぶんの regions に絞って返す。省略時は
        # 従来どおり全フレームぶん（後方互換。review.py の update_payload と同じ）
        frame_arg: int | None = None
        if "frame" in payload:
            try:
                frame_arg = int(payload["frame"])
            except (TypeError, ValueError):
                frame_arg = None
        try:
            with s.lock:
                s.set_corrections(items)
                # set_corrections は履歴を捨てるが、捨てたことを保存しない。
                # 保存しないと、セッションを開き直した瞬間に .progress.json から
                # 古い履歴が生き返り、次の undo が無関係な修正を末尾から削る
                s.save_progress()
                out = s.update_payload(frame_arg)
                sync_meta(job, s)
        except (KeyError, TypeError, ValueError) as e:
            raise _err(400, f"修正を適用できません: {e}")
        return out

    # ------------------------------------------------------------------
    # 4. 手描きモード
    # ------------------------------------------------------------------
    @app.get("/api/jobs/{job_id}/annotations")
    def api_annotations(job_id: str):
        job = get_job(job_id)
        return {"annotations": handdraw.load(job)}

    @app.post("/api/jobs/{job_id}/annotations")
    def api_put_annotation(job_id: str, payload: dict):
        """打点を1つ置く。box が null なら「ここには無い」。

        座標は正規化タップ (x, y) でも、動画座標の box でも受ける。
        端末から来るのは前者で、変換は review.tap_to_box に任せる。
        画面ピクセルを送らせると端末ごとの拡大率で静かにずれる。
        """
        job = get_job(job_id)
        s = get_session(job)
        try:
            frame = int(payload["frame"])
        except (KeyError, TypeError, ValueError) as e:
            raise _err(400, f"フレーム番号が不正です: {e}")
        # 端へ寄せない。寄せると、狙ったフレームは素通しのまま別のフレームに
        # 打点が乗り、そこを起点に補間が走る。DELETE は寄せないので、
        # 寄せられた打点は送った番号では消せない
        if not 0 <= frame < s.n_frames:
            raise _err(400, f"フレーム番号が範囲外です: {frame}（0〜{s.n_frames - 1}）")

        if payload.get("absent"):
            items = handdraw.put(job, frame, None, payload.get("class"))
            return {"ok": True, "frame": frame, "annotations": items}

        if "box" in payload and payload["box"]:
            box = [float(v) for v in payload["box"]][:4]
        elif "x" in payload and "y" in payload:
            size = (
                (float(payload["w"]), float(payload["h"]))
                if payload.get("w") and payload.get("h")
                else s.default_size
            )
            box = list(
                review.tap_to_box(
                    float(payload["x"]), float(payload["y"]), size, s.width, s.height
                )
            )
        else:
            raise _err(400, "位置が指定されていません")
        items = handdraw.put(job, frame, box, payload.get("class") or s.default_class)
        return {"ok": True, "frame": frame, "box": box, "annotations": items}

    @app.delete("/api/jobs/{job_id}/annotations/{frame}")
    def api_del_annotation(job_id: str, frame: int):
        job = get_job(job_id)
        return {"ok": True, "annotations": handdraw.delete(job, int(frame))}

    @app.post("/api/jobs/{job_id}/annotations/expand")
    def api_expand(job_id: str, payload: dict | None = None):
        job = get_job(job_id)
        payload = payload or {}
        s = get_session(job)
        try:
            out = handdraw.expand(
                job,
                max_interp=int(payload.get("max_interp", 20)),
                hold=int(payload.get("hold", 4)),
                default_class=payload.get("class") or s.default_class,
                # 既定は「既存の手修正に足す」。handdraw.expand の既定と揃える。
                # キーが無いだけで置き換えにすると、検査キューで塞いだ矩形が
                # 手描きを一度使うだけで全部消える。判定は「塞いだ」と表示された
                # まま、塞いだ矩形だけが無い状態になる
                merge=bool(payload.get("merge", True)),
            )
        except (OSError, ValueError, RuntimeError) as e:
            raise _err(400, f"展開できません: {e}")
        # corrections.json を直に書き換えたので、「ひとつ戻す」の履歴はもう
        # 別物を指している。set_corrections と同じ理由で捨てる。残すと次の
        # undo が、手描きで置いた矩形を末尾から件数ぶん削る
        with s.lock:
            s.history.clear()
            s.save_progress()
        # corrections.json を書き換えたので、開いているセッションは作り直す
        sessions.drop(job_id)
        out["ok"] = True
        return out

    # ------------------------------------------------------------------
    # 5. 完成品とデータセット
    # ------------------------------------------------------------------
    @app.get("/api/jobs/{job_id}/download")
    def api_download(job_id: str):
        job = get_job(job_id)
        if not os.path.exists(job.output):
            raise _err(404, "完成品がまだありません")
        base = os.path.splitext(job.meta.get("name") or job.id)[0]
        return FileResponse(
            job.output,
            media_type="video/mp4",
            filename=f"{base}_mosaic.mp4",
        )

    @app.get("/api/jobs/{job_id}/video")
    def api_video(job_id: str):
        """再生用。FileResponse が Range を扱うのでシークが効く。"""
        job = get_job(job_id)
        if not os.path.exists(job.output):
            raise _err(404, "完成品がまだありません")
        return FileResponse(job.output, media_type="video/mp4")

    @app.get("/api/jobs/{job_id}/proxy")
    def api_proxy(job_id: str):
        """確認用プロキシ動画（issue #18）。Range 付きで返す。

        FileResponse が Range を扱うので /video と同じくシークが効く
        （app.py の /video と同じ理由）。未生成・生成中・失敗のどれかは
        JSON で返し、実際の動画データと区別する。「まだ無い」と
        「作れなかった」を同じ見た目にしない。
        """
        job = get_job(job_id)
        if not os.path.exists(job.output):
            raise _err(404, "完成品がまだないので、プロキシも作れません")
        p = job.meta.get("proxy") or {}
        status = p.get("status")
        if status == proxy_mod.STATUS_DONE and os.path.exists(job.proxy):
            resp = FileResponse(job.proxy, media_type="video/mp4")
            # モザイク済みの派生物なのでキャッシュしてよい（原画ではない）
            resp.headers["Cache-Control"] = "private, max-age=3600"
            return resp
        if status == proxy_mod.STATUS_FAILED:
            return JSONResponse(
                {"status": "failed", "error": p.get("error")}, status_code=500
            )
        # 未生成・生成中はここに来る。get_job() が毎回 ensure_started を
        # 呼んでいるので、ここで改めて呼ばなくても既に始まっているはずだが、
        # 呼び直しても ensure_started 側が多重起動を弾くので安全側に倒す
        proxy_mod.ensure_started(job)
        return JSONResponse({"status": "generating"}, status_code=202)

    @app.get("/api/jobs/{job_id}/report")
    def api_report(job_id: str):
        job = get_job(job_id)
        if not os.path.exists(job.report):
            raise _err(404, "レポートがありません")
        with open(job.report, encoding="utf-8") as f:
            return json.load(f)

    @app.post("/api/jobs/{job_id}/dataset")
    def api_dataset(job_id: str):
        """手修正を YOLO 形式で書き出す。review.export_dataset をそのまま使う。"""
        job = get_job(job_id)
        s = get_session(job)
        with s.lock:
            written = review.export_dataset(s, job.dataset, quiet=True)
        job.update(dataset_frames=written, dataset_at=time.time())
        return {
            "ok": True,
            "dir": job.dataset,
            "frames": written,
            "classes": sorted(s.classes),
        }

    @app.get("/api/jobs/{job_id}/dataset")
    def api_dataset_info(job_id: str):
        job = get_job(job_id)
        img = os.path.join(job.dataset, "images")
        if not os.path.isdir(img):
            return {"exists": False, "frames": 0}
        files = sorted(os.listdir(img))
        return {
            "exists": True,
            "dir": job.dataset,
            "frames": len(files),
            "sample": files[:10],
        }

    return app
