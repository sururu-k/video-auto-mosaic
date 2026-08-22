"""検出設定を変えて Recall がどれだけ変わるかを実測する。

見るべき指標は frames_with_detection（生の検出力）と uncovered_gaps（素通しの残り）。

PowerShell 5.1 は BOM なし UTF-8 の .ps1 を ANSI として読むため、日本語を含む
スクリプトが壊れる。ドライバは Python に置いてある。
"""

import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 640x480 素材ではタイル分割は --infer-size を上げるのとほぼ等価で計算量だけ増える。
# 代わりに推論解像度そのものを振る。ONNX の入力は可変なので imgsz は自由に変えられる。
CONFIGS = [
    ("base", []),
    ("size960", ["--infer-size", "960"]),
    ("size1280", ["--infer-size", "1280"]),
    ("tta", ["--tta"]),
    ("tta_size1280", ["--tta", "--infer-size", "1280"]),
]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("video")
    p.add_argument("--out-dir", default=None)
    p.add_argument("--conf", type=float, default=0.04)
    p.add_argument("--limit-frames", type=int)
    p.add_argument("--only", help="カンマ区切りで設定名を絞る")
    args = p.parse_args()

    out_dir = args.out_dir or os.path.join(os.path.dirname(args.video), "compare")
    os.makedirs(out_dir, exist_ok=True)

    configs = CONFIGS
    if args.only:
        want = {s.strip() for s in args.only.split(",")}
        configs = [c for c in CONFIGS if c[0] in want]

    env = dict(os.environ)
    env["PATH"] = (
        os.path.join(env.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links")
        + os.pathsep
        + env.get("PATH", "")
    )

    timings = {}
    for name, extra in configs:
        rep = os.path.join(out_dir, f"report_{name}.json")
        det = os.path.join(out_dir, f"det_{name}.json")
        cmd = [
            sys.executable, "-m", "automosaic", args.video,
            "--detect-only", "--conf", str(args.conf), "--quiet",
            "--detections", det, "--report", rep,
        ] + extra
        if args.limit_frames:
            cmd += ["--limit-frames", str(args.limit_frames)]

        print(f"\n===== {name} =====", flush=True)
        t0 = time.time()
        r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        dt = time.time() - t0
        timings[name] = dt
        if r.returncode != 0:
            print(f"  失敗 (exit {r.returncode})")
            print((r.stderr or "")[-1500:])
            continue
        print(f"  {dt:.1f} 秒")

    print("\n===== まとめ =====")
    summarize(out_dir, timings)


def summarize(out_dir: str, timings: dict | None = None) -> None:
    rows = []
    for name, _ in CONFIGS:
        path = os.path.join(out_dir, f"report_{name}.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        st = d["stats"]
        gaps = d.get("uncovered_ranges", [])
        rows.append({
            "name": name,
            "det": st["frames_with_detection"],
            "mosaic": st["frames_with_mosaic"],
            "total": st["frames"],
            "raw": st["raw_detections"],
            "gaps": st["uncovered_gaps"],
            "gap_frames": sum(g["frames"] for g in gaps),
            "tracks": st["tracks_final"],
            "sec": (timings or {}).get(name),
        })

    if not rows:
        print(f"レポートが見つかりません: {out_dir}")
        return

    print(f"総フレーム {rows[0]['total']}\n")
    header = (
        f"{'設定':<11s}{'検出F':>7s}{'適用率':>8s}{'生検出':>8s}"
        f"{'未処理区間':>7s}{'未処理F':>8s}{'トラック':>7s}{'秒':>8s}"
    )
    print(header)
    for r in rows:
        sec = f"{r['sec']:.0f}" if r["sec"] else "-"
        print(
            f"{r['name']:<11s}{r['det']:>7d}"
            f"{100.0 * r['mosaic'] / r['total']:>7.1f}%"
            f"{r['raw']:>8d}{r['gaps']:>7d}{r['gap_frames']:>8d}"
            f"{r['tracks']:>7d}{sec:>8s}"
        )

    base = next((r for r in rows if r["name"] == "base"), None)
    if base:
        print("\nbase との差:")
        for r in rows:
            if r["name"] == "base":
                continue
            d_det = r["det"] - base["det"]
            print(
                f"  {r['name']:<11s} 検出フレーム {d_det:+5d} "
                f"({100.0 * d_det / max(1, base['det']):+6.1f}%)   "
                f"未処理フレーム {r['gap_frames'] - base['gap_frames']:+5d}"
            )


if __name__ == "__main__":
    main()
