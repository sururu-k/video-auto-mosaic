"""監査 2026-08-23 / 再監査（検証担当実測分）で挙がった「漏れる側に外れる」欠陥の回帰テスト。

対象は C-5（手修正の黙殺）、C-7（大面積検出の全落ち）、
推定のみ区間の報告漏れ、冒頭 docstring の処理順、および再監査での積み残し
A（視面積比ちょうど1.0の全面検出が既定で drop される）、
I（min_area_ratio が可視面積で判定され画面端の検出を誤って落とす）、
H（--frame-step > 1 の素材で推定のみ区間の報告が間引き由来のノイズで水増しされる）、
issue #10（bridge_uncovered / estimated_only_ranges がフレーム単位で「矩形が
1個でもあれば覆われている」と判定していたため、同じフレームに複数対象が
映る場面で片方の対象の穴がもう片方の矩形に隠れて報告からも橋渡しからも
消えていた件）。
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automosaic.temporal as temporal  # noqa: E402
from automosaic import corrections as corr  # noqa: E402
from automosaic.detector import Detection  # noqa: E402
from automosaic.temporal import (  # noqa: E402
    TemporalConfig,
    estimated_only_ranges,
    geometric_filter,
    process,
)

CLS = "FEMALE_GENITALIA_EXPOSED"
W, H = 640, 480


def _square_box(ratio: float) -> tuple[float, float, float, float]:
    """フレーム面積比が ratio になる、中央寄せの正方形。"""
    side = math.sqrt(ratio * W * H)
    return ((W - side) / 2, (H - side) / 2, side, side)


def test_large_detections_are_not_dropped():
    """面積比 0.35 を超える検出でモザイクが消えないこと。

    以前は max_area_ratio=0.35 で捨てていたため、比 0.34 なら 60/60 覆えるのに
    0.36 だと 0/60 になった。「大きく映っている＝いちばん塞ぐべき」場面で
    被覆が丸ごと消える、いちばん悪い外れ方だった。
    """
    for ratio in (0.30, 0.34, 0.36, 0.50, 0.90):
        box = _square_box(ratio)
        dets = {f: [Detection(CLS, 0.9, box)] for f in range(60)}
        _, stats = process(dets, 60, W, H, {CLS}, TemporalConfig())
        assert stats["frames_with_mosaic"] == 60, (
            f"面積比 {ratio} で被覆が {stats['frames_with_mosaic']}/60 に落ちている"
        )
        assert stats["geometric_dropped"] == 0, f"面積比 {ratio} が捨てられている"
    print("  大面積検出の残存 OK（比 0.36 / 0.50 / 0.90 でも 60/60）")


def test_oversized_detections_are_counted():
    """捨てはしないが、大きすぎた件数は黙らずに数えること。"""
    dets = {0: [Detection(CLS, 0.9, _square_box(0.60))]}
    _, stats = process(dets, 1, W, H, {CLS}, TemporalConfig())
    assert stats["oversized_kept"] == 1, stats["oversized_kept"]
    assert stats["geometric_dropped"] == 0

    dets = {0: [Detection(CLS, 0.9, _square_box(0.20))]}
    _, stats = process(dets, 1, W, H, {CLS}, TemporalConfig())
    assert stats["oversized_kept"] == 0
    print("  大きすぎた検出の件数報告 OK")


def test_full_frame_detection_is_not_dropped_by_default():
    """フレームをまるごと覆う検出が既定では捨てられないこと（再監査 A）。

    検出器は box をフレームにクランプせず外接矩形を出すので、画面ぴったりでも
    四方にはみ出していても visible_ratio は等しく厳密に 1.0 になる。以前の既定
    drop_area_ratio=1.0 は、この「ちょうど1.0」をすべて drop していた。
    全面ドアップは「いちばん塞ぐべき場面」であり、全画面がモザイクになるのは
    正しい出力。誤検出が画面全体を覆っても塗り過ぎ側（許容できる）に倒れるだけ
    なので、既定では捨てない。

    実測で確認した組み合わせ:
      - 全編ドアップ（60フレーム全部が全面検出）: 修正前 0/60 -> 修正後 60/60
      - 画面ぴったり／四方はみ出し／左だけはみ出し、いずれも visible_ratio==1.0
    """
    boxes = [
        ("画面ぴったり", (0, 0, W, H)),
        ("四方はみ出し", (-10, -10, W + 20, H + 20)),
        ("左だけはみ出し", (-10, 0, W + 10, H)),
    ]
    for name, box in boxes:
        assert temporal._visible_ratio(box, W, H) == 1.0, (name, box)
        dets = {f: [Detection(CLS, 0.9, box)] for f in range(60)}
        _, stats = process(dets, 60, W, H, {CLS}, TemporalConfig())
        assert stats["frames_with_mosaic"] == 60, (
            f"{name} で被覆が {stats['frames_with_mosaic']}/60 に落ちている"
        )
        assert stats["geometric_dropped"] == 0, name

    # 明示的に指定すれば従来どおり捨てることもできる（値そのものは残してある）
    dets = {0: [Detection(CLS, 0.9, (0, 0, W, H))]}
    _, stats = process(dets, 1, W, H, {CLS}, TemporalConfig(drop_area_ratio=1.0))
    assert stats["geometric_dropped"] == 1
    assert stats["frames_with_mosaic"] == 0
    print("  全面検出は既定で残る OK（明示指定すれば従来どおり捨てられる）")


def test_opening_closeup_is_not_leaked():
    """冒頭が全面ドアップ、以降が通常の検出という素材で先頭が素通しにならないこと。

    再監査での実測: 修正前は frame 0-23 の24フレームが完全な素通しになっていた。
    """
    dets = {}
    for f in range(60):
        if f < 20:
            dets[f] = [Detection(CLS, 0.9, (0, 0, W, H))]
        else:
            dets[f] = [Detection(CLS, 0.9, (200, 200, 60, 60))]
    _, stats = process(dets, 60, W, H, {CLS}, TemporalConfig())
    assert stats["frames_with_mosaic"] == 60, (
        f"冒頭ドアップ区間が素通しになっている: {stats['frames_with_mosaic']}/60"
    )
    print("  冒頭ドアップでも素通しにならない OK")


def test_min_area_ratio_uses_raw_area_not_visible_area():
    """画面端にはみ出した検出が、可視面積の縮小を理由に誤って落とされないこと（再監査 I）。

    可視面積で判定すると、対象自体は十分な大きさでも画面端に来た瞬間に比が
    縮んで「小さすぎる」として捨てられる方向に倒れる（漏れる側）。
    実測: box=(600,440,200,200)（右下にはみ出し）は生の比0.1302・可視比0.0052で、
    min_area_ratio=0.01 のとき可視比だと drop、生の比なら keep だった。
    """
    box = (600.0, 440.0, 200.0, 200.0)
    cfg = TemporalConfig(min_area_ratio=0.01)
    kept, dropped, _ = geometric_filter({0: [Detection(CLS, 0.9, box)]}, W, H, cfg)
    assert dropped == 0, "はみ出しただけの検出が「小さすぎる」として捨てられている"
    assert len(kept[0]) == 1
    print("  min_area_ratio は生の面積で判定する OK")


def test_overflowing_box_is_judged_by_visible_area():
    """はみ出した矩形は、見えている面積で判定すること。

    生の面積で見ると、画面の一部しか覆っていない検出が比 1.0 超と数えられて
    「全面検出」として捨てられる。
    """
    box = (300.0, 200.0, 700.0, 500.0)  # 生の面積比は 1.1 を超える
    assert box[2] * box[3] / (W * H) > 1.0
    dets = {0: [Detection(CLS, 0.9, box)]}
    out, n_dropped, _ = geometric_filter(dets, W, H, TemporalConfig())
    assert n_dropped == 0, "はみ出しただけの検出が捨てられている"
    assert len(out[0]) == 1
    print("  はみ出し矩形の面積判定 OK")


def test_out_of_range_corrections_are_reported():
    """範囲外フレームの手修正を黙って捨てないこと。

    手修正は最後の砦なので、反映できなかったなら件数が呼び出し側に届く必要がある。
    戻り値の形は変えていないので、既存の呼び出しはそのまま動く。
    """
    dets = {0: [Detection(CLS, 0.9, (100, 100, 40, 40))]}
    regions, _ = process(dets, 10, W, H, {CLS}, TemporalConfig())

    cs = corr.CorrectionSet(
        items=[
            corr.Correction(3, (10.0, 10.0, 20.0, 20.0), CLS, "add"),
            corr.Correction(999, (10.0, 10.0, 20.0, 20.0), CLS, "add"),
            corr.Correction(1000, (10.0, 10.0, 20.0, 20.0), CLS, "add"),
            corr.Correction(-1, (10.0, 10.0, 20.0, 20.0), CLS, "remove"),
        ]
    )

    stats: dict = {}
    out = corr.apply(regions, cs, stats=stats)

    assert stats["total"] == 4
    assert stats["applied"] == 1
    assert stats["dropped_out_of_range"] == 3
    assert stats["dropped_frames"] == [-1, 999, 1000]

    # 戻り値は従来どおり領域の辞書だけ
    assert isinstance(out, dict) and len(out) == 10
    assert "manual" in [r.source for _, r in out[3]], out[3]
    print("  範囲外の手修正の件数報告 OK（4 件中 3 件が範囲外）")


def test_apply_stats_when_all_applied():
    """全部反映できたときは、落とした件数が0で立つこと。"""
    dets = {0: [Detection(CLS, 0.9, (100, 100, 40, 40))]}
    regions, _ = process(dets, 10, W, H, {CLS}, TemporalConfig())
    cs = corr.CorrectionSet(
        items=[
            corr.Correction(2, (10.0, 10.0, 20.0, 20.0), CLS, "add"),
            corr.Correction(2, (0.0, 0.0, 640.0, 480.0), CLS, "remove"),
        ]
    )
    stats: dict = {}
    corr.apply(regions, cs, stats=stats)
    assert stats["dropped_out_of_range"] == 0
    assert stats["applied"] == 2
    assert stats["applied_add"] == 1 and stats["applied_remove"] == 1

    # 修正が空でも統計は返る
    stats2: dict = {}
    corr.apply(regions, corr.CorrectionSet(), stats=stats2)
    assert stats2["total"] == 0 and stats2["dropped_out_of_range"] == 0
    print("  反映できたときの統計 OK")


def test_apply_has_no_shared_mutable_state():
    """apply() が関数属性など呼び出し間で共有される可変状態を持たないこと（再監査 E）。

    以前は `apply.last_stats` という関数属性に直近の結果を残していたが、これは
    全呼び出しで共有される1個のグローバル変数だった。stats 引数だけを正とする
    設計に変えたので、この属性自体が存在しない。
    """
    assert not hasattr(corr.apply, "last_stats")
    print("  last_stats 属性の廃止 OK")


def test_apply_stats_is_call_local_under_concurrency():
    """並行呼び出しでも stats が他スレッドの値と混ざらないこと（再監査 E）。

    以前の apply.last_stats（関数属性）は3スレッド×200回の実測で、呼び出しと
    読み取りの間に 0.2ms 置くと 600 回の読み取り中 512 回が他スレッドの値を
    返していた。stats 引数は呼び出しごとのローカル変数なのでそもそも共有されない。
    """
    import threading
    import time

    mismatches = [0]
    lock = threading.Lock()

    def worker(tid: int) -> None:
        for _ in range(200):
            items = [
                corr.Correction(0, (0.0, 0.0, 10.0, 10.0), CLS, "add")
                for _ in range(tid + 1)
            ]
            cs = corr.CorrectionSet(items=items)
            stats: dict = {}
            corr.apply({0: []}, cs, stats=stats)
            time.sleep(0.0002)
            if stats["applied_add"] != tid + 1:
                with lock:
                    mismatches[0] += 1

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert mismatches[0] == 0, f"{mismatches[0]}/600 回、他スレッドの結果と混ざった"
    print("  並行呼び出しでも stats が混ざらない OK")


def test_short_estimated_only_ranges_are_reported():
    """2〜4フレームの推定のみ区間が報告から消えないこと。

    実素材では漏れが 0.2 秒（数フレーム）の粒度で出入りするので、
    短い区間ほど落としてはいけない。以前の既定 min_len=5 は逆だった。
    """
    for gap in (1, 2, 3, 4, 5):
        d: dict[int, list[Detection]] = {f: [] for f in range(40)}
        d[5] = [Detection(CLS, 0.9, (100, 100, 40, 40))]
        d[5 + gap + 1] = [Detection(CLS, 0.9, (100, 100, 40, 40))]
        cfg = TemporalConfig(memory=0, min_track_len=0, bridge_max=0)
        regions, _ = process(d, 40, W, H, {CLS}, cfg)
        est = estimated_only_ranges(regions, 40)
        assert len(est) == 1, f"推定のみ {gap} フレームの区間が報告されていない"
        s_, e_, _ = est[0]
        assert e_ - s_ + 1 == gap, (gap, est)
    print("  短い推定のみ区間の報告 OK（1〜5フレーム）")


def test_frame_step_sampling_noise_is_suppressed_but_real_gaps_survive():
    """--frame-step > 1 の間引き検出で、刻み由来のノイズだけを落とすこと（再監査 H）。

    実測（data/bench3/det.json, 55303フレーム, 5フレーム刻み検出）: min_len=1 だと
    3524 区間のうち 2399 件がちょうど間引き幅(4フレーム)の構造的な穴で、情報量が
    無かった。ここでは5フレーム刻みのサンプリングを模し、通常の4フレーム区間
    （間引きそのもの）は報告から消え、2連続でサンプルが欠けた本物の長い漏れ
    （14フレーム）は消えずに残ることを確認する。

    estimated_only_ranges() は regions_per_frame から frame_step を自動推定する
    （呼び出し元の cli.py 等はこの引数を渡していないため、シグネチャは
    2引数のままで後方互換を保っている）。
    """
    N = 300
    dets: dict[int, list[Detection]] = {f: [] for f in range(N)}
    skip = {150, 155}  # 2連続のサンプル欠損 = 本物の長い漏れを模す
    for f in range(0, N, 5):
        if f in skip:
            continue
        dets[f] = [Detection(CLS, 0.9, (100, 100, 40, 40))]

    cfg = TemporalConfig(
        max_gap=20, memory=0, min_track_len=0, bridge_max=0, stitch_max_gap=0, frame_step=5
    )
    regions, _ = process(dets, N, W, H, {CLS}, cfg)

    # 引数を足さず既定のまま呼ぶ（cli.py の実際の呼び出し方と同じ）
    est = estimated_only_ranges(regions, N)
    lengths = sorted(e_ - s_ + 1 for s_, e_, _ in est)
    assert 4 not in lengths, f"間引き由来の4フレーム区間が残っている: {lengths}"
    assert lengths == [14], f"本物の長い漏れが消えている、または余計な区間がある: {lengths}"

    # 間引きなし（frame_step=1）の素材では従来どおり全区間が報告される
    cfg1 = TemporalConfig(memory=0, min_track_len=0, bridge_max=0, stitch_max_gap=0)
    d1: dict[int, list[Detection]] = {f: [] for f in range(40)}
    d1[5] = [Detection(CLS, 0.9, (100, 100, 40, 40))]
    d1[8] = [Detection(CLS, 0.9, (100, 100, 40, 40))]
    regions1, _ = process(d1, 40, W, H, {CLS}, cfg1)
    est1 = estimated_only_ranges(regions1, 40)
    assert len(est1) == 1 and est1[0][1] - est1[0][0] + 1 == 2
    print(f"  間引き由来ノイズの抑制 OK（4フレーム区間は消え、本物の14フレーム漏れは残る）")


def test_bridge_is_not_masked_by_other_object():
    """issue #10: 対象Aの穴が、同じフレームに映る対象Bの矩形に隠れて
    bridge が起動しない不具合の回帰テスト。

    修正前は `covered = [bool(per_frame.get(f)) for f in range(n_frames)]` が
    フレーム単位だったため、対象Bが常に映っていると、対象Aのトラックが
    max_gap を超えて分断されても bridge が一切起動しなかった
    （実測: regions_bridged が 0 のまま、50フレーム丸ごと対象Aが素通しになる）。

    対象Aは同じ位置に静止（gap前後で位置・大きさが同一）なので、stitch と
    同じ判定基準を使う系統判定なら同一対象と分かる。ただし stitch_max_gap は
    意図的に対象Aの穴(50フレーム)より短く設定し、stitch_tracks 自体では
    繋がらないようにしてある（純粋に bridge 側の系統判定を検証するため）。
    """
    N = 150
    dets: dict[int, list[Detection]] = {f: [] for f in range(N)}
    # 対象A: 同じ位置に静止。フレーム50-99が検出漏れ
    for f in list(range(0, 50)) + list(range(100, 150)):
        dets[f].append(Detection(CLS, 0.8, (50, 50, 40, 40)))
    # 対象B: 別の位置に、全フレームで途切れず映り続ける
    for f in range(N):
        dets[f].append(Detection(CLS, 0.8, (400, 400, 40, 40)))

    cfg = TemporalConfig(
        max_gap=12, memory=0, stitch_max_gap=40, bridge_max=150, min_track_len=0
    )
    regions, stats = process(dets, N, W, H, {CLS}, cfg)

    assert stats["tracks_stitched"] == 0, "対象Aの前後トラックが stitch で繋がってしまっている（前提が崩れている）"
    assert stats["regions_bridged"] > 0, "対象Aの穴が一切橋渡しされていない"

    for f in range(50, 100):
        assert len(regions[f]) == 2, (
            f"フレーム{f}: 対象Bの矩形に隠れて対象Aの穴が橋渡しされていない "
            f"(regions={[r[0] for r in regions[f]]})"
        )
        xs = sorted(b[0] for b, _ in regions[f])
        assert xs[0] < 100, f"フレーム{f}: 対象A側の矩形が見当たらない ({xs})"
    print(f"  他対象に隠れた穴の橋渡し OK (regions_bridged={stats['regions_bridged']})")


def test_estimated_only_is_not_masked_by_other_object():
    """issue #10: 対象Aの「推定のみ」区間が、同じフレームの対象Bの実観測に
    隠れて estimated_only_ranges から消える不具合の回帰テスト。

    修正前は `has_real = any(...)` がフレーム内の全領域を対象にしていたため、
    対象Aが補間だけで対象Bが毎フレーム実観測なら、対象Aの補間区間が
    一件も報告されなかった。
    """
    N = 20
    dets: dict[int, list[Detection]] = {f: [] for f in range(N)}
    # 対象A: フレーム0と10だけ実観測、1-9は補間のみ
    dets[0].append(Detection(CLS, 0.8, (50, 50, 40, 40)))
    dets[10].append(Detection(CLS, 0.8, (50, 50, 40, 40)))
    # 対象B: 毎フレーム実観測
    for f in range(N):
        dets[f].append(Detection(CLS, 0.8, (400, 400, 40, 40)))

    cfg = TemporalConfig(max_gap=12, memory=0, min_track_len=0, bridge_max=0)
    regions, _ = process(dets, N, W, H, {CLS}, cfg)
    est = estimated_only_ranges(regions, N)

    assert est, "対象Aの推定のみ区間(1-9)が対象Bの実観測に隠れて報告から消えている"
    assert any(s == 1 and e == 9 for s, e, _ in est), est
    print(f"  他対象の実観測に隠れた推定のみ区間の報告 OK ({est})")


def test_estimated_only_range_split_across_lineages_is_not_dropped():
    """系統ごとの判定に切り替えた副作用で、フレーム単位なら effective_min_len
    以上あった推定のみ区間が、系統の境目で分断されて両方とも
    effective_min_len 未満に落ち、報告からまるごと消える回帰テスト。

    対象A（frame0のみ実観測、その後 memory で3フレーム推定=1-3）と
    対象B（frame7のみ実観測、その手前に memory_before で3フレーム推定=4-6）
    は別の場所にいる別対象で、系統としては繋がらない
    （中心距離が stitch_dist_ratio の許容範囲を大きく超える）。

    フレーム単位で見れば 1-6 の6フレームが連続して「どの対象も実観測が無い」
    区間だが、系統ごとに見ると A は 1-3（3フレーム）、B は 4-6（3フレーム）
    に分断され、どちらも min_len=5 未満で個別には報告されない。
    系統ごとの判定とフレーム単位の判定の**両方**を計算して和を取らないと、
    この6フレームは報告から丸ごと消える（人手レビューの導線が消える＝
    漏れる方向）。
    """
    N = 20
    dets: dict[int, list[Detection]] = {f: [] for f in range(N)}
    dets[0].append(Detection(CLS, 0.9, (50, 50, 40, 40)))   # 対象A
    dets[7].append(Detection(CLS, 0.9, (500, 400, 40, 40)))  # 対象B（遠い別対象）

    cfg = TemporalConfig(
        max_gap=12, memory=3, memory_before=3, min_track_len=0,
        bridge_max=0, stitch_max_gap=0,
    )
    regions, stats = process(dets, N, W, H, {CLS}, cfg)
    assert stats["tracks_stitched"] == 0, "対象Aと対象Bが stitch で繋がってしまっている（前提が崩れている）"

    est = estimated_only_ranges(regions, N, min_len=5)
    covered = set()
    for s, e, _ in est:
        covered.update(range(s, e + 1))
    missing = [f for f in range(1, 7) if f not in covered]
    assert not missing, (
        f"系統の境目で分断され、フレーム単位なら6フレーム連続だった推定のみ"
        f"区間が報告から消えている（消えたフレーム: {missing}, 報告: {est}）"
    )
    print(f"  系統の境目で分断される推定のみ区間の報告 OK ({est})")


def test_lineage_groups_never_merges_temporally_overlapping_tracks():
    """_lineage_groups() の gap<=0 除外を検証する回帰テスト。

    docstring は「同時に映る別対象2体が1系統に潰れると issue #10 の欠陥を
    そのまま再現する」と明記している。ここでは、位置が完全に同一で
    （素性判定だけなら確実にマッチする）、かつ時間的に重なる2トラックが、
    重なっている一点をもって絶対に同じ系統にならないことを直接確認する。
    """
    tracks = [
        temporal.Track(cls=CLS, obs={0: ((100.0, 100.0, 40.0, 40.0), 0.9)}),
        temporal.Track(cls=CLS, obs={0: ((100.0, 100.0, 40.0, 40.0), 0.9)}),
    ]
    cfg = TemporalConfig()
    ids = temporal._lineage_groups(tracks, W, H, cfg)
    assert ids[0] != ids[1], (
        f"完全に同時刻・同位置の別トラックが同じ系統に潰れている（gap<=0 除外が"
        f"効いていない）: ids={ids}"
    )
    print(f"  同時に映るトラックは系統を分ける OK (ids={ids})")


def test_lineage_groups_does_not_transitively_merge_via_shared_candidate():
    """_lineage_groups() の claimed 除外を検証する回帰テスト。

    docstring は「1つの後続トラックを複数の先行トラックが取り合うと、
    無関係な同時対象2体が共通の後続候補を介して間接的に同じ系統へ潰れる」と
    明記している。同時刻に映る2トラック A・B が、どちらも同じ後続トラック C
    と（時間的にも空間的にも）良くマッチする場面を作り、A が先に C を
    掴んだあとは B が C 経由で A と同じ系統へ潰れないことを確認する。
    """
    box = (100.0, 100.0, 40.0, 40.0)
    a = temporal.Track(cls=CLS, obs={0: (box, 0.9)})
    b = temporal.Track(cls=CLS, obs={0: (box, 0.9)})
    c = temporal.Track(cls=CLS, obs={10: (box, 0.9)})
    tracks = [a, b, c]
    cfg = TemporalConfig()
    ids = temporal._lineage_groups(tracks, W, H, cfg)

    assert ids[0] != ids[1], (
        f"同時に映る A・B が、共通の後続候補 C を介して間接的に同じ系統へ"
        f"潰れている（claimed 除外が効いていない）: ids={ids}"
    )
    print(f"  共通候補の取り合いで系統が潰れない OK (ids={ids})")


def test_module_docstring_matches_implementation():
    """冒頭 docstring の処理順が、process() の実装順と食い違わないこと。"""
    import inspect

    doc = temporal.__doc__ or ""
    src = inspect.getsource(temporal.process)

    steps = [
        ("幾何フィルタ", "geometric_filter("),
        ("トラッキング", "build_tracks("),
        ("結合", "stitch_tracks("),
        ("デスパイク", "despike("),
        ("補間", "densify("),
        ("橋渡し", "bridge_uncovered("),
        ("膨張", "expand("),
    ]

    # docstring の処理順の行だけを見る（後段の説明文に同じ語が出るため）
    lines = doc.splitlines()
    head = "\n".join(lines[: next(i for i, ln in enumerate(lines) if "docs/" in ln)])

    doc_pos = [head.find(label) for label, _ in steps]
    assert all(p >= 0 for p in doc_pos), [
        label for (label, _), p in zip(steps, doc_pos) if p < 0
    ]
    assert doc_pos == sorted(doc_pos), "docstring の処理順が実装と違う"

    src_pos = [src.find(fn) for _, fn in steps]
    assert all(p >= 0 for p in src_pos)
    assert src_pos == sorted(src_pos), "process() の呼び出し順が docstring と違う"
    print("  冒頭 docstring と実装順の一致 OK")


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
