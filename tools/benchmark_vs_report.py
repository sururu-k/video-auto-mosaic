"""他社ツールの漏れ一覧と、うちの検出結果を突き合わせる。

一覧の各区間は「他社ツールがモザイクを外した＝そこに局部が確実に映っていた」
ことを人間が確認したもの。つまり **正解が保証された区間** である。

うちの検出器がその区間で反応していれば、同じ場面を捕まえられている。
沈黙していれば、それがうちの穴になる。

注意: これは「うちが漏らす」ことの証明ではない。検出できていても矩形が足りずに
はみ出す型の漏れは別にあるので、最終的な確認は出力の目視になる。ここで測るのは
**検出段階での取りこぼし**だけ。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic.detector import DEFAULT_CLASSES, Detection  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("detections", help="うちの検出結果 JSON")
    p.add_argument("--intervals", required=True, help="他社の漏れ一覧 JSON")
    p.add_argument("--classes", default=",".join(DEFAULT_CLASSES))
    p.add_argument("--out", help="結果の Markdown 出力先")
    args = p.parse_args()

    classes = {c.strip() for c in args.classes.split(",") if c.strip()}

    with open(args.detections, encoding="utf-8") as f:
        det = json.load(f)
    per_frame = {
        int(k): [Detection.from_dict(x) for x in v]
        for k, v in det["detections"].items()
    }
    n_frames = det["n_frames"]

    with open(args.intervals, encoding="utf-8") as f:
        rep = json.load(f)
    intervals = rep["intervals"]
    fps = rep.get("fps", 30.0)

    rows = []
    for it in intervals:
        s, e = it["start_frame"], min(it["end_frame"], n_frames - 1)
        hits = []
        for f in range(s, e + 1):
            for d in per_frame.get(f, []):
                if d.cls in classes:
                    hits.append((f, d.cls, d.score))
        frames_with = len({f for f, _, _ in hits})
        # 間引いて検出しているので、区間内で実際に推論したフレーム数を数える
        sampled = sum(1 for f in range(s, e + 1) if f in per_frame)
        rows.append(
            {
                "start_sec": it["start_sec"],
                "end_sec": it["end_sec"],
                "desc": it["desc"],
                "frames": e - s + 1,
                "sampled": sampled,
                "hit_frames": frames_with,
                "max_score": max((sc for _, _, sc in hits), default=0.0),
                "classes": sorted({c for _, c, _ in hits}),
                "detected": frames_with > 0,
            }
        )

    n_det = sum(1 for r in rows if r["detected"])
    total_sec = sum(r["end_sec"] - r["start_sec"] + 1 for r in rows)

    lines = []
    lines.append("# ベンチマーク: 他社ツールが漏らした区間でうちは検出できるか\n")
    lines.append(
        f"素材 マイビデオ-3.mp4 / {n_frames} フレーム / {n_frames / fps / 60:.1f} 分\n"
    )
    lines.append(
        "他社ツールの処理結果に対する人間検証済みの漏れ一覧を正解として使う。"
        "各区間は「そこに局部が確実に映っていた」ことが保証されている。\n"
    )
    lines.append("## 結果\n")
    lines.append("| | |")
    lines.append("|---|---|")
    lines.append(f"| 他社が漏らした区間 | {len(rows)} 件 / 合計 {total_sec} 秒 |")
    lines.append(
        f"| **うちの検出器が反応した区間** | **{n_det} 件 ({100 * n_det / len(rows):.0f}%)** |"
    )
    lines.append(f"| 沈黙した区間 | {len(rows) - n_det} 件 |")
    lines.append("")

    silent = [r for r in rows if not r["detected"]]
    if silent:
        lines.append(f"## 沈黙した区間 {len(silent)} 件\n")
        lines.append("| 時刻 | 長さ | 内容 |")
        lines.append("|---|---|---|")
        for r in silent:
            lines.append(
                f"| {r['start_sec'] // 60}:{r['start_sec'] % 60:02d}"
                f"-{r['end_sec'] // 60}:{r['end_sec'] % 60:02d} "
                f"| {r['end_sec'] - r['start_sec'] + 1}秒 | {r['desc']} |"
            )
        lines.append("")

    lines.append("## 全区間\n")
    lines.append("| 時刻 | 長さ | 検出 | 最大score | クラス | 内容 |")
    lines.append("|---|---|---|---|---|---|")
    for r in rows:
        mark = f"{r['hit_frames']}/{r['sampled']}" if r["detected"] else "**なし**"
        cls = ", ".join(c.replace("_EXPOSED", "").replace("_GENITALIA", "") for c in r["classes"])
        lines.append(
            f"| {r['start_sec'] // 60}:{r['start_sec'] % 60:02d}"
            f"-{r['end_sec'] // 60}:{r['end_sec'] % 60:02d} "
            f"| {r['end_sec'] - r['start_sec'] + 1}秒 | {mark} | {r['max_score']:.2f} "
            f"| {cls} | {r['desc']} |"
        )

    text = "\n".join(lines) + "\n"
    print(f"区間 {len(rows)} 件 / 検出あり {n_det} 件 ({100 * n_det / len(rows):.0f}%)")
    if silent:
        print(f"沈黙 {len(silent)} 件:")
        for r in silent[:15]:
            print(
                f"  {r['start_sec'] // 60}:{r['start_sec'] % 60:02d}"
                f"-{r['end_sec'] // 60}:{r['end_sec'] % 60:02d}  {r['desc']}"
            )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"\n{args.out} に保存")


if __name__ == "__main__":
    main()
