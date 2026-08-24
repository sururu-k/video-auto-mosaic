"""レビュー UI の検証。

大半はサーバを立てずに回る。「間違えると静かに壊れる」ところ:
Range のパース、検査キューの組み立て、タップ座標の変換、修正の往復、
YOLO 座標変換。

トークン検証と API の往復だけは、実際に ThreadingHTTPServer を立てて
urllib から叩いて確かめる。403 を返すつもりで 200 を返していた、が
いちばん困る種類の壊れ方で、関数を直接呼んでいると気づけない。
"""

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic import cli  # noqa: E402
from automosaic import review  # noqa: E402
from automosaic.corrections import Correction, CorrectionSet  # noqa: E402
from automosaic.corrections import apply as apply_corrections  # noqa: E402
from automosaic.detector import Detection  # noqa: E402
from automosaic.review import (  # noqa: E402
    COOKIE_NAME,
    COV_ESTIMATED,
    COV_NONE,
    COV_REAL,
    ReviewHandler,
    ReviewSession,
    build_queue,
    cookie_token,
    cover_box,
    dominant_class,
    export_dataset,
    lan_addresses,
    median_box_size,
    mosaic_bgr,
    parse_range,
    qr_matrix,
    runs_of,
    tap_to_box,
    to_yolo,
    token_matches,
)
from automosaic.detector import CONSERVATIVE_CLASSES, DEFAULT_CLASSES  # noqa: E402
from automosaic.temporal import (  # noqa: E402
    TemporalConfig,
    effective_settings,
    effective_settings_sha256,
    resolve_classes,
)

CLS = "MALE_GENITALIA_EXPOSED"


def _have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg"))


def _make_tiny_video(path: str, w: int, h: int, frames: int, fps: int = 30) -> None:
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:r={fps}",
            "-frames:v", str(frames), "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-g", "5", path,
        ],
        check=True,
    )


def make_session(tmp, per_frame=None, n_frames=60, corrections=None):
    """実ファイル無しで組み立てたセッション。FrameReader は遅延で開くので動く。"""
    if per_frame is None:
        # 0-9 と 40-49 だけ検出。あいだは補間と memory の推定だけになる
        per_frame = {f: [] for f in range(n_frames)}
        for f in list(range(0, 10)) + list(range(40, 50)):
            per_frame[f] = [Detection(CLS, 0.8, (200, 200, 60, 60))]
    return ReviewSession(
        video=os.path.join(tmp, "src.mp4"),
        rendered=None,
        corrections_path=os.path.join(tmp, "corrections.json"),
        width=640,
        height=480,
        fps=30.0,
        n_frames=n_frames,
        classes={CLS},
        cfg=TemporalConfig(max_gap=12, memory=6, bridge_max=150),
        per_frame=per_frame,
        corrections=corrections or CorrectionSet(),
        block=8,
        default_size=(64, 64),
        default_class=CLS,
    )


# -- Range ヘッダ --------------------------------------------------------


def test_parse_range_forms():
    assert parse_range("bytes=0-99", 1000) == (0, 99)
    assert parse_range("bytes=100-", 1000) == (100, 999)   # 開区間
    assert parse_range("bytes=-200", 1000) == (800, 999)   # 末尾指定
    assert parse_range(" bytes=0-0 ", 1000) == (0, 0)
    # 終端が実サイズを超えていたら詰める。ここを通さないと Content-Length がずれる
    assert parse_range("bytes=990-5000", 1000) == (990, 999)
    print("  Range パース OK")


def test_parse_range_rejects():
    assert parse_range(None, 1000) is None
    assert parse_range("", 1000) is None
    assert parse_range("bytes=abc-1", 1000) is None
    assert parse_range("items=0-1", 1000) is None
    assert parse_range("bytes=0-9,20-29", 1000) is None  # 複数レンジは非対応
    assert parse_range("bytes=1000-", 1000) is None      # 範囲外 -> 416
    assert parse_range("bytes=50-10", 1000) is None      # 逆転
    assert parse_range("bytes=0-99", 0) is None
    print("  不正な Range の拒否 OK")


# -- /api/state ----------------------------------------------------------


def test_state_payload_shape():
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        d = s.state_payload()
        for key in (
            "width", "height", "fps", "n_frames", "block", "classes",
            "default_size", "default_class", "coverage", "regions",
            "uncovered_ranges", "estimated_only_ranges", "n_corrections",
        ):
            assert key in d, f"{key} が無い"
        assert len(d["coverage"]) == d["n_frames"], "被覆文字列がフレーム数と違う"
        assert set(d["coverage"]) <= {COV_NONE, COV_REAL, COV_ESTIMATED}
        # 検出のあるフレームは実観測、あいだは推定のみになっているはず
        assert d["coverage"][0] == COV_REAL
        assert d["coverage"][25] == COV_ESTIMATED
        assert d["coverage"][-1] == COV_NONE
        # 矩形は [x, y, w, h, 由来, スコア] の6要素
        r = d["regions"]["0"][0]
        assert len(r) == 6 and r[4] == "d"
        print(f"  /api/state の構造 OK（推定のみ {d['coverage'].count(COV_ESTIMATED)} フレーム）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_state_payload_light_drops_heavy_arrays():
    """light=1 で全フレームぶんの配列を落とすこと。

    検査キュー画面が要るのは解像度とクラスだけ。1時間の動画で 10MB を超える
    regions を端末に落とさせると、起動そのものが終わらない。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        light = s.state_payload(light=True)
        for key in ("width", "height", "n_frames", "classes", "default_size", "default_class"):
            assert key in light, key
        for key in ("regions", "coverage", "estimated_only_ranges"):
            assert key not in light, f"{key} が light に残っている"
        full = json.dumps(s.state_payload(), ensure_ascii=False)
        assert len(json.dumps(light, ensure_ascii=False)) < len(full) / 2
        print("  /api/state?light=1 の軽量化 OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_queue_items_carry_their_boxes():
    """キューの各枚に、その場で重ねる矩形が同梱されること。"""
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        q = s.queue_payload()
        assert q["items"], "キューが空"
        for it in q["items"]:
            assert "boxes" in it
            assert it["boxes"] == s.frame_regions(it["frame"])
        print("  キュー項目の矩形同梱 OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ranges_match_coverage():
    """リストに出す区間が、帯に塗る色と食い違わないこと。

    別々の元データから作ると、クリックしてジャンプした先が緑、が起きる。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        d = s.ranges_payload()
        for r in d["estimated_only_ranges"]:
            for f in range(r["start"], r["end"] + 1):
                assert s.coverage[f] == COV_ESTIMATED, f"frame {f} が推定のみでない"
        for r in d["uncovered_ranges"]:
            for f in range(r["start"], r["end"] + 1):
                assert s.coverage[f] == COV_NONE
        assert d["estimated_only_ranges"], "推定のみ区間が拾えていない"
        print(
            f"  区間リストと被覆の一致 OK"
            f"（推定のみ {len(d['estimated_only_ranges'])} 件 / "
            f"未処理 {len(d['uncovered_ranges'])} 件）"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_runs_of():
    assert runs_of("0011100", "1") == [(2, 4)]
    assert runs_of("1111", "0") == []
    assert runs_of("0110", "1", min_len=3) == []
    assert runs_of("111", "1") == [(0, 2)]  # 末尾まで続く場合
    print("  連続区間の抽出 OK")


# -- 修正の往復 ----------------------------------------------------------


def test_corrections_roundtrip_on_disk():
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "sub", "c.json")
        cs = CorrectionSet(video="a.mp4", width=640, height=480)
        cs.add(Correction(frame=12, box=(10.0, 20.0, 30.0, 40.0), cls=CLS))
        cs.save(path)
        back = CorrectionSet.load(path)
        assert back.width == 640 and back.video == "a.mp4"
        assert len(back.items) == 1
        c = back.items[0]
        assert c.frame == 12 and c.cls == CLS and c.kind == "add"
        assert c.box == (10.0, 20.0, 30.0, 40.0)
        print("  修正ファイルの往復 OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_set_corrections_updates_coverage():
    """手修正を入れると、その区間が「推定のみ」から外れること。

    手で置いた領域は実観測なので、直したところが黄色のまま残ってはいけない。
    残ると「まだ直っていない」と読めてしまい、レビューの導線が壊れる。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        assert s.coverage[25] == COV_ESTIMATED
        s.set_corrections(
            [
                {"frame": f, "box": [300, 300, 64, 64], "class": CLS, "kind": "add"}
                for f in range(20, 30)
            ]
        )
        assert s.coverage[25] == COV_REAL, "手修正が実観測として扱われていない"
        assert os.path.exists(s.corrections_path), "自動保存されていない"
        saved = json.load(open(s.corrections_path, encoding="utf-8"))
        assert len(saved["corrections"]) == 10
        # 手で置いた矩形は膨張させずそのまま乗る
        boxes = [r for r in s.regions_payload()["25"] if r[4] == "x"]
        assert boxes and boxes[0][:4] == [300, 300, 64, 64]
        print("  手修正の反映 OK（推定のみ -> 実観測）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- #24: 判定1回ごとの領域マップ往復 -------------------------------------


def test_update_payload_defaults_to_full_regions():
    """frame を渡さない update_payload() は、これまでどおり全フレームぶん返す。

    後方互換の確認。webapp/app.py の POST /corrections など、frame を渡さない
    既存の呼び出し元がここで挙動を変えてはいけない。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        d = s.update_payload()
        assert d["regions"] == s.regions_payload()
        print("  update_payload() 既定は全フレームぶん OK（後方互換）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_update_payload_scoped_to_one_frame_is_much_smaller():
    """frame を渡すと、その1コマぶんの regions だけになること（#24）。

    タイムライン画面（frontend/src/timeline/timeline.tsx）は表示中の1コマしか
    描かないので、動画全体の領域マップを往復させる理由が無い。修正前は
    矩形を1個置くたびに動画全体ぶんが返っていた（review.py:1132-1135 のコメントが
    「1時間の動画で10MBを超える」と書いていた箇所）。coverage・区間情報は
    frame の有無に関係なく一致すること（規模を削るのは regions だけ）も確かめる。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp, n_frames=2000)
        full = s.update_payload()
        scoped = s.update_payload(frame=0)
        assert set(scoped["regions"].keys()) <= {"0"}, scoped["regions"].keys()
        assert scoped["coverage"] == full["coverage"]
        assert scoped["estimated_only_ranges"] == full["estimated_only_ranges"]
        assert scoped["uncovered_ranges"] == full["uncovered_ranges"]
        full_len = len(json.dumps(full["regions"], ensure_ascii=False))
        scoped_len = len(json.dumps(scoped["regions"], ensure_ascii=False))
        assert scoped_len < full_len / 10, (scoped_len, full_len)
        print(
            f"  update_payload(frame=0) は1コマぶんだけ OK"
            f"（regions {full_len}B -> {scoped_len}B、n_frames=2000）"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_frame_regions_payload_matches_frame_regions():
    """frame_regions_payload() が /api/regions（#24）の中身そのものであること。"""
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        d = s.frame_regions_payload(0)
        assert d["frame"] == 0
        assert d["version"] == s.version
        assert d["regions"] == s.frame_regions(0)
        # 範囲外は端に寄せる（/frame や /api/state と同じ規則）
        clamped = s.frame_regions_payload(999999)
        assert clamped["frame"] == s.n_frames - 1
        print("  frame_regions_payload() OK（範囲外は端に寄せる）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_apply_removes_only_automatic_regions():
    """remove は自動領域だけを落とし、手で足した領域は残すこと。"""
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        cs = CorrectionSet(items=[Correction(0, (0.0, 0.0, 640.0, 480.0), CLS, "remove")])
        out = apply_corrections(s.regions, cs)
        assert not out[0], "全面 remove なのに自動領域が残っている"

        cs2 = CorrectionSet(
            items=[
                Correction(0, (300.0, 300.0, 40.0, 40.0), CLS, "add"),
                Correction(0, (0.0, 0.0, 640.0, 480.0), CLS, "remove"),
            ]
        )
        out2 = apply_corrections(s.regions, cs2)
        assert len(out2[0]) == 1 and out2[0][0][1].source == "manual"
        print("  remove の適用範囲 OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- 既定サイズ ----------------------------------------------------------


def test_median_box_size_ignores_other_classes():
    per_frame = {
        0: [Detection(CLS, 0.5, (0, 0, 40, 30)), Detection("FACE_FEMALE", 0.9, (0, 0, 900, 900))],
        1: [Detection(CLS, 0.5, (0, 0, 50, 40))],
        2: [Detection(CLS, 0.5, (0, 0, 60, 50))],
    }
    assert median_box_size(per_frame, {CLS}, 99) == (50, 40)
    assert median_box_size({}, {CLS}, 99) == (99, 99)
    print("  既定サイズの中央値 OK")


def test_dominant_class():
    per_frame = {
        0: [Detection("ANUS_EXPOSED", 0.5, (0, 0, 10, 10))],
        1: [Detection(CLS, 0.5, (0, 0, 10, 10)), Detection(CLS, 0.4, (5, 5, 10, 10))],
    }
    assert dominant_class(per_frame, {CLS, "ANUS_EXPOSED"}) == CLS
    print("  既定クラスの決定 OK")


# -- YOLO 形式 -----------------------------------------------------------


def test_to_yolo_normalizes():
    cx, cy, w, h = to_yolo((100, 50, 200, 100), 400, 200)
    assert abs(cx - 0.5) < 1e-9 and abs(cy - 0.5) < 1e-9
    assert abs(w - 0.5) < 1e-9 and abs(h - 0.5) < 1e-9
    for v in to_yolo((0, 0, 400, 200), 400, 200):
        assert 0.0 <= v <= 1.0
    print("  YOLO 正規化 OK")


def test_to_yolo_clips_out_of_frame():
    """はみ出した矩形はクリップしてから中心を取ること。

    クリップせずに中心だけ正規化すると、画面外のぶんだけ中心がずれた教師になる。
    """
    cx, cy, w, h = to_yolo((-50, -50, 100, 100), 400, 200)
    assert abs(cx - (25 / 400)) < 1e-9, cx
    assert abs(cy - (25 / 200)) < 1e-9, cy
    assert abs(w - (50 / 400)) < 1e-9
    assert abs(h - (50 / 200)) < 1e-9
    for v in (cx, cy, w, h):
        assert 0.0 <= v <= 1.0
    print("  画面外のクリップ OK")


def test_export_dataset_includes_detections_not_estimates():
    """手修正のフレームに自動検出があれば一緒に出し、推定は出さないこと。

    手修正だけを教師にすると「他には何も写っていない」を教えることになるが、
    補間や memory の矩形は実観測ではないので入れてはいけない。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)

        class StubReader:
            """動画ファイルを用意せずに書き出しだけ検証するための差し替え。"""

            def read(self, n):
                return np.zeros((480, 640, 3), dtype=np.uint8)

        s.reader = StubReader()
        # frame 5 は検出あり、frame 25 は推定のみ。両方に手修正を入れる
        s.set_corrections(
            [
                {"frame": 5, "box": [10, 10, 40, 40], "class": CLS, "kind": "add"},
                {"frame": 25, "box": [10, 10, 40, 40], "class": CLS, "kind": "add"},
            ]
        )
        out = os.path.join(tmp, "ds")
        n = export_dataset(s, out, quiet=True)
        assert n == 2, n

        assert os.path.exists(os.path.join(out, "images", "000005.png"))
        assert os.path.exists(os.path.join(out, "dataset.yaml"))
        classes = open(os.path.join(out, "classes.txt"), encoding="utf-8").read().split()
        assert classes == [CLS]

        l5 = open(os.path.join(out, "labels", "000005.txt"), encoding="utf-8").read().strip()
        l25 = open(os.path.join(out, "labels", "000025.txt"), encoding="utf-8").read().strip()
        assert len(l5.splitlines()) == 2, f"検出が一緒に出ていない: {l5}"
        assert len(l25.splitlines()) == 1, f"推定が混ざっている: {l25}"
        for line in (l5 + "\n" + l25).splitlines():
            parts = line.split()
            assert len(parts) == 5 and parts[0] == "0"
            assert all(0.0 <= float(v) <= 1.0 for v in parts[1:])
        print("  学習データ書き出し OK（検出は同梱・推定は除外）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- プレビューのモザイク ------------------------------------------------


def test_mosaic_bgr_covers_box_only():
    """プレビューが領域内だけを潰していること。

    ここが素通しだと、レビュー画面自体が原画ビューアになる。
    素材は無彩色のノイズにする。彩色ノイズだと 4:2:0 の彩度間引きで
    領域外まで大きく変わり、「潰したのか往復で変わったのか」を分けられない。
    """
    rng = np.random.default_rng(0)
    gray = rng.integers(0, 256, size=(64, 64), dtype=np.uint8)
    frame = np.dstack([gray, gray, gray])
    out = mosaic_bgr(frame.copy(), [(16.0, 16.0, 32.0, 32.0)], block=8)
    assert out.shape == frame.shape

    # 8px ブロックが単色になっていること
    cell = out[24:32, 24:32]
    assert int(cell.max()) - int(cell.min()) <= 2, "領域内が潰れていない"
    # 領域外は YUV 往復の丸め誤差ぶんしか動かない
    assert np.abs(out[:8, :8].astype(int) - frame[:8, :8].astype(int)).max() <= 3
    print("  プレビューのモザイク OK")


# -- 検査キューの組み立て ------------------------------------------------


def _regs(box, score=0.9, source="detected"):
    from automosaic.temporal import Region

    return [(box, Region(box, CLS, score, source))]


def test_build_queue_priority_order():
    """推定のみ -> 未処理 の順に並び、区間は間引いて出ること。

    ここが崩れると、いちばん怪しい区間が数百枚の後ろに沈む。
    「順に叩けば重要なところから片付く」がキュー方式の唯一の取り柄なので、
    並び順は仕様そのもの。
    """
    cov = COV_REAL * 20 + COV_ESTIMATED * 20 + COV_NONE * 5 + COV_REAL * 15
    # 「推定のみ」の判定は regions（temporal.estimated_only_ranges 経由）を見る。
    # coverage 文字列だけでは判定できないので、20-39 を推定由来の領域にしておく
    regions = {f: _regs((0.0, 0.0, 10.0, 10.0), 0.1, "interpolated") for f in range(20, 40)}
    q = build_queue(cov, regions, 60, step=5)
    prios = [it["priority"] for it in q]
    assert prios == sorted(prios), "優先度の順に並んでいない"
    est = [it["frame"] for it in q if it["reason"] == "estimated"]
    unc = [it["frame"] for it in q if it["reason"] == "uncovered"]
    assert est == [20, 25, 30, 35], est
    # step 以下の短い区間は中央の1枚だけ。端を取ると隣の区間が写る
    assert unc == [42], unc
    print("  キューの並びと間引き OK")


def test_build_queue_dedupes_by_priority():
    """同じフレームが複数の理由に当たっても、重い理由で1回だけ出すこと。"""
    cov = COV_ESTIMATED * 30
    regions = {f: _regs((0, 0, 10, 10), 0.1, "interpolated") for f in range(30)}
    q = build_queue(cov, regions, 30, step=5)
    frames = [it["frame"] for it in q]
    assert len(frames) == len(set(frames)), "同じフレームが二重に出ている"
    assert all(it["reason"] == "estimated" for it in q), [it["reason"] for it in q]
    print("  キューの重複排除 OK")


def test_build_queue_reports_despiked_ranges():
    """despike が捨てた帯が、他の理由に一切引っかからなくてもキューに出ること。

    temporal.despike() は捨てた場所を返す契約になっていて、docstring は
    「呼び出し側はこれを必ず報告すること」と明記している。ここが抜けると、
    despike が実観測を丸ごと捨てたのに、そのフレームが別の理由（他の対象の
    補間など）で被覆済みに見えてキューから完全に消える。
    """
    cov = COV_REAL * 60  # 被覆はすべて実観測済み。他のどの基準にも引っかからない
    despiked = [(20, 24, CLS, 0.2), (45, 45, CLS, 0.1)]
    q = build_queue(cov, {}, 60, step=5, despiked_ranges=despiked)
    assert [it["reason"] for it in q] == ["despiked", "despiked"], q
    assert [it["priority"] for it in q] == [1, 1], q
    frames = [it["frame"] for it in q]
    assert frames == [22, 45], frames  # 5フレーム区間は中央の1枚、1枚区間はそのまま
    print("  despike が捨てた帯の報告 OK")


def test_build_queue_despiked_outranks_everything():
    """検出破棄は最優先。同じフレームで他の理由と当たっても検出破棄で出ること。"""
    cov = COV_ESTIMATED * 30
    regions = {f: _regs((0, 0, 10, 10), 0.1, "interpolated") for f in range(30)}
    q = build_queue(cov, regions, 30, step=5, despiked_ranges=[(10, 14, CLS, 0.1)])
    hit = [it for it in q if it["frame"] == 12]
    assert hit and hit[0]["reason"] == "despiked", q
    prios = [it["priority"] for it in q]
    assert prios == sorted(prios), "優先度の順に並んでいない"
    print("  検出破棄が推定のみより優先されること OK")


def test_build_queue_flags_area_jump():
    cov = COV_REAL * 40
    regions = {}
    for f in range(40):
        w = 100 if f < 20 else 300  # frame 20 で面積が 9 倍になる
        regions[f] = _regs((0.0, 0.0, float(w), float(w)))
    q = build_queue(cov, regions, 40, step=5)
    assert [it["frame"] for it in q] == [20], q
    assert q[0]["reason"] == "area_jump"
    print("  面積の急変の検出 OK")


def test_build_queue_all_frames():
    """問題が無くても全部見たい場合の経路。"""
    cov = COV_REAL * 100
    assert build_queue(cov, {}, 100, step=10) == [], "問題が無いのにキューが出来ている"
    q = build_queue(cov, {}, 100, step=10, all_frames=True)
    assert [it["frame"] for it in q] == list(range(0, 100, 10))
    assert all(it["reason"] == "sampled" for it in q)
    print("  全フレーム対象の指定 OK")


def test_session_queue_targets_estimated_ranges():
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        assert s.queue, "キューが空"
        # 先頭は推定のみ。実際に検出のあるフレームは出てこない
        assert s.queue[0]["reason"] == "estimated"
        for it in s.queue:
            if it["reason"] == "estimated":
                assert s.coverage[it["frame"]] == COV_ESTIMATED
        print(f"  セッションのキュー OK（{len(s.queue)} 枚）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_session_surfaces_despiked_ranges_hidden_by_other_coverage():
    """despike が捨てた帯が、他対象の実観測でフレームが被覆済みに見えても出ること。

    track A は全編を通して塗られる実観測。frame 25 だけ、track A とは
    重ならない別対象 track B の単発・低スコア検出があり、--despike 相当の
    設定（min_track_len=2）ではトラック長1・スコア0.2 で丸ごと捨てられる。
    frame 25 は track A のおかげで coverage 上は実観測扱いのままなので、
    despiked_ranges を報告しない実装ではこのフレームはキューに一度も
    現れない（uncovered にも low_conf にも area_jump にも当たらない）。
    """
    tmp = tempfile.mkdtemp()
    try:
        n_frames = 60
        per_frame = {f: [Detection(CLS, 0.9, (200, 200, 60, 60))] for f in range(n_frames)}
        per_frame[25] = per_frame[25] + [Detection(CLS, 0.2, (500, 50, 40, 40))]
        s = ReviewSession(
            video=os.path.join(tmp, "src.mp4"),
            rendered=None,
            corrections_path=os.path.join(tmp, "corrections.json"),
            width=640,
            height=480,
            fps=30.0,
            n_frames=n_frames,
            classes={CLS},
            cfg=TemporalConfig(max_gap=12, memory=6, bridge_max=150, min_track_len=2),
            per_frame=per_frame,
            corrections=CorrectionSet(),
            block=8,
            default_size=(64, 64),
            default_class=CLS,
        )
        assert s.despiked_ranges, "捨てたはずの track B が報告されていない"
        assert any(s == e == 25 for s, e, _c, _sc in s.despiked_ranges), s.despiked_ranges
        # frame 25 は track A のおかげで実観測扱いのまま
        assert s.coverage[25] == COV_REAL, s.coverage[25]
        hit = [it for it in s.queue if it["frame"] == 25]
        assert hit, "track B を捨てたフレームがキューに一度も現れていない"
        assert hit[0]["reason"] == "despiked", hit
        d = s.ranges_payload()
        assert d["despiked_ranges"], "ranges_payload に despiked_ranges が出ていない"
        print(f"  despike で隠れた帯のキュー反映 OK（{len(s.despiked_ranges)} 件）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- タップ座標の変換 ----------------------------------------------------


def test_tap_to_box_centers_on_tap():
    assert tap_to_box(0.5, 0.5, (100, 80), 640, 480) == (270.0, 200.0, 100.0, 80.0)
    assert tap_to_box(0.25, 0.5, (100, 80), 640, 480) == (110.0, 200.0, 100.0, 80.0)
    print("  タップ位置が矩形の中心になる OK")


def test_tap_to_box_pushes_back_at_edges():
    """端を突いても矩形を痩せさせないこと。

    切り詰めると、画面端に寄った対象を覆いきれない。「押したのに漏れたまま」は
    このツールで唯一やってはいけない失敗。
    """
    assert tap_to_box(0.0, 0.0, (100, 80), 640, 480) == (0.0, 0.0, 100.0, 80.0)
    assert tap_to_box(1.0, 1.0, (100, 80), 640, 480) == (540.0, 400.0, 100.0, 80.0)
    # 範囲外の入力もフレーム内に収める
    assert tap_to_box(-3.0, 9.0, (100, 80), 640, 480) == (0.0, 400.0, 100.0, 80.0)
    # フレームより大きい指定はフレーム全体まで
    assert tap_to_box(0.5, 0.5, (9999, 9999), 640, 480) == (0.0, 0.0, 640.0, 480.0)
    print("  画面端でのタップ OK")


# -- 判定の記録と取り消し ------------------------------------------------


def test_mark_fixed_covers_span():
    """「漏れている」で置いた矩形が、前後のフレームにも入ること。

    キューは間引いて出しているので、1コマだけ塞いでも隣は漏れたまま。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        assert s.coverage[25] == COV_ESTIMATED
        n = s.mark(25, "fixed", tap=(0.5, 0.5), size=(64, 64), span=2, cls=CLS)
        assert n == 5, n
        for f in range(23, 28):
            assert s.coverage[f] == COV_REAL, f
        assert s.verdicts[25] == "fixed"
        assert len(s.corrections.items) == 5
        box = s.corrections.items[0].box
        assert box == (288.0, 208.0, 64.0, 64.0), box
        print("  漏れている判定の反映 OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_undo_restores_corrections_and_verdict():
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        s.mark(25, "fixed", tap=(0.5, 0.5), size=(64, 64), span=2)
        s.mark(30, "ok")
        assert s.progress_payload()["done"] >= 1

        h = s.undo()
        assert h["frame"] == 30 and 30 not in s.verdicts
        h = s.undo()
        assert h["frame"] == 25
        assert not s.corrections.items, "戻したのに修正が残っている"
        assert s.coverage[25] == COV_ESTIMATED, "戻したのに被覆が実観測のまま"
        assert s.undo() is None, "戻すものが無いのに戻っている"
        print("  ひとつ戻す OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_mark_interval_interpolates_between_different_boxes():
    """区間の両端に違う矩形を置くと、複製ではなく補間になること（issue #46）。

    複製（mark()）は前後のフレームに同じ矩形を撒くだけだが、mark_interval()
    は両端の座標が違えば、あいだの座標もそれに応じて動く。

    自動検出を持たないセッションを使う（`per_frame={}`）。既定のセッション
    には検出（0-9, 40-49）があり、envelope（RULES.md 0）がそれを包もうと
    座標を動かすので、ここで見たい「補間か複製か」の違いが埋もれてしまう。
    envelope 自体は別のテスト（test_mark_interval_envelopes_existing_detection_mid_span）
    で見る。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp, per_frame={})
        n = s.mark_interval(
            frame=10, tap=(0.9, 0.9), start_frame=0, start_tap=(0.1, 0.1),
            size=(20, 20), start_size=(20, 20), cls=CLS,
        )
        assert n == 11, n
        by_frame = {c.frame: c.box for c in s.corrections.items}
        assert by_frame[0][0] < by_frame[5][0] < by_frame[10][0], by_frame
        assert by_frame[0][1] < by_frame[5][1] < by_frame[10][1], by_frame
        print("  中間フレームの座標が両端の中間になっている（複製ではない）OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_mark_interval_sets_both_endpoint_verdicts_and_undo_restores_both():
    """区間の両端に verdict が付き、ひとつ戻すで両方の verdict が戻ること。"""
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        n = s.mark_interval(
            frame=10, tap=(0.5, 0.5), start_frame=5, start_tap=(0.5, 0.5), size=(20, 20),
        )
        assert n == 6, n
        assert s.verdicts[5] == "fixed" and s.verdicts[10] == "fixed", s.verdicts
        assert len(s.corrections.items) == 6
        for f in range(5, 11):
            assert s.coverage[f] == COV_REAL, f

        h = s.undo()
        assert h["frame"] == 10, h
        assert not s.corrections.items, "戻したのに修正が残っている"
        assert 5 not in s.verdicts and 10 not in s.verdicts, s.verdicts
        print("  区間の両端に verdict が付き、undo で両方戻る OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_mark_interval_accepts_reversed_endpoints():
    """始点が終点より後ろのフレームでもよい（打った操作順に依らない）。

    envelope の影響を避けるため検出の無いセッションを使う（上のテストと同じ理由）。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp, per_frame={})
        n = s.mark_interval(
            frame=5, tap=(0.1, 0.1), start_frame=10, start_tap=(0.9, 0.9), size=(20, 20),
        )
        assert n == 6, n
        assert s.verdicts[5] == "fixed" and s.verdicts[10] == "fixed", s.verdicts
        by_frame = {c.frame: c.box for c in s.corrections.items}
        assert by_frame[5][0] < by_frame[10][0], by_frame
        print("  始点が終点より後ろでも正しく並べ替わる OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_mark_interval_rejects_out_of_range_frame():
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        try:
            s.mark_interval(
                frame=5, tap=(0.5, 0.5),
                start_frame=s.n_frames + 5, start_tap=(0.5, 0.5),
            )
            raise AssertionError("範囲外のフレームが通ってしまった")
        except ValueError as e:
            print(f"  範囲外フレームを拒否: {e}")
        assert not s.corrections.items, "拒否したのに修正が残っている"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_mark_interval_envelopes_existing_detection_mid_span():
    """RULES.md 0: 補間した矩形が既存の検出矩形より小さければ、大きいほうを採る。

    区間の両端をどちらも画面の隅（自動検出とは無関係な位置）に置く。
    区間の途中の frame8 には make_session() の既定の検出（(200,200,60,60)
    付近）がある。envelope が効いていなければ、frame8 の add 矩形は隅の
    小さい矩形のままで、既存の検出とは重ならない。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        auto_cov_before = s.auto_cover_box(8)
        assert auto_cov_before is not None, "前提が崩れている: frame8 に自動検出が無い"

        s.mark_interval(
            frame=45, tap=(0.02, 0.02), start_frame=5, start_tap=(0.02, 0.02),
            size=(10, 10), start_size=(10, 10),
        )
        by_frame = {c.frame: c.box for c in s.corrections.items}
        box8 = by_frame[8]
        ax, ay, aw, ah = auto_cov_before
        bx, by, bw, bh = box8
        assert bx <= ax and by <= ay, (box8, auto_cov_before)
        assert bx + bw >= ax + aw and by + bh >= ay + ah, (box8, auto_cov_before)
        # 隅の 10x10 のままなら envelope が効いていない。明確に大きいこと
        assert bw * bh > 10 * 10 * 4, box8
        print(f"  frame8 envelope: 自動検出={auto_cov_before} 置いた矩形={box8}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _cell_set(session, frame, grid=4):
    """そのフレームで塗られているセルの集合（粗いグリッドでの近似）。

    ピクセル単位で比較すると座標の丸めのぶれを拾ってしまうので、4px 格子で
    見る。矩形を1つでも失えばそのぶん確実にセルが減るので、粗くても
    「減った/減っていない」の判定には十分。
    """
    out = set()
    for box, _reg in session.regions.get(frame, []):
        x, y, w, h = box
        x0, y0 = int(x // grid), int(y // grid)
        x1, y1 = int((x + w) // grid), int((y + h) // grid)
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                out.add((gx, gy))
    return out


def test_mark_interval_never_shrinks_painted_cells_across_all_frames():
    """RULES.md 0: 区間を埋めた結果、塗られる範囲が減ってはいけない。

    #66 / #58 で「包含しているはず」が実測せずに2回とも外れた
    （736,126セル / 14,556矩形が失われた）。同じ間違いをしないため、
    区間追従を複数回・複数の位置に適用する前後で、**全フレーム**の
    塗られるセルを突き合わせ、1フレームも減っていないことを実測する。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)  # 既定: 0-9, 40-49 に検出あり（envelope の効く区間を含む）
        before = {f: _cell_set(s, f) for f in range(s.n_frames)}
        before_total = sum(len(v) for v in before.values())

        # 実際の操作を模して、いろいろな位置・大きさで複数回かける
        s.mark_interval(frame=25, tap=(0.05, 0.05), start_frame=15, start_tap=(0.95, 0.95), size=(30, 30))
        s.mark_interval(frame=45, tap=(0.5, 0.5), start_frame=42, start_tap=(0.3, 0.3), size=(15, 15))
        s.mark_interval(frame=5, tap=(0.1, 0.9), start_frame=2, start_tap=(0.9, 0.1), size=(40, 40))

        after = {f: _cell_set(s, f) for f in range(s.n_frames)}
        after_total = sum(len(v) for v in after.values())

        shrunk = [f for f in range(s.n_frames) if before[f] - after[f]]
        print(f"  before_total_cells={before_total} after_total_cells={after_total}")
        print(f"  frames_with_shrinkage={len(shrunk)}")
        assert not shrunk, f"塗られるセルが減ったフレームがある: {shrunk}"
        assert after_total > before_total, "区間を埋めたのに塗布量が増えていない"
        print("  全フレーム突き合わせ: 塗られるセルは1つも減っていない OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_progress_survives_restart():
    """判定の記録がファイルに残り、開き直しても続きから戻れること。

    端末を閉じただけで最初からやり直しになると、長い動画は絶対に終わらない。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        target = s.queue[0]["frame"]
        s.mark(target, "ok")
        s.mark(s.queue[1]["frame"], "unsure")
        assert os.path.exists(s.progress_path)

        again = make_session(tmp)
        assert again.verdicts.get(target) == "ok"
        p = again.progress_payload()
        assert p["done"] == 2 and p["counts"]["unsure"] == 1, p
        assert p["can_undo"], "履歴が読み戻せていない"
        print("  進捗の保存と読み戻し OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_mark_rejects_unknown_verdict():
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        for bad in ("yes", "", "OK"):
            try:
                s.mark(0, bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{bad!r} が通ってしまった")
        try:
            s.mark(0, "fixed")  # 位置なし
        except ValueError:
            pass
        else:
            raise AssertionError("位置なしの fixed が通ってしまった")
        print("  不正な判定の拒否 OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- でかすぎる（塗り過ぎを狭める） --------------------------------------


def _auto_boxes(session, frame):
    """そのフレームで自動的に置かれている矩形（手修正を除く）。"""
    return [b for b, r in session.regions.get(frame, []) if r.source != "manual"]


def test_cover_box_wraps_and_clips():
    """包む矩形は外側に丸め、フレームからははみ出さないこと。

    内側に丸めると、接しているだけの自動領域が remove から外れて残る。
    """
    assert cover_box([(10.2, 20.7, 30.0, 40.0)], 640, 480) == (10.0, 20.0, 31.0, 41.0)
    assert cover_box(
        [(0.0, 0.0, 10.0, 10.0), (600.0, 400.0, 100.0, 100.0)], 640, 480
    ) == (0.0, 0.0, 640.0, 480.0)
    assert cover_box([], 640, 480) is None
    print("  自動領域を包む矩形 OK")


def test_mark_toobig_saves_remove_and_add():
    """「でかすぎる」で remove と add が組で保存されること。

    remove だけだとそのコマが素通しになる。add だけだと塗る範囲が増えるだけで
    「でかすぎる」の意味が逆になる。必ず両方でなければならない。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        auto = _auto_boxes(s, 25)
        assert auto, "前提が崩れている（frame 25 に自動領域が無い）"

        n = s.mark(25, "toobig", tap=(0.5, 0.5), size=(20, 20), span=0, cls=CLS)
        assert n == 2, n
        kinds = sorted(c.kind for c in s.corrections.items)
        assert kinds == ["add", "remove"], kinds
        assert s.verdicts[25] == "toobig"

        rem = next(c for c in s.corrections.items if c.kind == "remove")
        for b in auto:
            assert rem.box[0] <= b[0] and rem.box[1] <= b[1], (rem.box, b)
            assert rem.box[0] + rem.box[2] >= b[0] + b[2], (rem.box, b)
            assert rem.box[1] + rem.box[3] >= b[1] + b[3], (rem.box, b)

        add = next(c for c in s.corrections.items if c.kind == "add")
        assert add.box == (310.0, 230.0, 20.0, 20.0), add.box
        # 狭める操作なので、残る面積は元より小さくなければ意味が無い
        assert add.box[2] * add.box[3] < sum(b[2] * b[3] for b in auto)
        print("  でかすぎる判定で remove + add が保存される OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_toobig_remove_drops_automatic_region():
    """保存した remove が、実際に自動領域を落とすこと。

    セッション内の値だけを見ても、修正ファイルを経由したときに効くとは
    限らない（保存で小数第1位に丸められる）。ファイルから読み直し、
    process() -> apply() の本来の経路を通して確かめる。
    """
    tmp = tempfile.mkdtemp()
    try:
        from automosaic.temporal import process

        s = make_session(tmp)
        before = _auto_boxes(s, 25)
        assert before

        s.mark(25, "toobig", tap=(0.5, 0.5), size=(20, 20), span=0, cls=CLS)
        assert [r.source for _, r in s.regions[25]] == ["manual"], s.regions[25]

        loaded = CorrectionSet.load(s.corrections_path)
        base, _ = process(
            s.per_frame, s.n_frames, s.width, s.height, s.classes, s.cfg
        )
        assert _auto_boxes(s, 25) == [], "セッション側で自動領域が残っている"
        out = apply_corrections(base, loaded)
        assert [r.source for _, r in out[25]] == ["manual"], out[25]
        assert out[25][0][0] == (310.0, 230.0, 20.0, 20.0), out[25][0][0]
        # 素通しにはしない。狭めた範囲は残る
        assert s.coverage[25] == COV_REAL, s.coverage[25]
        print("  remove が自動領域を落とす OK（ファイル経由で確認）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_toobig_rejects_missing_box():
    """範囲の指定なしでは確定させないこと。

    remove だけ置くと、そのコマは何も塗られない状態で残る。「でかすぎる」は
    狭める操作であって、塗らない操作ではない。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        try:
            s.mark(25, "toobig")
        except ValueError:
            pass
        else:
            raise AssertionError("位置なしの toobig が通ってしまった")
        assert not s.corrections.items, "拒否したのに修正が残っている"
        assert 25 not in s.verdicts, "拒否したのに判定が記録されている"
        assert not s.history, "拒否したのに履歴が積まれている"
        assert s.coverage[25] == COV_ESTIMATED, "自動領域が消えている"
        print("  範囲なしの でかすぎる の拒否 OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_undo_toobig_restores_both():
    """ひとつ戻すで remove と add が同時に取り消されること。

    片方だけ残ると、素通し（remove だけ）か塗り過ぎのまま二重（add だけ）に
    なる。どちらも取り消したつもりの利用者には見えない壊れ方をする。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        before = _auto_boxes(s, 25)
        s.mark(25, "toobig", tap=(0.5, 0.5), size=(20, 20), span=0, cls=CLS)
        assert len(s.corrections.items) == 2

        h = s.undo()
        assert h["frame"] == 25 and h["added"] == 2, h
        assert not s.corrections.items, "戻したのに修正が残っている"
        assert 25 not in s.verdicts
        assert _auto_boxes(s, 25) == before, "戻したのに自動領域が復活していない"
        assert s.coverage[25] == COV_ESTIMATED
        print("  でかすぎる のひとつ戻す OK（remove と add の両方）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_toobig_span_applies_to_both():
    """span が remove と add の両方に効くこと。

    キューは間引いて出しているので、1コマだけ狭めても隣のコマは大きいまま。
    remove はフレームごとに作り直す。対象が動いていると位置も大きさも違い、
    1枚を使い回すと端が残る。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        for f in range(23, 28):
            assert _auto_boxes(s, f), f

        n = s.mark(25, "toobig", tap=(0.5, 0.5), size=(20, 20), span=2, cls=CLS)
        assert n == 10, n  # 5 フレーム x (remove + add)
        for f in range(23, 28):
            kinds = sorted(c.kind for c in s.corrections.items if c.frame == f)
            assert kinds == ["add", "remove"], (f, kinds)
            assert [r.source for _, r in s.regions[f]] == ["manual"], (f, s.regions[f])
            assert s.coverage[f] == COV_REAL, f
        # 範囲の外は触らない
        assert _auto_boxes(s, 22) and _auto_boxes(s, 28)
        print("  でかすぎる の適用範囲 OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_progress_keeps_toobig_and_drops_unknown():
    """判定の記録に toobig が増えても、読み書きが壊れないこと。

    知らない判定は黙って捨てる。古い版が書いた記録も、新しい版が書いた記録も、
    開けなくなるくらいなら判定が1つ消えたほうがいい。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        s.mark(25, "toobig", tap=(0.5, 0.5), size=(20, 20), span=0, cls=CLS)
        other = next(it["frame"] for it in s.queue if it["frame"] != 25)
        s.mark(other, "ok")
        p = s.progress_payload()
        assert "toobig" in p["counts"], p["counts"]

        with open(s.progress_path, encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["verdicts"]["25"] == "toobig", saved["verdicts"]
        saved["verdicts"]["31"] = "mirai_no_hantei"  # 知らない判定を混ぜる
        with open(s.progress_path, "w", encoding="utf-8") as f:
            json.dump(saved, f, ensure_ascii=False)

        again = make_session(tmp, corrections=CorrectionSet.load(s.corrections_path))
        assert again.verdicts.get(25) == "toobig"
        assert 31 not in again.verdicts, "知らない判定が読み込まれている"
        print("  判定記録の互換 OK（toobig は残り、未知の判定は捨てる）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- 誤検知（局部ではないところのモザイクを消す） ------------------------


def make_two_region_session(tmp, right=None, n_frames=60):
    """1コマに自動領域が2つ出るセッション。

    誤検知は「選んだ領域だけ」を消す操作なので、区別できる相手がいないと
    確かめられない。時間方向の補完（memory・橋渡し・繋ぎ直し）は全部切って、
    検出を出したフレームがそのまま領域になるようにしてある。

    左は frame 20-40 に居座り、右は frame 24-28 にだけ出る。right に
    (frame -> box) を渡せば右側を動かせる。
    """
    per_frame = {f: [] for f in range(n_frames)}
    for f in range(20, 41):
        per_frame[f].append(Detection(CLS, 0.8, (80, 80, 60, 60)))
    for f in range(24, 29):
        box = right(f) if right else (420, 300, 60, 60)
        per_frame[f].append(Detection(CLS, 0.8, box))
    s = make_session(tmp, per_frame=per_frame, n_frames=n_frames)
    s.cfg = TemporalConfig(
        max_gap=0,
        memory=0,
        memory_before=0,
        bridge_max=0,
        stitch_max_gap=0,
        min_track_len=0,
    )
    s.recompute()
    s.rebuild_queue()
    return s


def _removes(session, frame=None):
    return [
        c
        for c in session.corrections.items
        if c.kind == "remove" and (frame is None or c.frame == frame)
    ]


def test_false_positive_saves_remove_only():
    """「誤検知」で remove だけが保存され、add が保存されないこと。

    そこは局部ではなかったのだから、代わりに塗るものは無い。add が付くと
    「消したはずの場所に塗り直す」という、判定と逆の修正になる。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_two_region_session(tmp)
        autos = [b for b, _ in s.auto_regions(26)]
        assert len(autos) == 2, autos
        right = autos[1]

        n = s.mark(26, "false_positive", pick=[right], span=0, cls=CLS)
        assert n == 1, n
        assert [c.kind for c in s.corrections.items] == ["remove"], s.corrections.items
        assert s.verdicts[26] == "false_positive"

        rem = s.corrections.items[0]
        assert rem.frame == 26
        # 選んだ領域を包み、選んでいない領域には掛からないこと
        assert rem.box[0] <= right[0] and rem.box[1] <= right[1], rem.box
        assert rem.box[0] + rem.box[2] >= right[0] + right[2], rem.box
        assert rem.box[1] + rem.box[3] >= right[1] + right[3], rem.box
        assert rem.box[0] > autos[0][0] + autos[0][2], "選んでいない領域に掛かっている"

        # 否定の例として、検出そのものの矩形が残ること（膨張後ではない）
        assert len(s.false_positives) == 1, s.false_positives
        fp = s.false_positives[0]
        assert fp["frame"] == 26 and fp["class"] == CLS, fp
        assert fp["box"] == [420.0, 300.0, 60.0, 60.0], fp["box"]
        print("  誤検知で remove だけが保存される OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_false_positive_remove_drops_automatic_region():
    """保存した remove が、実際に選んだ自動領域だけを落とすこと。

    セッション内の値だけでは、修正ファイルを経由したときに効くとは限らない
    （保存で小数第1位に丸められる）。ファイルから読み直し、
    process() -> apply() の本来の経路を通して確かめる。
    """
    tmp = tempfile.mkdtemp()
    try:
        from automosaic.temporal import process

        s = make_two_region_session(tmp)
        left, right = [b for b, _ in s.auto_regions(26)]

        s.mark(26, "false_positive", pick=[right], span=0, cls=CLS)
        remaining = [b for b, _ in s.auto_regions(26)]
        assert remaining == [left], remaining

        loaded = CorrectionSet.load(s.corrections_path)
        base, _ = process(s.per_frame, s.n_frames, s.width, s.height, s.classes, s.cfg)
        out = apply_corrections(base, loaded)
        got = [b for b, r in out[26] if r.source != "manual"]
        assert got == [left], got
        # 左が残っているので素通しにはならない
        assert s.coverage[26] == COV_REAL, s.coverage[26]
        print("  誤検知の remove が選んだ領域だけを落とす OK（ファイル経由で確認）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_false_positive_rewraps_each_frame():
    """複数フレーム適用で、フレームごとに対応する領域を包み直すこと。

    対象が動いていると位置も大きさも違うので、1枚を使い回すと端が残るか、
    逆に隣の領域まで巻き込む。各フレームで IoU が最大の領域を選び直す。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_two_region_session(tmp, right=lambda f: (380 + (f - 24) * 20, 300, 60, 60))
        autos = {f: [b for b, _ in s.auto_regions(f)] for f in range(24, 29)}
        right = autos[26][1]

        n = s.mark(26, "false_positive", pick=[right], span=2, cls=CLS)
        assert n == 5, n  # 24-28 の5フレーム、各1件

        for f in range(24, 29):
            rems = _removes(s, f)
            assert len(rems) == 1, (f, rems)
            want, other = autos[f][1], autos[f][0]
            box = rems[0].box
            assert box[0] <= want[0] and box[1] <= want[1], (f, box, want)
            assert box[0] + box[2] >= want[0] + want[2], (f, box, want)
            assert box[0] > other[0] + other[2], (f, "左に掛かっている", box)
            # 各フレームの領域だけを残していること（左は生き残る）
            assert [b for b, _ in s.auto_regions(f)] == [other], f

        # 動いているので、フレームごとに違う矩形になっていること
        xs = {round(_removes(s, f)[0].box[0], 1) for f in range(24, 29)}
        assert len(xs) == 5, xs
        print("  誤検知のフレームごとの包み直し OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_false_positive_skips_frames_without_match():
    """対応する領域が無いフレームには remove を置かないこと。

    重なるものが無いのに近いものを消すと、選んだ覚えのない領域が消える。
    効果の無い remove をばら撒くと、検出をやり直したときに効いてしまう。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_two_region_session(tmp)
        right = [b for b, _ in s.auto_regions(26)][1]

        n = s.mark(26, "false_positive", pick=[right], span=5, cls=CLS)
        assert n == 5, n  # 21-31 のうち、右が出ている 24-28 だけ
        frames = sorted({c.frame for c in _removes(s)})
        assert frames == [24, 25, 26, 27, 28], frames
        # 右が居ないフレームの左は無傷
        for f in (21, 22, 23, 29, 30, 31):
            assert len(s.auto_regions(f)) == 1, f
        assert {p["frame"] for p in s.false_positives} == set(range(24, 29))
        print("  対応する領域が無いフレームは飛ばす OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_false_positive_rejects_missing_pick():
    """どれを消すか決まっていない状態では確定させないこと。

    モザイクを消す方向の操作なので、あいまいなら通さない。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_two_region_session(tmp)
        for pick in (None, [], [[600.0, 10.0, 20.0, 20.0]]):  # 最後は重ならない矩形
            try:
                s.mark(26, "false_positive", pick=pick)
            except ValueError:
                pass
            else:
                raise AssertionError(f"pick={pick} が通ってしまった")
        assert not s.corrections.items, "拒否したのに修正が残っている"
        assert not s.false_positives, "拒否したのに否定例が残っている"
        assert 26 not in s.verdicts, "拒否したのに判定が記録されている"
        assert not s.history, "拒否したのに履歴が積まれている"
        assert len(s.auto_regions(26)) == 2, "自動領域が消えている"
        print("  選択なしの誤検知の拒否 OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_undo_false_positive_restores():
    """ひとつ戻すで、消えた領域も否定例の記録も戻ること。

    修正だけ戻して否定例が残ると、取り消したはずの矩形が学習データ側にだけ
    「局部ではない」として残る。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_two_region_session(tmp)
        before = [b for b, _ in s.auto_regions(26)]
        s.mark(26, "false_positive", pick=[before[1]], span=1, cls=CLS)
        assert len(s.corrections.items) == 3 and len(s.false_positives) == 3

        h = s.undo()
        assert h["frame"] == 26 and h["added"] == 3 and h["fp"] == 3, h
        assert not s.corrections.items, "戻したのに修正が残っている"
        assert not s.false_positives, "戻したのに否定例が残っている"
        assert 26 not in s.verdicts
        assert [b for b, _ in s.auto_regions(26)] == before, "自動領域が復活していない"
        print("  誤検知のひとつ戻す OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_export_dataset_excludes_false_positives():
    """誤検知の矩形をラベルから外し、false_positives.json に出すこと。

    誤検知を「これは局部だ」と教え続けると、ファインチューンでいちばん
    直したいものが直らない。画像そのものは hard negative として使えるので、
    ラベルを空にして残す。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)

        class StubReader:
            def read(self, n):
                return np.zeros((480, 640, 3), dtype=np.uint8)

        s.reader = StubReader()
        # frame 5 は正規の手順で誤検知にする（remove が入り、領域が消える）
        auto5 = [b for b, _ in s.auto_regions(5)]
        s.mark(5, "false_positive", pick=auto5, span=0, cls=CLS)
        assert not s.auto_regions(5)

        # frame 45 は否定例の記録だけを直接置く。remove が効いていなくても
        # ラベルを組む側で落とせることを確かめる（修正ファイルの差し替えや
        # 検出のやり直しで、消したはずの矩形が復活しうる）
        raw45 = next(r.box for _, r in s.regions[45] if r.source == "detected")
        s.false_positives.append(
            {"frame": 45, "box": [round(v, 1) for v in raw45], "class": CLS}
        )

        out = os.path.join(tmp, "ds")
        export_dataset(s, out, quiet=True)

        for f in (5, 45):
            assert os.path.exists(os.path.join(out, "images", f"{f:06d}.png")), f
            body = open(
                os.path.join(out, "labels", f"{f:06d}.txt"), encoding="utf-8"
            ).read().strip()
            assert body == "", f"誤検知がラベルに残っている: {f} {body!r}"

        items = json.load(open(os.path.join(out, "false_positives.json"), encoding="utf-8"))
        assert len(items) == 2, items
        by_frame = {it["frame"]: it for it in items}
        assert sorted(by_frame) == [5, 45], items
        for f, it in by_frame.items():
            assert set(it) == {"frame", "box", "class", "note"}, it
            assert it["class"] == CLS
            assert len(it["box"]) == 4
            assert f"images/{f:06d}.png" in it["note"], it["note"]
        print("  誤検知の学習データ書き出し OK（ラベル除外 + false_positives.json）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_progress_keeps_false_positive_and_reads_old_records():
    """記録に false_positive が残り、古い記録が壊れないこと。

    古い .progress.json には false_positives も履歴の fp も無い。
    無いものを 0 件として読めれば、版が混ざっても続きから再開できる。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_two_region_session(tmp)
        right = [b for b, _ in s.auto_regions(26)][1]
        s.mark(26, "false_positive", pick=[right], span=0, cls=CLS)
        p = s.progress_payload()
        assert "false_positive" in p["counts"], p["counts"]

        again = make_two_region_session(tmp)
        again.corrections = CorrectionSet.load(s.corrections_path)
        again.load_progress()
        assert again.verdicts.get(26) == "false_positive"
        assert len(again.false_positives) == 1, again.false_positives

        # 旧版が書いた形（fp も false_positives も無い）を読ませる
        with open(s.progress_path, encoding="utf-8") as f:
            saved = json.load(f)
        saved.pop("false_positives", None)
        for h in saved["history"]:
            h.pop("fp", None)
        with open(s.progress_path, "w", encoding="utf-8") as f:
            json.dump(saved, f, ensure_ascii=False)

        old = make_two_region_session(tmp)
        old.load_progress()
        assert old.verdicts.get(26) == "false_positive"
        assert old.false_positives == [], old.false_positives
        assert old.history and old.history[-1]["fp"] == 0, old.history
        print("  判定記録の互換 OK（false_positive は残り、旧形式も読める）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- トークン ------------------------------------------------------------


def test_token_matches():
    assert token_matches("abc123", "abc123")
    assert not token_matches("abc123", "abc124")
    assert not token_matches("abc123", "abc1234")  # 前方一致で通さない
    assert not token_matches("abc123", "")
    assert not token_matches("abc123", None)
    # --no-token のときだけ素通し
    assert token_matches("", None) and token_matches("", "なんでも")
    print("  トークンの照合 OK")


def test_cookie_token():
    assert cookie_token("automosaic_t=xyz") == "xyz"
    assert cookie_token("a=1; automosaic_t=xyz; b=2") == "xyz"
    assert cookie_token("automosaic_token=xyz") is None  # 前方一致で拾わない
    assert cookie_token("other=1") is None
    assert cookie_token(None) is None
    print("  Cookie からのトークン取り出し OK")


def test_lan_addresses_are_plausible():
    for a in lan_addresses():
        assert a.count(".") == 3 and not a.startswith("127."), a
    print(f"  LAN アドレスの取得 OK（{lan_addresses() or 'なし'}）")


# -- QR --------------------------------------------------------------------


def test_qr_decodes_with_opencv():
    """自前で組んだ QR が、実際の読み取り器で復号できること。

    外部ライブラリを使えないので符号化は手書きになる。目で見て確かめられない
    種類のコードなので、cv2 の読み取り器に通して往復を確認する。
    """
    det = cv2.QRCodeDetector()
    for text in (
        "http://127.0.0.1:8765/?t=abcdefghijkm",
        "http://192.168.100.23:8765/?t=qwertyuiop23",
        "x" * 100,   # 型番が上がってブロック分割に入る長さ
    ):
        m = qr_matrix(text)
        size = len(m)
        quiet = 4
        img = np.full((size + quiet * 2, size + quiet * 2), 255, np.uint8)
        for r in range(size):
            for c in range(size):
                if m[r][c]:
                    img[r + quiet, c + quiet] = 0
        big = cv2.resize(img, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
        data, _, _ = det.detectAndDecode(big)
        assert data == text, f"復号できない: {data!r}"
    print("  QR の生成と復号 OK")


# -- HTTP ------------------------------------------------------------------


def _serve(session, token):
    ReviewHandler.session = session
    ReviewHandler.token = token
    ReviewHandler.verbose = False
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ReviewHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), r
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e


def _post(url, obj):
    req = urllib.request.Request(
        url,
        data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_http_requires_token():
    """トークンが無い・違う要求は全部 403 で落ちること。

    LAN に出す前提なので、ここが抜けると同じネットワークの誰でも素材を見られる。
    画面も API も画像も、例外なく閉じていることを確かめる。
    """
    tmp = tempfile.mkdtemp()
    httpd = None
    try:
        s = make_session(tmp)
        httpd, base = _serve(s, "testtoken1234")
        for path in ("/", "/timeline", "/static/app.js", "/api/state", "/api/queue", "/frame?n=0"):
            code, _, _ = _get(base + path)
            assert code == 403, f"{path} がトークン無しで {code}"
            code, _, _ = _get(base + path + ("&" if "?" in path else "?") + "t=chigau")
            assert code == 403, f"{path} が誤ったトークンで {code}"
        code, _ = _post(base + "/api/mark", {"frame": 0, "verdict": "ok"})
        assert code == 403, code
        code, _ = _post(base + "/api/undo", {})
        assert code == 403, code
        assert not s.verdicts, "403 なのに判定が記録されている"
        print("  トークン無しの遮断 OK")
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_http_accepts_token_in_query_cookie_and_header():
    tmp = tempfile.mkdtemp()
    httpd = None
    try:
        s = make_session(tmp)
        tok = "testtoken1234"
        httpd, base = _serve(s, tok)

        code, body, res = _get(f"{base}/api/state?t={tok}")
        assert code == 200, code
        assert json.loads(body)["n_frames"] == s.n_frames
        # 通ったら Cookie に移す。以降の画像取得に毎回付けさせないため
        assert f"{COOKIE_NAME}={tok}" in (res.headers.get("Set-Cookie") or "")

        code, _, _ = _get(f"{base}/api/state", {"Cookie": f"automosaic_t={tok}"})
        assert code == 200, code
        code, _, _ = _get(f"{base}/api/state", {"X-Review-Token": tok})
        assert code == 200, code
        code, _, _ = _get(f"{base}/", {"X-Review-Token": tok})
        assert code == 200, "画面本体が開けない"
        print("  URL / Cookie / ヘッダのトークン受け入れ OK")
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_http_queue_and_mark_roundtrip():
    """キューを取り、判定を投げ、進捗が進み、戻せること。"""
    tmp = tempfile.mkdtemp()
    httpd = None
    try:
        s = make_session(tmp)
        tok = "testtoken1234"
        httpd, base = _serve(s, tok)

        code, body, _ = _get(f"{base}/api/queue?t={tok}")
        assert code == 200
        q = json.loads(body)
        assert q["items"], "キューが空"
        prios = [it["priority"] for it in q["items"]]
        assert prios == sorted(prios)
        assert q["progress"]["done"] == 0
        assert all(it["verdict"] is None for it in q["items"])

        first = q["items"][0]["frame"]
        code, d = _post(f"{base}/api/mark?t={tok}", {"frame": first, "verdict": "ok"})
        assert code == 200 and d["progress"]["done"] == 1, d
        second = q["items"][1]["frame"]
        code, d = _post(
            f"{base}/api/mark?t={tok}",
            {"frame": second, "verdict": "fixed", "x": 0.5, "y": 0.5,
             "w": 64, "h": 64, "span": 1, "class": CLS},
        )
        assert code == 200 and d["added"] == 3, d
        assert d["n_corrections"] == 3
        assert any(r[4] == "x" for r in d["regions"]), "手修正が領域に入っていない"

        # 判定済みの印がキューにも載ること（続きから再開できる根拠）
        code, body, _ = _get(f"{base}/api/queue?t={tok}")
        items = {it["frame"]: it["verdict"] for it in json.loads(body)["items"]}
        assert items[first] == "ok" and items[second] == "fixed"

        code, d = _post(f"{base}/api/undo?t={tok}", {})
        assert code == 200 and d["ok"] and d["frame"] == second, d
        assert d["n_corrections"] == 0 and d["progress"]["done"] == 1
        print("  キュー取得 -> 判定 -> 取り消し OK")
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_http_frame_image_is_scaled_and_mosaicked():
    """/frame が縮小と JPEG 化に対応していること。

    端末に原寸 PNG を投げるとタップの手応えが消えるので、幅を指定できることは
    体感速度そのもの。実ファイルが要るので小さい動画をその場で作る。
    """
    tmp = tempfile.mkdtemp()
    httpd = None
    try:
        path = os.path.join(tmp, "src.avi")
        vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 30.0, (640, 480))
        if not vw.isOpened():
            print("  /frame の確認は飛ばします（VideoWriter を開けない）")
            return
        rng = np.random.default_rng(0)
        for _ in range(60):
            vw.write(rng.integers(0, 256, (480, 640, 3), dtype=np.uint8))
        vw.release()

        s = make_session(tmp)
        s.video = path
        from automosaic.review import FrameReader

        s.reader = FrameReader(path)
        tok = "testtoken1234"
        httpd, base = _serve(s, tok)

        code, body, res = _get(f"{base}/frame?n=5&t={tok}")
        assert code == 200 and res.headers["Content-Type"] == "image/png", code
        full = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
        assert full.shape[1] == 640

        code, body, res = _get(f"{base}/frame?n=5&fmt=jpg&w=320&v=1&t={tok}")
        assert code == 200 and res.headers["Content-Type"] == "image/jpeg"
        assert "max-age" in (res.headers.get("Cache-Control") or ""), "先読みが効かない"
        small = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
        assert small.shape[1] == 320, small.shape
        assert len(body) < 200_000, len(body)
        print(f"  /frame の縮小と JPEG 化 OK（640px PNG {len(full.tobytes())//1024}KB 相当 "
              f"-> 320px JPEG {len(body)//1024}KB）")
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_http_frame_not_blocked_by_recompute():
    """手修正の保存中（recompute()）でも /frame が止まらないこと。

    ReviewHandler._serve_frame は以前 s.lock を取っていたので、
    /api/mark が recompute() で止まっている間は /frame が1本も返らなかった
    （issue #25）。実測の「初回構築 5.2秒」と同じ形（重い計算の最中）を
    作るため、review.process を一時的に遅くする（遅延の注入自体は PR #45 の
    TOCTOU 再現と同じ手法）。固まらないこと・壊れた画像を返さないこと・
    recompute 完了後は必ず新しい絵になることを実際の HTTP で確かめる。
    """
    tmp = tempfile.mkdtemp()
    httpd = None
    orig_process = review.process
    try:
        path = os.path.join(tmp, "src.avi")
        vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 30.0, (640, 480))
        if not vw.isOpened():
            print("  recompute 中の /frame の確認は飛ばします（VideoWriter を開けない）")
            return
        rng = np.random.default_rng(0)
        for _ in range(60):
            vw.write(rng.integers(0, 256, (480, 640, 3), dtype=np.uint8))
        vw.release()

        s = make_session(tmp)
        s.video = path
        from automosaic.review import FrameReader

        s.reader = FrameReader(path)
        tok = "testtoken1234"
        httpd, base = _serve(s, tok)

        # フレーム 30 は make_session の既定検出（0-9, 40-49）の外なので、
        # 修正前は素通しの絵のはず
        code, before, _ = _get(f"{base}/frame?n=30&t={tok}")
        assert code == 200, code

        started = threading.Event()
        release = threading.Event()

        def slow_process(*a, **kw):
            started.set()
            release.wait(timeout=5)
            return orig_process(*a, **kw)

        review.process = slow_process
        try:
            result = {}

            def do_mark():
                code, d = _post(
                    f"{base}/api/mark?t={tok}",
                    {"frame": 30, "verdict": "fixed", "x": 0.5, "y": 0.5, "w": 80, "h": 80},
                )
                result["code"] = code
                result["body"] = d

            th = threading.Thread(target=do_mark)
            t0 = time.monotonic()
            th.start()
            assert started.wait(timeout=5), "recompute が呼ばれなかった"

            frame_times: list[float] = []
            frame_codes: list[int] = []

            def hit_frame():
                t1 = time.monotonic()
                code, body, _ = _get(f"{base}/frame?n=5&t={tok}")
                frame_times.append(time.monotonic() - t1)
                frame_codes.append(code)
                img = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
                assert img is not None and img.shape[1] == 640, "壊れた画像が返った"

            with ThreadPoolExecutor(max_workers=10) as ex:
                list(ex.map(lambda _: hit_frame(), range(10)))
            during_elapsed = time.monotonic() - t0

            release.set()
            th.join(timeout=10)
        finally:
            review.process = orig_process

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

        code, after, _ = _get(f"{base}/frame?n=30&t={tok}")
        assert code == 200
        assert after != before, "recompute 後なのに古い絵のまま"
        print("  recompute 完了後の /frame は新しい領域を反映する OK")
    finally:
        review.process = orig_process
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_http_toobig_roundtrip():
    """HTTP 越しに でかすぎる を投げ、狭まって、戻せること。"""
    tmp = tempfile.mkdtemp()
    httpd = None
    try:
        s = make_session(tmp)
        tok = "testtoken1234"
        httpd, base = _serve(s, tok)

        code, d = _post(
            f"{base}/api/mark?t={tok}",
            {"frame": 25, "verdict": "toobig", "x": 0.5, "y": 0.5,
             "w": 20, "h": 20, "span": 0, "class": CLS},
        )
        assert code == 200, d
        assert d["added"] == 2 and d["n_corrections"] == 2, d
        assert [r[4] for r in d["regions"]] == ["x"], d["regions"]
        assert d["progress"]["counts"]["toobig"] == 1, d["progress"]

        # 範囲なしは 400。素通しになる修正をサーバ側でも作らせない
        code, d = _post(f"{base}/api/mark?t={tok}", {"frame": 30, "verdict": "toobig"})
        assert code == 400, (code, d)
        assert len(s.corrections.items) == 2, "拒否したのに修正が増えている"

        code, d = _post(f"{base}/api/undo?t={tok}", {})
        assert code == 200 and d["removed"] == 2, d
        assert d["n_corrections"] == 0
        assert [r[4] for r in d["regions"]] != ["x"], d["regions"]
        print("  HTTP 越しの でかすぎる OK")
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_http_false_positive_roundtrip():
    """HTTP 越しに 誤検知 を投げ、選んだ領域だけが消えて、戻せること。"""
    tmp = tempfile.mkdtemp()
    httpd = None
    try:
        s = make_two_region_session(tmp)
        # 検出が続いている区間は既定ではキューに載らない。進捗の集計まで
        # 見たいので、全フレームを対象にして 25 が並ぶようにする
        s.queue_all = True
        s.rebuild_queue()
        assert any(it["frame"] == 25 for it in s.queue)
        left, right = [b for b, _ in s.auto_regions(25)]
        tok = "testtoken1234"
        httpd, base = _serve(s, tok)

        # 画面は丸めた整数の矩形を返してくるので、そのまま送っても
        # 実体に寄せ直せることを確かめる
        pick = [[int(round(v)) for v in right]]
        code, d = _post(
            f"{base}/api/mark?t={tok}",
            {"frame": 25, "verdict": "false_positive", "pick": pick,
             "span": 0, "class": CLS},
        )
        assert code == 200, d
        assert d["added"] == 1 and d["n_corrections"] == 1, d
        assert len(d["regions"]) == 1, d["regions"]
        assert d["regions"][0][:4] == [int(round(v)) for v in left], d["regions"]
        assert d["progress"]["counts"]["false_positive"] == 1, d["progress"]
        assert [c.kind for c in s.corrections.items] == ["remove"], s.corrections.items

        # 選択なしは 400。何が消えるか決まっていない修正は作らせない
        code, d = _post(f"{base}/api/mark?t={tok}", {"frame": 27, "verdict": "false_positive"})
        assert code == 400, (code, d)
        assert len(s.corrections.items) == 1, "拒否したのに修正が増えている"

        code, d = _post(f"{base}/api/undo?t={tok}", {})
        assert code == 200 and d["removed"] == 1, d
        assert d["n_corrections"] == 0 and len(d["regions"]) == 2, d
        assert not s.false_positives, s.false_positives
        print("  HTTP 越しの 誤検知 OK")
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_http_corrections_scoped_to_frame():
    """POST /api/corrections に frame を渡すと、応答の regions がそのコマぶんだけに
    絞られること（#24）。渡さなければ従来どおり全フレームぶん（後方互換）。
    """
    tmp = tempfile.mkdtemp()
    httpd = None
    try:
        s = make_session(tmp, n_frames=2000)
        tok = "testtoken1234"
        httpd, base = _serve(s, tok)

        code, body, _ = _get(f"{base}/api/corrections?t={tok}")
        cur = json.loads(body)["corrections"]
        extra = {"frame": 3, "box": [10, 10, 20, 20], "class": CLS, "kind": "add"}

        code, d = _post(
            f"{base}/api/corrections?t={tok}", {"corrections": cur + [extra], "frame": 3}
        )
        assert code == 200, d
        assert list(d["regions"].keys()) == ["3"], d["regions"].keys()

        code, d2 = _post(f"{base}/api/corrections?t={tok}", {"corrections": cur + [extra]})
        assert code == 200, d2
        assert len(d2["regions"]) > 1, "frame 未指定なのに全フレームぶんに戻っていない"
        print("  POST /api/corrections の frame 指定で regions を絞れる OK（未指定は従来どおり）")
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_http_regions_endpoint_returns_single_frame():
    """GET /api/regions が1コマぶんの矩形だけを返すこと（#24 のタイムライン用エンドポイント）。

    修正のたびに動画全体の regions を積む代わりに、画面はここへ来て
    表示中の1コマぶんだけを取り直す。
    """
    tmp = tempfile.mkdtemp()
    httpd = None
    try:
        s = make_session(tmp)
        tok = "testtoken1234"
        httpd, base = _serve(s, tok)

        code, body, _ = _get(f"{base}/api/regions?n=0&t={tok}")
        assert code == 200, body
        d = json.loads(body)
        assert d["frame"] == 0
        assert d["regions"] == s.frame_regions(0)
        assert d["version"] == s.version

        # トークン無しは 403（他の API と揃える。素材が漏れる経路なので必須）
        code, _, _ = _get(f"{base}/api/regions?n=0")
        assert code == 403, code
        print("  GET /api/regions が1コマぶんだけ返す OK")
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_corrections_payload_must_be_a_list():
    """corrections キーの無い本文で全消ししないこと（webapp と同じ修正）。

    「空の一覧で置き換えろ」と読むと、壊れた本文ひとつで手修正が全部消える。
    判定（.progress.json）は残るので、「塞いだ」と表示されたまま
    塞いだ矩形だけが無い状態、つまりそのフレームは素通しになる。
    """
    tmp = tempfile.mkdtemp()
    httpd = None
    try:
        s = make_session(tmp)
        tok = "testtoken1234"
        httpd, base = _serve(s, tok)

        code, d = _post(
            f"{base}/api/mark?t={tok}",
            {"frame": 5, "verdict": "fixed", "x": 0.5, "y": 0.5, "w": 40, "h": 40},
        )
        assert code == 200 and d["added"] == 1, d

        for bad in ({}, {"foo": "bar"}, {"corrections": None}, {"corrections": {}}):
            code, d = _post(f"{base}/api/corrections?t={tok}", bad)
            assert code == 400, f"{bad} を通した: {code} {d}"
        assert len(s.corrections.items) == 1, "本文が壊れているのに消えた"

        # 空配列を明示で送ったときだけ全消しになる
        code, d = _post(f"{base}/api/corrections?t={tok}", {"corrections": []})
        assert code == 200 and d["n_corrections"] == 0, d
        print("  corrections キーの無い本文を拒否 OK（明示の空配列だけ通る）")
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_corrections_replace_does_not_resurrect_history_on_reopen():
    """一覧を差し替えたあと、履歴がセッションの作り直しで生き返らないこと（W-1）。

    set_corrections は履歴を捨てるが、捨てたことを保存しないと
    .progress.json から読み戻される。生き返った履歴は差し替え後の一覧では
    別物を指すので、undo が無関係な修正（漏れを塞いだ矩形）を末尾から削る。

    監査文書の3手をそのまま再現する。
    A) frame 10 を span=5 で塞ぐ（add 11件、frame 5-15）
    B) 別端末のタイムラインが、frame 40-50 の add 11件を足した一覧を
       丸ごと POST（22件）
    C) セッションを開き直して「ひとつ戻す」
       -> 直したはずの frame 40-50 が無関係に消えてはいけない
    """
    tmp = tempfile.mkdtemp()
    httpd = None
    try:
        s = make_session(tmp)
        tok = "testtoken1234"
        httpd, base = _serve(s, tok)

        # A
        code, d = _post(
            f"{base}/api/mark?t={tok}",
            {"frame": 10, "verdict": "fixed", "x": 0.5, "y": 0.5, "w": 64, "h": 64, "span": 5},
        )
        assert code == 200 and d["added"] == 11, d

        code, body, _ = _get(f"{base}/api/corrections?t={tok}")
        cur = json.loads(body)

        # B（別端末視点。この画面自身の session オブジェクトに直接 POST する）
        extra = [
            {"frame": f, "box": [400, 400, 60, 60], "class": CLS, "kind": "add"}
            for f in range(40, 51)
        ]
        code, d = _post(
            f"{base}/api/corrections?t={tok}", {"corrections": cur["corrections"] + extra}
        )
        assert code == 200 and d["n_corrections"] == 22, d

        httpd.shutdown()
        httpd.server_close()
        httpd = None

        # C（開き直し。別端末・サーバ再起動・展開など、ディスクから読み直す
        # すべての経路の代表として、ここでは新しい ReviewSession を作る）
        again = make_session(tmp, corrections=CorrectionSet.load(s.corrections_path))
        assert not again.history, "捨てたはずの履歴が生き返っている"

        httpd, base = _serve(again, tok)
        code, d = _post(f"{base}/api/undo?t={tok}", {})
        assert d["ok"] is False, "戻す履歴が無いはずなのに戻ってしまった"

        code, body, _ = _get(f"{base}/api/corrections?t={tok}")
        frames = sorted({c["frame"] for c in json.loads(body)["corrections"]})
        assert frames == list(range(5, 16)) + list(range(40, 51)), (
            f"frame 40-50 が無関係に消えている: {frames}"
        )
        print("  修正の差し替え後、履歴が生き返らず frame 40-50 が消えない OK（W-1）")
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_mark_rejects_out_of_range_frame():
    """範囲外のフレームを拒否すること（webapp と揃える）。

    黙って端へ寄せると、応答は送った番号を返すのに記録は最終フレーム（や 0）に
    付く。「判定したはずのコマが未判定のまま」「触っていないコマに判定が付く」
    の両方が画面には出ないまま起きる。同じ素材を review.py と webapp の
    両画面で見たときに判定が噛み合わなくなる。
    """
    tmp = tempfile.mkdtemp()
    try:
        s = make_session(tmp)
        for bad in (s.n_frames, s.n_frames + 1, 999999, -1, -5):
            try:
                s.mark(bad, "ok")
            except ValueError:
                pass
            else:
                raise AssertionError(f"frame={bad} が通ってしまった")
        assert not s.verdicts, "拒否したのに判定が記録されている"
        print("  範囲外フレームの拒否 OK（session.mark 直接）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_http_mark_rejects_out_of_range_frame():
    tmp = tempfile.mkdtemp()
    httpd = None
    try:
        s = make_session(tmp)
        tok = "testtoken1234"
        httpd, base = _serve(s, tok)
        n = s.n_frames
        for bad in (n, n + 1, 999999, -1):
            code, d = _post(f"{base}/api/mark?t={tok}", {"frame": bad, "verdict": "ok"})
            assert code == 400, f"frame={bad} を通した: {code} {d}"
        assert not s.verdicts, "拒否したのに判定が記録されている"
        assert not os.path.exists(s.progress_path) or not json.load(
            open(s.progress_path, encoding="utf-8")
        ).get("verdicts"), "拒否したのに進捗ファイルに記録されている"

        # 範囲内は通ること
        code, d = _post(f"{base}/api/mark?t={tok}", {"frame": n - 1, "verdict": "ok"})
        assert code == 200 and d["frame"] == n - 1, d
        print(f"  範囲外フレーム（{n}, 999999, -1）の HTTP 越しの拒否 OK")
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


# -- 推定のみ区間（サンプリング刻みを考慮する） ---------------------------


def test_ranges_keep_short_estimated_gaps():
    """1〜4フレームの短い推定のみ区間も、報告とキューから落とさないこと。

    以前はここを自前の runs_of(coverage, COV_ESTIMATED, min_len=5) で
    実装しており、2〜4フレームの推定のみ区間がまるごと報告から消えていた。
    人手レビューが実際に行われる画面（タイムラインの区間リストと G キー）が
    見ているのはここなので、ここが見えないのがいちばん悪い。
    temporal.estimated_only_ranges() はサンプリング刻みを自動推定したうえで
    min_len=1 を効かせるので、間引きなしの素材では短い区間も残る。
    """
    tmp = tempfile.mkdtemp()
    try:
        n_frames = 40
        per_frame = {f: [] for f in range(n_frames)}
        # 連続検出 0-4 と 7-39。5-6 の2フレームだけが推定のみの短い区間になる
        for f in list(range(0, 5)) + list(range(7, n_frames)):
            per_frame[f] = [Detection(CLS, 0.8, (200, 200, 60, 60))]
        s = make_session(tmp, per_frame=per_frame, n_frames=n_frames)

        assert s.coverage[5] == COV_ESTIMATED and s.coverage[6] == COV_ESTIMATED
        d = s.ranges_payload()
        assert {"start": 5, "end": 6, "frames": 2} in d["estimated_only_ranges"], (
            d["estimated_only_ranges"]
        )
        assert any(
            it["reason"] == "estimated" and 5 <= it["frame"] <= 6 for it in s.queue
        ), s.queue
        print("  短い推定のみ区間（2フレーム）が報告・キューに残る OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- review.py と cli.py の TemporalConfig 一致（issue #14） --------------


def test_review_cfg_matches_cli_cfg_for_default_job():
    """review.py が組む cfg が、cli.py が実際に焼くときの cfg とフィールド単位で
    一致すること（既定ジョブ相当。どちらも明示的な時間方向オプション無し）。

    直す前は margin_scale/margin_cap の既定値が cli.py と食い違い、かつ
    review.py に --estimate-gaps の絞り込みが無かったため、レビュー画面は
    焼き込みで使われない memory・橋渡し・不確かさ膨張ぶんの領域を「塗られて
    いる」と見せていた（実測: bench3 素材で 9,344 フレーム。issue #14 記載の
    実素材では 5,950 フレーム）。人が「塞がっている」と見て OK にしたフレームが
    出力では素通しになる、という漏れる方向の壊れ方だった。

    cli.py 側は python -m automosaic を --detect-only --reuse-detections で
    実際に走らせ、本番と同じコード経路（--estimate-gaps 無しの絞り込みを含む）で
    組んだ TemporalConfig を temporal.process() の呼び出しから捕まえる。
    review.py 側も session_from_args() の実コード経路から同様に捕まえ、
    dataclasses.fields() で全フィールドを突き合わせる。
    """
    if not _have_ffmpeg():
        print("  review cfg == cli cfg (既定ジョブ) SKIP (ffmpeg 無し)")
        return

    tmp = tempfile.mkdtemp()
    try:
        src = os.path.join(tmp, "in.mp4")
        det = os.path.join(tmp, "det.json")
        n = 10
        _make_tiny_video(src, 64, 64, n)
        with open(det, "w", encoding="utf-8") as f:
            json.dump(
                {"n_frames": n, "width": 64, "height": 64,
                 "complete": True, "detections": {}},
                f,
            )

        # cli.py が実際に焼くときに組む cfg を、temporal.process() の呼び出しから
        # 捕まえる。--detect-only で描画までは進めない
        captured_cli = {}
        orig_cli_process = cli.process

        def fake_cli_process(*a, **kw):
            captured_cli["cfg"] = kw.get("cfg", a[-1] if a else None)
            return orig_cli_process(*a, **kw)

        cli.process = fake_cli_process
        try:
            rc = cli.main(
                [src, "--detections", det, "--reuse-detections",
                 "--detect-only", "--quiet"]
            )
        finally:
            cli.process = orig_cli_process
        assert rc == 0, "cli.main が既定ジョブ相当の引数で失敗した"
        assert "cfg" in captured_cli, "cli.py 側の cfg を捕まえられなかった"

        # review.py が組む cfg を、同じ経路（session_from_args）から捕まえる
        captured_rev = {}
        orig_rev_process = review.process

        def fake_rev_process(*a, **kw):
            captured_rev["cfg"] = kw.get("cfg", a[-1] if a else None)
            return orig_rev_process(*a, **kw)

        review.process = fake_rev_process
        try:
            argv = [src, "--detections", det]
            args = review.build_parser().parse_args(argv)
            session = review.session_from_args(args, argv)
        finally:
            review.process = orig_rev_process
        assert "cfg" in captured_rev, "review.py 側の cfg を捕まえられなかった"
        session.reader.close()

        cli_cfg = captured_cli["cfg"]
        rev_cfg = captured_rev["cfg"]
        mismatches = []
        for fld in dataclasses.fields(cli_cfg):
            a = getattr(cli_cfg, fld.name)
            b = getattr(rev_cfg, fld.name)
            if a != b:
                mismatches.append((fld.name, a, b))
        assert not mismatches, (
            f"review 側と cli 側の TemporalConfig が食い違う（フィールド, cli値, "
            f"review値）: {mismatches}"
        )
        print(
            "  review cfg == cli cfg (既定ジョブ, "
            f"{len(dataclasses.fields(cli_cfg))} フィールド全一致) OK"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- review.py と cli.py の block/mode/classes 既定値一致（issue #33） -----


def test_review_block_mode_classes_defaults_match_cli():
    """review.py の --block / --mode / --classes の既定値が cli.py と一致すること。

    この3つは TemporalConfig のフィールドではないため、
    test_review_cfg_matches_cli_cfg_for_default_job（#14）の比較対象に
    含まれない。実際、review.py の --block と --mode は cli.py と同じ値を
    ここに直書きしているだけで、cli.py の build_parser() から借りていなかった
    （--classes は #14 で既に借りる形になっていた）。

    このテストを追加する前に、review.py の --block の既定値だけを 0 -> 40 に、
    --mode を "pixelize" -> "black" に書き換えて
    tests/test_review.py tests/test_render.py tests/test_webapp.py を
    フルスイート実行し、**1件も失敗しないこと**を確認した（issue #33 記載の
    変異D/Eの再現）。焼き込みと違うブロックサイズ・モードでレビュー画面を
    見せても、既存のどのテストも検出しない構造的な穴だった。
    """
    cli_defaults = review._cli_defaults()
    rev_args = review.build_parser().parse_args([])

    mismatches = []
    if rev_args.block != cli_defaults["--block"]:
        mismatches.append(("block", cli_defaults["--block"], rev_args.block))
    if rev_args.mode != cli_defaults["--mode"]:
        mismatches.append(("mode", cli_defaults["--mode"], rev_args.mode))
    if rev_args.classes != cli_defaults["--classes"]:
        mismatches.append(("classes", cli_defaults["--classes"], rev_args.classes))

    assert not mismatches, (
        f"review.py の既定値が cli.py と食い違う（フィールド, cli値, review値）: "
        f"{mismatches}"
    )
    print(
        "  review --block/--mode/--classes 既定値 == cli 既定値 "
        f"(block={rev_args.block}, mode={rev_args.mode}) OK"
    )


# -- resolve_classes() の解決先そのものの検証（issue #33 / #56） ----------


def test_resolve_classes_resolves_to_known_class_sets():
    """resolve_classes() が spec 文字列をどの class 集合に解決するかを直接見る。

    test_review_block_mode_classes_defaults_match_cli は args.classes という
    文字列（"default"）の一致しか見ておらず、その文字列が実際にどの class
    集合に解決されるかを検証していなかった（#56 の却下理由）。ここでは
    resolve_classes() の "default" 分岐を CONSERVATIVE_CLASSES に差し替える
    ような変異（DEFAULT_CLASSES と CONSERVATIVE_CLASSES は要素が異なる）を
    直接検出できるよう、既知の集合と突き合わせる。
    """
    assert resolve_classes("default") == set(DEFAULT_CLASSES)
    assert resolve_classes("conservative") == set(CONSERVATIVE_CLASSES)
    assert resolve_classes("default") != set(CONSERVATIVE_CLASSES), (
        "DEFAULT_CLASSES と CONSERVATIVE_CLASSES が区別できない前提が壊れている"
    )
    assert resolve_classes("FEMALE_GENITALIA_EXPOSED, ANUS_EXPOSED") == {
        "FEMALE_GENITALIA_EXPOSED",
        "ANUS_EXPOSED",
    }
    assert resolve_classes("") == set()
    print("  resolve_classes() の解決先 (default/conservative/明示指定) OK")


def test_review_resolved_classes_match_cli_and_known_default():
    """cli.main() と review.session_from_args() が実際に process() へ渡す
    classes 集合が、(1) 既知の正解（DEFAULT_CLASSES / CONSERVATIVE_CLASSES）
    と一致し、(2) 互いにも一致すること。

    文字列 "default" の一致だけを見るテストでは、resolve_classes() の中身が
    どちらの側でも同じように壊れていると検出できない（1か所化した後は
    cli.py と review.py が同じ関数を呼ぶため、cli 側と review 側の比較だけ
    では共有関数そのものの壊れを検出できない）。そのため、既知の正解集合
    との比較を必須にしている。process() は positional で
    (per_frame, n_frames, w, h, classes, cfg) を受け取るので、その呼び出しを
    monkeypatch で捕まえて実際に渡った classes 集合そのものを見る。
    """
    if not _have_ffmpeg():
        print("  review classes == cli classes (既定ジョブ) SKIP (ffmpeg 無し)")
        return

    def _capture_classes(argv_extra: list[str]) -> tuple[set[str], set[str]]:
        tmp = tempfile.mkdtemp()
        try:
            src = os.path.join(tmp, "in.mp4")
            det = os.path.join(tmp, "det.json")
            n = 10
            _make_tiny_video(src, 64, 64, n)
            with open(det, "w", encoding="utf-8") as f:
                json.dump(
                    {"n_frames": n, "width": 64, "height": 64,
                     "complete": True, "detections": {}},
                    f,
                )

            captured_cli = {}
            orig_cli_process = cli.process

            def fake_cli_process(*a, **kw):
                captured_cli["classes"] = (
                    kw["classes"] if "classes" in kw else a[4]
                )
                return orig_cli_process(*a, **kw)

            cli.process = fake_cli_process
            try:
                rc = cli.main(
                    [src, "--detections", det, "--reuse-detections",
                     "--detect-only", "--quiet", *argv_extra]
                )
            finally:
                cli.process = orig_cli_process
            assert rc == 0, "cli.main が失敗した"
            assert "classes" in captured_cli, "cli.py 側の classes を捕まえられなかった"

            captured_rev = {}
            orig_rev_process = review.process

            def fake_rev_process(*a, **kw):
                captured_rev["classes"] = (
                    kw["classes"] if "classes" in kw else a[4]
                )
                return orig_rev_process(*a, **kw)

            review.process = fake_rev_process
            try:
                argv = [src, "--detections", det, *argv_extra]
                args = review.build_parser().parse_args(argv)
                session = review.session_from_args(args, argv)
            finally:
                review.process = orig_rev_process
            assert "classes" in captured_rev, (
                "review.py 側の classes を捕まえられなかった"
            )
            session.reader.close()

            return captured_cli["classes"], captured_rev["classes"]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    cases = [
        ([], set(DEFAULT_CLASSES)),
        (["--classes", "default"], set(DEFAULT_CLASSES)),
        (["--classes", "conservative"], set(CONSERVATIVE_CLASSES)),
        (
            ["--classes", "FEMALE_GENITALIA_EXPOSED,MALE_GENITALIA_EXPOSED"],
            {"FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED"},
        ),
    ]
    for argv_extra, expected in cases:
        cli_classes, rev_classes = _capture_classes(argv_extra)
        assert cli_classes == expected, (
            f"cli.py が process() に渡した classes が既知の正解と食い違う "
            f"(argv={argv_extra}): 実際={sorted(cli_classes)} 正解={sorted(expected)}"
        )
        assert rev_classes == expected, (
            f"review.py が process() に渡した classes が既知の正解と食い違う "
            f"(argv={argv_extra}): 実際={sorted(rev_classes)} 正解={sorted(expected)}"
        )
        assert cli_classes == rev_classes, (
            f"process() へ渡る classes 集合が cli.py と review.py で食い違う "
            f"(argv={argv_extra}): cli={sorted(cli_classes)} "
            f"review={sorted(rev_classes)}"
        )
    print(
        f"  review classes == cli classes == 既知の正解 "
        f"({len(cases)} 通りの --classes 指定) OK"
    )


# -- 実効設定フィンガープリント（issue #16） --------------------------------


def test_effective_settings_fingerprint_catches_margin_scale_regression():
    """issue #16 の完了条件: #14 の再現条件（margin_scale だけが食い違う）を
    作ると、フィンガープリントが実際に落ちる（一致しない）こと。

    #14 は margin_scale の既定値が review.py と cli.py で食い違っていた壊れ方
    だった（review 側が margin_scale=1.0 に「戻って」いた）。ここでは実際に
    margin_scale だけを変えた2つの TemporalConfig からフィンガープリントを
    作り、他のフィールドが完全に同じでも一致しなくなることを確かめる。
    一致してしまうなら、この検査機構自体が #14 を検出できないことになる。
    """
    base = dataclasses.asdict(TemporalConfig())
    cfg_burned = TemporalConfig(**{**base, "margin_scale": 0.6})  # 実際に焼いた側
    cfg_reviewed = TemporalConfig(**{**base, "margin_scale": 1.0})  # #14 の壊れ方

    classes = {"MALE_GENITALIA_EXPOSED"}
    eff_burned = effective_settings(cfg_burned, classes, 40, "black")
    eff_reviewed = effective_settings(cfg_reviewed, classes, 40, "black")
    sha_burned = effective_settings_sha256(eff_burned)
    sha_reviewed = effective_settings_sha256(eff_reviewed)

    assert sha_burned != sha_reviewed, (
        "margin_scale だけが違う2つの実効設定が同じフィンガープリントになった。"
        "#14 と同じ壊れ方をこの検査は検出できない"
    )
    # 逆に、他のフィールドも含めて完全に同じ入力なら同じフィンガープリントに
    # なること（誤検出しないことの確認）
    eff_same = effective_settings(cfg_burned, classes, 40, "black")
    assert effective_settings_sha256(eff_same) == sha_burned
    print(
        "  実効設定フィンガープリント: margin_scale の食い違い(#14相当)を検出 OK"
        f"（burned={sha_burned[:12]}... / reviewed={sha_reviewed[:12]}...）"
    )


def test_effective_settings_fingerprint_ignores_class_set_order():
    """クラス集合の順序（set の反復順）でフィンガープリントが変わらないこと。

    変わってしまうと、同じ --classes 指定でもプロセスをまたぐたびに 409 が
    発火しうる（Python の set 反復順はハッシュシードで変わりうる）。
    effective_settings() 内で sorted() しているので、集合の構築順序を変えても
    一致するはずというのを実測で確かめる。
    """
    cfg = TemporalConfig()
    a = effective_settings_sha256(
        effective_settings(cfg, {"MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED"}, 32, "pixelize")
    )
    b = effective_settings_sha256(
        effective_settings(cfg, {"ANUS_EXPOSED", "MALE_GENITALIA_EXPOSED"}, 32, "pixelize")
    )
    assert a == b, "クラス集合の構築順序でフィンガープリントが変わった"
    print("  実効設定フィンガープリント: クラス集合の順序非依存 OK")


def test_review_session_effective_settings_matches_cli_report_for_default_job():
    """cli.py が report.json に書く effective_sha256 と、review.py の
    ReviewSession.effective_sha256() が、既定ジョブ相当で一致すること。

    ここが一致しないと、設定を何も変えていない普通のジョブまで 409 になる
    （過剰に塞ぐ誤検出。RULES.md が禁じる漏れ方向ではないが、道具として使えなくなる）。
    """
    if not _have_ffmpeg():
        print("  session.effective_sha256 == report effective_sha256 SKIP (ffmpeg 無し)")
        return

    tmp = tempfile.mkdtemp()
    try:
        src = os.path.join(tmp, "in.mp4")
        det = os.path.join(tmp, "det.json")
        rep = os.path.join(tmp, "report.json")
        n = 10
        _make_tiny_video(src, 64, 64, n)
        with open(det, "w", encoding="utf-8") as f:
            json.dump(
                {"n_frames": n, "width": 64, "height": 64,
                 "complete": True, "detections": {}},
                f,
            )

        rc = cli.main(
            [src, "--detections", det, "--reuse-detections",
             "--detect-only", "--quiet", "--report", rep]
        )
        assert rc == 0, "cli.main が既定ジョブ相当の引数で失敗した"
        with open(rep, encoding="utf-8") as f:
            report = json.load(f)
        assert "effective" in report and "effective_sha256" in report, (
            "report.json に effective / effective_sha256 が書かれていない"
        )

        argv = [src, "--detections", det]
        args = review.build_parser().parse_args(argv)
        session = review.session_from_args(args, argv)
        try:
            session_sha = session.effective_sha256()
        finally:
            session.reader.close()

        assert session_sha == report["effective_sha256"], (
            f"cli 側 report.effective_sha256={report['effective_sha256']} / "
            f"review 側 session.effective_sha256()={session_sha}\n"
            f"cli effective={report['effective']}\n"
            f"review effective={session.effective_settings()}"
        )
        print(
            "  session.effective_sha256() == report.effective_sha256 "
            f"(既定ジョブ) OK（{session_sha[:16]}...）"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"{len(tests)} 件のテストを実行\n")
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{'すべて通過' if failed == 0 else f'{failed} 件失敗'}")
    sys.exit(1 if failed else 0)
