"""Web アプリの検証。

ブラウザが使えないので、実際に uvicorn を立てて HTTP で叩く。
関数を直接呼ぶ形にすると「403 を返すつもりで 200 を返していた」
「アップロードした中身が 0 バイトだった」という、いちばん困る種類の
壊れ方に気づけない。

`fastapi.testclient` は httpx を要求して依存が増えるので使わない。
標準ライブラリの urllib で足りる（tests/test_review.py と同じ流儀）。

素材は `ffmpeg -f lavfi -i testsrc2` で作った 2 秒の動画を使う。
既存の data/myvideo5 と data/bench3 には一切触らず、テンポラリに
作ったライブラリの中だけで完結させる。
"""

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic.corrections import Correction, CorrectionSet  # noqa: E402
from automosaic.webapp import jobs as jobs_mod  # noqa: E402
from automosaic.webapp import runner as runner_mod  # noqa: E402
from automosaic.webapp.app import create_app  # noqa: E402

TOKEN = "webapptesttoken"
CLS = "MALE_GENITALIA_EXPOSED"

_sample_cache: str | None = None


# --------------------------------------------------------------------------
# 素材
# --------------------------------------------------------------------------


def ffmpeg_path() -> str | None:
    """ffmpeg を探す。winget 版は PATH に載っていないことがある。"""
    p = shutil.which("ffmpeg")
    if p:
        return p
    links = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links"
    )
    cand = os.path.join(links, "ffmpeg.exe")
    return cand if os.path.exists(cand) else None


def sample_video() -> str:
    """検証用の 2 秒の動画。1回作って使い回す。"""
    global _sample_cache
    if _sample_cache and os.path.exists(_sample_cache):
        return _sample_cache
    ff = ffmpeg_path()
    if not ff:
        raise RuntimeError("ffmpeg が見つかりません")
    d = tempfile.mkdtemp(prefix="automosaic_sample_")
    out = os.path.join(d, "sample.mp4")
    subprocess.run(
        [
            ff, "-y", "-f", "lavfi",
            "-i", "testsrc2=size=320x240:rate=30:duration=2",
            "-pix_fmt", "yuv420p", out,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _sample_cache = out
    return out


# --------------------------------------------------------------------------
# サーバ
# --------------------------------------------------------------------------


class Server:
    """テスト用に uvicorn を1本立てる。ポートは空きを自分で取る。"""

    def __init__(self, library: str, token: str = TOKEN, require_token: bool = True):
        import uvicorn

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()

        self.app = create_app(library_dir=library, token=token, require_token=require_token)
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("サーバが立ち上がりません")

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def request(
    url: str, method: str = "GET", data=None, headers=None, ctype=None
) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            return res.status, res.read(), dict(res.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def get_json(url: str, headers=None):
    code, body, _ = request(url, headers=headers)
    assert code == 200, f"{url} -> {code} {body[:300]!r}"
    return json.loads(body)


def post_json(url: str, obj, headers=None):
    code, body, _ = request(
        url,
        method="POST",
        data=json.dumps(obj).encode("utf-8"),
        headers=headers,
        ctype="application/json",
    )
    return code, (json.loads(body) if body else None)


def upload(base: str, path: str, name: str = "sample.mp4") -> dict:
    """PUT で素材を丸ごと送る。画面側と同じ経路。"""
    with open(path, "rb") as f:
        data = f.read()
    code, body, _ = request(
        f"{base}/api/upload?name={urllib.parse.quote(name)}&t={TOKEN}", method="PUT", data=data,
        ctype="application/octet-stream",
    )
    assert code == 200, f"upload -> {code} {body[:300]!r}"
    return json.loads(body)


def make_lib() -> str:
    return tempfile.mkdtemp(prefix="automosaic_lib_")


# --------------------------------------------------------------------------
# ジョブID とライブラリ
# --------------------------------------------------------------------------


def test_job_id_and_ext():
    lib = jobs_mod.Library(make_lib())
    drawn = [jobs_mod.new_job_id(lib.root) for _ in range(50)]
    ids = set(drawn)
    assert len(ids) == 50, "ジョブID が衝突している"
    for i in ids:
        assert jobs_mod.JOB_ID_RE.match(i), i
        # 発番と同時にディレクトリで確保していること。ここが「見るだけ」だと
        # 実際の一意性が乱数4桁（16bit）にしか乗らず、同じ秒に 50 件作ると
        # 2% ほどで衝突して、あとから来たほうが先の案件を上書きする
        assert os.path.isdir(os.path.join(lib.root, i)), f"確保されていない: {i}"
    # 保存名は拡張子だけ引き継ぐ。パス区切りを混ぜた名前で外へ書かせない
    assert jobs_mod.safe_ext("a.MOV") == ".mov"
    assert jobs_mod.safe_ext("../../evil.exe") == ".mp4"
    assert jobs_mod.safe_ext("") == ".mp4"
    print(f"  ジョブID {len(ids)} 件が全て一意・その場で確保・拡張子の絞り込み OK")


def test_job_id_is_reserved_under_race():
    """同時に引いても衝突しないこと。サーバは要求を並行で処理する。

    「そのIDのディレクトリが無ければ返す」だけの実装だと、見てから使うまでの
    あいだに同じIDを別の要求が引き当てられる。
    """
    from concurrent.futures import ThreadPoolExecutor

    lib = jobs_mod.Library(make_lib())
    with ThreadPoolExecutor(max_workers=16) as ex:
        ids = list(ex.map(lambda _: jobs_mod.new_job_id(lib.root), range(50)))
    assert len(set(ids)) == 50, "同時発番でジョブID が衝突している"
    print("  同時に 50 件引いても一意 OK")


def test_library_rejects_bad_id():
    lib = jobs_mod.Library(make_lib())
    for bad in ("..", "../etc", "abc", "20260823-999999", ""):
        try:
            lib.get(bad)
        except KeyError:
            continue
        raise AssertionError(f"通してはいけない ID を通した: {bad}")
    print("  不正なジョブID の拒否 OK")


# --------------------------------------------------------------------------
# トークン
# --------------------------------------------------------------------------


def test_token_required():
    srv = Server(make_lib())
    try:
        code, _, _ = request(f"{srv.base}/api/jobs")
        assert code == 403, f"トークン無しで {code} が返った"
        code, _, _ = request(f"{srv.base}/api/jobs?t=wrong")
        assert code == 403, f"違うトークンで {code} が返った"
        code, body, hdr = request(f"{srv.base}/api/jobs?t={TOKEN}")
        assert code == 200, code
        # 1回 URL で通れば以降は Cookie で通る
        cookie = hdr.get("set-cookie") or ""
        assert TOKEN in cookie, f"Cookie が発行されない: {cookie!r}"
        code, _, _ = request(
            f"{srv.base}/api/jobs", headers={"Cookie": f"automosaic_t={TOKEN}"}
        )
        assert code == 200, f"Cookie で通らない: {code}"
        code, _, _ = request(
            f"{srv.base}/api/jobs", headers={"X-Review-Token": TOKEN}
        )
        assert code == 200, f"ヘッダで通らない: {code}"
        # 画面もトークン必須。素材が漏れる経路を1つも空けない
        assert request(f"{srv.base}/")[0] == 403
        assert request(f"{srv.base}/static/style.css")[0] == 403
        print("  トークン検証（URL / Cookie / ヘッダ / 画面）OK")
    finally:
        srv.close()


def test_static_traversal_blocked():
    srv = Server(make_lib())
    try:
        for bad in ("../review.py", "..%2f..%2fcli.py", "../../automosaic/cli.py"):
            code, _, _ = request(f"{srv.base}/static/{bad}?t={TOKEN}")
            assert code == 404, f"{bad} が {code} で通った"
        code, _, _ = request(f"{srv.base}/static/style.css?t={TOKEN}")
        assert code == 200
        print("  static の外を読ませない OK")
    finally:
        srv.close()


# --------------------------------------------------------------------------
# 1. アップロードとジョブ一覧
# --------------------------------------------------------------------------


def test_upload_and_list():
    lib = make_lib()
    srv = Server(lib)
    try:
        src = sample_video()
        d = upload(srv.base, src, "テスト素材.mp4")
        assert jobs_mod.JOB_ID_RE.match(d["id"]), d["id"]
        assert d["name"] == "テスト素材.mp4"
        assert d["size_bytes"] == os.path.getsize(src), "サイズが合わない"
        # 中身が同じであること。ストリームで書いているので取りこぼしが怖い
        saved = os.path.join(lib, d["id"], "source.mp4")
        assert os.path.getsize(saved) == os.path.getsize(src)
        with open(saved, "rb") as a, open(src, "rb") as b:
            assert a.read() == b.read(), "保存された中身が元と違う"
        # cv2 で解像度が読めていること
        assert (d["width"], d["height"]) == (320, 240), d
        assert d["n_frames"] > 0
        assert d["status"] == "new"

        lst = get_json(f"{srv.base}/api/jobs?t={TOKEN}")
        assert [j["id"] for j in lst["jobs"]] == [d["id"]]
        # meta.json から復元できること（サーバを立て直しても一覧が残る）
        meta = json.load(open(os.path.join(lib, d["id"], "meta.json"), encoding="utf-8"))
        assert meta["id"] == d["id"] and meta["source"] == "source.mp4"
        print(f"  アップロード {d['size_bytes']} バイトが完全一致・一覧と meta OK")
    finally:
        srv.close()


def test_upload_multipart():
    """JS を切っていても投稿できる経路。python-multipart はこれのために入れている。"""
    lib = make_lib()
    srv = Server(lib)
    try:
        src = sample_video()
        with open(src, "rb") as f:
            content = f.read()
        boundary = "----automosaictest"
        buf = io.BytesIO()
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(
            b'Content-Disposition: form-data; name="file"; filename="form.mp4"\r\n'
        )
        buf.write(b"Content-Type: video/mp4\r\n\r\n")
        buf.write(content)
        buf.write(f"\r\n--{boundary}--\r\n".encode())
        code, body, _ = request(
            f"{srv.base}/api/jobs?t={TOKEN}",
            method="POST",
            data=buf.getvalue(),
            ctype=f"multipart/form-data; boundary={boundary}",
        )
        assert code == 200, f"{code} {body[:300]!r}"
        d = json.loads(body)
        assert d["size_bytes"] == len(content), d
        assert os.path.getsize(os.path.join(lib, d["id"], "source.mp4")) == len(content)
        print(f"  multipart 投稿 OK（{len(content)} バイト）")
    finally:
        srv.close()


def test_upload_empty_is_rejected():
    lib = make_lib()
    srv = Server(lib)
    try:
        code, _, _ = request(
            f"{srv.base}/api/upload?name=x.mp4&t={TOKEN}", method="PUT", data=b""
        )
        assert code == 400, code
        # 失敗したジョブの残骸を置いていかない
        assert not [d for d in os.listdir(lib) if jobs_mod.JOB_ID_RE.match(d)]
        print("  空のアップロードを弾き、残骸も残さない OK")
    finally:
        srv.close()


def test_delete_job():
    lib = make_lib()
    srv = Server(lib)
    try:
        d = upload(srv.base, sample_video())
        code, body, _ = request(f"{srv.base}/api/jobs/{d['id']}?t={TOKEN}", method="DELETE")
        assert code == 200, body
        assert not os.path.exists(os.path.join(lib, d["id"]))
        assert get_json(f"{srv.base}/api/jobs?t={TOKEN}")["jobs"] == []
        print("  ジョブの削除 OK")
    finally:
        srv.close()


# --------------------------------------------------------------------------
# 2. 進捗の解釈と処理の起動
# --------------------------------------------------------------------------


def test_progress_regex_matches_cli_output():
    """cli.Progress が実際に書く文字列で当てる。

    ここは形が変わっても例外にならず、静かに進捗が出なくなるだけなので、
    CLI の実装から文字列を作らせて突き合わせる。
    """
    from automosaic.cli import Progress

    cap = io.StringIO()
    real, sys.stderr = sys.stderr, cap
    try:
        p = Progress(4560, "パス1 検出")
        p.update(123, force=True)
        q = Progress(None, "パス2 描画")
        q.update(77, force=True)
    finally:
        sys.stderr = real
    out = cap.getvalue()
    lines = [x for x in out.replace("\r", "\n").split("\n") if x.strip()]

    m = runner_mod.RE_PROGRESS.search(lines[0])
    assert m, f"進捗行に当たらない: {lines[0]!r}"
    assert m.group(1) == "パス1" and int(m.group(3)) == 123 and int(m.group(4)) == 4560

    m2 = runner_mod.RE_PROGRESS_OPEN.search(lines[1])
    assert m2, f"総数不明の進捗行に当たらない: {lines[1]!r}"
    assert m2.group(1) == "パス2" and int(m2.group(3)) == 77
    print(f"  進捗行の解釈 OK（{lines[0].strip()!r}）")


class _FakePipe:
    """`_pump` に渡す偽のパイプ。バイト列をどこで chunk に切るか自分で決められる。

    実際の OS パイプだと chunk 境界を狙って作れないので、
    read1() が返す chunk をあらかじめ指定できるようにしてある。
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks) + [b""]
        self._i = 0

    def read1(self, n: int = -1) -> bytes:
        c = self._chunks[self._i]
        self._i += 1
        return c

    def close(self) -> None:
        pass


def test_pump_survives_multibyte_split_at_chunk_boundary():
    """issue #30: 日本語の多バイト文字が chunk 境界をまたいでも壊れないこと。

    `_pump` は chunk ごとに独立して `decode("utf-8", "replace")` していたため、
    UTF-8 の継続バイトのど真ん中で chunk を切ると、前後どちらの chunk でも
    デコードに失敗し、化けた上に進捗の正規表現が当たらなくなっていた
    （稼働中ジョブの実ログで実測: 'ブロック' -> 'ブロ\\udc83ク' 等）。

    cli.Progress が実際に書く2行ぶんの出力を、マルチバイト文字の
    継続バイト位置すべてで2分割し、それぞれ `_pump` に流し込んで
    (1) 置換文字が残らないこと (2) 進捗の正規表現が当たり、値が
    正しく拾えることを確認する。2行目は末尾に区切り文字が無いので
    ストリーム終端（final=True でのデコーダ確定）の経路も通る。
    """
    from automosaic.cli import Progress

    cap = io.StringIO()
    real, sys.stderr = sys.stderr, cap
    try:
        p = Progress(4560, "パス1 検出")
        p.update(123, force=True)
        p.update(4560, force=True)
    finally:
        sys.stderr = real
    data = cap.getvalue().encode("utf-8")

    lib = jobs_mod.Library(make_lib())
    job = lib.create("split.mp4")

    n_boundaries = 0
    for cut in range(1, len(data)):
        b = data[cut]
        if not (0x80 <= b <= 0xBF):
            continue  # UTF-8 の継続バイトでない = 文字の途中で切れていない
        n_boundaries += 1
        r = runner_mod.JobRunner(job, {})
        r._pump(_FakePipe([data[:cut], data[cut:]]), is_err=True)

        joined = "".join(e["text"] for e in r.log)
        assert "�" not in joined, f"cut={cut}バイト目: 化けが残った: {joined!r}"

        assert "pass1" in r.progress, (
            f"cut={cut}バイト目: 境界に当たって進捗の正規表現が外れた: {joined!r}"
        )
        # 2行目（4560/4560）が最後に処理されて上書きすること
        assert r.progress["pass1"]["n"] == 4560, f"cut={cut}バイト目: {r.progress}"
        assert r.progress["pass1"]["total"] == 4560

    assert n_boundaries > 0, "境界候補が無い（テストとして無意味）"
    print(f"  chunk境界（継続バイト {n_boundaries} 箇所）をまたいでも化けず進捗も拾える OK")


def test_build_argv():
    lib = jobs_mod.Library(make_lib())
    job = lib.create("a.mp4")
    argv = runner_mod.build_argv(job, {"infer_size": 640, "tta": True, "conf": 0.1})
    assert argv[1:3] == ["-m", "automosaic"]
    assert "--infer-size" in argv and "640" in argv
    assert "--tta" in argv
    assert argv[argv.index("--conf") + 1] == "0.1"
    # 検出結果が無いのに --reuse-detections を付けない（付くと必ず失敗する）
    assert "--reuse-detections" not in runner_mod.build_argv(job, {}, reuse=True)
    # complete が無い古い形式は、cli.py と同じく完了扱い
    with open(job.detections, "w", encoding="utf-8") as f:
        json.dump({"n_frames": 1, "width": 1, "height": 1, "detections": {}}, f)
    assert "--reuse-detections" in runner_mod.build_argv(job, {}, reuse=True)
    with open(job.detections, "w", encoding="utf-8") as f:
        json.dump(
            {"n_frames": 1, "width": 1, "height": 1, "complete": True, "detections": {}}, f
        )
    assert "--reuse-detections" in runner_mod.build_argv(job, {}, reuse=True)

    # 途中保存（complete: false）は再利用できない。渡すと cli.py がエラーで止まる。
    # 止まったところから検出を続ける（--resume）に振り替えること。
    # 足りないぶんを直前フレームの領域で埋める逃げ道は絶対に付けない
    with open(job.detections, "w", encoding="utf-8") as f:
        json.dump(
            {"n_frames": 1, "width": 1, "height": 1, "complete": False, "detections": {}}, f
        )
    argv = runner_mod.build_argv(job, {}, reuse=True)
    assert "--reuse-detections" not in argv, argv
    assert "--resume" in argv, argv
    assert "--allow-short-detections" not in argv, argv

    # 読めない JSON は再利用も再開もできない。パス1をやり直す
    with open(job.detections, "w", encoding="utf-8") as f:
        f.write("{ここで壊れている")
    argv = runner_mod.build_argv(job, {}, reuse=True)
    assert "--reuse-detections" not in argv and "--resume" not in argv, argv
    print("  CLI 引数の組み立て OK（途中保存は --resume に振り替える）")


def wait_finished(srv, jid: str, timeout: float = 120.0) -> str:
    """処理が終わるまで待つ。戻り値は最終の status。"""
    end = time.time() + timeout
    st = ""
    while time.time() < end:
        st = get_json(f"{srv.base}/api/jobs/{jid}?t={TOKEN}")["status"]
        if st in ("done", "failed", "canceled", "interrupted"):
            return st
        time.sleep(0.1)
    raise AssertionError(f"終わりません: status={st}")


def write_fake_detections(job, n_frames: int, width: int, height: int) -> None:
    """検出器を通さずに det.json を置く。

    実際の推論には重みと数分の時間が要る。ここで確かめたいのは
    サブプロセスの起動・進捗の取り出し・出力の生成であって検出精度ではない。

    前半と中盤にだけ検出を置き、あいだと末尾を空ける。全フレーム埋まった
    検出だと「推定のみ」も「未処理」も生まれず、検査キューが空になって
    往復の検証にならない（tests/test_review.py と同じ組み方）。
    """
    dets = {
        str(f): [{"class": CLS, "score": 0.9, "box": [80, 60, 60, 60]}]
        for f in list(range(0, 10)) + list(range(30, 40))
    }
    with open(job.detections, "w", encoding="utf-8") as f:
        json.dump(
            {
                "n_frames": n_frames,
                "width": width,
                "height": height,
                # save_detections が書くものと同じ形にする。これが無いと
                # 「途中保存かどうか」を見分ける経路を素通りしてしまう
                "complete": True,
                "detections": dets,
            },
            f,
        )


def test_run_and_progress_stream():
    """通し。検出を再利用してパス2だけ走らせ、完成品が出るまで見る。"""
    lib = make_lib()
    srv = Server(lib)
    try:
        d = upload(srv.base, sample_video())
        jid = d["id"]
        job = jobs_mod.Library(lib).get(jid)
        write_fake_detections(job, d["n_frames"], d["width"], d["height"])

        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/start?t={TOKEN}",
            {"reuse": True, "settings": {"crf": 28}},
        )
        assert code == 200, r
        assert "--reuse-detections" in r["argv"]

        # 二重起動を弾くこと。同じジョブを2本走らせると出力が壊れる
        code2, _ = post_json(f"{srv.base}/api/jobs/{jid}/start?t={TOKEN}", {"reuse": True})
        assert code2 == 409, code2

        seen_progress = False
        status = None
        for _ in range(600):  # 最大60秒
            snap = get_json(f"{srv.base}/api/jobs/{jid}/progress?t={TOKEN}")
            if (snap.get("progress") or {}).get("pass2"):
                seen_progress = True
            status = snap["status"]
            if status in ("done", "failed", "canceled"):
                break
            time.sleep(0.1)

        detail = get_json(f"{srv.base}/api/jobs/{jid}?t={TOKEN}")
        assert status == "done", f"{status} / {detail.get('error')}"
        assert detail["has_output"], "output.mp4 が無い"
        assert detail["output_size_bytes"] > 0
        assert detail["elapsed_sec"] is not None
        # meta.json にレポートの数字が取り込まれていること
        assert "stats" in detail and detail["stats"], detail["stats"]
        # 素通しの区間数が API に出ること。出ていないと、完成品を受け取る前に
        # 「モザイクが1つも乗っていない区間がある」ことに誰も気づけない
        assert detail["n_uncovered_ranges"] is not None, detail
        assert detail["n_estimated_only_ranges"] is not None, detail
        print(
            f"  通し（パス2のみ）OK: {detail['output_size_bytes']} バイト / "
            f"{detail['elapsed_sec']}秒 / 進捗を拾えた={seen_progress}"
        )

        code, body, _ = request(f"{srv.base}/api/jobs/{jid}/download?t={TOKEN}")
        assert code == 200 and len(body) == detail["output_size_bytes"], code
        assert body[4:8] == b"ftyp", "mp4 として読めない"
        print(f"  完成品のダウンロード OK（{len(body)} バイト）")
    finally:
        srv.close()


def test_sse_stream_sends_snapshot():
    """SSE が現状を1件目として送ること。

    途中で切れても繋ぎ直せば追いつける、というのがこの経路の要件。
    繋いだ瞬間に何も来ない作りだと、切れたあと画面が空のままになる。
    """
    lib = make_lib()
    srv = Server(lib)
    try:
        d = upload(srv.base, sample_video())
        jid = d["id"]
        req = urllib.request.Request(f"{srv.base}/api/jobs/{jid}/events?t={TOKEN}")
        with urllib.request.urlopen(req, timeout=15) as res:
            assert res.headers["Content-Type"].startswith("text/event-stream")
            # 1件ぶん（空行まで）だけ読む。長さを決めて読むと、まだ来ない
            # 次の1件を待って固まる
            lines = []
            while True:
                line = res.readline().decode("utf-8", "replace")
                if not line or line.strip() == "":
                    break
                lines.append(line)
        chunk = "".join(lines)
        assert "event: " in chunk and "data: " in chunk, chunk[:200]
        payload = json.loads(chunk.split("data: ", 1)[1].split("\n", 1)[0])
        assert payload["job"] == jid
        assert payload["status"] == "new"
        print("  SSE の初回スナップショット OK")
    finally:
        srv.close()


def test_reconcile_after_restart():
    """サーバ再起動で「実行中のまま」を残さない。

    ここを直さないと、そのジョブは二度と起動できない見た目になる。
    """
    lib = make_lib()
    srv = Server(lib)
    try:
        d = upload(srv.base, sample_video())
        jid = d["id"]
    finally:
        srv.close()

    L = jobs_mod.Library(lib)
    job = L.get(jid)
    # 存在しない PID を実行中として書き込む（落ちたサーバの残骸を再現）
    job.update(status="running", pid=99999999)
    changed = jobs_mod.reconcile(L)
    assert jid in changed, changed
    assert L.get(jid).status == "interrupted"

    # 完成品が残っていれば完了として扱う。焼き上がってから落ちた場合
    job = L.get(jid)
    with open(job.output, "wb") as f:
        f.write(b"x" * 100)
    job.update(status="running", pid=99999999)
    srv = Server(lib)
    try:
        detail = get_json(f"{srv.base}/api/jobs/{jid}?t={TOKEN}")
        assert detail["status"] == "done", detail["status"]
        print("  再起動後の状態復元 OK（中断 / 完成品ありなら完了）")
    finally:
        srv.close()


def test_no_double_start_across_restart():
    """サーバ再起動をまたいだ二重起動を弾くこと。

    走っているサブプロセスはサーバの再起動では死なない（detached）。
    弾く根拠がプロセス内のレジストリだけだと、再起動した瞬間に同じ
    output.mp4 へ2本が書き込める。meta.json の pid でも確かめること。

    同時に、死んだプロセスの残骸で永久にロックされないことも見る。
    そこを間違えると、落ちたサーバの pid が残っただけでそのジョブは
    二度と起動できなくなる。
    """
    lib = make_lib()
    srv = Server(lib)
    try:
        d = upload(srv.base, sample_video())
        jid = d["id"]
        job = jobs_mod.Library(lib).get(jid)
        write_fake_detections(job, d["n_frames"], d["width"], d["height"])
    finally:
        srv.close()  # ここでレジストリが消える

    # 走りっぱなしのサブプロセスを模す。生きている pid を meta に残す
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        jobs_mod.Library(lib).get(jid).update(
            status="running", pid=sleeper.pid, detached=True
        )
        srv = Server(lib)  # 再起動
        try:
            st = get_json(f"{srv.base}/api/jobs/{jid}?t={TOKEN}")
            assert st["status"] == "running" and st["alive"], st
            code, r = post_json(
                f"{srv.base}/api/jobs/{jid}/start?t={TOKEN}", {"reuse": True}
            )
            assert code == 409, f"二重起動を通した: {code} {r}"
            assert str(sleeper.pid) in r["detail"], r
        finally:
            srv.close()
    finally:
        sleeper.terminate()
        sleeper.wait()

    # 死んだ pid が残っているだけなら起動できること
    jobs_mod.Library(lib).get(jid).update(
        status="running", pid=sleeper.pid, detached=True
    )
    srv = Server(lib)
    try:
        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/start?t={TOKEN}", {"reuse": True}
        )
        assert code == 200, f"死んだ pid の残骸で塞がっている: {code} {r}"
        for _ in range(600):
            s = get_json(f"{srv.base}/api/jobs/{jid}?t={TOKEN}")
            if s["status"] in ("done", "failed", "canceled"):
                break
            time.sleep(0.1)
        print("  再起動をまたいだ二重起動を拒否・死んだ pid では起動できる OK")
    finally:
        srv.close()


# --------------------------------------------------------------------------
# 3. 検査キュー
# --------------------------------------------------------------------------


def prepared_job(lib_dir: str, srv: Server) -> dict:
    """検出結果まで用意したジョブ。検査キューの検証に使う。"""
    d = upload(srv.base, sample_video())
    job = jobs_mod.Library(lib_dir).get(d["id"])
    write_fake_detections(job, d["n_frames"], d["width"], d["height"])
    return d


def test_state_and_queue():
    lib = make_lib()
    srv = Server(lib)
    try:
        d = prepared_job(lib, srv)
        jid = d["id"]
        st = get_json(f"{srv.base}/api/jobs/{jid}/state?light=1&t={TOKEN}")
        for k in ("width", "height", "n_frames", "classes", "default_size", "default_class"):
            assert k in st, f"{k} が無い"
        # light では重い配列を落とす。1時間の動画で 10MB を超える
        assert "regions" not in st and "coverage" not in st
        assert st["width"] == 320 and st["height"] == 240

        full = get_json(f"{srv.base}/api/jobs/{jid}/state?light=0&t={TOKEN}")
        assert len(full["coverage"]) == full["n_frames"]

        q = get_json(f"{srv.base}/api/jobs/{jid}/queue?t={TOKEN}")
        assert q["items"], "キューが空"
        it = q["items"][0]
        for k in ("frame", "reason", "priority", "label", "boxes"):
            assert k in it, f"{k} が無い"
        assert q["progress"]["total"] == len(q["items"])
        # 間隔を変えると枚数が変わること
        q2 = get_json(f"{srv.base}/api/jobs/{jid}/queue?step=2&rebuild=1&t={TOKEN}")
        assert q2["step"] == 2
        print(f"  /state と /queue OK（{len(q['items'])} 枚 -> step2 で {len(q2['items'])} 枚）")
    finally:
        srv.close()


def test_frame_image():
    lib = make_lib()
    srv = Server(lib)
    try:
        import cv2
        import numpy as np

        d = prepared_job(lib, srv)
        jid = d["id"]
        code, body, hdr = request(f"{srv.base}/api/jobs/{jid}/frame?n=5&fmt=png&t={TOKEN}")
        assert code == 200 and hdr["content-type"] == "image/png", (code, hdr)
        img = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
        assert img.shape[1] == 320, img.shape

        code, body, hdr = request(
            f"{srv.base}/api/jobs/{jid}/frame?n=5&fmt=jpg&w=160&v=3&t={TOKEN}"
        )
        assert code == 200 and hdr["content-type"] == "image/jpeg"
        # 世代番号付きなら先読みが効くようにキャッシュを許す
        assert "max-age" in hdr.get("cache-control", ""), hdr.get("cache-control")
        small = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
        assert small.shape[1] == 160, small.shape
        print(f"  /frame の縮小と JPEG 化 OK（{len(body)} バイト）")
    finally:
        srv.close()


def test_mark_roundtrip_and_undo():
    """判定 -> 次のフレーム -> 取り消し の往復。

    実際に corrections.json に落ちて、取り消しで消えることまで見る。
    ここが片道でも壊れると、直したつもりの箇所が焼かれない。
    """
    lib = make_lib()
    srv = Server(lib)
    try:
        d = prepared_job(lib, srv)
        jid = d["id"]
        job = jobs_mod.Library(lib).get(jid)
        q = get_json(f"{srv.base}/api/jobs/{jid}/queue?t={TOKEN}")
        f0 = q["items"][0]["frame"]

        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/mark?t={TOKEN}", {"frame": f0, "verdict": "ok"}
        )
        assert code == 200 and r["added"] == 0, r
        assert r["progress"]["done"] == 1, r["progress"]

        f1 = q["items"][1]["frame"]
        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/mark?t={TOKEN}",
            {"frame": f1, "verdict": "fixed", "x": 0.5, "y": 0.5, "w": 40, "h": 40, "span": 2},
        )
        assert code == 200, r
        assert r["added"] == 5, f"span=2 なら前後2コマで5件のはず: {r['added']}"
        assert r["n_corrections"] == 5
        # 手で置いた矩形が manual として乗っていること
        assert any(b[4] == "x" for b in r["regions"]), r["regions"]

        cs = CorrectionSet.load(job.corrections)
        assert len(cs.items) == 5
        boxes = {c.frame: c.box for c in cs.items}
        assert set(boxes) == set(range(f1 - 2, f1 + 3)), sorted(boxes)
        bx, by, bw, bh = boxes[f1]
        assert (bw, bh) == (40, 40)
        # 中心タップ (0.5, 0.5) が画面中央に来ること
        assert abs(bx + bw / 2 - 160) < 1 and abs(by + bh / 2 - 120) < 1, boxes[f1]

        # 不正な判定は 400。静かに無視すると「押したのに残らない」になる
        code, _ = post_json(
            f"{srv.base}/api/jobs/{jid}/mark?t={TOKEN}", {"frame": f1, "verdict": "maybe"}
        )
        assert code == 400, code
        code, _ = post_json(
            f"{srv.base}/api/jobs/{jid}/mark?t={TOKEN}", {"frame": f1, "verdict": "fixed"}
        )
        assert code == 400, "位置なしの fixed を通してはいけない"

        code, r = post_json(f"{srv.base}/api/jobs/{jid}/undo?t={TOKEN}", {})
        assert code == 200 and r["ok"] and r["removed"] == 5, r
        assert r["n_corrections"] == 0
        assert len(CorrectionSet.load(job.corrections).items) == 0

        code, r = post_json(f"{srv.base}/api/jobs/{jid}/undo?t={TOKEN}", {})
        assert r["ok"] is True and r["removed"] == 0, r  # ok の判定を戻す
        code, r = post_json(f"{srv.base}/api/jobs/{jid}/undo?t={TOKEN}", {})
        assert r["ok"] is False, r

        # 検査の結果が meta に戻っていること（案件ごとの集計に使う）
        meta = jobs_mod.Library(lib).get(jid).meta
        assert "review" in meta, meta.keys()
        print("  判定 -> 次 -> 取り消しの往復 OK（corrections への反映も一致）")
    finally:
        srv.close()


def test_toobig_stacks_remove_and_add_as_pairs():
    """「でかすぎる」が remove と add を隣り合わせの組で積むこと。

    タイムライン画面の取り消し（frontend/src/shared/review-logic.ts の
    correctionsAfterDrop）は、この並びを見て組を割らないようにしている。
    積み方が変わればあちらが黙って壊れるので、ここで並びを固定しておく。

    末尾の add だけを落とすと remove が残り、そのフレームは自動領域も
    手修正も無い完全な素通しになる。それも合わせて示す。
    """
    lib = make_lib()
    srv = Server(lib)
    try:
        d = prepared_job(lib, srv)
        jid = d["id"]
        f = 5  # 検出がある区間（0〜9）
        st = get_json(f"{srv.base}/api/jobs/{jid}/state?light=0&t={TOKEN}")
        assert st["regions"].get(str(f)), "判定前に自動領域が無い"

        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/mark?t={TOKEN}",
            {"frame": f, "verdict": "toobig", "x": 0.5, "y": 0.5, "w": 30, "h": 30, "span": 1},
        )
        assert code == 200, r
        items = get_json(f"{srv.base}/api/jobs/{jid}/corrections?t={TOKEN}")["corrections"]
        want = []
        for n in range(f - 1, f + 2):
            want += [(n, "remove"), (n, "add")]
        assert [(c["frame"], c["kind"]) for c in items] == want, items

        # 末尾1件だけ落とすと素通しになる（画面側が組で落とすべき理由）
        post_json(
            f"{srv.base}/api/jobs/{jid}/corrections?t={TOKEN}", {"corrections": items[:-1]}
        )
        st = get_json(f"{srv.base}/api/jobs/{jid}/state?light=0&t={TOKEN}")
        assert not st["regions"].get(str(f + 1)), "remove だけ残っても素通しにならない?"

        # 組で落とせば自動領域が戻る
        post_json(
            f"{srv.base}/api/jobs/{jid}/corrections?t={TOKEN}", {"corrections": items[:-2]}
        )
        st = get_json(f"{srv.base}/api/jobs/{jid}/state?light=0&t={TOKEN}")
        assert st["regions"].get(str(f + 1)), "組で落としたのに素通しのまま"
        print("  でかすぎる が remove+add を組で積むこと OK（組を割ると素通し）")
    finally:
        srv.close()


def test_mark_rejects_out_of_range_frame():
    """範囲外のフレームは弾くこと。端へ寄せない。

    黙って寄せると、応答は送った番号を返すのに記録は最終フレームに付く。
    判定したはずのコマが未判定のまま残り、触っていないコマに判定が付く。
    どちらも画面には出ないので、検査が一周したという数字だけが嘘になる。
    """
    lib = make_lib()
    srv = Server(lib)
    try:
        d = prepared_job(lib, srv)
        jid = d["id"]
        job = jobs_mod.Library(lib).get(jid)
        n = d["n_frames"]
        for bad in (n, n + 1, 999999, -1):
            code, r = post_json(
                f"{srv.base}/api/jobs/{jid}/mark?t={TOKEN}",
                {"frame": bad, "verdict": "ok"},
            )
            assert code == 400, f"frame={bad} を通した: {code} {r}"
        prog = os.path.splitext(job.corrections)[0] + ".progress.json"
        assert not os.path.exists(prog), "弾いたはずの判定が記録されている"

        # 範囲内は通ること
        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/mark?t={TOKEN}", {"frame": n - 1, "verdict": "ok"}
        )
        assert code == 200 and r["frame"] == n - 1, r
        with open(prog, encoding="utf-8") as f:
            assert json.load(f)["verdicts"] == {str(n - 1): "ok"}
        print(f"  範囲外フレーム（{n}, 999999, -1）の判定を拒否 OK")
    finally:
        srv.close()


def test_corrections_replace():
    lib = make_lib()
    srv = Server(lib)
    try:
        d = prepared_job(lib, srv)
        jid = d["id"]
        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/corrections?t={TOKEN}",
            {"corrections": [
                {"frame": 4, "box": [10, 10, 30, 30], "class": CLS, "kind": "add"}
            ]},
        )
        assert code == 200 and r["n_corrections"] == 1, r
        got = get_json(f"{srv.base}/api/jobs/{jid}/corrections?t={TOKEN}")
        assert got["corrections"][0]["frame"] == 4
        assert got["width"] == 320 and got["height"] == 240
        print("  修正一覧の差し替え OK")
    finally:
        srv.close()


# --------------------------------------------------------------------------
# 4. 手描きモード
# --------------------------------------------------------------------------


def test_hand_draw_and_expand():
    """検出なしで矩形を置き、あいだが補間で埋まること。

    補間は tools/annotations_to_corrections.py の build() をそのまま
    呼んでいる。同じ規則で埋まらないと、コマンドラインで作った
    corrections と画面で作ったものが食い違う。
    """
    lib = make_lib()
    srv = Server(lib)
    try:
        # 検出結果を置かない = 手描きモード
        d = upload(srv.base, sample_video())
        jid = d["id"]
        job = jobs_mod.Library(lib).get(jid)

        st = get_json(f"{srv.base}/api/jobs/{jid}/state?light=1&t={TOKEN}")
        assert st["n_frames"] > 20, st["n_frames"]

        # 正規化タップで置く（端末から来るのはこの形）
        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/annotations?t={TOKEN}",
            {"frame": 10, "x": 0.25, "y": 0.5, "w": 40, "h": 40},
        )
        assert code == 200, r
        assert r["box"][2:] == [40.0, 40.0], r["box"]
        # 動画座標の box を直接渡す経路
        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/annotations?t={TOKEN}",
            {"frame": 20, "box": [180, 100, 40, 40]},
        )
        assert code == 200, r
        # 「ここには無い」。補間をここで打ち切る
        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/annotations?t={TOKEN}",
            {"frame": 30, "absent": True},
        )
        assert code == 200 and len(r["annotations"]) == 3, r

        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/annotations/expand?t={TOKEN}",
            {"max_interp": 20, "hold": 4},
        )
        assert code == 200, r
        assert r["points"] == 2, r
        cs = CorrectionSet.load(job.corrections)
        frames = sorted({c.frame for c in cs.items})
        # 10 と 20 のあいだが埋まっていること
        assert set(range(10, 21)) <= set(frames), frames
        # 30 は「無い」なので、20 の先は hold ぶんで止まる
        assert 30 not in frames, frames
        assert max(frames) < 30, frames
        # 補間が線形であること。frame10 の x=60 と frame20 の x=180 の
        # 中央 120 に、frame15 が来る
        mid = [c for c in cs.items if c.frame == 15][0]
        assert abs(mid.box[0] - 120.0) < 1.0, mid.box
        assert abs(mid.box[1] - 100.0) < 1.0, mid.box
        print(
            f"  手描き -> 補間 OK（打点2個 -> 矩形 {len(cs.items)} 件 / "
            f"frame {min(frames)}〜{max(frames)}）"
        )

        # 打点の上書きと削除
        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/annotations?t={TOKEN}",
            {"frame": 10, "box": [1, 2, 3, 4]},
        )
        pts = [a for a in r["annotations"] if a["frame"] == 10]
        assert len(pts) == 1 and pts[0]["box"] == [1, 2, 3, 4], pts
        code, body, _ = request(
            f"{srv.base}/api/jobs/{jid}/annotations/30?t={TOKEN}", method="DELETE"
        )
        assert code == 200 and len(json.loads(body)["annotations"]) == 2
        print("  打点の上書き・削除 OK")

        # merge を明示で切れば置き換え。編集し直すたびに古い矩形が積み残らない
        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/annotations/expand?t={TOKEN}", {"merge": False}
        )
        cs2 = CorrectionSet.load(job.corrections)
        assert all(c.frame < 30 for c in cs2.items)
        assert len(cs2.items) == r["corrections"]
        print("  merge を切れば置き換えになる OK")
    finally:
        srv.close()


def test_corrections_payload_must_be_a_list():
    """corrections キーの無い本文で全消ししないこと。

    「空の一覧で置き換えろ」と読むと、壊れた本文ひとつで手修正が全部消える。
    判定（.progress.json）は残るので、「塞いだ」と表示されたまま
    塞いだ矩形だけが無い状態、つまりそのフレームは素通しになる。
    """
    lib = make_lib()
    srv = Server(lib)
    try:
        d = prepared_job(lib, srv)
        jid = d["id"]
        job = jobs_mod.Library(lib).get(jid)
        post_json(
            f"{srv.base}/api/jobs/{jid}/mark?t={TOKEN}",
            {"frame": 5, "verdict": "fixed", "x": 0.5, "y": 0.5, "w": 40, "h": 40},
        )
        assert len(CorrectionSet.load(job.corrections).items) == 1

        for bad in ({}, {"foo": "bar"}, {"corrections": None}, {"corrections": {}}):
            code, r = post_json(f"{srv.base}/api/jobs/{jid}/corrections?t={TOKEN}", bad)
            assert code == 400, f"{bad} を通した: {code} {r}"
        assert len(CorrectionSet.load(job.corrections).items) == 1, "本文が壊れているのに消えた"

        # 空配列を明示で送ったときだけ全消しになる
        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/corrections?t={TOKEN}", {"corrections": []}
        )
        assert code == 200 and r["n_corrections"] == 0, r
        print("  corrections キーの無い本文を拒否 OK（明示の空配列だけ通る）")
    finally:
        srv.close()


def test_undo_history_does_not_come_back():
    """一覧を差し替えたあと、履歴がセッションの作り直しで生き返らないこと。

    set_corrections は履歴を捨てるが、捨てたことを保存しないと
    .progress.json から読み戻される。生き返った履歴は差し替え後の一覧では
    別物を指すので、undo が無関係な修正（漏れを塞いだ矩形）を末尾から削る。
    """
    lib = make_lib()
    srv = Server(lib)
    try:
        d = prepared_job(lib, srv)
        jid = d["id"]
        job = jobs_mod.Library(lib).get(jid)
        for f in (5, 35):
            post_json(
                f"{srv.base}/api/jobs/{jid}/mark?t={TOKEN}",
                {"frame": f, "verdict": "fixed", "x": 0.5, "y": 0.5, "w": 40, "h": 40},
            )
        post_json(
            f"{srv.base}/api/jobs/{jid}/corrections?t={TOKEN}",
            {"corrections": [
                {"frame": 20, "box": [10, 10, 30, 30], "class": CLS, "kind": "add"}
            ]},
        )
    finally:
        srv.close()

    srv = Server(lib)  # セッションを開き直す（再起動・追い出し・expand と同じ）
    try:
        q = get_json(f"{srv.base}/api/jobs/{jid}/queue?t={TOKEN}")
        assert q["progress"]["can_undo"] is False, "捨てたはずの履歴が生き返っている"
        before = [(c.frame, c.kind) for c in CorrectionSet.load(job.corrections).items]
        code, r = post_json(f"{srv.base}/api/jobs/{jid}/undo?t={TOKEN}", {})
        after = [(c.frame, c.kind) for c in CorrectionSet.load(job.corrections).items]
        assert r["ok"] is False, r
        assert after == before, f"戻せないはずなのに消えた: {before} -> {after}"
        print("  一覧の差し替え後、履歴が生き返らない OK")
    finally:
        srv.close()


def test_out_of_range_is_rejected_everywhere():
    """範囲外のフレーム番号を、どの入口も端へ寄せないこと。

    寄せると、狙ったフレームは素通しのまま別のフレームに打点や判定が乗る。
    /frame だけ絵が返ると「絵は出るのに判定は付かない」食い違いにもなる。
    """
    lib = make_lib()
    srv = Server(lib)
    try:
        d = prepared_job(lib, srv)
        jid = d["id"]
        n = d["n_frames"]
        for bad in (n, 5060, -20):
            code, r = post_json(
                f"{srv.base}/api/jobs/{jid}/annotations?t={TOKEN}",
                {"frame": bad, "box": [10, 10, 30, 30]},
            )
            assert code == 400, f"打点 frame={bad} を通した: {code} {r}"
            code, _r = post_json(
                f"{srv.base}/api/jobs/{jid}/annotations?t={TOKEN}",
                {"frame": bad, "absent": True},
            )
            assert code == 400, f"「ここには無い」 frame={bad} を通した: {code}"
            code, _b, _h = request(f"{srv.base}/api/jobs/{jid}/frame?n={bad}&t={TOKEN}")
            assert code == 404, f"/frame n={bad} が {code} を返した"
        assert get_json(f"{srv.base}/api/jobs/{jid}/annotations?t={TOKEN}")["annotations"] == []

        # 範囲内は通ること
        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/annotations?t={TOKEN}",
            {"frame": n - 1, "box": [10, 10, 30, 30]},
        )
        assert code == 200 and r["frame"] == n - 1, r
        assert request(f"{srv.base}/api/jobs/{jid}/frame?n={n - 1}&t={TOKEN}")[0] == 200
        print(f"  範囲外（{n}, 5060, -20）を打点・/frame とも拒否 OK")
    finally:
        srv.close()


def test_settings_can_be_cleared():
    """一度入れた設定を画面から外せること。

    保存済み設定に混ぜるだけだと、試写で入れた --limit-frames が残り続ける。
    素材の一部しか処理していない完成品が「完了」として出てくる。
    """
    lib = make_lib()
    srv = Server(lib)
    try:
        d = upload(srv.base, sample_video())
        jid = d["id"]
        job = jobs_mod.Library(lib).get(jid)
        write_fake_detections(job, d["n_frames"], d["width"], d["height"])

        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/start?t={TOKEN}",
            {"reuse": True, "settings": {"limit_frames": 30, "block": 8}},
        )
        assert code == 200 and "--limit-frames" in r["argv"], r["argv"]
        wait_finished(srv, jid)

        # 画面が「指定しない」を null で送る
        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/start?t={TOKEN}",
            {"reuse": True, "settings": {"limit_frames": None, "block": None}},
        )
        assert code == 200, r
        assert "--limit-frames" not in r["argv"], r["argv"]
        assert "--block" not in r["argv"], r["argv"]
        wait_finished(srv, jid)
        meta = jobs_mod.Library(lib).get(jid).meta
        assert "limit_frames" not in (meta.get("settings") or {}), meta.get("settings")
        print("  設定の解除 OK（--limit-frames が残らない）")
    finally:
        srv.close()


def test_annotations_survive_parallel_writes():
    """同時に置いた打点が消えないこと。

    打点の保存は「読んで・足して・書く」なので、鍵が無いと片方が消える。
    しかも消えたほうも 200 を返すので、画面からは気づけない。
    """
    from concurrent.futures import ThreadPoolExecutor

    lib = make_lib()
    srv = Server(lib)
    try:
        d = prepared_job(lib, srv)
        jid = d["id"]
        frames = list(range(0, 40))

        def put(f):
            code, _ = post_json(
                f"{srv.base}/api/jobs/{jid}/annotations?t={TOKEN}",
                {"frame": f, "box": [10, 10, 30, 30]},
            )
            return code

        with ThreadPoolExecutor(max_workers=12) as ex:
            codes = list(ex.map(put, frames))
        assert set(codes) == {200}, sorted(set(codes))
        got = get_json(f"{srv.base}/api/jobs/{jid}/annotations?t={TOKEN}")["annotations"]
        assert sorted(int(a["frame"]) for a in got) == frames, len(got)
        print(f"  同時に置いた打点 {len(frames)} 件が全部残る OK")
    finally:
        srv.close()


def test_expand_keeps_review_work():
    """手描きの展開が、検査キューで積んだ修正を消さないこと。

    2つある。

    1. merge を送らなかったときの既定。置き換えを既定にすると、検査キューで
       塞いだ矩形が手描きを一度使うだけで全部消える。判定は「塞いだ」と
       表示されたまま、塞いだ矩形だけが無い状態になる（＝そのフレームは素通し）
    2. 展開後の「ひとつ戻す」。展開は corrections.json を直に書き換えるので、
       .progress.json に残った履歴はもう別物を指す。捨てておかないと、
       次の undo が手描きで置いた矩形を末尾から削る
    """
    lib = make_lib()
    srv = Server(lib)
    try:
        d = prepared_job(lib, srv)
        jid = d["id"]
        job = jobs_mod.Library(lib).get(jid)

        # 検査キューで1コマ塞ぐ
        f = 35  # 検出がある区間（30〜39）
        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/mark?t={TOKEN}",
            {"frame": f, "verdict": "fixed", "x": 0.5, "y": 0.5, "w": 40, "h": 40},
        )
        assert code == 200 and r["added"] == 1, r

        # 手描きで別の場所に打点を置いて展開する（merge を送らない）
        post_json(
            f"{srv.base}/api/jobs/{jid}/annotations?t={TOKEN}",
            {"frame": 20, "box": [10, 10, 30, 30]},
        )
        code, r = post_json(f"{srv.base}/api/jobs/{jid}/annotations/expand?t={TOKEN}", {})
        assert code == 200, r
        items = CorrectionSet.load(job.corrections).items
        assert any(c.frame == f for c in items), "検査キューで塞いだ矩形が消えた"

        # 展開したあとの「ひとつ戻す」が、別物を消さないこと
        before = [(c.frame, c.kind) for c in items]
        code, r = post_json(f"{srv.base}/api/jobs/{jid}/undo?t={TOKEN}", {})
        assert code == 200, r
        after = [(c.frame, c.kind) for c in CorrectionSet.load(job.corrections).items]
        assert after == before, f"戻せる履歴が無いのに修正が消えた: {before} -> {after}"
        assert r["ok"] is False, r  # 履歴は展開で捨てられている
        print("  展開が検査キューの修正を消さない・展開後の undo が暴走しない OK")
    finally:
        srv.close()


# --------------------------------------------------------------------------
# 5. データセット書き出し
# --------------------------------------------------------------------------


def test_dataset_export():
    lib = make_lib()
    srv = Server(lib)
    try:
        d = prepared_job(lib, srv)
        jid = d["id"]
        job = jobs_mod.Library(lib).get(jid)
        post_json(
            f"{srv.base}/api/jobs/{jid}/mark?t={TOKEN}",
            {"frame": 12, "verdict": "fixed", "x": 0.5, "y": 0.5, "w": 40, "h": 40, "span": 0},
        )
        code, r = post_json(f"{srv.base}/api/jobs/{jid}/dataset?t={TOKEN}", {})
        assert code == 200 and r["frames"] == 1, r
        img = os.path.join(job.dataset, "images", "000012.png")
        lbl = os.path.join(job.dataset, "labels", "000012.txt")
        assert os.path.exists(img) and os.path.exists(lbl), os.listdir(job.dataset)
        lines = open(lbl, encoding="utf-8").read().strip().split("\n")
        assert lines, "ラベルが空"
        cid, cx, cy, bw, bh = lines[0].split()
        # 画面中央に置いた 40x40。YOLO 正規化で 0.5, 0.5, 0.125, 0.1667
        assert abs(float(cx) - 0.5) < 0.01 and abs(float(cy) - 0.5) < 0.01, lines[0]
        assert abs(float(bw) - 40 / 320) < 0.01 and abs(float(bh) - 40 / 240) < 0.01
        assert os.path.exists(os.path.join(job.dataset, "classes.txt"))
        assert os.path.exists(os.path.join(job.dataset, "dataset.yaml"))

        info = get_json(f"{srv.base}/api/jobs/{jid}/dataset?t={TOKEN}")
        assert info["exists"] and info["frames"] == 1, info
        print(f"  データセット書き出し OK（{len(lines)} ラベル / 座標一致）")
    finally:
        srv.close()


def test_download_missing_is_404():
    lib = make_lib()
    srv = Server(lib)
    try:
        d = upload(srv.base, sample_video())
        code, _, _ = request(f"{srv.base}/api/jobs/{d['id']}/download?t={TOKEN}")
        assert code == 404, code
        code, _, _ = request(f"{srv.base}/api/jobs/20990101-000000-abcd/download?t={TOKEN}")
        assert code == 404, code
        print("  完成品が無いときの 404 OK")
    finally:
        srv.close()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"{len(tests)} 件のテストを実行\n")
    failed = 0
    for t in tests:
        t0 = time.time()
        try:
            t()
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            import traceback

            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
        else:
            print(f"       ({t.__name__} {time.time() - t0:.1f}s)")
    print(f"\n{'すべて通過' if failed == 0 else f'{failed} 件失敗'}")
    sys.exit(1 if failed else 0)
