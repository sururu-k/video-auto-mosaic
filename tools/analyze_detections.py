"""検出結果 JSON を集計して、しきい値とクラス設定の当たり具合を見る。

未処理区間に何が検出されているかが分かれば、「本当に何も映っていない」のか
「人は映っているのに局部を取りこぼしている」のかを、映像を開かずに切り分けられる。
後者なら --conf を下げるか --classes を広げる判断になる。
"""

import argparse
import json
from collections import Counter, defaultdict


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("detections")
    p.add_argument("--report", help="report.json（未処理区間の切り分けに使う）")
    p.add_argument("--fps", type=float, default=30.0)
    args = p.parse_args()

    with open(args.detections, encoding="utf-8") as f:
        data = json.load(f)
    n_frames = data["n_frames"]
    dets = {int(k): v for k, v in data["detections"].items()}

    print(f"総フレーム {n_frames}   検出のあるフレーム {len(dets)}")

    # クラス別の出現フレーム数とスコア分布
    per_class_frames: Counter = Counter()
    per_class_scores: dict[str, list[float]] = defaultdict(list)
    for f, ds in dets.items():
        seen = set()
        for d in ds:
            per_class_scores[d["class"]].append(d["score"])
            if d["class"] not in seen:
                per_class_frames[d["class"]] += 1
                seen.add(d["class"])

    print("\n[クラス別]")
    print(f"  {'クラス':30s} {'フレーム数':>8s} {'割合':>7s} {'最大score':>9s} {'中央score':>9s}")
    for cls, cnt in per_class_frames.most_common():
        s = sorted(per_class_scores[cls])
        print(
            f"  {cls:30s} {cnt:8d} {100.0 * cnt / n_frames:6.1f}% "
            f"{s[-1]:9.3f} {s[len(s) // 2]:9.3f}"
        )

    # スコアしきい値を上げ下げしたときに残るフレーム数
    targets = {"FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED"}
    print("\n[対象クラス(露出のみ)のしきい値感度]")
    for th in (0.10, 0.12, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50):
        cnt = sum(
            1
            for ds in dets.values()
            if any(d["class"] in targets and d["score"] >= th for d in ds)
        )
        print(f"  conf >= {th:.2f}  {cnt:6d} フレーム ({100.0 * cnt / n_frames:5.1f}%)")

    if not args.report:
        return

    with open(args.report, encoding="utf-8") as f:
        rep = json.load(f)
    ranges = rep.get("uncovered_ranges", [])
    if not ranges:
        print("\n未処理区間なし")
        return

    print(f"\n[未処理区間 {len(ranges)} 件の中身]")
    for r in ranges:
        s, e = r["start_frame"], r["end_frame"]
        inside: Counter = Counter()
        best: dict[str, float] = {}
        n_any = 0
        for f in range(s, e + 1):
            ds = dets.get(f)
            if not ds:
                continue
            n_any += 1
            for d in ds:
                inside[d["class"]] += 1
                best[d["class"]] = max(best.get(d["class"], 0.0), d["score"])

        print(
            f"\n  frame {s}-{e} ({r['start_sec']:.1f}s-{r['end_sec']:.1f}s, "
            f"{r['frames']} フレーム)"
        )
        if not inside:
            print("    何も検出されていない（人物自体が映っていない可能性）")
            continue
        print(f"    何かしら検出のあるフレーム: {n_any}/{r['frames']}")
        for cls, cnt in inside.most_common(8):
            print(f"      {cls:30s} {cnt:5d} 件  最大score {best[cls]:.3f}")


if __name__ == "__main__":
    main()
