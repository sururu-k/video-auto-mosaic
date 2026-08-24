"""tools/structural_silence_gate.py を実素材（他社ツールが漏らした78区間）で評価する。

issue #13 の完了条件:
- 判定基準（painted_flags の min_cells）の分離幅と、外れる向き（危険側）を数字で示す
- 78区間・21,030フレームでの評価（`docs/11-coverage-vs-leak-report.md` が突き合わせた
  「他社が漏らした場所」を独立に評価する）
- min_run_frames を振って、構造的沈黙ゲートがどれだけ「本番が申告した未塗装」を
  拾えるかを見る（review queue としての実用性）

必要な入力（すべて先に生成しておくこと。このスクリプト自体はffmpegを呼ばない）:
  --scan-report   tools/structural_silence_gate.py --report の出力
                  （collapsed_counts を含む、全フレーム走査の結果）
  --leaks         tools/parse_leak_report.py の出力（pooblem.md を区間化したもの）
  --production-report  automosaic の report.json（本番が自己申告した uncovered_ranges）

「本番が計算した領域」を GT に使う理由: `render.pixelize_plane` は決定的に動くので、
regionが1つも無ければそのフレームは絶対に塗られていない。これは検出器が対象を
見つけたかとは無関係な事実（検出器の盲点ではなく、render.py が実際に何をしたか）。
"""

from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

from structural_silence_gate import painted_flags, find_silent_runs  # noqa: E402


def frames_in_ranges(ranges: list[dict], key_start="start_frame", key_end="end_frame") -> set[int]:
    out: set[int] = set()
    for r in ranges:
        out.update(range(r[key_start], r[key_end] + 1))
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="structural_silence_gate を78区間の人間検証GTで評価する")
    p.add_argument("--scan-report", required=True)
    p.add_argument("--leaks", required=True)
    p.add_argument("--production-report", required=True)
    p.add_argument("--min-cells", type=int, default=3)
    args = p.parse_args()

    with open(args.scan_report, encoding="utf-8") as f:
        scan = json.load(f)
    with open(args.leaks, encoding="utf-8") as f:
        leaks = json.load(f)
    with open(args.production_report, encoding="utf-8") as f:
        prod = json.load(f)

    collapsed_counts = scan["collapsed_counts"]
    n_scanned = scan["n_frames_scanned"]
    min_cells = args.min_cells

    interval_frames: set[int] = set()
    for it in leaks["intervals"]:
        interval_frames.update(range(it["start_frame"], min(it["end_frame"], n_scanned - 1) + 1))
    print(f"78区間の対象フレーム: {len(interval_frames)}（走査済み {n_scanned} フレーム中）")

    prod_uncovered_frames = frames_in_ranges(prod["uncovered_ranges"])
    prod_uncovered_frames = {f for f in prod_uncovered_frames if f < n_scanned}

    painted = painted_flags(collapsed_counts, min_cells)

    # --- 78区間内の判定 ---
    interval_painted = sum(1 for f in interval_frames if painted[f])
    interval_unpainted = len(interval_frames) - interval_painted
    print(f"\n[78区間 {len(interval_frames)} フレームの画素判定 (min_cells={min_cells})]")
    print(f"  画素でモザイクあり  {interval_painted} ({100*interval_painted/len(interval_frames):.1f}%)")
    print(f"  画素でモザイク無し  {interval_unpainted} ({100*interval_unpainted/len(interval_frames):.1f}%)")

    # --- 「確実に未塗装」の集合: 本番が uncovered と自己申告し、かつ78区間内 ---
    known_gap = interval_frames & prod_uncovered_frames
    print(f"\n[「確実に未塗装」(本番の自己申告 uncovered_ranges と78区間の重なり)]  {len(known_gap)} フレーム")

    # --- 分離幅・外れる向き ---
    danger = sorted(f for f in known_gap if painted[f])  # 未塗装のはずが画素で「あり」
    blind_spot_candidates = interval_frames - prod_uncovered_frames  # 本番は「塗った」と主張
    blind_spot = sorted(f for f in blind_spot_candidates if not painted[f])  # なのに画素で見えない

    print(f"\n[外れる向き]")
    print(f"  危険側: 未塗装のはずが画素で「あり」と判定  {len(danger)} / {len(known_gap)}"
          f" ({100*len(danger)/len(known_gap) if known_gap else 0:.3f}%)")
    if danger:
        print(f"    frame例: {danger[:20]}")
    print(f"  盲点側: 本番塗装の申告があるのに画素で見えない  {len(blind_spot)} / {len(blind_spot_candidates)}"
          f" ({100*len(blind_spot)/len(blind_spot_candidates) if blind_spot_candidates else 0:.3f}%)")

    if danger:
        print(
            "\n  [判定] 危険側の外れが0件ではありません。このパラメータ(min_cells="
            f"{min_cells})はこの素材のゲートとしてそのまま使えません。"
        )
    else:
        print(
            f"\n  [判定] 危険側の外れは0件でした（{len(known_gap)}件中）。"
            "分離の余裕は下の分布セクションで確認すること。"
        )

    # --- 潰れたセル数の分布（分離幅） ---
    gap_counts = sorted(collapsed_counts[f] for f in known_gap)
    interval_painted_counts = sorted(
        collapsed_counts[f] for f in interval_frames if f not in prod_uncovered_frames
    )

    def pct(vals, p):
        if not vals:
            return None
        k = max(0, min(len(vals) - 1, int(round(p / 100 * (len(vals) - 1)))))
        return vals[k]

    print(f"\n[潰れたセル数の分布]")
    print(f"  「確実に未塗装」  n={len(gap_counts)}  "
          f"min={gap_counts[0] if gap_counts else None}  "
          f"p50={pct(gap_counts,50)}  p99={pct(gap_counts,99)}  max={gap_counts[-1] if gap_counts else None}")
    print(f"  「本番塗装の申告あり」  n={len(interval_painted_counts)}  "
          f"min={interval_painted_counts[0] if interval_painted_counts else None}  "
          f"p1={pct(interval_painted_counts,1)}  p50={pct(interval_painted_counts,50)}  "
          f"max={interval_painted_counts[-1] if interval_painted_counts else None}")
    if gap_counts and interval_painted_counts:
        margin = pct(interval_painted_counts, 1) - gap_counts[-1]
        print(f"  「確実に未塗装」の最大値 {gap_counts[-1]} 対 「申告塗装あり」の第1パーセンタイル "
              f"{pct(interval_painted_counts,1)}  -> 閾値min_cells={min_cells}に対する余裕 {margin}")

    # --- 構造的沈黙ゲート(run-length)としての実用性: min_run_frames を振る ---
    print(f"\n[min_run_frames を振った場合の「未塗装 {len(known_gap)} フレーム」の捕捉]")
    print(f"{'min_run':>8s}{'全体の沈黙件数':>16s}{'全体の沈黙フレーム数':>20s}{'捕捉':>14s}{'捕捉率':>8s}")
    for min_run in (1, 2, 4, 8, 16, 30, 60):
        runs = find_silent_runs(painted, min_run_frames=min_run)
        flagged_frames: set[int] = set()
        for r in runs:
            flagged_frames.update(range(r["start_frame"], r["end_frame"] + 1))
        caught = known_gap & flagged_frames
        print(
            f"{min_run:>8d}{len(runs):>16d}{len(flagged_frames):>20d}"
            f"{len(caught):>10d}/{len(known_gap):<3d}"
            f"{100*len(caught)/len(known_gap) if known_gap else 0:>7.1f}%"
        )

    missed_examples = sorted(known_gap - {
        f for r in find_silent_runs(painted, min_run_frames=8) for f in range(r["start_frame"], r["end_frame"] + 1)
    })
    print(f"\nmin_run_frames=8 で見つからなかった「確実に未塗装」フレーム: {len(missed_examples)} / {len(known_gap)}")
    if missed_examples:
        print(f"  例: {missed_examples[:20]}")


if __name__ == "__main__":
    main()
