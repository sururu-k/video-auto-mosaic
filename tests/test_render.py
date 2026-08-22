"""描画まわりの検証。ffmpeg 不要で回る部分だけ。"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic.render import (  # noqa: E402
    FrameBuffer,
    apply_regions,
    default_block_size,
    pixelize_plane,
)
from automosaic.detector import Detection  # noqa: E402
from automosaic.temporal import TemporalConfig, process  # noqa: E402


def test_block_size():
    assert default_block_size(1920) == 20  # 1920/100 = 19.2 -> 19 -> 偶数化 20
    assert default_block_size(1280) == 14  # 12.8 -> 13 -> 14
    assert default_block_size(200) == 4    # 最小4px
    assert default_block_size(3840) % 2 == 0
    print("  ブロックサイズ OK")


def test_pixelize_is_block_constant():
    """モザイク後の各ブロックが単色になっていること。"""
    rng = np.random.default_rng(0)
    plane = rng.integers(0, 256, size=(64, 64), dtype=np.uint8)
    block = 8
    pixelize_plane(plane, (16.0, 16.0, 32.0, 32.0), block)

    for by in range(16, 48, block):
        for bx in range(16, 48, block):
            cell = plane[by : by + block, bx : bx + block]
            assert cell.min() == cell.max(), f"ブロック({bx},{by})が単色でない"
    print("  ブロック単色化 OK")


def test_pixelize_grid_is_frame_aligned():
    """格子がフレーム座標に固定されていること（bbox 基準だとチラつく）。

    位置を1px ずらした2つの矩形が、同じ格子境界を使うことを確認する。
    """
    rng = np.random.default_rng(1)
    base = rng.integers(0, 256, size=(64, 64), dtype=np.uint8)

    a = base.copy()
    b = base.copy()
    pixelize_plane(a, (17.0, 17.0, 30.0, 30.0), 8)
    pixelize_plane(b, (18.0, 18.0, 30.0, 30.0), 8)

    # どちらも 16 にスナップされるので、16..48 の内側は完全一致する
    assert np.array_equal(a[24:40, 24:40], b[24:40, 24:40])
    print("  格子のフレーム固定 OK")


def test_pixelize_outside_untouched():
    rng = np.random.default_rng(2)
    plane = rng.integers(0, 256, size=(64, 64), dtype=np.uint8)
    orig = plane.copy()
    pixelize_plane(plane, (16.0, 16.0, 16.0, 16.0), 8)
    assert np.array_equal(plane[:16, :], orig[:16, :])
    assert np.array_equal(plane[48:, :], orig[48:, :])
    print("  領域外は不変 OK")


def test_frame_buffer_roundtrip():
    fb = FrameBuffer(64, 32)
    assert fb.nbytes == 64 * 32 * 3 // 2
    raw = bytes(range(256)) * (fb.nbytes // 256)
    y, u, v = fb.wrap(raw)
    assert y.shape == (32, 64)
    assert u.shape == (16, 32) and v.shape == (16, 32)
    assert fb.pack(y, u, v) == raw
    print("  FrameBuffer ラウンドトリップ OK")


def test_chroma_planes_are_mosaicked():
    """彩度平面も潰れていること。Y だけ潰すと色で形が残る。"""
    fb = FrameBuffer(64, 64)
    rng = np.random.default_rng(3)
    y = rng.integers(0, 256, size=(64, 64), dtype=np.uint8)
    u = rng.integers(0, 256, size=(32, 32), dtype=np.uint8)
    v = rng.integers(0, 256, size=(32, 32), dtype=np.uint8)
    u_before = u.copy()

    apply_regions(y, u, v, [(16.0, 16.0, 32.0, 32.0)], block=8)

    assert not np.array_equal(u, u_before), "U 平面が潰れていない"
    cell = u[12:16, 12:16]  # 彩度側のブロック(=4px)
    assert cell.min() == cell.max()
    print("  彩度平面のモザイク OK")


def test_temporal_fills_gaps():
    """検出漏れフレームが補間で埋まること。ここが法的に一番効く。"""
    dets = {}
    for f in range(30):
        dets[f] = []
    # 0-4 と 15-19 だけ検出、あいだの 5-14 は検出漏れという想定
    for f in list(range(0, 5)) + list(range(15, 20)):
        dets[f] = [Detection("FEMALE_GENITALIA_EXPOSED", 0.8, (100, 100, 50, 50))]

    cfg = TemporalConfig(max_gap=12, memory=6)
    regions, stats = process(
        dets, 30, 640, 480, {"FEMALE_GENITALIA_EXPOSED"}, cfg
    )

    for f in range(5, 15):
        assert regions[f], f"フレーム{f}が埋まっていない（検出漏れが素通し）"
    assert stats["regions_interpolated"] == 10
    # frame memory でトラック前後にも伸びる
    assert regions[20] and regions[25]
    assert not regions[26]
    print("  検出漏れの補間 OK")


def test_bridge_fills_gap_beyond_max_gap():
    """max_gap を超えてトラックが分断されても素通しにしないこと。

    これを入れる前は、15フレームのギャップで3フレームが完全に無処理になっていた。
    """
    dets = {f: [] for f in range(120)}
    for f in list(range(0, 40)) + list(range(55, 120)):
        dets[f] = [Detection("FEMALE_GENITALIA_EXPOSED", 0.85, (200 + f * 6, 300, 120, 100))]

    cfg = TemporalConfig(max_gap=12, memory=6, bridge_max=150)
    regions, stats = process(
        dets, 120, 1280, 720, {"FEMALE_GENITALIA_EXPOSED"}, cfg
    )

    assert stats["frames_with_mosaic"] == 120, (
        f"素通しフレームが残っている: {stats['frames_with_mosaic']}/120"
    )
    assert stats["uncovered_gaps"] == 0
    assert stats["frames_bridged"] > 0
    print(f"  ギャップ橋渡し OK ({stats['frames_bridged']} フレームを補填)")


def test_bridge_covers_both_endpoints():
    """橋渡しの矩形が、区間の前後どちらの位置も含んでいること。"""
    dets = {f: [] for f in range(60)}
    for f in range(0, 20):
        dets[f] = [Detection("FEMALE_GENITALIA_EXPOSED", 0.9, (100, 100, 50, 50))]
    for f in range(40, 60):
        dets[f] = [Detection("FEMALE_GENITALIA_EXPOSED", 0.9, (400, 300, 50, 50))]

    cfg = TemporalConfig(max_gap=5, memory=2, bridge_max=150, margin_scale=0.0)
    regions, _ = process(dets, 60, 640, 480, {"FEMALE_GENITALIA_EXPOSED"}, cfg)

    box = regions[30][0][0]
    assert box[0] <= 100 and box[1] <= 100
    assert box[0] + box[2] >= 450 and box[1] + box[3] >= 350
    print("  橋渡し矩形が両端を包含 OK")


def test_long_gap_is_reported_not_silently_filled():
    """長すぎる区間は埋めないが、必ず報告されること。"""
    dets = {f: [] for f in range(400)}
    for f in list(range(0, 20)) + list(range(380, 400)):
        dets[f] = [Detection("FEMALE_GENITALIA_EXPOSED", 0.9, (100, 100, 50, 50))]

    cfg = TemporalConfig(max_gap=12, memory=6, bridge_max=150)
    _, stats = process(dets, 400, 640, 480, {"FEMALE_GENITALIA_EXPOSED"}, cfg)

    assert stats["uncovered_gaps"] == 1
    assert stats["_left_open"][0][0] > 20
    print("  長い未処理区間の報告 OK")


def test_edge_gap_not_bridged():
    """先頭・末尾に接する区間は片側にしか根拠がないので埋めない。"""
    dets = {f: [] for f in range(60)}
    for f in range(30, 40):
        dets[f] = [Detection("FEMALE_GENITALIA_EXPOSED", 0.9, (100, 100, 50, 50))]

    cfg = TemporalConfig(max_gap=12, memory=2, bridge_max=150)
    _, stats = process(dets, 60, 640, 480, {"FEMALE_GENITALIA_EXPOSED"}, cfg)
    assert stats["uncovered_gaps"] == 2  # 先頭側と末尾側
    print("  端の区間は橋渡ししない OK")


def test_despike_before_interpolation():
    """孤立した低スコア検出が長い区間に引き伸ばされないこと。"""
    dets = {f: [] for f in range(60)}
    dets[0] = [Detection("FEMALE_GENITALIA_EXPOSED", 0.15, (10, 10, 20, 20))]
    dets[50] = [Detection("FEMALE_GENITALIA_EXPOSED", 0.15, (10, 10, 20, 20))]

    cfg = TemporalConfig(max_gap=12, memory=0, min_track_len=2, despike_conf=0.35)
    regions, stats = process(dets, 60, 640, 480, {"FEMALE_GENITALIA_EXPOSED"}, cfg)

    assert stats["tracks_despiked"] == 2
    assert stats["frames_with_mosaic"] == 0
    print("  デスパイク OK")


def test_high_score_single_frame_survives():
    """Recall 優先。スコアが高ければ1フレームでも残す。"""
    dets = {f: [] for f in range(10)}
    dets[5] = [Detection("MALE_GENITALIA_EXPOSED", 0.9, (10, 10, 20, 20))]
    cfg = TemporalConfig(memory=0, min_track_len=2, despike_conf=0.35)
    regions, stats = process(dets, 10, 640, 480, {"MALE_GENITALIA_EXPOSED"}, cfg)
    assert stats["tracks_despiked"] == 0
    assert regions[5]
    print("  高スコア単発の残存 OK")


def test_geometric_filter_drops_huge_boxes():
    dets = {0: [Detection("FEMALE_GENITALIA_EXPOSED", 0.9, (0, 0, 640, 480))]}
    cfg = TemporalConfig(max_area_ratio=0.35)
    _, stats = process(dets, 1, 640, 480, {"FEMALE_GENITALIA_EXPOSED"}, cfg)
    assert stats["geometric_dropped"] == 1
    print("  幾何フィルタ OK")


def test_margin_expands_region():
    """膨張マージンが実際に領域を広げていること。"""
    dets = {0: [Detection("FEMALE_GENITALIA_EXPOSED", 0.9, (200, 200, 40, 40))]}
    cfg = TemporalConfig(memory=0, min_track_len=0)
    regions, _ = process(dets, 1, 640, 480, {"FEMALE_GENITALIA_EXPOSED"}, cfg)
    box, region = regions[0][0]
    assert box[0] < 200 and box[1] < 200
    assert box[2] > 40 and box[3] > 40
    print(f"  膨張マージン OK (40x40 -> {box[2]:.0f}x{box[3]:.0f})")


def test_estimated_regions_get_thicker_margin():
    """推定で置いた領域のほうが厚く盛られること。"""
    dets = {f: [] for f in range(10)}
    dets[0] = [Detection("FEMALE_GENITALIA_EXPOSED", 0.9, (200, 200, 40, 40))]
    dets[5] = [Detection("FEMALE_GENITALIA_EXPOSED", 0.9, (200, 200, 40, 40))]
    cfg = TemporalConfig(memory=0, min_track_len=0)
    regions, _ = process(dets, 10, 640, 480, {"FEMALE_GENITALIA_EXPOSED"}, cfg)

    detected_w = regions[0][0][0][2]
    interpolated_w = regions[2][0][0][2]
    assert interpolated_w > detected_w
    print(f"  推定領域の厚盛り OK ({detected_w:.0f} -> {interpolated_w:.0f})")


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
