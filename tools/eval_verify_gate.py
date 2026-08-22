"""出力側の検証ゲートが、目視で見つけた漏れとどれだけ一致するかを測る。

ゲートは「別の検出器がモザイクの外に反応したら警告」という仕組みだが、
警告が多すぎると人が見なくなるので使い物にならない。
目視で確定している漏れを正解として、どのしきい値なら実用になるかを探す。

見るべき指標:
  再現率  目視で見つけた漏れのうち、ゲートが警告したものの割合
  警告数  総警告数。人が確認できる量に収まるか
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys


def load_known_leaks(pattern: str) -> tuple[set[int], set[int]]:
    """目視検査の結果から、漏れフレームと正常フレームを集める。"""
    leaks, clean = set(), set()
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        for fr in d.get("frames", []):
            if fr.get("leak"):
                leaks.add(int(fr["frame"]))
            else:
                clean.add(int(fr["frame"]))
    return leaks, clean - leaks


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("verify_report", help="verify_output.py が出した JSON")
    p.add_argument("--leakcheck", required=True, help="目視検査の JSON のグロブ")
    p.add_argument(
        "--tolerance",
        type=int,
        default=3,
        help="目視したフレームの前後このフレーム数までは同じ箇所とみなす",
    )
    args = p.parse_args()

    with open(args.verify_report, encoding="utf-8") as f:
        rep = json.load(f)
    findings = rep["findings"]

    leaks, clean = load_known_leaks(args.leakcheck)
    if not leaks:
        print("目視で確定した漏れが見つかりません")
        sys.exit(0)

    print(f"検証モデル {rep['verify_model']}  検証 {rep['frames_checked']} フレーム")
    print(f"目視の正解: 漏れ {len(leaks)} フレーム / 正常 {len(clean)} フレーム\n")

    print(
        f"{'conf':>6s}{'外側':>7s}{'警告数':>8s}{'漏れを捕捉':>12s}"
        f"{'再現率':>8s}{'正常への誤報':>14s}"
    )
    for conf in (0.10, 0.15, 0.20, 0.25, 0.30, 0.40):
        for outside in (0.5, 0.8):
            hit_frames = {
                fd["frame"]
                for fd in findings
                if fd["score"] >= conf and fd["outside"] >= outside
            }
            caught = {
                lf
                for lf in leaks
                if any(abs(lf - hf) <= args.tolerance for hf in hit_frames)
            }
            false_on_clean = {
                cf
                for cf in clean
                if any(abs(cf - hf) <= args.tolerance for hf in hit_frames)
            }
            print(
                f"{conf:>6.2f}{outside:>7.1f}{len(hit_frames):>8d}"
                f"{len(caught):>10d}/{len(leaks):<2d}"
                f"{100 * len(caught) / len(leaks):>7.0f}%"
                f"{len(false_on_clean):>10d}/{len(clean):<3d}"
            )

    # 捕捉できなかった漏れを出す。ゲートの穴がどこかを見るため
    hit = {fd["frame"] for fd in findings}
    missed = sorted(
        lf for lf in leaks if not any(abs(lf - hf) <= args.tolerance for hf in hit)
    )
    if missed:
        print(f"\n[ゲートが捕捉できなかった漏れ {len(missed)} 件]")
        print("  " + ", ".join(str(m) for m in missed[:30]))


if __name__ == "__main__":
    main()
