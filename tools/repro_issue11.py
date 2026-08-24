"""issue #11 の再現・検証用スクリプト。

memory 外挿区間・補間区間で、往復運動（正弦運動）の対象が完全にモザイクの外に
出るフレーム数を数える。`tests/test_temporal_fixes.py` の
test_memory_envelope_does_not_miss_reversing_motion 等が同じ内容を assert
形式で回帰テスト化している。こちらは生ログで内訳（各フレームの対象外率）を
確認したいときに使う。

使い方:
    PYTHONIOENCODING=utf-8 .venv\\Scripts\\python.exe tools\\repro_issue11.py
"""

from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic.detector import Detection  # noqa: E402
from automosaic.temporal import TemporalConfig, process  # noqa: E402

CLS = "MALE_GENITALIA_EXPOSED"
W, H = 1920, 1080
S = 191.0  # 実素材の検出矩形 sqrt(面積) 中央値
A = 250.0  # 振幅 px
T = 20.0   # 周期 f
CX, CY = 960.0, 540.0


def cx(t: float) -> float:
    return CX + A * math.sin(2 * math.pi * t / T)


def box_at(t: float) -> tuple[float, float, float, float]:
    return (cx(t) - S / 2, CY - S / 2, S, S)


def overlap_area(a, b) -> float:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ax1, ay1 = ax0 + aw, ay0 + ah
    bx1, by1 = bx0 + bw, by0 + bh
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    return max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)


def run(memory: int, obs_start: int, obs_end: int, n_frames: int):
    dets: dict[int, list[Detection]] = {f: [] for f in range(n_frames)}
    for f in range(obs_start, obs_end + 1):
        dets[f] = [Detection(CLS, 0.9, box_at(float(f)))]

    cfg = TemporalConfig(
        memory=memory,
        memory_before=0,
        min_track_len=0,
        bridge_max=0,
        stitch_max_gap=0,
    )
    regions, stats = process(dets, n_frames, W, H, {CLS}, cfg)

    mem_start = obs_end + 1
    mem_end = min(n_frames, obs_end + memory)
    total = 0
    miss = 0
    partial = 0
    rows = []
    for f in range(mem_start, mem_end):
        total += 1
        gt = box_at(float(f))
        drawn = regions.get(f, [])
        covered_area = sum(overlap_area(gt, box) for box, _ in drawn)
        gt_area = S * S
        frac_outside = 1.0 - min(1.0, covered_area / gt_area)
        if covered_area <= 0.0:
            miss += 1
        elif frac_outside > 0.0:
            partial += 1
        rows.append((f, frac_outside, [r.source for _, r in drawn]))

    print(f"memory={memory}  観測 {obs_start}..{obs_end}  memory窓 {mem_start}..{mem_end - 1}")
    print(f"  完全に外 (100%外): {miss}/{total}")
    print(f"  一部だけ外れ: {partial}/{total}")
    for f, frac, sources in rows:
        print(f"    f={f:3d} 対象外率={frac:5.1%} sources={sources}")
    return miss, total


def run_interp(gap_start: int, gap_len: int, n_frames: int, context: int = 3):
    """観測 gap_start と gap_start+gap_len+1 の間を補間で埋める区間の対象外率を測る。

    gap の両端は、実際のトラックと同じく直前 `context` フレームぶん連続観測させる
    （2点だけの孤立トラックだと局所速度が「gap 全体の平均変位」に潰れてしまい、
    末端の瞬間速度を過小評価するため）。
    """
    dets: dict[int, list[Detection]] = {f: [] for f in range(n_frames)}
    for f in range(gap_start - context + 1, gap_start + 1):
        dets[f] = [Detection(CLS, 0.9, box_at(float(f)))]
    b_frame = gap_start + gap_len + 1
    for f in range(b_frame, b_frame + context):
        dets[f] = [Detection(CLS, 0.9, box_at(float(f)))]

    cfg = TemporalConfig(
        memory=0, min_track_len=0, bridge_max=0, stitch_max_gap=0, max_gap=gap_len + 1
    )
    regions, _ = process(dets, n_frames, W, H, {CLS}, cfg)

    total = 0
    miss = 0
    partial = 0
    rows = []
    for f in range(gap_start + 1, b_frame):
        total += 1
        gt = box_at(float(f))
        drawn = regions.get(f, [])
        covered_area = sum(overlap_area(gt, box) for box, _ in drawn)
        gt_area = S * S
        frac_outside = 1.0 - min(1.0, covered_area / gt_area)
        if covered_area <= 0.0:
            miss += 1
        elif frac_outside > 0.0:
            partial += 1
        rows.append((f, frac_outside))

    print(f"補間ギャップ {gap_start}..{b_frame}（{gap_len}f 分の穴）")
    print(f"  完全に外 (100%外): {miss}/{total}")
    print(f"  一部だけ外れ: {partial}/{total}")
    for f, frac in rows:
        print(f"    f={f:3d} 対象外率={frac:5.1%}")
    return miss, total


if __name__ == "__main__":
    # 半周期(0..10)観測し、t=10（ゼロ交差=最大速度点）で打ち切る。
    # 以降 memory=20 フレームを外挿で埋める。
    run(memory=20, obs_start=0, obs_end=10, n_frames=40)
    print()
    # 観測は t=8 と t=26 それぞれの手前・向こうに3フレームずつだけで、
    # 間の t=15 前後にトラフ（往復の折り返し）が丸ごと入る。直線補間はほぼ
    # 横ばいの線を引くので、折り返し点を完全に外す。
    run_interp(gap_start=8, gap_len=17, n_frames=40)
