"""レビュー UI の検証。サーバを立てずに回る部分だけ。

HTTP そのものの確認（200/206 が返るか）は実際にサーバを起動して行う。
ここで見るのは、その手前にある「間違えると静かに壊れる」ところ:
Range のパース、/api/state の構造、修正の往復、YOLO 座標変換。
"""

import json
import os
import shutil
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic.corrections import Correction, CorrectionSet  # noqa: E402
from automosaic.corrections import apply as apply_corrections  # noqa: E402
from automosaic.detector import Detection  # noqa: E402
from automosaic.review import (  # noqa: E402
    COV_ESTIMATED,
    COV_NONE,
    COV_REAL,
    ReviewSession,
    dominant_class,
    export_dataset,
    median_box_size,
    mosaic_bgr,
    parse_range,
    runs_of,
    to_yolo,
)
from automosaic.temporal import TemporalConfig  # noqa: E402

CLS = "MALE_GENITALIA_EXPOSED"


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
