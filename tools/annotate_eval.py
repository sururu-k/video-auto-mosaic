"""評価セットに正解ラベルを付ける最小のビューア。

キーを叩くだけで進むようにしてある。1枚あたり1秒で終わるので80枚でも2分。

  Y / →      局部が映っている
  N / ←      映っていない
  S          判断保留（スキップ）
  Backspace  1枚戻る
  Q / Esc    終了（進捗は都度保存されるので途中でやめてよい）

矩形は取らない。フレーム単位の有無だけで「モザイクが要るのに何も塗られなかった
フレーム」を数えられ、それが本件で本当に知りたい指標だから。

tkinter は標準ライブラリ。Tk 8.6 は PNG を直接読めるので Pillow も不要。
"""

import argparse
import json
import os
import sys
import tkinter as tk


class Annotator:
    def __init__(self, root: tk.Tk, frames_dir: str, labels_path: str) -> None:
        self.root = root
        self.dir = frames_dir
        self.labels_path = labels_path

        with open(os.path.join(frames_dir, "index.json"), encoding="utf-8") as f:
            self.index = json.load(f)
        self.frames: list[int] = self.index["frames"]

        self.labels: dict[str, str] = {}
        if os.path.exists(labels_path):
            with open(labels_path, encoding="utf-8") as f:
                self.labels = json.load(f).get("labels", {})

        # 未ラベルの先頭から再開する
        self.pos = 0
        for i, fr in enumerate(self.frames):
            if str(fr) not in self.labels:
                self.pos = i
                break
        else:
            self.pos = len(self.frames) - 1

        root.title("局部の有無を判定")
        root.configure(bg="#1a1a1a")

        self.status = tk.Label(
            root, font=("Meiryo", 13), bg="#1a1a1a", fg="#eeeeee", pady=6
        )
        self.status.pack()

        self.canvas = tk.Label(root, bg="#1a1a1a")
        self.canvas.pack()

        self.help = tk.Label(
            root,
            text="Y/→ 映っている    N/← 映っていない    S 保留    Backspace 戻る    Q 終了",
            font=("Meiryo", 10),
            bg="#1a1a1a",
            fg="#999999",
            pady=8,
        )
        self.help.pack()

        for key in ("y", "Y", "Right"):
            root.bind(f"<{key}>", lambda e: self.label("yes"))
        for key in ("n", "N", "Left"):
            root.bind(f"<{key}>", lambda e: self.label("no"))
        for key in ("s", "S"):
            root.bind(f"<{key}>", lambda e: self.label("skip"))
        root.bind("<BackSpace>", lambda e: self.back())
        for key in ("q", "Q", "Escape"):
            root.bind(f"<{key}>", lambda e: self.quit())

        self.photo = None
        self.show()

    def show(self) -> None:
        if self.pos >= len(self.frames):
            self.quit()
            return
        fr = self.frames[self.pos]
        path = os.path.join(self.dir, f"{fr:06d}.png")
        self.photo = tk.PhotoImage(file=path)
        self.canvas.configure(image=self.photo)

        done = sum(1 for f in self.frames if str(f) in self.labels)
        cur = self.labels.get(str(fr), "-")
        sec = fr / self.index["fps"]
        self.status.configure(
            text=f"{self.pos + 1} / {len(self.frames)}   "
            f"frame {fr}  ({sec:.1f}s)   "
            f"ラベル済 {done}   現在: {cur}"
        )

    def label(self, value: str) -> None:
        fr = self.frames[self.pos]
        self.labels[str(fr)] = value
        self.save()
        self.pos += 1
        if self.pos >= len(self.frames):
            self.quit()
        else:
            self.show()

    def back(self) -> None:
        if self.pos > 0:
            self.pos -= 1
            self.show()

    def save(self) -> None:
        with open(self.labels_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "video": self.index["video"],
                    "total_frames": self.index["total_frames"],
                    "fps": self.index["fps"],
                    "labels": self.labels,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    def quit(self) -> None:
        self.save()
        done = sum(1 for f in self.frames if str(f) in self.labels)
        print(f"{done}/{len(self.frames)} 枚をラベル済み -> {self.labels_path}")
        self.root.destroy()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("frames_dir")
    p.add_argument("--labels", default=None)
    args = p.parse_args()

    labels = args.labels or os.path.join(args.frames_dir, "labels.json")
    root = tk.Tk()
    Annotator(root, args.frames_dir, labels)
    root.mainloop()


if __name__ == "__main__":
    main()
