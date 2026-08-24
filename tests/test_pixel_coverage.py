"""tools/pixel_coverage.py の判定ロジック。ffmpeg 不要で回る部分だけ。"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

from pixel_coverage import (  # noqa: E402
    box_mask_fraction,
    box_pixel_coverage,
    cell_analysis,
    cell_std_grid,
    collapsed_cells,
    denormalize_yolo,
    detect_block_size,
    detect_block_size_over_frames,
    frame_is_painted,
    GtItem,
)
from automosaic.render import pixelize_plane  # noqa: E402


def _make_source(h=64, w=64, seed=0):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(h, w), dtype=np.uint8)


def test_cell_std_grid_shape_and_values():
    frame = np.zeros((40, 60), dtype=np.uint8)
    frame[0:10, 0:10] = np.arange(100).reshape(10, 10) % 256
    grid = cell_std_grid(frame, 10)
    assert grid.shape == (4, 6)
    assert grid[0, 0] > 0
    assert grid[0, 1] == 0.0  # 残りは全部0で分散なし
    print("  cell_std_grid の形状と値 OK")


def test_collapsed_cells_detects_real_mosaic():
    """render.pixelize_plane で実際に潰したセルが collapsed と判定されること。"""
    source = _make_source(64, 64, seed=1)
    output = source.copy()
    block = 8
    pixelize_plane(output, (16.0, 16.0, 32.0, 32.0), block)

    collapsed = collapsed_cells(source, output, cell=block, std_min=6.0, ratio_max=0.35)

    # 塗った範囲 (16,16)-(48,48) は block=8 の格子で (2,2)-(6,6) のセル
    painted_region = collapsed[2:6, 2:6]
    assert painted_region.all(), f"塗った範囲が全部 collapsed になっていない: {painted_region}"

    # 塗っていない範囲は基本 collapsed でない（乱数ノイズがたまたま低分散でない限り）
    untouched = collapsed[0:2, :].sum() + collapsed[6:8, :].sum()
    assert untouched == 0, f"塗っていない範囲が誤って collapsed 判定された: {untouched}"
    print("  実際のモザイクブロックが collapsed と判定される OK")


def test_collapsed_cells_flat_source_is_excluded():
    """元がもともと平坦なセルは、モザイクの有無に関わらず対象外（docs/11 の盲点）。"""
    source = np.full((32, 32), 100, dtype=np.uint8)  # 完全に平坦
    output = source.copy()
    pixelize_plane(output, (0.0, 0.0, 32.0, 32.0), 8)  # 平坦なので何も変わらない

    collapsed = collapsed_cells(source, output, cell=8, std_min=6.0, ratio_max=0.35)
    assert not collapsed.any(), "平坦な元セルが collapsed と判定されてはいけない"
    print("  平坦な元セルは対象外 OK")


def test_frame_is_painted_threshold():
    grid = np.zeros((5, 5), dtype=bool)
    assert frame_is_painted(grid, min_cells=2) is False
    grid[0, 0] = True
    assert frame_is_painted(grid, min_cells=2) is False
    grid[0, 1] = True
    assert frame_is_painted(grid, min_cells=2) is True
    print("  frame_is_painted の閾値 OK")


def test_box_pixel_coverage_full_and_none():
    collapsed = np.ones((4, 4), dtype=bool)
    # box がセル格子全体(0,0)-(40,40)を覆う場合、被覆率は1.0
    assert box_pixel_coverage((0.0, 0.0, 40.0, 40.0), collapsed, cell=10) == 1.0

    collapsed_empty = np.zeros((4, 4), dtype=bool)
    assert box_pixel_coverage((0.0, 0.0, 40.0, 40.0), collapsed_empty, cell=10) == 0.0
    print("  box_pixel_coverage の全部/ゼロ OK")


def test_box_pixel_coverage_partial():
    """box の半分だけ塗られているセルにかかる場合、被覆率が概ね0.5になること。"""
    collapsed = np.zeros((2, 2), dtype=bool)
    collapsed[0, 0] = True  # 左上セル (0,0)-(10,10) だけ塗られている
    # box は (0,0)-(20,10)。左半分(0,0)-(10,10)が塗られたセル、右半分(10,0)-(20,10)は未塗装
    frac = box_pixel_coverage((0.0, 0.0, 20.0, 10.0), collapsed, cell=10)
    assert abs(frac - 0.5) < 1e-9, frac
    print("  box_pixel_coverage の部分被覆 OK")


def test_box_pixel_coverage_matches_manual_render():
    """実際に render.pixelize_plane で塗った box に対する被覆率が期待通りになること。

    box を格子ぴったりに置いて塗ると、その box 自身の被覆率は1.0、
    隣の未塗装 box は0.0になるはず。
    """
    source = _make_source(80, 80, seed=2)
    output = source.copy()
    block = 10
    target_box = (10.0, 10.0, 30.0, 30.0)  # 格子(10刻み)にぴったり合う
    pixelize_plane(output, target_box, block)

    collapsed = collapsed_cells(source, output, cell=block, std_min=6.0, ratio_max=0.35)
    cov_painted = box_pixel_coverage(target_box, collapsed, block)
    cov_untouched = box_pixel_coverage((50.0, 50.0, 20.0, 20.0), collapsed, block)

    assert cov_painted >= 0.99, f"塗った box の被覆率が低い: {cov_painted}"
    assert cov_untouched <= 0.01, f"塗っていない box の被覆率が高い: {cov_untouched}"
    print(f"  塗った box={cov_painted:.3f} / 未塗装 box={cov_untouched:.3f} OK")


def test_detect_block_size_matches_actual_block():
    """実際に焼いたブロックサイズを、出力画素から実測で言い当てられること。

    このツールの開発中、`render.default_block_size(640) == 6` を信じて既定に
    したところ、実際にこの素材(マイビデオ-5, v11.mp4)を焼いたブロックは
    12px だった（run-length を手で数えて確認）。default_block_size はその
    動画が実際にどう焼かれたかを知らないので信用できない、という再現。
    """
    source = _make_source(120, 120, seed=4)
    output = source.copy()
    true_block = 12
    pixelize_plane(output, (0.0, 0.0, 120.0, 120.0), true_block)

    detected = detect_block_size(output)
    assert detected == true_block, f"検出されたブロックサイズが違う: {detected} != {true_block}"
    print(f"  detect_block_size が実際のブロック({true_block}px)と一致 OK")


def test_detect_block_size_over_frames_uses_median():
    source = _make_source(80, 80, seed=5)
    out_a = source.copy()
    pixelize_plane(out_a, (0.0, 0.0, 80.0, 80.0), 8)
    out_b = source.copy()
    pixelize_plane(out_b, (0.0, 0.0, 80.0, 80.0), 8)
    result = detect_block_size_over_frames([out_a, out_b])
    assert result == 8, f"中央値が期待と違う: {result}"
    # モザイクの無いフレームだけなら検出できず None
    flat_frames = [np.full((40, 40), 128, dtype=np.uint8)]
    assert detect_block_size_over_frames(flat_frames) is None
    print("  detect_block_size_over_frames の中央値/フォールバック OK")


def test_cell_analysis_eligible_excludes_flat_source():
    """`cell_analysis` の eligible が、平坦な元セルを正しく除外すること。

    `box_mask_fraction` で "measurable_fraction"（測定可能面積）を計算する際の
    土台。docs/11 の「測定の盲点」をセル単位で切り出したもの。
    """
    source = np.full((24, 24), 100, dtype=np.uint8)
    source[0:12, 0:12] = _make_source(12, 12, seed=6)  # 左上だけテクスチャあり
    output = source.copy()
    pixelize_plane(output, (0.0, 0.0, 24.0, 24.0), 12)  # 全体を塗る

    eligible, collapsed = cell_analysis(source, output, cell=12, std_min=6.0, ratio_max=0.35)
    assert eligible[0, 0], "テクスチャのあるセルは eligible のはず"
    assert not eligible[1, 1], "平坦なセルは eligible でないはず"
    assert collapsed[0, 0], "eligible かつ塗られたセルは collapsed のはず"
    assert not collapsed[1, 1], "eligible でないセルは collapsed にならない"

    box = (0.0, 0.0, 24.0, 24.0)
    measurable = box_mask_fraction(box, eligible, 12)
    coverage = box_mask_fraction(box, collapsed, 12)
    assert abs(measurable - 0.25) < 1e-6, measurable  # 4セル中1セルだけ eligible
    assert abs(coverage - 0.25) < 1e-6, coverage
    assert abs(coverage / measurable - 1.0) < 1e-6  # 測定可能な範囲では100%塗られている
    print(f"  measurable={measurable:.2f} coverage={coverage:.2f} (測定可能部分では100%塗装) OK")


def test_denormalize_yolo():
    items = [GtItem(frame=5, box=(0.5, 0.5, 0.25, 0.5), cls="X")]
    out = denormalize_yolo(items, width=100, height=200)
    assert len(out) == 1
    x, y, w, h = out[0].box
    # cx=50, cy=100, w=25, h=100 -> x=37.5, y=50
    assert abs(x - 37.5) < 1e-6
    assert abs(y - 50.0) < 1e-6
    assert abs(w - 25.0) < 1e-6
    assert abs(h - 100.0) < 1e-6
    print("  YOLO 正規化解除 OK")


def test_regression_naive_existence_can_diverge_from_pixel_coverage():
    """旧指標(有無)と新指標(画素被覆)が異なる結論を出しうることの再現。

    フレーム全体のどこかにモザイクがあれば「旧指標」は真になるが、GT box の場所を
    塗っていなければ「新指標」は0になる。これが issue #7 の核心（矩形の有無では
    対象の上にあるかを見ていない）。この差が実際に発生することを確認する。
    """
    source = _make_source(64, 64, seed=3)
    output = source.copy()
    block = 8
    # GT box は (0,0)-(16,16) だが、実際に塗ったのは無関係な (48,48)-(64,64)
    gt_box = (0.0, 0.0, 16.0, 16.0)
    pixelize_plane(output, (48.0, 48.0, 16.0, 16.0), block)

    collapsed = collapsed_cells(source, output, cell=block, std_min=6.0, ratio_max=0.35)
    old_existence = frame_is_painted(collapsed, min_cells=2)
    new_coverage = box_pixel_coverage(gt_box, collapsed, block)

    assert old_existence is True, "フレームのどこかは塗られているはず"
    assert new_coverage == 0.0, f"GT box は塗られていないはずなのに被覆率 {new_coverage}"
    print(f"  旧指標=True(存在する) / 新指標=0.0(GT上には無い) の乖離を再現 OK")


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
