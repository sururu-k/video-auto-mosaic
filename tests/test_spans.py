"""automosaic/webapp/spans.py の検証。

#22 の第一歩ぶん。区間の両端に矩形を置いたときの補間が
`tools/annotations_to_corrections.py` の `build()` をそのまま使っていること、
そして「同じ矩形を複製する」現状の `mark()` より端で外れにくいことを実測する。

対象の動きは `docs/09-mosaic-quality.md` S2 の合成実測と同じ形（正弦運動）を使う。
振幅・周期はそこの「往復運動で完全に外す」条件（振幅250px/周期20f）をそのまま流用する。
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic.webapp.spans import interval_add_records, interval_records  # noqa: E402

CLS = "MALE_GENITALIA_EXPOSED"


def _box_at(t: float, amplitude: float, period: float, size: float, cx: float, cy: float):
    """時刻 t（フレーム）での対象の真の矩形。正弦運動、y は固定。"""
    x = cx + amplitude * math.sin(2 * math.pi * t / period) - size / 2
    y = cy - size / 2
    return (x, y, size, size)


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix1, iy1 = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _fully_outside(true_box, placed_box) -> bool:
    return _iou(true_box, placed_box) <= 0.0


def test_interval_records_matches_endpoints_and_reuses_build():
    """両端の矩形がそのまま出て、あいだは build() が返す座標と一致する。"""
    lo_box = (100.0, 100.0, 50.0, 50.0)
    hi_box = (200.0, 100.0, 50.0, 50.0)
    recs = interval_records(10, lo_box, 20, hi_box, CLS, kind="add")

    frames = [r.frame for r in recs]
    assert frames == list(range(10, 21)), frames
    assert recs[0].box == lo_box, recs[0].box
    assert recs[-1].box == hi_box, recs[-1].box
    assert all(r.kind == "add" for r in recs)
    assert all(r.cls == CLS for r in recs)

    # tools/annotations_to_corrections.build() を直接呼んだ場合と一致すること
    # （別実装を書いていないことの確認）
    from automosaic.webapp.spans import _tool

    tool = _tool()
    built = tool.build(
        [
            {"frame": 10, "box": list(lo_box), "class": CLS},
            {"frame": 20, "box": list(hi_box), "class": CLS},
        ],
        max_interp=10,
        default_class=CLS,
        hold=0,
    )
    assert [c.box for c in built] == [r.box for r in recs]
    print(f"  一致確認: {len(recs)} 件、build() と全一致")


def test_interval_records_zero_span():
    recs = interval_records(5, (0.0, 0.0, 10.0, 10.0), 5, (0.0, 0.0, 10.0, 10.0), CLS)
    assert [r.frame for r in recs] == [5], recs
    print(f"  span=0: {len(recs)} 件")


def test_interval_records_rejects_reversed_range():
    try:
        interval_records(20, (0, 0, 10, 10), 10, (0, 0, 10, 10), CLS)
        raise AssertionError("hi < lo が通ってしまった")
    except ValueError as e:
        print(f"  reversed range を拒否: {e}")


def test_interval_kind_override_add_and_remove():
    recs = interval_records(0, (0, 0, 10, 10), 4, (0, 0, 10, 10), CLS, kind="remove")
    assert all(r.kind == "remove" for r in recs)
    print(f"  kind=remove 上書き確認: {len(recs)} 件")


def test_interval_add_records_without_existing_cover_passes_through():
    """`existing_cover` を渡さなければ `interval_records()` と全く同じ結果。"""
    lo_box = (100.0, 100.0, 50.0, 50.0)
    hi_box = (200.0, 100.0, 50.0, 50.0)
    recs = interval_add_records(10, lo_box, 20, hi_box, CLS, 1920, 1080)
    plain = interval_records(10, lo_box, 20, hi_box, CLS, kind="add")
    assert [r.box for r in recs] == [r.box for r in plain], (recs, plain)
    print(f"  existing_cover 無し: {len(recs)} 件、interval_records と一致")


def test_interval_add_records_envelopes_with_existing_detection():
    """RULES.md 0: 補間した矩形が既存の検出矩形より小さければ、大きいほうを採る。

    区間の両端は小さい矩形（(100,100,20,20) と (120,100,20,20)）で近接している。
    途中の frame15 にだけ、遠く離れた大きい既存検出（(50,50,200,200)）がある
    ことにする。envelope 無しなら frame15 の矩形は補間そのまま（小さい・
    (100,100,20,20) 付近）で、既存の検出とは無関係になる。envelope 有りなら
    その大きい検出を包む大きさになる。
    """
    lo_box = (100.0, 100.0, 20.0, 20.0)
    hi_box = (120.0, 100.0, 20.0, 20.0)
    big_cover = {15: (50.0, 50.0, 200.0, 200.0)}

    recs = interval_add_records(
        10, lo_box, 20, hi_box, CLS, 1920, 1080,
        existing_cover=lambda f: big_cover.get(f),
    )
    by_frame = {r.frame: r.box for r in recs}
    box15 = by_frame[15]
    cx, cy, cw, ch = big_cover[15]
    assert box15[0] <= cx and box15[1] <= cy, (box15, big_cover[15])
    assert box15[0] + box15[2] >= cx + cw, (box15, big_cover[15])
    assert box15[1] + box15[3] >= cy + ch, (box15, big_cover[15])
    # envelope していないフレームは補間そのまま（小さい矩形が勝手に膨らまない）
    assert by_frame[10] == lo_box, by_frame[10]
    assert by_frame[20] == hi_box, by_frame[20]
    print(f"  frame15 envelope: 補間のみだと小さい矩形 -> envelope込み={box15}")


def test_interval_add_records_small_existing_detection_does_not_shrink_interp():
    """既存の検出が補間より小さい場合は、補間の矩形がそのまま勝つこと。

    envelope は「小さいほうを捨てる」処理であって、「補間を既存の検出の
    大きさに揃える」処理ではない。既存の検出のほうが小さければ、補間の
    矩形を狭めてはいけない（RULES.md 0: 塗り過ぎ側は許容、漏れる側は不可）。
    """
    lo_box = (100.0, 100.0, 100.0, 100.0)
    hi_box = (300.0, 100.0, 100.0, 100.0)
    tiny_cover = {15: (150.0, 120.0, 5.0, 5.0)}

    recs = interval_add_records(
        10, lo_box, 20, hi_box, CLS, 1920, 1080,
        existing_cover=lambda f: tiny_cover.get(f),
    )
    by_frame = {r.frame: r.box for r in recs}
    plain_by_frame = {
        r.frame: r.box for r in interval_records(10, lo_box, 20, hi_box, CLS, kind="add")
    }
    b15, p15 = by_frame[15], plain_by_frame[15]
    assert b15[0] <= p15[0] and b15[1] <= p15[1], (b15, p15)
    assert b15[0] + b15[2] >= p15[0] + p15[2], (b15, p15)
    assert b15[1] + b15[3] >= p15[1] + p15[3], (b15, p15)
    print(f"  小さい既存検出は補間を狭めない: 補間={p15} envelope込み={b15}")


def measure_edge_drift(span: int, amplitude: float, period: float, size: float):
    """span=span（前後 span フレーム）での「複製」対「区間補間」の外れ幅を測る。

    複製（現状の mark()）: 判定した中心フレームでのタップ位置の矩形を、
    区間全体に複製する。
    区間補間（interval_records）: 区間の両端での真の位置に矩形を置き、
    あいだを線形補間する。

    どちらも「ユーザーがタップした場所」は真の位置と一致する前提（タップの
    誤差そのものはここでは測らない）。測るのは、複製 or 補間という展開方法
    そのものが持つ誤差。
    """
    cx, cy = 960.0, 540.0
    lo, hi = -span, span
    center_box = _box_at(0, amplitude, period, size, cx, cy)
    lo_box = _box_at(lo, amplitude, period, size, cx, cy)
    hi_box = _box_at(hi, amplitude, period, size, cx, cy)

    recs = interval_records(lo, lo_box, hi, hi_box, CLS, kind="add")
    interp_by_frame = {r.frame: r.box for r in recs}

    dup_ious, interp_ious = [], []
    dup_outside, interp_outside = 0, 0
    for t in range(lo, hi + 1):
        true_box = _box_at(t, amplitude, period, size, cx, cy)
        dup_iou = _iou(true_box, center_box)
        interp_iou = _iou(true_box, interp_by_frame[t])
        dup_ious.append(dup_iou)
        interp_ious.append(interp_iou)
        if _fully_outside(true_box, center_box):
            dup_outside += 1
        if _fully_outside(true_box, interp_by_frame[t]):
            interp_outside += 1

    n = len(dup_ious)
    return {
        "n_frames": n,
        "dup_mean_iou": sum(dup_ious) / n,
        "interp_mean_iou": sum(interp_ious) / n,
        "dup_min_iou": min(dup_ious),
        "interp_min_iou": min(interp_ious),
        "dup_fully_outside": dup_outside,
        "interp_fully_outside": interp_outside,
    }


def test_measure_edge_drift_span15_amplitude250_period20():
    """issue #22 の例（span=15、31件）と同じ span で、docs/09 S2 の最悪条件
    （振幅250px/周期20f）を使って複製 vs 区間補間の外れを測る。
    """
    r = measure_edge_drift(span=15, amplitude=250.0, period=20.0, size=191.0)
    print(
        f"  span=15 振幅250px/周期20f: n={r['n_frames']} "
        f"複製 mean_iou={r['dup_mean_iou']:.3f} min_iou={r['dup_min_iou']:.3f} "
        f"fully_outside={r['dup_fully_outside']}/{r['n_frames']} | "
        f"区間補間 mean_iou={r['interp_mean_iou']:.3f} min_iou={r['interp_min_iou']:.3f} "
        f"fully_outside={r['interp_fully_outside']}/{r['n_frames']}"
    )
    # ここで選んだ2条件では区間補間のほうが良い。**これは実装の不変条件ではない。**
    #
    # 独立検証で150条件を掃いた結果、26条件で mean_iou が複製より悪く、
    # 9条件で「完全に外れたフレーム」が増えた。例:
    #
    #   span=30 振幅120px/周期24f  複製 min_iou=0.228 完全外れ=0
    #                              区間補間 min_iou=0.000 完全外れ=4
    #
    # 幾何的にも整合する。振幅 A の往復で両端が逆位相にあると、直線は -A から +A へ
    # 渡るので真の位置と最大 2A（240px）離れる。中心での複製は |A|=120px しか離れない。
    # 対象の矩形は 191px なので、2A では完全に外れる。
    #
    # つまり「両端を真の位置に合わせたのだから常に良くなる」は成り立たない。
    # 悪化する向きは**漏れる側**なので、#22 の次の一歩で mark() に配線するときは
    # 「補間にすれば必ず良くなる」を前提にしないこと。往復運動の速い対象では
    # 中心での複製のほうが安全な領域がある。
    assert r["interp_mean_iou"] >= r["dup_mean_iou"], r
    assert r["interp_fully_outside"] <= r["dup_fully_outside"], r


def test_measure_edge_drift_span5_amplitude120_period24():
    """docs/09 のもう一方の条件（振幅120px/周期24f）でも測る。動きが小さければ
    複製との差も小さいはずで、それも実測しておく。
    """
    r = measure_edge_drift(span=5, amplitude=120.0, period=24.0, size=191.0)
    print(
        f"  span=5 振幅120px/周期24f: n={r['n_frames']} "
        f"複製 mean_iou={r['dup_mean_iou']:.3f} | 区間補間 mean_iou={r['interp_mean_iou']:.3f}"
    )
    assert r["interp_mean_iou"] >= r["dup_mean_iou"], r


def main() -> None:
    tests = [
        test_interval_records_matches_endpoints_and_reuses_build,
        test_interval_records_zero_span,
        test_interval_records_rejects_reversed_range,
        test_interval_kind_override_add_and_remove,
        test_interval_add_records_without_existing_cover_passes_through,
        test_interval_add_records_envelopes_with_existing_detection,
        test_interval_add_records_small_existing_detection_does_not_shrink_interp,
        test_measure_edge_drift_span15_amplitude250_period20,
        test_measure_edge_drift_span5_amplitude120_period24,
    ]
    for t in tests:
        print(f"{t.__name__} ...")
        t()
        print("  OK")
    print(f"\n{len(tests)} 件すべて通過")


if __name__ == "__main__":
    main()
