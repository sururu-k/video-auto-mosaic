"""出力の目視検査の結果を annotations.json にまとめる。

検査は「モザイク済みの出力を見て、性器・肛門が外に出ていないか」を判定したもの。
モデルの自己申告スコアから独立した唯一の事実なので、これを正として修正に流す。

漏れ無しと判定されたフレームは box を null にして残す。annotations_to_corrections
側で「ここには無い」の意味になり、補間がそこで止まる。漏れ有りのフレームだけを
残すと、間の漏れ無し区間まで補間で塗ってしまう。

入力は各エージェントが返した JSON をファイルに保存したもの、または
{"frames":[...]} を並べた1つのファイル。
"""

from __future__ import annotations

import argparse
import glob
import json
import os


def load_reports(paths: list[str]) -> list[dict]:
    frames: list[dict] = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
        # ```json ... ``` で囲まれていても読めるようにする
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text)
        if isinstance(data, dict):
            frames.extend(data.get("frames", []))
        elif isinstance(data, list):
            frames.extend(data)
    return frames


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("reports", nargs="+", help="検査結果の JSON（グロブ可）")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--class", dest="cls", default="MALE_GENITALIA_EXPOSED")
    args = p.parse_args()

    paths: list[str] = []
    for r in args.reports:
        paths.extend(sorted(glob.glob(r)) or [r])

    frames = load_reports(paths)
    if not frames:
        print("検査結果が読めませんでした")
        return

    # 同じフレームが重複したら「漏れあり」を優先する。見逃しより過剰を選ぶ
    by_frame: dict[int, dict] = {}
    for fr in frames:
        n = int(fr["frame"])
        cur = by_frame.get(n)
        if cur is None or (fr.get("leak") and not cur.get("leak")):
            by_frame[n] = fr

    annotations = []
    n_leak = 0
    for n in sorted(by_frame):
        fr = by_frame[n]
        if fr.get("leak") and fr.get("box"):
            annotations.append(
                {
                    "frame": n,
                    "box": [int(v) for v in fr["box"]],
                    "class": fr.get("class") or args.cls,
                    "note": fr.get("what", ""),
                }
            )
            n_leak += 1
        else:
            annotations.append({"frame": n, "box": None})

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(annotations, f, ensure_ascii=False, indent=1)

    print(f"検査 {len(by_frame)} フレーム / 漏れ {n_leak} 箇所")
    if n_leak:
        print("漏れの内訳:")
        for a in annotations:
            if a["box"]:
                print(f"  frame {a['frame']:>5}  {a['box']}  {a.get('note','')}")
    print(f"{args.out} に保存")


if __name__ == "__main__":
    main()
