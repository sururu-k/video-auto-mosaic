"""監査 2026-08-23 / 再監査（検証担当実測分）で挙がった「漏れる側に外れる」欠陥の回帰テスト。

対象は C-5（手修正の黙殺）、C-7（大面積検出の全落ち）、
推定のみ区間の報告漏れ、冒頭 docstring の処理順、および再監査での積み残し
A（視面積比ちょうど1.0の全面検出が既定で drop される）、
I（min_area_ratio が可視面積で判定され画面端の検出を誤って落とす）、
H（--frame-step > 1 の素材で推定のみ区間の報告が間引き由来のノイズで水増しされる）、
issue #10（bridge_uncovered / estimated_only_ranges がフレーム単位で「矩形が
1個でもあれば覆われている」と判定していたため、同じフレームに複数対象が
映る場面で片方の対象の穴がもう片方の矩形に隠れて報告からも橋渡しからも
消えていた件）、
issue #11（推定区間のマージンが速度の外挿だけで、往復運動で対象を完全に外す）。
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
    Region,
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


def test_bridge_globally_runs_before_bridge_by_lineage():
    """issue #10 の再検証（PR #66）で見つかった退行の回帰テスト。

    `bridge_uncovered()` は `_bridge_globally`（フレーム丸ごと未処理の区間を
    対象の同定をせず時間的な近さだけで埋める最終防波堤）を先に、
    `_bridge_by_lineage`（系統単位の橋渡し）を後に呼ぶ必要がある。
    順序を逆にすると、系統単位の橋渡しが先に region を足してしまい、続く
    `_bridge_globally` の「フレームが覆われているか」判定が誤って True になって
    最終防波堤が一切起動しなくなる。系統として復活しない対象（トラックが
    そこで終わって二度と戻ってこない）は、系統単位の橋渡し候補が無いまま
    素通しになる（実測: bench3 で 130 矩形・736,126 セルが失われた退行と
    同じ機構）。

    対象A: frame 0-49 のみ観測され、以後二度と現れない。
    対象B: frame 0-49 と 60-149 に同じ位置で観測される（間の 50-59 は欠損）。
    50-59 は A・B どちらの観測も無い、フレーム丸ごと空の区間にしてある。

    「対象Aの位置が覆われているか」を直接 assert すると、正しい実装でも
    落ちる（防波堤矩形は前後の外接矩形を lerp するので、区間の終盤に近づく
    ほど対象Bの位置へ寄っていき、対象Aの位置からは離れる）。ここでは代わりに
    「_bridge_globally が足す lineage=-1 の防波堤矩形が区間の全フレームに
    存在すること」を見る。順序を逆にした版では、対象Bの系統単位の橋渡し
    だけでフレームが「覆われた」ことになり、_bridge_globally が一切起動しなく
    なるため、lineage=-1 の矩形が区間全体から消える
    （実測: `automosaic/temporal.py` の `bridge_uncovered()` 内で
    `_bridge_by_lineage` を `_bridge_globally` より先に呼ぶよう入れ替えると、
    frame 50-59 の矩形数が 2 から 1 に減り、lineage=-1 の矩形が全フレームで
    消えることを確認済み）。
    """
    N = 150
    dets: dict[int, list[Detection]] = {f: [] for f in range(N)}
    for f in range(0, 50):
        dets[f].append(Detection(CLS, 0.8, (0, 0, 80, 80)))       # 対象A（以後復活しない）
        dets[f].append(Detection(CLS, 0.8, (400, 400, 40, 40)))   # 対象B
    for f in range(60, N):
        dets[f].append(Detection(CLS, 0.8, (400, 400, 40, 40)))   # 対象Bのみ復活

    cfg = TemporalConfig(
        max_gap=5, memory=0, stitch_max_gap=0, bridge_max=150, min_track_len=0
    )
    regions, stats = process(dets, N, W, H, {CLS}, cfg)

    assert stats["tracks_stitched"] == 0, "対象A・Bが stitch で繋がってしまっている（前提が崩れている）"
    assert stats["regions_bridged"] > 0, "橋渡しが一切起動していない"

    for f in range(50, 60):
        lineages = sorted(r.lineage for _, r in regions[f])
        assert -1 in lineages, (
            f"フレーム{f}: 系統をまたぐ最終防波堤(lineage=-1)の矩形が消えている "
            f"(regions={[(b, r.source, r.lineage) for b, r in regions[f]]})"
        )
    print(
        "  系統として復活しない対象がいても最終防波堤(lineage=-1)が全区間に残る OK "
        f"(regions_bridged={stats['regions_bridged']})"
    )


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


def _overlap_area(a, b) -> float:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ax1, ay1 = ax0 + aw, ay0 + ah
    bx1, by1 = bx0 + bw, by0 + bh
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    return max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)


def _sine_box(t: float, cx0: float, cy0: float, amplitude: float, period: float, side: float):
    cx = cx0 + amplitude * math.sin(2 * math.pi * t / period)
    return (cx - side / 2, cy0 - side / 2, side, side)


def test_memory_envelope_does_not_miss_reversing_motion():
    """issue #11: memory 外挿区間は往復運動（正弦運動）の対象を完全には外さないこと。

    直す前は端点速度で矩形そのものを直進外挿しており、往復運動では速度の符号が
    反転するため外挿が誤った方向へ伸び続けた。実測（振幅250px/周期20f、W=1920,
    H=1080、対象サイズ191px、memory=20、観測は半周期ぶんの t=0..10 で
    ゼロ交差=最大速度点の t=10 で打ち切り）で 12/19 フレームが対象を完全に外した。
    """
    W_, H_ = 1920, 1080
    S = 191.0
    A, T = 250.0, 20.0
    CX, CY = 960.0, 540.0
    n_frames = 40
    obs_end = 10

    dets: dict[int, list[Detection]] = {f: [] for f in range(n_frames)}
    for f in range(0, obs_end + 1):
        dets[f] = [Detection(CLS, 0.9, _sine_box(float(f), CX, CY, A, T, S))]

    cfg = TemporalConfig(memory=20, memory_before=0, min_track_len=0, bridge_max=0, stitch_max_gap=0)
    regions, _ = process(dets, n_frames, W_, H_, {CLS}, cfg)

    miss = 0
    for f in range(obs_end + 1, min(n_frames, obs_end + 20)):
        gt = _sine_box(float(f), CX, CY, A, T, S)
        drawn = regions.get(f, [])
        covered = sum(_overlap_area(gt, box) for box, _ in drawn)
        if covered <= 0.0:
            miss += 1
    assert miss == 0, f"往復運動の memory 外挿区間で {miss} フレームが対象を完全に外した"
    print("  memory 包絡は往復運動を完全には外さない OK")


def test_memory_envelope_still_tracks_straight_moving_target():
    """issue #11 の修正が直進する対象への追従を退行させないこと。

    外挿をやめて直前の観測位置に固定するだけの実装だと、速度 20px/frame の
    直進で 0/19 -> 3/19 に悪化する（実測）。「止まった」仮説と「その速度で
    進み続けた」仮説の両方を外接矩形の和で覆うことで両立させている。
    """
    W_, H_ = 1920, 1080
    S = 191.0
    speed = 20.0
    n_frames = 40
    obs_end = 10

    def box_at(t: float):
        cx = 500.0 + speed * t
        return (cx - S / 2, 540.0 - S / 2, S, S)

    dets: dict[int, list[Detection]] = {f: [] for f in range(n_frames)}
    for f in range(0, obs_end + 1):
        dets[f] = [Detection(CLS, 0.9, box_at(float(f)))]

    cfg = TemporalConfig(memory=20, memory_before=0, min_track_len=0, bridge_max=0, stitch_max_gap=0)
    regions, _ = process(dets, n_frames, W_, H_, {CLS}, cfg)

    miss = 0
    for f in range(obs_end + 1, min(n_frames, obs_end + 20)):
        gt = box_at(float(f))
        drawn = regions.get(f, [])
        covered = sum(_overlap_area(gt, box) for box, _ in drawn)
        if covered <= 0.0:
            miss += 1
    assert miss == 0, f"直進する対象で memory 区間が {miss} フレーム外れた（regression）"
    print("  memory 包絡は直進する対象への追従を退行させない OK")


def test_interpolation_envelope_does_not_miss_reversal_inside_gap():
    """issue #11: 補間区間の中に往復の折り返し（トラフ）が丸ごと入る場合でも
    完全に対象を外さないこと。

    直す前は2点を結ぶ直線上の1点を矩形にしていたため、折り返しが区間の
    中間に収まっていると直線から大きく外れる（実測: 8/17 -> 修正後 0/17）。
    観測は gap の両端で3フレームずつ連続させ、実際のトラックと同じく
    局所速度を持たせている。
    """
    W_, H_ = 1920, 1080
    S = 191.0
    A, T = 250.0, 20.0
    CX, CY = 960.0, 540.0
    n_frames = 40
    gap_start, gap_len, context = 8, 17, 3
    b_frame = gap_start + gap_len + 1

    dets: dict[int, list[Detection]] = {f: [] for f in range(n_frames)}
    for f in range(gap_start - context + 1, gap_start + 1):
        dets[f] = [Detection(CLS, 0.9, _sine_box(float(f), CX, CY, A, T, S))]
    for f in range(b_frame, b_frame + context):
        dets[f] = [Detection(CLS, 0.9, _sine_box(float(f), CX, CY, A, T, S))]

    cfg = TemporalConfig(
        memory=0, min_track_len=0, bridge_max=0, stitch_max_gap=0, max_gap=gap_len + 1
    )
    regions, _ = process(dets, n_frames, W_, H_, {CLS}, cfg)

    miss = 0
    for f in range(gap_start + 1, b_frame):
        gt = _sine_box(float(f), CX, CY, A, T, S)
        drawn = regions.get(f, [])
        covered = sum(_overlap_area(gt, box) for box, _ in drawn)
        if covered <= 0.0:
            miss += 1
    assert miss == 0, f"補間区間内の折り返しで {miss} フレームが対象を完全に外した"
    print("  補間の包絡は区間内の折り返しを完全には外さない OK")


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


def test_cut_frames_prevents_track_spanning():
    """切れ目を指定したときにトラックが切れ目をまたがないこと（issue #92）。

    フレーム 0-20 に検出1、フレーム 30-50 に検出2 を配置し、同じクラス・位置。
    通常は max_gap=12 を超えるので分断されるが、もし gap が小さければ繋がる。
    フレーム 25 に切れ目を指定すれば、0-20 と 30-50 は絶対に繋がらない。
    """
    dets: dict[int, list[Detection]] = {f: [] for f in range(60)}
    box = (100, 100, 50, 50)
    for f in range(0, 21):
        dets[f] = [Detection(CLS, 0.9, box)]
    for f in range(30, 51):
        dets[f] = [Detection(CLS, 0.9, box)]

    cfg_no_cut = TemporalConfig(max_gap=12, memory=0, memory_before=0, bridge_max=0, stitch_max_gap=0)
    cfg_with_cut = TemporalConfig(
        max_gap=12, memory=0, memory_before=0, bridge_max=0, stitch_max_gap=0, cut_frames={25}
    )

    regions_no_cut, _ = process(dets, 60, W, H, {CLS}, cfg_no_cut)
    regions_with_cut, _ = process(dets, 60, W, H, {CLS}, cfg_with_cut)

    # どちらでも 0-20 は覆われている（観測がある）
    assert all(len(regions_no_cut.get(f, [])) > 0 for f in range(0, 21))
    assert all(len(regions_with_cut.get(f, [])) > 0 for f in range(0, 21))

    # 観測のあるフレームの被覆だけを見ても、トラックが繋がったかは分からない。
    # 隙間（21-29）が補間で埋まるかどうかで見る。gap は 10 で max_gap=12 より
    # 小さいので、切れ目が無ければ同じトラックになって densify が埋める。
    filled_no_cut = sum(1 for f in range(21, 30) if len(regions_no_cut.get(f, [])) > 0)
    filled_with_cut = sum(1 for f in range(21, 30) if len(regions_with_cut.get(f, [])) > 0)
    assert filled_no_cut == 9, f"切れ目なしなら隙間が埋まるはず: {filled_no_cut}/9"
    assert filled_with_cut == 0, (
        f"切れ目をまたいで補間されている: {filled_with_cut}/9"
        "（build_tracks が切れ目を見ていない）"
    )

    # どちらでも 30-50 は覆われている
    assert all(len(regions_no_cut.get(f, [])) > 0 for f in range(30, 51))
    assert all(len(regions_with_cut.get(f, [])) > 0 for f in range(30, 51))

    # 21-29 は観測がない。通常（no_cut）では補間や橋渡しで埋まるかもしれない。
    # with_cut では切れ目があるので、21-29 は埋まらない。
    covered_no_cut = sum(1 for f in range(21, 30) if len(regions_no_cut.get(f, [])) > 0)
    covered_with_cut = sum(1 for f in range(21, 30) if len(regions_with_cut.get(f, [])) > 0)

    assert covered_with_cut == 0, (
        f"切れ目を指定しても区間 21-29 が {covered_with_cut} フレーム埋まっている"
    )
    print(f"  切れ目がトラック生成を区切る OK （no_cut: 21-29 で {covered_no_cut} フレーム、with_cut: 0 フレーム）")


def test_cut_frames_prevents_stitching():
    """切れ目を指定したときに stitch_tracks が切れ目をまたぐ結合をしないこと。

    フレーム 10-15 にトラック1、フレーム 25-30 にトラック2 を配置。
    同じクラス・位置で、gap=10 は stitch_max_gap=20 以内なので、通常は結合される。
    フレーム 20 に切れ目を指定すれば、2つのトラックは結合されない。
    """
    dets: dict[int, list[Detection]] = {f: [] for f in range(40)}
    box = (100, 100, 50, 50)
    for f in [10, 11, 12, 13, 14, 15]:
        dets[f] = [Detection(CLS, 0.9, box)]
    for f in [25, 26, 27, 28, 29, 30]:
        dets[f] = [Detection(CLS, 0.9, box)]

    cfg_no_cut = TemporalConfig(
        max_gap=1, memory=0, memory_before=0, bridge_max=0, stitch_max_gap=20
    )
    cfg_with_cut = TemporalConfig(
        max_gap=1, memory=0, memory_before=0, bridge_max=0, stitch_max_gap=20, cut_frames={20}
    )

    regions_no_cut, stats_no_cut = process(dets, 40, W, H, {CLS}, cfg_no_cut)
    regions_with_cut, stats_with_cut = process(dets, 40, W, H, {CLS}, cfg_with_cut)

    # no_cut では 2 つのトラックが結合される可能性がある
    # with_cut では結合されない
    assert stats_with_cut["tracks_stitched"] == 0, (
        f"切れ目を指定しても {stats_with_cut['tracks_stitched']} トラックが結合されている"
    )
    print(f"  切れ目が結合を防止する OK （no_cut: {stats_no_cut['tracks_stitched']} stitched, with_cut: 0）")


def test_cut_frames_prevents_interpolation():
    """切れ目を指定したときに densify が切れ目をまたいで補間しないこと。

    フレーム 10 と 30 に検出を配置。gap=20 で補間される。
    フレーム 20 に切れ目を指定すれば、11-29 は補間されず、観測がない区間は素通しになる。
    """
    dets: dict[int, list[Detection]] = {f: [] for f in range(40)}
    box = (100, 100, 50, 50)
    dets[10] = [Detection(CLS, 0.9, box)]
    dets[30] = [Detection(CLS, 0.9, box)]

    cfg_no_cut = TemporalConfig(
        max_gap=25, memory=0, memory_before=0, bridge_max=0, stitch_max_gap=0
    )
    cfg_with_cut = TemporalConfig(
        max_gap=25, memory=0, memory_before=0, bridge_max=0, stitch_max_gap=0, cut_frames={20}
    )

    regions_no_cut, stats_no_cut = process(dets, 40, W, H, {CLS}, cfg_no_cut)
    regions_with_cut, stats_with_cut = process(dets, 40, W, H, {CLS}, cfg_with_cut)

    # フレーム 11-19 (切れ目前) と 21-29 (切れ目後) の補間
    interp_before_cut = sum(1 for f in range(11, 20) if len(regions_with_cut.get(f, [])) > 0)
    interp_after_cut = sum(1 for f in range(21, 30) if len(regions_with_cut.get(f, [])) > 0)

    # フレーム 10 から切れ目 20 までは補間可能だが、切れ目で止まるはず
    assert interp_before_cut == 0, (
        f"切れ目を指定しても 11-19 が {interp_before_cut} フレーム補間されている"
    )
    assert interp_after_cut == 0, (
        f"切れ目を指定しても 21-29 が {interp_after_cut} フレーム補間されている"
    )
    print(f"  切れ目が補間を防止する OK （no_cut: {stats_no_cut['regions_interpolated']} interpolated, with_cut: 0）")


def test_cut_frames_prevents_bridging():
    """切れ目を指定したときに bridge_uncovered が切れ目をまたいで埋めないこと。

    フレーム 0-10 に検出、フレーム 40-50 に検出を配置。
    通常は 11-39 が前後の根拠で橋渡しされる（bridge_max=40）。
    フレーム 25 に切れ目を指定すれば、11-24 と 26-39 に分かれ、
    各区間は bridge_max=40 を超えないが、切れ目をまたがないので埋まらない。
    """
    dets: dict[int, list[Detection]] = {f: [] for f in range(60)}
    box = (100, 100, 50, 50)
    for f in range(0, 11):
        dets[f] = [Detection(CLS, 0.9, box)]
    for f in range(40, 51):
        dets[f] = [Detection(CLS, 0.9, box)]

    cfg_no_cut = TemporalConfig(
        max_gap=1, memory=0, memory_before=0, bridge_max=40, stitch_max_gap=0
    )
    cfg_with_cut = TemporalConfig(
        max_gap=1, memory=0, memory_before=0, bridge_max=40, stitch_max_gap=0, cut_frames={25}
    )

    regions_no_cut, stats_no_cut = process(dets, 60, W, H, {CLS}, cfg_no_cut)
    regions_with_cut, stats_with_cut = process(dets, 60, W, H, {CLS}, cfg_with_cut)

    # 11-39 の中間区間の被覆
    bridged_with_cut = sum(1 for f in range(11, 40) if len(regions_with_cut.get(f, [])) > 0)

    # 切れ目前 (11-24) と 切れ目後 (26-39) に分かれるので、どちらも bridge_max 内だが
    # 切れ目をまたがないので埋まらない
    assert bridged_with_cut == 0, (
        f"切れ目を指定しても 11-39 が {bridged_with_cut} フレーム橋渡しされている"
    )
    print(f"  切れ目が橋渡しを防止する OK （no_cut: {stats_no_cut['frames_bridged']} bridged, with_cut: 0）")


def test_cut_frames_empty_matches_original():
    """空集合の cut_frames を指定したときに、指定なしのときと同じ結果になること。

    これが最も重要なテスト。修正前後で被覆が変わってはいけない。
    """
    # 複雑な検出パターンを生成（トラック複数、結合、補間、橋渡しが全部起こる設定）
    dets: dict[int, list[Detection]] = {f: [] for f in range(100)}
    box1 = (50, 50, 60, 60)
    box2 = (300, 300, 80, 80)

    # トラック1: 0-15（観測）-> 20-30（gap）-> 35-50（観測、結合の対象）
    for f in range(0, 16):
        dets[f].append(Detection(CLS, 0.9, box1))
    for f in range(35, 51):
        dets[f].append(Detection(CLS, 0.9, box1))

    # トラック2: 60-70（観測）-> gap -> 80-90（観測）
    for f in range(60, 71):
        dets[f].append(Detection(CLS, 0.9, box2))
    for f in range(80, 91):
        dets[f].append(Detection(CLS, 0.9, box2))

    cfg_original = TemporalConfig()
    cfg_with_empty_cut = TemporalConfig(cut_frames=set())

    regions_orig, stats_orig = process(dets, 100, W, H, {CLS}, cfg_original)
    regions_empty, stats_empty = process(dets, 100, W, H, {CLS}, cfg_with_empty_cut)

    # 全フレームの被覆が完全に一致すること
    for f in range(100):
        orig_boxes = sorted([b for b, _ in regions_orig.get(f, [])])
        empty_boxes = sorted([b for b, _ in regions_empty.get(f, [])])
        assert orig_boxes == empty_boxes, (
            f"フレーム {f}: 被覆が異なる (original={len(orig_boxes)}, empty={len(empty_boxes)})"
        )

    # 統計が完全に一致すること
    for key in ["frames_with_mosaic", "regions_interpolated", "regions_from_memory", "frames_bridged"]:
        assert stats_orig[key] == stats_empty[key], (
            f"統計 {key} が異なる (original={stats_orig[key]}, empty={stats_empty[key]})"
        )

    print("  空集合 cut_frames で元と完全一致 OK")

def test_weak_evidence_frames_detects_weak_reasons():
    """weak_evidence_frames が弱い根拠を検出すること。

    推定領域 / 低信頼 / 面積の急変を検出し、いずれも無い場合は空リストを返す。
    """
    # フレーム0: 検出済み、高信頼（0.5）-> 根拠なし
    r0_detected = Region(source="detected", score=0.5, category="sexual")

    # フレーム1: 推定領域あり -> 推定領域の理由が付く
    r1_estimated = Region(source="interpolated", score=0.5, category="sexual")

    # フレーム2: 低信頼（0.2 < 0.30） -> 低信頼の理由が付く
    r2_lowconf = Region(source="detected", score=0.2, category="sexual")

    # フレーム3,4: 面積が2倍以上に変わる -> 面積の急変の理由が付く
    r3_area = Region(source="detected", score=0.5, category="sexual")
    r4_area = Region(source="detected", score=0.5, category="sexual")

    regions_per_frame = {
        0: [((10, 20, 100, 100), r0_detected)],  # area = 10000
        1: [((10, 20, 100, 100), r1_estimated)],  # area = 10000
        2: [((10, 20, 100, 100), r2_lowconf)],   # area = 10000
        3: [((10, 20, 100, 100), r3_area)],      # area = 10000
        4: [((10, 20, 200, 200), r4_area)],      # area = 40000
    }

    flags = temporal.weak_evidence_frames(regions_per_frame, 5)

    # フレーム0は根拠なし -> 記録されない
    assert not any(f["frame"] == 0 for f in flags), "フレーム0は根拠がないので記録されないはず"

    # フレーム1は推定領域 -> 記録される
    f1 = next((f for f in flags if f["frame"] == 1), None)
    assert f1 is not None, "フレーム1は記録されるはず"
    assert any("推定領域" in r for r in f1["reasons"]), "フレーム1に推定領域の理由が付くはず"

    # フレーム2は低信頼 -> 記録される
    f2 = next((f for f in flags if f["frame"] == 2), None)
    assert f2 is not None, "フレーム2は記録されるはず"
    assert any("低信頼" in r for r in f2["reasons"]), "フレーム2に低信頼の理由が付くはず"

    # フレーム4は面積の急変 -> 記録される
    f4 = next((f for f in flags if f["frame"] == 4), None)
    assert f4 is not None, "フレーム4は記録されるはず"
    assert any("面積の急変" in r for r in f4["reasons"]), "フレーム4に面積の急変の理由が付くはず"

    print("  weak_evidence_frames が弱い根拠を検出する OK")


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
