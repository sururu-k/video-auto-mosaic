"""実素材 (data/library/20260823-234604-9be9) での完全一致テスト。

切れ目を指定しないときに、修正前後で完全に同じ結果が得られることを確認する。
全55,303フレームの矩形を突き合わせ。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic.detector import Detection  # noqa: E402
from automosaic.temporal import TemporalConfig, process  # noqa: E402


# 元のリポジトリディレクトリを探す（worktree では data/ が見えないため）
_test_dir = os.path.dirname(__file__)
_base_dir = _test_dir
for _ in range(10):  # 最大10階層上へ探索
    _git_path = os.path.join(_base_dir, ".git")
    if os.path.exists(_git_path):
        # .git がファイル（worktree）か ディレクトリ か確認
        if os.path.isfile(_git_path):
            # worktree: gitdir: /path/to/repo/.git/worktrees/xxx を読む
            with open(_git_path, encoding="utf-8") as f:
                line = f.read().strip()
                if line.startswith("gitdir:"):
                    git_dir = line.split(":", 1)[1].strip()
                    # worktrees の2階層上（/repo/.git/worktrees/xxx -> /repo）
                    _base_dir = os.path.dirname(os.path.dirname(os.path.dirname(git_dir)))
                    break
        else:
            # 通常のリポジトリ
            break
    _base_dir = os.path.dirname(_base_dir)

DATA_DIR = os.path.join(_base_dir, "data", "library", "20260823-234604-9be9")


def test_empty_cut_frames_matches_original_real_material():
    """実素材で、空の cut_frames が元と完全一致すること。

    全55,303フレーム、1920x1080、22887検出。
    """
    if not os.path.exists(DATA_DIR):
        print(f"  実素材が見つかりません ({DATA_DIR})。スキップ")
        return

    # det.json を読む
    det_path = os.path.join(DATA_DIR, "det.json")
    with open(det_path, encoding="utf-8") as f:
        det_data = json.load(f)

    # 検出を dict[frame, list[Detection]] に変換
    per_frame_dets: dict[int, list[Detection]] = {}
    for frame_str, dets_list in det_data["detections"].items():
        frame = int(frame_str)
        per_frame_dets[frame] = [
            Detection(d["class"], d["score"], tuple(d["box"]))
            for d in dets_list
        ]

    # meta.json から情報を取得
    meta_path = os.path.join(DATA_DIR, "meta.json")
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)

    n_frames = meta["n_frames"]
    width = meta["width"]
    height = meta["height"]
    classes = {"FEMALE_GENITALIA_EXPOSED"}  # デフォルトクラス

    # 修正前（デフォルト）で実行
    cfg_original = TemporalConfig()
    regions_orig, stats_orig = process(per_frame_dets, n_frames, width, height, classes, cfg_original)

    # 修正後（空集合）で実行
    cfg_with_empty_cut = TemporalConfig(cut_frames=set())
    regions_empty, stats_empty = process(per_frame_dets, n_frames, width, height, classes, cfg_with_empty_cut)

    # 全フレームの矩形を比較
    mismatches = 0
    for f in range(n_frames):
        orig_boxes = sorted([b for b, _ in regions_orig.get(f, [])])
        empty_boxes = sorted([b for b, _ in regions_empty.get(f, [])])
        if orig_boxes != empty_boxes:
            mismatches += 1
            if mismatches <= 5:  # 最初の5件は詳細を表示
                print(f"  ミスマッチ フレーム {f}: original={len(orig_boxes)}, empty={len(empty_boxes)}")

    assert mismatches == 0, (
        f"全 {n_frames} フレーム中 {mismatches} フレームで矩形が異なる"
    )

    # 統計も確認
    for key in ["frames_with_mosaic", "regions_interpolated", "regions_from_memory", "frames_bridged"]:
        assert stats_orig[key] == stats_empty[key], (
            f"統計 {key} が異なる (original={stats_orig[key]}, empty={stats_empty[key]})"
        )

    print(f"  実素材での完全一致確認 OK （全 {n_frames} フレーム）")
    print(f"    frames_with_mosaic={stats_orig['frames_with_mosaic']}")
    print(f"    regions_interpolated={stats_orig['regions_interpolated']}")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"{len(tests)} 件のテストを実行\n")
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{'すべて通過' if failed == 0 else f'{failed} 件失敗'}")
    sys.exit(1 if failed else 0)
