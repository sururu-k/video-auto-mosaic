"""他社ツールの漏れ一覧（人間検証済み）を機械可読な形に変換する。

`data/bench3/leaks_reported.md` は「0:18～0:19 陰茎 外れ」のような自然文の羅列。
これを (開始秒, 終了秒, 部位) に落として、うちの検出器がその区間で反応しているかを
突き合わせられるようにする。

この一覧の価値は、**そこに局部が確実に映っていることが人間によって保証されている**
点にある。他社ツールが漏らした＝モザイクが無い状態で局部が見えていた、ということ
なので、うちがそこで沈黙していれば、それがそのままうちの穴になる。
"""

from __future__ import annotations

import argparse
import json
import re

# 「0:18～0:19」「3:39～4:53」「7:41」（単一時刻）の両方に対応する。
# 波ダッシュは全角チルダ(U+FF5E)と波ダッシュ(U+301C)が混在しうるので両方見る。
TIME = r"(\d+):(\d{2})"
RANGE = re.compile(TIME + r"\s*[~〜～\-]\s*" + TIME)
SINGLE = re.compile(TIME)


def to_sec(m: str, s: str) -> int:
    return int(m) * 60 + int(s)


def parse(text: str) -> list[dict]:
    """本文を区間の並びに分解する。

    区切りが改行とは限らず、1行に全部詰まっている場合もある。時刻表現を
    見つけた位置で切って、次の時刻表現までを説明文として扱う。
    """
    items: list[dict] = []
    # 時刻表現の開始位置を全部拾い、そこで区切る
    starts = [m.start() for m in SINGLE.finditer(text)]
    # 範囲表現の後半の時刻は区切りにしない
    ranges = {(m.start(), m.end()) for m in RANGE.finditer(text)}
    heads = []
    for s in starts:
        if any(a < s < b for a, b in ranges):
            continue
        heads.append(s)

    for i, s in enumerate(heads):
        e = heads[i + 1] if i + 1 < len(heads) else len(text)
        chunk = text[s:e].strip()
        rm = RANGE.match(chunk)
        if rm:
            start = to_sec(rm.group(1), rm.group(2))
            end = to_sec(rm.group(3), rm.group(4))
            desc = chunk[rm.end():].strip()
        else:
            sm = SINGLE.match(chunk)
            if not sm:
                continue
            start = end = to_sec(sm.group(1), sm.group(2))
            desc = chunk[sm.end():].strip()

        desc = desc.strip(" 　")
        if not desc:
            continue
        items.append(
            {
                "start_sec": start,
                "end_sec": end,
                "duration_sec": max(1, end - start),
                "desc": desc,
                "intermittent": "断続" in desc,
            }
        )
    return items


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("md", help="漏れ一覧の Markdown")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--fps", type=float, default=30.0)
    args = p.parse_args()

    with open(args.md, encoding="utf-8") as f:
        text = f.read()

    items = parse(text)
    for it in items:
        it["start_frame"] = int(round(it["start_sec"] * args.fps))
        it["end_frame"] = int(round(it["end_sec"] * args.fps))

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"fps": args.fps, "intervals": items}, f, ensure_ascii=False, indent=1)

    total = sum(it["duration_sec"] for it in items)
    print(f"{len(items)} 区間 / 合計 {total} 秒 ({total / 60:.1f} 分)")
    print(f"うち断続的と記載されたもの {sum(1 for it in items if it['intermittent'])} 件")
    print(f"\n最初の5件:")
    for it in items[:5]:
        print(
            f"  {it['start_sec'] // 60}:{it['start_sec'] % 60:02d}"
            f"-{it['end_sec'] // 60}:{it['end_sec'] % 60:02d}  "
            f"frame {it['start_frame']}-{it['end_frame']}  {it['desc']}"
        )
    print(f"\n{args.out} に保存")


if __name__ == "__main__":
    main()
