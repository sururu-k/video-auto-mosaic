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
    # 位置が近いのでトラックレット結合が先に繋ぐ。繋がらない場合は橋渡しが埋める
    assert stats["tracks_stitched"] > 0 or stats["frames_bridged"] > 0
    print(
        f"  ギャップ補填 OK (結合 {stats['tracks_stitched']} / "
        f"橋渡し {stats['frames_bridged']} フレーム)"
    )


def test_stitch_joins_intermittent_tracks():
    """モザイクが「有り・なし・有り」と途切れる場面を1本に繋ぐこと。

    位置と大きさが近い検出が数秒スパンで飛び飛びに出ると、max_gap を超えて
    トラックが分断され、そのたびにモザイクが切れる。実素材で実際に起きた症状。
    """
    dets = {f: [] for f in range(200)}
    # 30フレーム(1秒)おきに5フレームだけ検出される、という飛び飛びの出方
    for start in range(0, 200, 30):
        for f in range(start, min(200, start + 5)):
            dets[f] = [
                Detection("MALE_GENITALIA_EXPOSED", 0.2, (300 + start // 6, 200, 60, 60))
            ]

    off = TemporalConfig(max_gap=12, memory=6, stitch_max_gap=0, bridge_max=0)
    on = TemporalConfig(max_gap=12, memory=6, stitch_max_gap=90, bridge_max=0)

    _, s_off = process(dets, 200, 640, 480, {"MALE_GENITALIA_EXPOSED"}, off)
    _, s_on = process(dets, 200, 640, 480, {"MALE_GENITALIA_EXPOSED"}, on)

    assert s_on["tracks_stitched"] > 0, "トラックが繋がっていない"
    assert s_on["frames_with_mosaic"] > s_off["frames_with_mosaic"], (
        f"結合しても被覆が増えていない: {s_off['frames_with_mosaic']} -> "
        f"{s_on['frames_with_mosaic']}"
    )
    # 最後の検出以降は memory の分しか伸びないので、そこは覆われなくてよい。
    # 見たいのは「検出区間のあいだの途切れ」が消えていること。
    r_on, _ = process(dets, 200, 640, 480, {"MALE_GENITALIA_EXPOSED"}, on)
    last_detected = max(f for f, ds in dets.items() if ds)
    for f in range(0, last_detected + 1):
        assert r_on[f], f"検出区間のあいだのフレーム{f}が途切れている"
    print(
        f"  途切れトラックの結合 OK "
        f"({s_off['frames_with_mosaic']} -> {s_on['frames_with_mosaic']}/200, "
        f"検出区間内は連続)"
    )


def test_assignment_does_not_swap_nearby_objects():
    """近接した2つの対象の軌跡が入れ替わらないこと。

    IoU の大きい順に貪欲で取ると、片方が先に別のトラックへ吸われて
    軌跡が交差する。線形割当なら全体最適で解くので入れ替わりにくい。
    """
    dets = {}
    for f in range(20):
        # 2つの対象が近接しながら並んで動く
        dets[f] = [
            Detection("MALE_GENITALIA_EXPOSED", 0.5, (100 + f * 4, 200, 60, 60)),
            Detection("MALE_GENITALIA_EXPOSED", 0.5, (170 + f * 4, 200, 60, 60)),
        ]

    cfg = TemporalConfig(memory=0, min_track_len=0, stitch_max_gap=0, bridge_max=0)
    regions, stats = process(dets, 20, 640, 480, {"MALE_GENITALIA_EXPOSED"}, cfg)

    # 2本のトラックが最後まで維持されること（入れ替わると分断されて増える）
    assert stats["tracks_final"] == 2, f"トラックが {stats['tracks_final']} 本になっている"
    for f in range(20):
        assert len(regions[f]) == 2, f"フレーム{f}の領域が {len(regions[f])} 個"
    print("  近接対象の割当 OK")


def test_stitch_does_not_join_distant_objects():
    """離れた位置の別対象までは繋がないこと。過剰に潰さないための歯止め。"""
    dets = {f: [] for f in range(100)}
    for f in range(0, 10):
        dets[f] = [Detection("MALE_GENITALIA_EXPOSED", 0.5, (20, 20, 40, 40))]
    for f in range(60, 70):
        dets[f] = [Detection("MALE_GENITALIA_EXPOSED", 0.5, (560, 400, 40, 40))]

    cfg = TemporalConfig(max_gap=12, memory=2, stitch_max_gap=90, bridge_max=0)
    _, stats = process(dets, 100, 640, 480, {"MALE_GENITALIA_EXPOSED"}, cfg)
    assert stats["tracks_stitched"] == 0, "離れた対象を繋いでしまっている"
    print("  離れた対象は繋がない OK")


def test_memory_before_extends_backwards():
    """検出が遅れて始まる分を、開始前へ遡って塞げること。"""
    dets = {f: [] for f in range(60)}
    for f in range(30, 40):
        dets[f] = [Detection("MALE_GENITALIA_EXPOSED", 0.5, (100, 100, 50, 50))]

    cfg = TemporalConfig(memory=6, memory_before=20, stitch_max_gap=0, bridge_max=0)
    regions, _ = process(dets, 60, 640, 480, {"MALE_GENITALIA_EXPOSED"}, cfg)

    assert regions[10], "開始20フレーム前が塞がれていない"
    assert not regions[9]
    print("  開始前への遡り OK")


def test_bridge_interpolates_between_endpoints():
    """橋渡しは前後の外接矩形でなく線形補間になっていること。

    外接矩形で塞ぐと、対象が大きく動いた区間で矩形が画面の大半を覆う。
    「潰しすぎ」の主要因だったので補間に変更した。
    """
    dets = {f: [] for f in range(60)}
    for f in range(0, 20):
        dets[f] = [Detection("FEMALE_GENITALIA_EXPOSED", 0.9, (100, 100, 50, 50))]
    for f in range(40, 60):
        dets[f] = [Detection("FEMALE_GENITALIA_EXPOSED", 0.9, (400, 300, 50, 50))]

    cfg = TemporalConfig(max_gap=5, memory=2, bridge_max=150, margin_scale=0.0)
    regions, stats = process(dets, 60, 640, 480, {"FEMALE_GENITALIA_EXPOSED"}, cfg)

    assert stats["frames_with_mosaic"] == 60, "素通しフレームが残っている"

    box = regions[30][0][0]
    # 両端を包含する外接矩形（幅350以上）にはならず、元の大きさ程度に収まる
    assert box[2] < 150, f"橋渡し矩形が大きすぎる: 幅 {box[2]}"
    # 位置は前後のあいだにある
    assert 100 < box[0] < 400
    assert 100 < box[1] < 300
    print(f"  橋渡しの線形補間 OK (幅 {box[2]:.0f}, x {box[0]:.0f})")


def test_margin_does_not_explode_on_low_scores():
    """低スコアでもマージンが暴走しないこと。

    以前は confidence 項が (1 - score) * base * 2 で、このモデルのように
    スコアが常に低いと基礎マージンの何倍にも膨らんでいた。
    """
    dets = {0: [Detection("FEMALE_GENITALIA_EXPOSED", 0.15, (200, 200, 60, 60))]}
    cfg = TemporalConfig(memory=0, min_track_len=0)
    regions, _ = process(dets, 1, 640, 480, {"FEMALE_GENITALIA_EXPOSED"}, cfg)
    box = regions[0][0][0]
    ratio = (box[2] * box[3]) / (60 * 60)
    assert ratio < 6.0, f"面積が {ratio:.1f} 倍に膨らんでいる"
    print(f"  低スコア時のマージン抑制 OK (面積 {ratio:.2f}倍)")


def test_long_gap_is_reported_not_silently_filled():
    """長すぎる区間は埋めないが、必ず報告されること。"""
    dets = {f: [] for f in range(400)}
    for f in list(range(0, 20)) + list(range(380, 400)):
        dets[f] = [Detection("FEMALE_GENITALIA_EXPOSED", 0.9, (100, 100, 50, 50))]

    # 結合も無効にして、橋渡しの判断だけを見る
    cfg = TemporalConfig(max_gap=12, memory=6, bridge_max=150, stitch_max_gap=0)
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


def test_despike_drops_scattered_noise():
    """ばらけた低スコアの誤検出が長い区間に引き伸ばされないこと。

    同じ位置に出る弱い検出は結合されて生き残る（実在する対象を弱く拾っている
    可能性が高い）。位置が離れているものはノイズとみなして落とす。
    """
    dets = {f: [] for f in range(60)}
    dets[0] = [Detection("FEMALE_GENITALIA_EXPOSED", 0.15, (10, 10, 20, 20))]
    dets[50] = [Detection("FEMALE_GENITALIA_EXPOSED", 0.15, (560, 440, 20, 20))]

    cfg = TemporalConfig(max_gap=12, memory=0, min_track_len=2, despike_conf=0.35)
    regions, stats = process(dets, 60, 640, 480, {"FEMALE_GENITALIA_EXPOSED"}, cfg)

    assert stats["tracks_stitched"] == 0, "離れたノイズを繋いでしまっている"
    assert stats["tracks_despiked"] == 2
    assert stats["frames_with_mosaic"] == 0
    print("  ばらけたノイズのデスパイク OK")


def test_stitch_runs_before_despike():
    """結合がデスパイクより先に走ること。

    順序が逆だと、単発の低スコア検出が先に捨てられ、結合処理がそれを見る前に
    消える。実素材で、15pxしか離れていない検出が繋がらず1.8秒の穴になっていた。
    """
    dets = {f: [] for f in range(60)}
    # 同じ位置に、単発の弱い検出が離れて2回出る
    dets[0] = [Detection("FEMALE_GENITALIA_EXPOSED", 0.15, (100, 100, 40, 40))]
    dets[30] = [Detection("FEMALE_GENITALIA_EXPOSED", 0.15, (103, 102, 40, 40))]

    cfg = TemporalConfig(max_gap=12, memory=2, min_track_len=2, despike_conf=0.35)
    regions, stats = process(dets, 60, 640, 480, {"FEMALE_GENITALIA_EXPOSED"}, cfg)

    assert stats["tracks_stitched"] == 1, "結合されていない（デスパイクが先に消している）"
    assert stats["tracks_despiked"] == 0
    for f in range(0, 31):
        assert regions[f], f"フレーム{f}が埋まっていない"
    print("  結合がデスパイクより先 OK")


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


def test_checkpoint_roundtrip():
    """途中保存が読み戻せること。長尺の検出が落ちたときに全損しないための機構。"""
    import tempfile
    from automosaic.cli import load_partial, save_detections
    from automosaic.video import VideoInfo

    info = VideoInfo(
        width=640, height=480, fps_num=30, fps_den=1, nb_frames=100,
        duration=3.3, pix_fmt="yuv420p", color_primaries=None, color_trc=None,
        colorspace=None, color_range=None, has_audio=False,
    )
    per_frame = {
        0: [Detection("MALE_GENITALIA_EXPOSED", 0.5, (10, 20, 30, 40))],
        5: [],
        7: [Detection("ANUS_EXPOSED", 0.3, (50, 60, 20, 25))],
    }

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "det.json")
        save_detections(path, per_frame, 10, info, complete=False)
        loaded, n = load_partial(path)

        assert n == 10
        # 空のフレームは保存されない（容量の無駄なので）。中身のあるものは一致する
        assert set(loaded) == {0, 7}
        assert loaded[0][0].cls == "MALE_GENITALIA_EXPOSED"
        assert loaded[0][0].box == (10, 20, 30, 40)
        assert abs(loaded[7][0].score - 0.3) < 1e-6

        # 存在しないパスは空で返る（初回実行時に例外にしない）
        empty, n0 = load_partial(os.path.join(d, "nope.json"))
        assert empty == {} and n0 == 0
    print("  途中保存のラウンドトリップ OK")


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
