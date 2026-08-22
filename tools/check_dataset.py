"""書き出した学習データの座標が正しいかを目で確かめられる形にする。

YOLO形式は中心座標と幅高さを 0〜1 で持つ。正規化の掛け違いや xy の取り違えは
数字を眺めても気づけないので、実際に画像へ矩形を描いて確認する。
ここを間違えると、学習データ全体が静かに壊れる。
"""

from __future__ import annotations

import argparse
import glob
import os
import random

import cv2


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("dataset_dir")
    p.add_argument("--out-dir", default=None)
    p.add_argument("--count", type=int, default=8)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    out_dir = args.out_dir or os.path.join(args.dataset_dir, "_check")
    os.makedirs(out_dir, exist_ok=True)

    classes_path = os.path.join(args.dataset_dir, "classes.txt")
    with open(classes_path, encoding="utf-8") as f:
        classes = [ln.strip() for ln in f if ln.strip()]

    images = sorted(glob.glob(os.path.join(args.dataset_dir, "images", "*.png")))
    if not images:
        print("画像が見つかりません")
        return

    random.seed(args.seed)
    picked = random.sample(images, min(args.count, len(images)))

    colors = [(80, 200, 255), (120, 255, 120), (255, 140, 120)]
    stats = {"boxes": 0, "outside": 0, "tiny": 0}

    for img_path in picked:
        stem = os.path.splitext(os.path.basename(img_path))[0]
        lbl_path = os.path.join(args.dataset_dir, "labels", stem + ".txt")
        img = cv2.imread(img_path)
        if img is None:
            continue
        h, w = img.shape[:2]

        if os.path.exists(lbl_path):
            with open(lbl_path, encoding="utf-8") as f:
                for ln in f:
                    parts = ln.split()
                    if len(parts) != 5:
                        continue
                    ci = int(parts[0])
                    cx, cy, bw, bh = (float(v) for v in parts[1:])
                    stats["boxes"] += 1
                    # 正規化が壊れていれば 0〜1 を外れる
                    if not (0 <= cx <= 1 and 0 <= cy <= 1 and 0 < bw <= 1 and 0 < bh <= 1):
                        stats["outside"] += 1
                    if bw * w < 4 or bh * h < 4:
                        stats["tiny"] += 1

                    x1 = int((cx - bw / 2) * w)
                    y1 = int((cy - bh / 2) * h)
                    x2 = int((cx + bw / 2) * w)
                    y2 = int((cy + bh / 2) * h)
                    col = colors[ci % len(colors)]
                    cv2.rectangle(img, (x1, y1), (x2, y2), col, 2)
                    name = classes[ci] if ci < len(classes) else str(ci)
                    cv2.putText(
                        img, name.replace("_EXPOSED", ""), (x1, max(12, y1 - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, col, 1, cv2.LINE_AA,
                    )

        cv2.imwrite(os.path.join(out_dir, f"{stem}_boxed.png"), img)

    print(f"{len(picked)} 枚に矩形を描いて {out_dir} に保存")
    print(f"矩形 {stats['boxes']} 件 / 0〜1を外れたもの {stats['outside']} 件 / 4px未満 {stats['tiny']} 件")
    if stats["outside"]:
        print("  正規化が壊れている可能性がある")


if __name__ == "__main__":
    main()
