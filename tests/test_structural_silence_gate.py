"""tools/structural_silence_gate.py の回帰テスト（issue #13）。

ffmpeg を要求する部分（動画デコード）はここでは検証しない。`cell_analysis` 自体
（構造判定のコア）は `tests/test_pixel_coverage.py` が既にカバーしている。
ここで見るのは、そのコアの上に積んだ新しいロジック
（連続区間の検出・閾値未満の穴の無視・レポート・終了コード判定）。
"""

import io
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

from structural_silence_gate import (  # noqa: E402
    build_report,
    decide_exit_code,
    find_silent_runs,
    painted_flags,
    scan_video_pair,
    NOT_MEASURED,
)
from automosaic.render import pixelize_plane  # noqa: E402


def test_painted_flags_threshold_is_inclusive():
    """min_cells ちょうどで「塗装あり」になること（未満は無し）。"""
    counts = [0, 1, 2, 3, 5]
    flags = painted_flags(counts, min_cells=2)
    assert flags == [False, False, True, True, True], flags
    print("  painted_flags の閾値(>=) OK")


def test_find_silent_runs_ignores_short_gaps():
    """min_run_frames 未満の穴は「見つからない」。閾値ちょうどは見つかる。"""
    # False(無塗装)が index 2..4 の3連続。min_run=3なら見つかる。
    painted = [True, True, False, False, False, True, True]
    assert find_silent_runs(painted, min_run_frames=4) == []
    runs = find_silent_runs(painted, min_run_frames=3)
    assert len(runs) == 1, runs
    assert runs[0] == {"start_frame": 2, "end_frame": 4, "length": 3}, runs[0]
    print("  短い穴は無視され、閾値ちょうどは検出される OK")


def test_find_silent_runs_boundary_at_start_and_end():
    """配列の先頭・末尾にかかる無塗装区間も正しく拾えること（off-by-one の典型的な穴）。"""
    painted = [False, False, False, True, True, False, False, False]
    runs = find_silent_runs(painted, min_run_frames=3)
    assert len(runs) == 2, runs
    assert runs[0] == {"start_frame": 0, "end_frame": 2, "length": 3}, runs[0]
    assert runs[1] == {"start_frame": 5, "end_frame": 7, "length": 3}, runs[1]
    print("  先頭・末尾にかかる区間も正しく検出される OK")


def test_find_silent_runs_frame_offset():
    """フレーム番号のオフセットが正しく反映されること（区間だけを切り出して走査する用途）。"""
    painted = [False, False, False]
    runs = find_silent_runs(painted, min_run_frames=3, frame_offset=1000)
    assert runs == [{"start_frame": 1000, "end_frame": 1002, "length": 3}], runs
    print("  frame_offset の反映 OK")


def test_find_silent_runs_length_is_exact():
    """独立検証で狙う変異1: length を (end - start) ではなく (end - start + 1 + 1) 等に
    ずらすと、長さがぴったりの区間の判定が閾値の境界でずれる。ここではちょうど
    閾値と同じ長さの区間が2通り（境界の内側/外側）を作って区別する。"""
    exact = [False] * 5  # ちょうど5
    assert len(find_silent_runs(exact, min_run_frames=5)) == 1
    assert len(find_silent_runs(exact, min_run_frames=6)) == 0
    print("  区間長がちょうど閾値のときの境界判定 OK")


def test_scan_video_pair_raises_on_frame_index_mismatch(monkeypatch):
    """元動画と出力動画でフレーム番号がずれたら、黙って進まず例外を出すこと。

    `RULES.md` 0「黙って素通しを作らない」の実装箇所。ここがもし黙って
    zip し続けると、ずれたフレーム同士を比較して構造判定そのものが無意味になる。
    """
    import structural_silence_gate as ssg

    def fake_iter_src(path, limit_frames=None):
        yield 0, np.zeros((4, 4), dtype=np.uint8)
        yield 1, np.zeros((4, 4), dtype=np.uint8)

    def fake_iter_out(path, limit_frames=None):
        yield 0, np.zeros((4, 4), dtype=np.uint8)
        yield 5, np.zeros((4, 4), dtype=np.uint8)  # わざとずらす

    calls = {"n": 0}

    def fake_iter_gray_frames(path, limit_frames=None):
        calls["n"] += 1
        return fake_iter_src(path, limit_frames) if calls["n"] == 1 else fake_iter_out(path, limit_frames)

    monkeypatch.setattr(ssg, "iter_gray_frames", fake_iter_gray_frames)

    raised = False
    try:
        list(ssg.scan_video_pair("s.mp4", "o.mp4", cell=2, std_min=6.0, ratio_max=0.35))
    except RuntimeError as e:
        raised = True
        assert "ずれています" in str(e)
    assert raised, "フレーム番号のずれを検出できていない"
    print("  フレーム番号ずれで例外 OK")


def test_scan_video_pair_matches_manual_pixelize():
    """実際に render.pixelize_plane で塗った合成フレーム列を、scan_video_pair 相当の
    ロジック(cell_analysis経由)に通したときの潰れセル数が期待通りであること。
    ffmpeg を使わず in-memory の frame 列で確認する。"""
    from structural_silence_gate import cell_analysis  # noqa: E402  (再エクスポート経由)

    rng = np.random.default_rng(7)
    source = rng.integers(0, 256, size=(40, 40), dtype=np.uint8)
    painted_frame = source.copy()
    pixelize_plane(painted_frame, (0.0, 0.0, 40.0, 40.0), 10)  # 全面塗装
    unpainted_frame = source.copy()  # 塗っていない

    _, coll_painted = cell_analysis(source, painted_frame, cell=10, std_min=6.0, ratio_max=0.35)
    _, coll_unpainted = cell_analysis(source, unpainted_frame, cell=10, std_min=6.0, ratio_max=0.35)

    assert int(coll_painted.sum()) >= 2, "塗った合成フレームが潰れたセル2個未満と判定された"
    assert int(coll_unpainted.sum()) == 0, "塗っていない合成フレームが潰れたと誤判定された"
    print(f"  塗装={int(coll_painted.sum())}セル / 無塗装={int(coll_unpainted.sum())}セル OK")


def test_build_report_always_includes_not_measured():
    """レポートJSONから `not_measured` が省略されないこと（0件のときも）。

    これが抜けると「0件=漏れなし」に読める出力になってしまう。
    """
    report_empty = build_report(
        "s.mp4", "o.mp4", cell=20, std_min=6.0, ratio_max=0.35,
        min_cells=2, min_run_frames=8, n_scanned=100, runs=[], collapsed_counts=[0] * 100,
    )
    assert report_empty["runs"] == []
    assert report_empty["not_measured"] == NOT_MEASURED
    assert len(report_empty["not_measured"]) > 0
    print("  runs=0 でも not_measured が出る OK")


def test_build_report_n_silent_frames_sums_run_lengths():
    runs = [{"start_frame": 10, "end_frame": 19, "length": 10}, {"start_frame": 100, "end_frame": 104, "length": 5}]
    report = build_report(
        "s.mp4", "o.mp4", cell=20, std_min=6.0, ratio_max=0.35,
        min_cells=2, min_run_frames=8, n_scanned=1000, runs=runs, collapsed_counts=[0] * 1000,
    )
    assert report["n_silent_frames"] == 15, report["n_silent_frames"]
    print("  n_silent_frames の合計 OK")


def test_decide_exit_code_fails_closed_by_default():
    """`RULES.md` 0: 判断がつかないときは止める。既定では沈黙が見つかれば非0。"""
    runs = [{"start_frame": 0, "end_frame": 9, "length": 10}]
    assert decide_exit_code(runs, no_fail_on_silence=False) == 1
    assert decide_exit_code(runs, no_fail_on_silence=True) == 0
    assert decide_exit_code([], no_fail_on_silence=False) == 0
    print("  decide_exit_code: 既定で失敗方向に倒れる OK")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"{len(tests)} 件のテストを実行\n")
    failed = 0
    for t in tests:
        try:
            # monkeypatch を使うテストだけ簡易フィクスチャを渡す
            if "monkeypatch" in t.__code__.co_varnames[: t.__code__.co_argcount]:
                class _MonkeyPatch:
                    def __init__(self):
                        self._orig = []

                    def setattr(self, obj, name, value):
                        self._orig.append((obj, name, getattr(obj, name)))
                        setattr(obj, name, value)

                    def undo(self):
                        for obj, name, val in self._orig:
                            setattr(obj, name, val)

                mp = _MonkeyPatch()
                try:
                    t(mp)
                finally:
                    mp.undo()
            else:
                t()
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{'すべて通過' if failed == 0 else f'{failed} 件失敗'}")
    sys.exit(1 if failed else 0)
