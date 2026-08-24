"""tools/pixel_coverage.py の判定ロジック。ffmpeg 不要で回る部分だけ。"""

import io
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
    print_calibrate_section,
    print_old_existence_section,
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


def test_regression_box_mask_fraction_weights_by_overlap_area():
    """`box_mask_fraction` はセルとの重なり面積そのもので重み付けする。

    独立検証で見つかった生存変異(MUT6): `covered_area += ow * oh` を
    `covered_area += cell * cell` に変えると、box にまたがるセルの端が
    セル全体の面積で数えられ、被覆率が塗り過ぎ側に水増しされる(実測: 実データ
    で平均62.5%→88.7%、171/433 が1.0を超える)。既存のテストは box を必ず
    セル格子ぴったりに置いていたのでこの変異を検出できなかった
    (境界の重なりが常に0か1セル分ぴったりになり、ow*oh も cell*cell も
    同じ値になっていた)。ここでは格子からずらした box と、一部だけ
    collapsed なセルを使って区別する。
    """
    mask = np.zeros((4, 4), dtype=bool)
    mask[0, 0] = True  # 左上のセルだけ塗られている。他は全部 False
    cell = 10
    # box (5,5)-(25,25) は格子(10刻み)からずれており、左上セル(0,0)-(10,10)とは
    # (5,5)-(10,10) の 25px^2 だけ重なる。重なる残り8セルは全部 False。
    box = (5.0, 5.0, 20.0, 20.0)
    frac = box_mask_fraction(box, mask, cell)
    expected = (5.0 * 5.0) / (20.0 * 20.0)  # 実際の重なり面積 / box 全体面積 = 0.0625
    assert abs(frac - expected) < 1e-9, (
        f"面積重みが overlap 面積になっていない(cell*cell を使っている疑い): "
        f"{frac} != {expected}"
    )
    print(f"  box_mask_fraction がセル全体でなく重なり面積で重み付けされている OK ({frac:.4f})")

    # 上のケースは重なり (5,5)-(10,10) が 5x5 の正方形で、独立検証が見つけた
    # 生存変異 MUT6b (`ow * oh` を `max(ow, oh) ** 2` に変える) が
    # ow == oh のときは元の式と同じ値になってしまい検出できなかった。
    # ここでは重なりを非正方 (5 x 8) にして区別する。
    box_nonsquare = (5.0, 2.0, 20.0, 30.0)
    # 左上セル(0,0)-(10,10)との重なりは x:[5,10) y:[2,10) = 5 x 8 = 40px^2。
    frac_ns = box_mask_fraction(box_nonsquare, mask, cell)
    expected_ns = (5.0 * 8.0) / (20.0 * 30.0)  # 0.0666...
    assert abs(frac_ns - expected_ns) < 1e-9, (
        f"非正方の重なりで面積重みが overlap 面積になっていない"
        f"(max(ow,oh)^2 を使っている疑い、MUT6b): {frac_ns} != {expected_ns}"
    )
    print(f"  非正方の重なり(5x8)でも overlap 面積で重み付けされている OK ({frac_ns:.4f})")


def test_regression_detect_block_size_picks_mode_not_min_gap():
    """`detect_block_size` は最頻の gap を取る(最小 gap ではない)。

    独立検証で見つかった生存変異(MUT7): `vals[np.argmax(counts)]`
    (最頻値)を`arr.min()`(最小gap)に変えると、テクスチャノイズが作る
    min_gap 下限付近の小さい gap を実際のブロック境界より優先してしまう
    (実測: v11.mp4 の同一フレームで最小gap=4 / 最頻gap=12)。既存のテストは
    「一様乱数の全面モザイク」だけを使っており、この場合は最小gapと最頻gapが
    偶然一致するため区別できなかった。ここではフレームの一部だけをモザイク化
    し、残りは生のテクスチャノイズのままにして区別する。
    """
    rng = np.random.default_rng(0)
    source = rng.integers(0, 256, size=(120, 120), dtype=np.uint8)
    output = source.copy()
    true_block = 12
    pixelize_plane(output, (0.0, 0.0, 60.0, 60.0), true_block)  # 左上だけ塗る。右下は生テクスチャのまま
    detected = detect_block_size(output)
    assert detected == true_block, (
        f"部分モザイクのフレームでブロックサイズ検出が違う(最小gapを拾っている疑い): "
        f"{detected} != {true_block}"
    )
    print(f"  detect_block_size が部分モザイクのフレームでも最頻値ベースで{true_block}pxを検出 OK")


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


def _captured_stdout_stderr(fn, *args, **kwargs):
    """`fn` を呼び、stdout/stderr を文字列として返す（呼び出し自体は実行する）。"""
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        fn(*args, **kwargs)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return out.getvalue(), err.getvalue()


def test_regression_cell_mismatch_suppresses_old_metric_and_calibrate():
    """`--cell` 省略時、実測値が理論値と食い違ったら旧指標と`--calibrate`を抑止する。

    独立検証の指摘: 警告(a)〔実測値が理論値と食い違う〕は640x480の出荷既定
    実行でも必ず発火し判別力が無いのに、旧指標は警告を出したまま走り続けて
    未塗装フレームを「塗装あり」と誤判定していた（1080pコントロールで実測:
    docs/12参照）。`cell_mismatch=True` のときは旧指標と`--calibrate`の
    出力そのものを抑止し、box指標だけを出す。
    """
    result = {
        "old_existence_metric_true": 999,
        "n_gt": 999,
        "old_existence_metric_pct": 100.0,
    }

    # 食い違いが無い場合: 旧指標の数字がそのまま出る
    out, err = _captured_stdout_stderr(print_old_existence_section, result, False)
    assert "999 / 999" in out, f"cell が一致するときは旧指標の数字が出るはず: {out!r}"
    assert "抑止" not in out and "抑止" not in err

    # 食い違いがある場合: 旧指標の数字が出ず、抑止の警告だけが出る
    out, err = _captured_stdout_stderr(print_old_existence_section, result, True)
    assert "999 / 999" not in out, (
        f"cell_mismatch=True なのに旧指標の数字(素通し側に誤りうる)が出ている: {out!r}"
    )
    assert "抑止" in err, f"抑止の警告が出ていない: {err!r}"
    print("  print_old_existence_section: cell_mismatch=True で数字を抑止 OK")

    cal = {
        "gt_frame_collapsed_cells": {"n": 999, "median": 12345},
        "other_frame_collapsed_cells": {"n": 1, "median": 1},
    }

    # calibrate も同じ扱い
    out, err = _captured_stdout_stderr(print_calibrate_section, cal, False)
    assert "12345" in out, f"cell が一致するときは分布が出るはず: {out!r}"

    out, err = _captured_stdout_stderr(print_calibrate_section, None, True)
    assert "12345" not in out, f"cell_mismatch=True なのに分布(cal)が参照されている: {out!r}"
    assert "抑止" in err, f"抑止の警告が出ていない: {err!r}"
    print("  print_calibrate_section: cell_mismatch=True で分布を抑止 OK")


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
