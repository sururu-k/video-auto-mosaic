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

from automosaic import video as video_mod  # noqa: E402
from automosaic import review as review_mod  # noqa: E402
from automosaic.corrections import Correction, CorrectionSet  # noqa: E402
from automosaic.webapp import jobs as jobs_mod  # noqa: E402
from automosaic.webapp import proxy as proxy_mod  # noqa: E402
from automosaic.webapp import runner as runner_mod  # noqa: E402
from automosaic.webapp import session as session_mod  # noqa: E402
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


def wait_proxy(srv, jid: str, timeout: float = 60.0) -> dict:
    """プロキシの生成が終わる（done か failed になる）まで待つ。"""
    end = time.time() + timeout
    detail = {}
    while time.time() < end:
        detail = get_json(f"{srv.base}/api/jobs/{jid}?t={TOKEN}")
        if detail.get("proxy_status") in ("done", "failed"):
            return detail
        time.sleep(0.1)
    raise AssertionError(f"プロキシの生成が終わりません: {detail}")


def test_proxy_generated_after_done_and_served_with_range():
    """パス2完了後にプロキシが自動で作られ、Range 付きで配信されること。

    issue #18 の核心である「プロキシのフレーム数が output.mp4 と一致する」を、
    ここでは job.meta が言っている数字を信じず、ffprobe で両方を独立に数えて
    突き合わせる。
    """
    lib = make_lib()
    srv = Server(lib)
    try:
        d = upload(srv.base, sample_video())
        jid = d["id"]
        job = jobs_mod.Library(lib).get(jid)
        write_fake_detections(job, d["n_frames"], d["width"], d["height"])

        # 未完了のあいだは「完成品が無い」で 404 になり、「作れなかった」と
        # 同じ顔をしないこと
        code, body, _ = request(f"{srv.base}/api/jobs/{jid}/proxy?t={TOKEN}")
        assert code == 404, (code, body[:200])

        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/start?t={TOKEN}", {"reuse": True}
        )
        assert code == 200, r
        status = wait_finished(srv, jid)
        assert status == "done", status

        detail = wait_proxy(srv, jid)
        assert detail["proxy_status"] == "done", detail
        assert detail["has_proxy"] is True
        assert detail["proxy_size_bytes"] > 0
        print(
            f"  プロキシ生成 OK: {detail['proxy_size_bytes']} バイト"
            f"（元 output.mp4 {detail['output_size_bytes']} バイト）"
        )

        # 実体を取得できること・mp4 として読めること
        code, body, hdr = request(f"{srv.base}/api/jobs/{jid}/proxy?t={TOKEN}")
        assert code == 200, code
        assert body[4:8] == b"ftyp", "mp4 として読めない"
        assert body == open(job.proxy, "rb").read(), "配信された中身がファイルと違う"

        # Range が効くこと（/video と同じ FileResponse 経路）
        code, partial, hdr = request(
            f"{srv.base}/api/jobs/{jid}/proxy?t={TOKEN}",
            headers={"Range": "bytes=0-99"},
        )
        assert code == 206, (code, hdr)
        assert len(partial) == 100, len(partial)
        assert partial == body[:100]

        # フレーム数の一致を、job.meta を信じずに ffprobe で独立に検査する
        n_output = video_mod.probe(job.output).nb_frames
        n_proxy = video_mod.probe(job.proxy).nb_frames
        assert n_output is not None and n_proxy is not None
        assert n_output == n_proxy, (
            f"output.mp4 と proxy.mp4 のフレーム数が違う: {n_output} != {n_proxy}"
        )
        # 全デコードでも一致することを二重に確かめる（ヘッダの nb_frames が
        # 実体と食い違っていないか。issue #18 の完了条件そのもの）
        def decode_count(path: str) -> int:
            # video_mod._require と同じ解決を使う。winget 版 ffprobe は
            # 素の PATH に無いことがあり、テストプロセス自身の PATH に
            # 依存させると環境差で落ちる
            ffprobe = video_mod._require("ffprobe")
            return int(
                subprocess.run(
                    [
                        ffprobe, "-v", "error", "-select_streams", "v:0",
                        "-count_frames", "-show_entries", "stream=nb_read_frames",
                        "-of", "default=nw=1:nk=1", path,
                    ],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()
            )

        assert decode_count(job.output) == decode_count(job.proxy) == n_output, (
            "全デコードで数えたフレーム数が一致しない"
        )
        print(f"  フレーム数一致 OK（output={n_output} / proxy={n_proxy}、全デコードでも一致）")

        # 原画からは作っていないこと（間接確認）: 解像度が長辺640に落ちている。
        # 原画のプロキシが残っていたら「モザイク前を端末に残さない」前提が壊れる
        proxy_info = video_mod.probe(job.proxy)
        assert max(proxy_info.width, proxy_info.height) <= 640, proxy_info

        # ジョブごと消せば一緒に消えること
        code, _, _ = request(
            f"{srv.base}/api/jobs/{jid}?t={TOKEN}", method="DELETE"
        )
        assert code == 200, code
        assert not os.path.exists(job.proxy), "DELETE してもプロキシが残っている"
        print("  DELETE でプロキシも一緒に消える OK")
    finally:
        srv.close()


def test_proxy_frame_mismatch_is_rejected():
    """フレーム数が1つでもずれたら「失敗」にすること（黙って公開しない）。

    RULES.md 2 に従い、この検査を一時的に外すと実際に落ちることを
    ここで確かめる: video.nb_frames を差し替えて output 側と proxy 側で
    違う値を返させ、_run が status=failed にして proxy.mp4 を削除する
    ことを見る。差し替えを戻せば同じジョブが今度は成功することも見て、
    「失敗記録が残っているだけなら再試行する」ことも確かめる。
    """
    lib = make_lib()
    srv = Server(lib)
    try:
        d = upload(srv.base, sample_video())
        jid = d["id"]
        job = jobs_mod.Library(lib).get(jid)
        write_fake_detections(job, d["n_frames"], d["width"], d["height"])
        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/start?t={TOKEN}", {"reuse": True}
        )
        assert code == 200, r
        assert wait_finished(srv, jid) == "done"
        # 最初の自動生成が終わる（成功する）のを、サーバ越しに確認してから閉じる。
        # ここを待たずに閉じると、「まだ始まってすらいない」瞬間に
        # is_generating() を見て素通りするレースになる
        detail = wait_proxy(srv, jid)
        assert detail["proxy_status"] == "done", detail
    finally:
        srv.close()

    L = jobs_mod.Library(lib)
    job = L.get(jid)
    assert job.meta.get("proxy", {}).get("status") == "done", job.meta.get("proxy")
    assert os.path.exists(job.proxy)

    real_nb_frames = video_mod.nb_frames

    def lying_nb_frames(path: str):
        n = real_nb_frames(path)
        # output.mp4 に対しては本当の値、proxy.mp4 に対しては1ずらした値を返す。
        # 「本当はズレていないのに検査が誤爆する」側を混ぜないよう、
        # ずらすのは常にプロキシ側だけにする
        if os.path.basename(path) == "proxy.mp4":
            return (n or 0) + 1
        return n

    video_mod.nb_frames = lying_nb_frames
    try:
        # 生成し直す。既に status=done かつファイルがあるので、まず消して
        # ensure_started が「未生成」経路を通るようにする
        os.remove(job.proxy)
        job.update(proxy=None)
        proxy_mod.ensure_started(job)
        end = time.time() + 30
        while time.time() < end and proxy_mod.is_generating(jid):
            time.sleep(0.1)
        failed_job = L.get(jid)
        p = failed_job.meta.get("proxy") or {}
        assert p.get("status") == "failed", p
        assert "フレーム数" in (p.get("error") or ""), p
        assert not os.path.exists(failed_job.proxy), "失敗したのに proxy.mp4 が残っている"
        print(f"  フレーム数不一致を検出して失敗にする OK: {p.get('error')}")
    finally:
        video_mod.nb_frames = real_nb_frames

    # 差し替えを戻せば、同じジョブが再試行で成功すること
    # （失敗記録が残っているだけなら ensure_started がもう一度試す）
    retry_job = L.get(jid)
    proxy_mod.ensure_started(retry_job)
    end = time.time() + 30
    while time.time() < end and proxy_mod.is_generating(jid):
        time.sleep(0.1)
    recovered = L.get(jid)
    p = recovered.meta.get("proxy") or {}
    assert p.get("status") == "done", p
    assert os.path.exists(recovered.proxy)
    print("  検査を元に戻すと同じジョブが再試行で成功する OK")


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


def test_pid_reuse_does_not_permanently_lock_job():
    """meta.json の pid が無関係な生きたプロセスと一致しても、永久に固まらない。

    OS が pid を再利用すると、meta.json に残った pid はそのジョブとは
    無関係な、たまたま生きている別プロセスを指すようになる。pid の生死
    だけで判定すると running_pid() が「まだ実行中」と答え続け、start も
    cancel も拒否されたまま UI から解除する手段が無くなる（issue #44）。

    起動時刻（pid_started_ticks、JobRunner.start が記録）が実際の起動時刻と
    食い違う pid は「別プロセスに入れ替わった」とみなし、走っていない
    扱いにする。ジョブ画面を開いた時点の settle() で自然に running から
    抜け、起動もやり直せることを確かめる。
    """
    lib = make_lib()
    srv = Server(lib)
    try:
        d = upload(srv.base, sample_video())
        jid = d["id"]
        job = jobs_mod.Library(lib).get(jid)
        write_fake_detections(job, d["n_frames"], d["width"], d["height"])
    finally:
        srv.close()

    # このジョブとは無関係な、常に生きている「他人の」プロセスを用意する
    impostor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        real_ticks = jobs_mod.process_creation_ticks(impostor.pid)
        assert real_ticks is not None, "起動時刻を取得できない環境（このテストは対象外）"

        # 「昔このジョブを処理していたプロセスの起動時刻」として、いま
        # impostor が実際に起動した時刻とは異なる値を記録する。OS が
        # pid を再利用した状態を模す（本物の起動時刻とはまず一致しない）
        jobs_mod.Library(lib).get(jid).update(
            status="running",
            pid=impostor.pid,
            pid_started_ticks=real_ticks - 10_000_000_000,
            detached=True,
        )

        srv = Server(lib)  # レジストリを持たない新しいサーバから見る
        try:
            st = get_json(f"{srv.base}/api/jobs/{jid}?t={TOKEN}")
            assert st["status"] != "running", (
                f"pid 再利用された無関係なプロセスを本人と誤認し、"
                f"running のまま固まった: {st}"
            )

            code, r = post_json(
                f"{srv.base}/api/jobs/{jid}/start?t={TOKEN}", {"reuse": True}
            )
            assert code == 200, f"pid 再利用で起動不能のまま固まった: {code} {r}"
            for _ in range(600):
                s = get_json(f"{srv.base}/api/jobs/{jid}?t={TOKEN}")
                if s["status"] in ("done", "failed", "canceled"):
                    break
                time.sleep(0.1)
            print(
                "  pid 再利用された無関係なプロセスを本人と誤認せず、"
                "起動をやり直せる OK"
            )
        finally:
            srv.close()
    finally:
        impostor.terminate()
        impostor.wait()


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


def test_api_frame_not_blocked_by_correction_recompute():
    """POST /corrections が recompute() で止まっている間も /frame が動くこと。

    api_frame は以前 s.lock を取っていたので、手修正の保存中は
    フレーム画像が1本も出なかった（issue #25）。実測の「初回構築 5.2秒」と
    同じ形（重い計算の最中）を作るため、review.process を一時的に遅くする
    （遅延の注入自体は PR #45 の TOCTOU 再現と同じ手法。ここでは実際の
    HTTP 経由の並行アクセスで、固まらないこと・壊れた画像を返さないこと・
    recompute 完了後は必ず新しい絵になることを確かめる）。
    """
    import cv2
    import numpy as np
    from concurrent.futures import ThreadPoolExecutor

    lib = make_lib()
    srv = Server(lib)
    orig_process = review_mod.process
    try:
        d = prepared_job(lib, srv)
        jid = d["id"]

        # frame 20 は write_fake_detections が検出を置かない区間（0-9, 30-39
        # にしか置いていない）ので、修正前は素通しの絵のはず
        code, before, _ = request(f"{srv.base}/api/jobs/{jid}/frame?n=20&fmt=png&t={TOKEN}")
        assert code == 200, code

        started = threading.Event()
        release = threading.Event()

        def slow_process(*a, **kw):
            started.set()
            release.wait(timeout=5)
            return orig_process(*a, **kw)

        review_mod.process = slow_process
        try:
            result = {}

            def do_post():
                code, r = post_json(
                    f"{srv.base}/api/jobs/{jid}/corrections?t={TOKEN}",
                    {"corrections": [
                        {"frame": 20, "box": [40, 40, 80, 80], "class": CLS, "kind": "add"}
                    ]},
                )
                result["code"] = code
                result["body"] = r

            th = threading.Thread(target=do_post)
            t0 = time.monotonic()
            th.start()
            assert started.wait(timeout=5), "recompute が呼ばれなかった"

            frame_times: list[float] = []
            frame_codes: list[int] = []

            def hit_frame():
                t1 = time.monotonic()
                code, body, _ = request(
                    f"{srv.base}/api/jobs/{jid}/frame?n=5&fmt=png&t={TOKEN}"
                )
                frame_times.append(time.monotonic() - t1)
                frame_codes.append(code)
                img = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
                assert img is not None and img.shape[1] == 320, "壊れた画像が返った"

            with ThreadPoolExecutor(max_workers=10) as ex:
                list(ex.map(lambda _: hit_frame(), range(10)))
            # recompute はまだ release.wait() で足止めされている。ここまでの
            # 経過が短ければ、/frame は recompute の完了を待たずに返っている
            during_elapsed = time.monotonic() - t0

            release.set()
            th.join(timeout=10)
        finally:
            review_mod.process = orig_process

        assert result.get("code") == 200, result
        assert all(c == 200 for c in frame_codes), frame_codes
        assert during_elapsed < 3.0, (
            f"/frame が recompute の完了を待っている疑い: {during_elapsed:.2f}s "
            f"(release.wait のタイムアウトは 5s)"
        )
        print(
            f"  recompute 中でも /frame 10本が固まらず・壊れず返る OK"
            f"（経過 {during_elapsed:.2f}s、個々 {min(frame_times):.3f}"
            f"〜{max(frame_times):.3f}s）"
        )

        # recompute が完了したあとは、必ず新しい領域を反映した絵を返すこと。
        # 「直したのに直っていない絵」を返すのは漏れる方向の壊れ方（RULES 0）
        code, after, _ = request(f"{srv.base}/api/jobs/{jid}/frame?n=20&fmt=png&t={TOKEN}")
        assert code == 200
        assert after != before, "recompute 後なのに古い絵のまま"
        print("  recompute 完了後の /frame は新しい領域を反映する OK")
    finally:
        review_mod.process = orig_process
        srv.close()


def test_session_cache_build_does_not_block_other_jobs():
    """あるジョブのセッション構築中、無関係なジョブの get() が止まらないこと。

    SessionCache.get() は元々 self._lock を構築（session_for_job、実測
    5.2秒/32,000フレーム）の間ずっと持っていたので、無関係なジョブの
    get() まで待たされていた（issue #25）。session_for_job を一時的に
    遅くして、実測と同じ形の「構築中」を作る。
    同じジョブへ同時に来た2件の get() が二重に構築しない
    （後発が先発の完了を待って使い回す）ことも合わせて確かめる。
    """
    lib_dir = make_lib()
    lib = jobs_mod.Library(lib_dir)

    job_slow = lib.create("slow")
    job_fast = lib.create("fast")
    src = sample_video()
    for j in (job_slow, job_fast):
        shutil.copyfile(src, j.source)

    cache = session_mod.SessionCache()
    orig_build = session_mod.session_for_job
    started = threading.Event()
    release = threading.Event()
    build_count = {"slow": 0}

    def patched(job, **overrides):
        if job.id == job_slow.id:
            build_count["slow"] += 1
            started.set()
            release.wait(timeout=10)
        return orig_build(job, **overrides)

    session_mod.session_for_job = patched
    try:
        result: dict = {}

        def get_slow():
            result["slow"] = cache.get(job_slow)

        def get_slow2():
            result["slow2"] = cache.get(job_slow)

        th = threading.Thread(target=get_slow)
        th.start()
        assert started.wait(timeout=10), "job_slow の構築が始まらなかった"

        # 構築中に2件目の要求が来ても、二重に構築せず先発の完了を待つこと
        th2 = threading.Thread(target=get_slow2)
        th2.start()
        time.sleep(0.2)  # th2 が _building の待ちに入るのを待つ

        # job_slow はまだ release.wait() で止まっている。無関係な job_fast の
        # get() がここで止まらずに返ることを確かめる
        t1 = time.monotonic()
        s_fast = cache.get(job_fast)
        fast_elapsed = time.monotonic() - t1

        release.set()
        th.join(timeout=10)
        th2.join(timeout=10)
    finally:
        session_mod.session_for_job = orig_build
        cache.close_all()

    assert s_fast is not None
    assert fast_elapsed < 1.0, (
        f"無関係なジョブの get() が構築中の別ジョブに巻き込まれて止まった: "
        f"{fast_elapsed:.2f}s"
    )
    assert result.get("slow") is not None and result.get("slow2") is not None
    assert result["slow"] is result["slow2"], "同時要求が二重に別のセッションを作った"
    assert build_count["slow"] == 1, f"二重に構築した: {build_count['slow']} 回"
    print(
        f"  構築中の別ジョブが get() を止めない OK（fast側 {fast_elapsed:.3f}s）、"
        f"同時要求は二重構築しない OK（{build_count['slow']} 回）"
    )


def test_session_cache_get_no_toctou_under_tight_switching():
    """SessionCache.get() の _building.pop/ev.set() と self._items[job.id]=new_s の間に
    ロック解放の隙間があると、目覚めた待機スレッドが「まだキャッシュに無い・building 印も
    消えている」を見て自分も構築側に回り、同じジョブを二重に構築してしまう（TOCTOU）。

    通常のスイッチ間隔ではまず発火しない窓なので、sys.setswitchinterval で頻繁に
    スレッド切り替えを起こして突く。他のテストに影響しないよう、必ず元に戻す。
    """
    class DummyJob:
        def __init__(self, jid: str) -> None:
            self.id = jid

    n_trials = 150
    n_threads = 8
    dup_trials = 0
    max_dup = 0

    orig_switch = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for t in range(n_trials):
            cache = session_mod.SessionCache()
            dummy_job = DummyJob(f"toctou-{t}")
            build_count = {"n": 0}
            lock = threading.Lock()

            def fake_build(j, **overrides):
                with lock:
                    build_count["n"] += 1
                return object()

            orig_build = session_mod.session_for_job
            session_mod.session_for_job = fake_build
            try:
                barrier = threading.Barrier(n_threads)

                def worker() -> None:
                    barrier.wait()
                    cache.get(dummy_job)

                threads = [threading.Thread(target=worker) for _ in range(n_threads)]
                for th in threads:
                    th.start()
                for th in threads:
                    th.join(timeout=10)
            finally:
                session_mod.session_for_job = orig_build
            cache.close_all()

            if build_count["n"] > 1:
                dup_trials += 1
                max_dup = max(max_dup, build_count["n"])
    finally:
        sys.setswitchinterval(orig_switch)

    assert dup_trials == 0, (
        f"SessionCache.get() が同じジョブを二重に構築した: "
        f"{dup_trials}/{n_trials} 試行、最大 {max_dup} 重"
    )
    print(
        f"  setswitchinterval(1e-6) 下 {n_trials} 試行・{n_threads} スレッドで"
        f" 二重構築 0 件 OK"
    )


def test_review_session_uses_job_settings():
    """レビューが焼き込みと同じ mode/block/classes で絵を作ること（issue #15）。

    直す前は app.py の get_session() が sessions.get(job) しか呼ばず、job.meta の
    settings を1つも渡していなかった。black で焼いたジョブを常にモザイク
    （pixelize）で見せ、block を大きくしても常に自動サイズ、
    classes=conservative で焼いても COVERED 系の領域がレビュー画面から
    丸ごと消えていた（漏れる方向の壊れ方）。
    """
    from automosaic.detector import CONSERVATIVE_CLASSES, DEFAULT_CLASSES
    import cv2
    import numpy as np

    lib = make_lib()
    srv = Server(lib)
    try:
        d = upload(srv.base, sample_video())
        jid = d["id"]
        job = jobs_mod.Library(lib).get(jid)
        # ANUS_COVERED は default クラスに無く、conservative にだけ含まれる。
        # classes の絞り込みが実際に効くかどうかを、この箱1つで確かめられる
        box = [40, 30, 30, 30]
        dets = {
            str(f): [{"class": "ANUS_COVERED", "score": 0.9, "box": box}]
            for f in range(d["n_frames"])
        }
        with open(job.detections, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "n_frames": d["n_frames"], "width": d["width"], "height": d["height"],
                    "complete": True, "detections": dets,
                },
                f,
            )
        cx, cy = box[0] + box[2] // 2, box[1] + box[3] // 2

        # -- settings 無し（既定）: block は自動、classes は default。
        #    ANUS_COVERED は default に無いので、この箱は塗られない
        st0 = get_json(f"{srv.base}/api/jobs/{jid}/state?light=1&t={TOKEN}")
        assert st0["block"] != 40, st0
        assert set(st0["classes"]) == set(DEFAULT_CLASSES), st0["classes"]

        code, body, _ = request(f"{srv.base}/api/jobs/{jid}/frame?n=3&fmt=png&t={TOKEN}")
        assert code == 200, (code, body)
        before = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
        px_before = before[cy, cx].tolist()
        assert max(px_before) > 40, (
            f"前提が崩れている: settings 無しなのに箱の中心が既に暗い {px_before}"
        )

        # -- ジョブの meta.settings に mode=black / block=40 / classes=conservative
        job.update(settings={"mode": "black", "block": "40", "classes": "conservative"})

        st1 = get_json(f"{srv.base}/api/jobs/{jid}/state?light=1&t={TOKEN}")
        assert st1["block"] == 40, st1
        assert set(st1["classes"]) == set(CONSERVATIVE_CLASSES), st1["classes"]

        code, body, _ = request(f"{srv.base}/api/jobs/{jid}/frame?n=3&fmt=png&t={TOKEN}")
        assert code == 200, (code, body)
        after = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
        px_after = after[cy, cx].tolist()
        assert max(px_after) <= 5, f"mode=black のはずが黒くなっていない: {px_after}"
        print(
            f"  settings 反映 OK: block {st0['block']}->{st1['block']}, "
            f"classes {sorted(st0['classes'])}->{sorted(st1['classes'])}, "
            f"箱の中心の画素 {px_before}->{px_after}"
        )

        # -- 壊れた値は 400 で止める。黙って無視するのも、そのまま渡して
        #    原因不明の例外にするのも良くない（RULES.md 0）
        job.update(settings={"mode": "black", "block": "abc", "classes": "conservative"})
        code, body, _ = request(f"{srv.base}/api/jobs/{jid}/state?light=1&t={TOKEN}")
        assert code == 400, (code, body)
        print(f"  壊れた block 値は 400 で止まる（{body[:100]!r}）")

        # -- レビューの絵に関係ない項目（検出・最終エンコードにしか効かない）が
        #    壊れていても、レビューは開けること。review.py の argparse は
        #    infer_size / limit_frames を知らないので対象外になる
        job.update(settings={
            "mode": "black", "block": "40", "classes": "conservative",
            "infer_size": "not-a-number", "limit_frames": "not-a-number",
        })
        code, body, _ = request(f"{srv.base}/api/jobs/{jid}/state?light=1&t={TOKEN}")
        assert code == 200, (code, body)
        print("  レビューに関係ない項目が壊れていてもレビューは開ける OK")
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


def test_mark_interval_roundtrip_via_webapp():
    """区間の両端（issue #46）が /mark から通り、あいだが補間されること。

    フロントエンド（review.tsx の I / O キー）が送る形をそのまま模す。
    start_frame/start_x/start_y が付くと ReviewSession.mark_interval() に
    回る（review.py:api_mark）。

    区間は 15〜25（write_fake_detections が検出を置く 0-9 / 30-39 の
    どちらからも離れた帯）を使う。検出の近くだと RULES.md 0 の envelope
    （既存の自動検出領域を包む安全側寄せ）が効いて、見たい「複製ではなく
    補間になっている」座標の違いが埋もれてしまう（envelope 自体は
    tests/test_review.py の test_mark_interval_envelopes_existing_detection_mid_span
    で別途見ている）。
    """
    lib = make_lib()
    srv = Server(lib)
    try:
        d = prepared_job(lib, srv)
        jid = d["id"]

        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/mark?t={TOKEN}",
            {
                "frame": 25, "verdict": "fixed",
                "x": 0.9, "y": 0.9, "w": 20, "h": 20,
                "start_frame": 15, "start_x": 0.1, "start_y": 0.1,
                "start_w": 20, "start_h": 20,
            },
        )
        assert code == 200, r
        assert r["added"] == 11, r  # frame 15..25

        items = get_json(f"{srv.base}/api/jobs/{jid}/corrections?t={TOKEN}")["corrections"]
        assert len(items) == 11, items
        by_frame = {c["frame"]: c["box"] for c in items}
        assert set(by_frame) == set(range(15, 26)), sorted(by_frame)
        # 複製ではなく補間になっていること。webapp の既定設定は bridge_max が
        # 広く、この動画では 15〜25 のあいだにも自動検出の推定被覆が及ぶため
        # （write_fake_detections の 0-9/30-39 が bridge で繋がる）、RULES.md 0
        # の envelope が効いて厳密な単調増加にはならない区間がある。それでも
        # 「複製（同じ矩形の繰り返し）ではない」ことは、矩形が1種類に潰れて
        # いないことと、両端がはっきり違うことで確かめる。envelope 自体の
        # 単調性は tests/test_review.py（自動検出の無いセッション）で見ている
        boxes = [tuple(c["box"]) for c in items]
        assert len(set(boxes)) > 1, "全フレーム同じ矩形＝複製になっている"
        assert by_frame[15] != by_frame[25], by_frame
        assert by_frame[15][0] <= by_frame[20][0] <= by_frame[25][0], by_frame

        # 両端の判定が付くこと
        q = get_json(f"{srv.base}/api/jobs/{jid}/queue?all=1&rebuild=1&t={TOKEN}")
        verdicts = {it["frame"]: it["verdict"] for it in q["items"]}
        assert verdicts.get(15) == "fixed" and verdicts.get(25) == "fixed", verdicts

        # ひとつ戻すで両端の判定・修正がまとめて消えること
        code, r = post_json(f"{srv.base}/api/jobs/{jid}/undo?t={TOKEN}", {})
        assert code == 200 and r["ok"] and r["removed"] == 11, r
        items = get_json(f"{srv.base}/api/jobs/{jid}/corrections?t={TOKEN}")["corrections"]
        assert not items, items

        # 「でかすぎる」に区間指定を使わせない（remove を区間補間で動かす危険を防ぐ。
        # review.mark_interval のドキュストリング参照）
        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/mark?t={TOKEN}",
            {
                "frame": 20, "verdict": "toobig",
                "x": 0.5, "y": 0.5, "w": 20, "h": 20,
                "start_frame": 15, "start_x": 0.5, "start_y": 0.5,
            },
        )
        assert code == 400, r
        items = get_json(f"{srv.base}/api/jobs/{jid}/corrections?t={TOKEN}")["corrections"]
        assert not items, "拒否したはずなのに修正が残っている"
        print("  区間の /mark 往復 OK（補間・両端の判定・undo・toobig 拒否）")
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


def test_false_positive_via_webapp_mark_matches_review_session():
    """「誤検知」（false_positive + pick）が webapp の /mark からも通ること。

    直す前は api_mark() が payload["pick"] を読み捨てていたので、webapp からは
    常に「消す領域が指定されていません」で 400 になっていた（issue #28）。
    「狭める」（toobig）は x/y/w/h だけで通っていたので気づかれずに残っていた。

    review.ReviewSession.mark() を同じ pick で直接呼んだ結果とも突き合わせ、
    webapp 経由でも旧レビュー UI（python -m automosaic.review）と同じ
    corrections.json になることを確かめる。両者は同じ ReviewSession クラスを
    呼んでいるので、ここで見ているのは pick の受け渡しがずれていないかだけだが、
    その受け渡し自体が直す前は欠けていた。
    """
    lib = make_lib()
    srv = Server(lib)
    ref_dir = None
    try:
        d = prepared_job(lib, srv)
        jid = d["id"]
        job = jobs_mod.Library(lib).get(jid)

        # progress の done/counts はキューに載った項目でしか進まないので、
        # 実在のキュー項目から選ぶ（f=5 決め打ちだと間引きで落ちることがある）。
        # 既定キュー（despiked/uncovered のみ、issue #21）は自動領域が無い
        # フレームだけなので、pick を試すには all=1 で領域つきの項目を拾う
        q = get_json(f"{srv.base}/api/jobs/{jid}/queue?all=1&t={TOKEN}")
        item = next((it for it in q["items"] if it["boxes"]), None)
        assert item, "自動領域のあるキュー項目が無い"
        f = item["frame"]
        pick = [item["boxes"][0][:4]]

        # 直す前はここが 400（「消す領域が指定されていません」）だった
        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/mark?t={TOKEN}",
            {"frame": f, "verdict": "false_positive", "pick": pick, "span": 0},
        )
        assert code == 200, r
        assert r["added"] == 1, r
        assert r["progress"]["counts"]["false_positive"] == 1, r["progress"]
        assert not r["regions"], "誤検知で消したはずの領域が残っている"

        web_items = CorrectionSet.load(job.corrections).items
        assert len(web_items) == 1, [c.as_dict() for c in web_items]

        # 同じ pick を review.ReviewSession に直接与える。review.py の
        # do_POST("/api/mark") がやっているのと同じ呼び方
        from automosaic import review as review_mod

        ref_dir = tempfile.mkdtemp(prefix="automosaic_ref_")
        ref_corrections = os.path.join(ref_dir, "corrections.json")
        argv = [job.source, "--detections", job.detections, "--corrections", ref_corrections]
        args = review_mod.build_parser().parse_args(argv)
        ref_session = review_mod.session_from_args(args, argv)
        try:
            ref_session.mark(f, "false_positive", pick=pick, span=0, cls=None)
        finally:
            ref_session.reader.close()

        ref_items = CorrectionSet.load(ref_corrections).items
        assert len(ref_items) == 1, [c.as_dict() for c in ref_items]
        assert (web_items[0].frame, web_items[0].box, web_items[0].kind) == (
            ref_items[0].frame,
            ref_items[0].box,
            ref_items[0].kind,
        ), (web_items[0].as_dict(), ref_items[0].as_dict())

        # corrections.progress.json も同じ内容になること（issue #28 の完了条件）。
        # video は両方とも同じ job.source から作っているので一致するはず
        web_progress_path = os.path.splitext(job.corrections)[0] + ".progress.json"
        ref_progress_path = os.path.splitext(ref_corrections)[0] + ".progress.json"
        with open(web_progress_path, encoding="utf-8") as fh:
            web_progress = json.load(fh)
        with open(ref_progress_path, encoding="utf-8") as fh:
            ref_progress = json.load(fh)
        assert web_progress["video"] == ref_progress["video"], (
            web_progress["video"], ref_progress["video"],
        )
        assert web_progress["verdicts"] == ref_progress["verdicts"] == {str(f): "false_positive"}, (
            web_progress["verdicts"], ref_progress["verdicts"],
        )
        assert web_progress["false_positives"] == ref_progress["false_positives"], (
            web_progress["false_positives"], ref_progress["false_positives"],
        )
        # history は「直前の判定」も持つので、同じ手順で作れば同じ形になる
        assert [h["added"] for h in web_progress["history"]] == [h["added"] for h in ref_progress["history"]]
        assert [h["fp"] for h in web_progress["history"]] == [h["fp"] for h in ref_progress["history"]]
        print(
            "  webapp /mark と review.ReviewSession 直呼びで corrections.json / "
            f"corrections.progress.json が一致 OK: {web_items[0].as_dict()} / "
            f"verdicts={web_progress['verdicts']}"
        )
    finally:
        srv.close()
        if ref_dir:
            shutil.rmtree(ref_dir, ignore_errors=True)


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


def test_session_overrides_transfers_critical_keys():
    """session_overrides() が margin_scale/margin_cap/frame_step/estimate_gaps
    を settings から overrides に転送すること。

    M7 変異（これらのキーを落とす）が落ちることを確認するための
    回帰テスト（issue #43）。レビューがこれらの設定を無視すると、
    既定より小さい値で焼いたジョブが広く塗られて見える（漏れ隠し方向）。
    """
    from automosaic.webapp.session import session_overrides

    # margin_scale / margin_cap（float）/ frame_step（int）/ estimate_gaps（bool）
    overrides = session_overrides({
        "margin_scale": "1.5",
        "margin_cap": "50",
        "frame_step": "2",
        "estimate_gaps": "true",
    })

    assert "margin_scale" in overrides, f"margin_scale が落ちた: {overrides.keys()}"
    assert overrides["margin_scale"] == 1.5, f"margin_scale 値が違う: {overrides['margin_scale']}"

    assert "margin_cap" in overrides, f"margin_cap が落ちた: {overrides.keys()}"
    assert overrides["margin_cap"] == 50, f"margin_cap 値が違う: {overrides['margin_cap']}"

    assert "frame_step" in overrides, f"frame_step が落ちた: {overrides.keys()}"
    assert overrides["frame_step"] == 2, f"frame_step 値が違う: {overrides['frame_step']}"

    assert "estimate_gaps" in overrides, f"estimate_gaps が落ちた: {overrides.keys()}"
    assert overrides["estimate_gaps"] is True, f"estimate_gaps 値が違う: {overrides['estimate_gaps']}"

    print("  session_overrides が4キー（margin_scale/margin_cap/frame_step/estimate_gaps）を転送 OK")

def test_effective_settings_mismatch_returns_409_and_report_records_burn():
    """issue #16: 焼き込みが実際に使った実効設定が report.json に残ること、
    レビューがそれと食い違ったら /frame /state /queue が絵を返さず 409 で
    止まること、ただし /api/jobs（一覧・詳細・ダウンロード）は開けたまま
    であること。

    #15（webapp がジョブ設定をレビューへ渡す）が入ったので、ふつうに焼いて
    ふつうに開くかぎり焼き込みとレビューは一致する。食い違いが残るのは
    **焼き直さずに設定だけ変えたとき**。完成品は古い設定のまま、レビューは
    新しい設定で領域を計算するので、画面と動画が別物になる。
    ここでは既定で焼いたあと meta.settings の margin_scale だけを 0.4 に
    書き換え（焼き直さない）、この検査がそれを検出できることを実測する。
    """
    lib = make_lib()
    srv = Server(lib)
    try:
        d = upload(srv.base, sample_video())
        jid = d["id"]
        job = jobs_mod.Library(lib).get(jid)
        write_fake_detections(job, d["n_frames"], d["width"], d["height"])

        # 完成品(job.output)がまだ無い間は、report が無くても止めない。
        # 検出だけ済ませてレビュー/手描きをする普通の使い方を塞がないため
        code, body, _ = request(f"{srv.base}/api/jobs/{jid}/state?t={TOKEN}")
        assert code == 200, (code, body[:300])

        # 既定（margin_scale=1.0）で実際に焼く
        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/start?t={TOKEN}",
            {"reuse": True},
        )
        assert code == 200, r
        st = wait_finished(srv, jid)
        assert st == "done", st

        job = jobs_mod.Library(lib).get(jid)
        assert os.path.exists(job.output), "完成品が無い"
        with open(job.report, encoding="utf-8") as f:
            rep = json.load(f)
        assert rep.get("effective_sha256"), "report.json に effective_sha256 が無い"
        # 既定は 0.35（1.0 ではない。実測して確かめた値）
        assert rep["effective"]["cfg"]["margin_scale"] == 0.35, rep["effective"]["cfg"]

        # 焼き直さずに設定だけ変える。完成品は margin_scale=0.35 のまま、
        # レビューは 0.4 で領域を計算するので、画面と動画が別物になる
        job.meta.setdefault("settings", {})["margin_scale"] = 0.4
        job.save()
        code, body, _ = request(f"{srv.base}/api/jobs/{jid}/frame?n=0&t={TOKEN}")
        assert code == 409, (code, body[:300])
        detail = json.loads(body).get("detail", "")
        assert "margin_scale" in detail, detail

        code, body, _ = request(f"{srv.base}/api/jobs/{jid}/state?t={TOKEN}")
        assert code == 409, (code, body[:300])

        code, body, _ = request(f"{srv.base}/api/jobs/{jid}/queue?t={TOKEN}")
        assert code == 409, (code, body[:300])

        # 一覧・詳細・ダウンロードは開けたまま（issue #16 の完了条件:
        # ジョブ一覧が開けなくなると詰むので /api/jobs は通す）
        detail_job = get_json(f"{srv.base}/api/jobs/{jid}?t={TOKEN}")
        assert detail_job["status"] == "done", detail_job
        code, body, _ = request(f"{srv.base}/api/jobs/{jid}/download?t={TOKEN}")
        assert code == 200, (code, body[:300])

        print(
            "  実効設定の食い違い(#14再現条件)で /frame /state /queue が 409"
            "（/api/jobs は通る）OK"
        )

        # report.json が effective / effective_sha256 を持たない
        # （古い形式・破損）場合、そのまま「記録がありません」にはせず、
        # 同じジョブの meta.argv から復元して比較する（マージ前指摘: 唯一
        # 実在する完走ジョブが report.json の形式差だけでレビュー不能に
        # なる事態を避けるため）。この job は既定(margin_scale=0.35)で焼いて
        # おり、meta.settings だけ 0.4 に書き換えてあるので、argv から復元
        # しても 0.35 のままで、レビュー側の 0.4 と不一致になり 409 になる。ただしメッセージは
        # 「記録がありません」ではなく、復元した値で比較した不一致に変わる
        rep.pop("effective", None)
        rep.pop("effective_sha256", None)
        with open(job.report, "w", encoding="utf-8") as f:
            json.dump(rep, f)
        code, body, _ = request(f"{srv.base}/api/jobs/{jid}/frame?n=0&t={TOKEN}")
        assert code == 409, (code, body[:300])
        detail2 = json.loads(body).get("detail", "")
        assert "margin_scale" in detail2, detail2
        assert "復元した値" in detail2, detail2
        print(
            "  report.json が effective を持たない場合も meta.argv から復元して"
            "比較し、不一致なら 409（復元した旨を明記）OK"
        )

        # meta.argv 自体が無ければ復元しようがなく、「記録がありません」で 409
        job.meta["argv"] = []
        job.save()
        code, body, _ = request(f"{srv.base}/api/jobs/{jid}/frame?n=0&t={TOKEN}")
        assert code == 409, (code, body[:300])
        detail3 = json.loads(body).get("detail", "")
        assert "記録がありません" in detail3, detail3
        assert "復元もできません" in detail3, detail3
        print("  report.json も meta.argv も無ければ復元できず 409 OK")
    finally:
        srv.close()


def test_effective_settings_missing_restores_from_argv_and_shows_in_state():
    """マージ前指摘（issue #16 PR レビュー）: report.json に effective が
    無くても、meta.argv から復元できて実際に一致するなら 409 にせず
    表示できること。かつ「復元した」ことが /state に出ること
    （RULES.md 0: 黙って通さない）。

    レビュー側は #43 未マージのため常に既定値で cfg を組む。ここでは
    焼き込み側も設定を明示指定せず既定値のまま焼き、両者を一致させる。
    唯一実在する完走ジョブ（20260823-234604-9be9）でこの経路を実際に
    通したログは PR に別途貼ってある（この場は read-only 素材に触れない
    合成テスト）。
    """
    lib = make_lib()
    srv = Server(lib)
    try:
        d = upload(srv.base, sample_video())
        jid = d["id"]
        job = jobs_mod.Library(lib).get(jid)
        write_fake_detections(job, d["n_frames"], d["width"], d["height"])

        # 設定を明示指定せず既定値のまま焼く
        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/start?t={TOKEN}",
            {"reuse": True, "settings": {}},
        )
        assert code == 200, r
        st = wait_finished(srv, jid)
        assert st == "done", st

        job = jobs_mod.Library(lib).get(jid)
        with open(job.report, encoding="utf-8") as f:
            rep = json.load(f)
        assert rep.get("effective_sha256"), "前提: 通常は effective が書かれるはず"

        # report.json から effective / effective_sha256 を消す
        # （issue #16 マージ前に焼かれたジョブの形を再現）
        rep.pop("effective", None)
        rep.pop("effective_sha256", None)
        with open(job.report, "w", encoding="utf-8") as f:
            json.dump(rep, f)

        code, body, _ = request(f"{srv.base}/api/jobs/{jid}/state?t={TOKEN}")
        assert code == 200, (code, body[:500])
        st_json = json.loads(body)
        chk = st_json.get("effective_check")
        assert chk is not None, "effective_check が state に無い"
        assert chk["restored"] is True, chk
        assert chk["note"], "復元したことの注記が空"
        assert "仮定" in chk["note"], chk["note"]

        code, body, _ = request(f"{srv.base}/api/jobs/{jid}/frame?n=0&t={TOKEN}")
        assert code == 200, (code, body[:300])

        print(
            "  report.json に effective が無くても meta.argv から復元でき、"
            "一致するなら 200 で、復元したことが /state に載る OK"
        )
    finally:
        srv.close()


def test_effective_settings_restore_refuses_contradictory_argv():
    """meta.argv が矛盾していたら、復元したふりをして通さないこと。

    `--despike` と `--no-despike` を同時に指定した argv は cli.py 側で
    弾かれる（cli.py の `if args.despike and args.no_despike:`）。復元経路が
    このガードを通っていないと、どちらか一方を黙って採用した cfg を
    「復元できた」として比較してしまう。焼き込みと違う設定で「一致」と
    判定すれば、それは #14 と同じ「塗ったつもりで塗れていない」になる。

    復元できないときは 409 で止まること（RULES.md 0: 判断がつかないときは止める）。
    """
    lib = make_lib()
    srv = Server(lib)
    try:
        d = upload(srv.base, sample_video())
        jid = d["id"]
        job = jobs_mod.Library(lib).get(jid)
        write_fake_detections(job, d["n_frames"], d["width"], d["height"])

        code, r = post_json(
            f"{srv.base}/api/jobs/{jid}/start?t={TOKEN}", {"reuse": True}
        )
        assert code == 200, r
        assert wait_finished(srv, jid) == "done"

        job = jobs_mod.Library(lib).get(jid)
        with open(job.report, encoding="utf-8") as f:
            rep = json.load(f)
        rep.pop("effective", None)
        rep.pop("effective_sha256", None)
        with open(job.report, "w", encoding="utf-8") as f:
            json.dump(rep, f)

        # 矛盾した argv にする（焼いたときの argv を書き換え）
        job.meta["argv"] = list(job.meta["argv"]) + ["--despike", "--no-despike"]
        job.save()

        code, body, _ = request(f"{srv.base}/api/jobs/{jid}/frame?n=0&t={TOKEN}")
        assert code == 409, (code, body[:300])
        detail = json.loads(body).get("detail", "")
        # 「復元できなかった」であること。「復元したが不一致だった」では駄目。
        # 後者だと、矛盾した argv からどちらか一方を黙って採った cfg を
        # 使って比較していることになる（この検査が守りたいのはそこ）
        assert "復元もできません" in detail or "復元も試みましたが失敗" in detail, detail
        assert "復元した値" not in detail, detail

        print("  meta.argv が矛盾していたら復元せず 409 OK")
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
