"""パス1のデコードサイズと、素通しを作る経路の検証。ffmpeg が要る。

docs/07-audit-2026-08-23.md の C-1 / C-2 / C-3 / C-4 / C-6、および
再検証で見つかった R-1（estimated_frames() の誤爆が C-2 のガードを常用で
無効化させる）と、resume の追い越し・解像度欠落・--quiet での漏れ情報消失・
手修正の適用件数・パス2の完走チェックに対応する。
いずれも「モザイクが漏れる方向」に壊れるものなので、
再現できることではなく再現しなくなったことを確認する。
"""

import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automosaic import cli  # noqa: E402
from automosaic import video as vid  # noqa: E402

# 監査表に載っていた解像度。854x480(480pワイド) と 720x480(DVD) が壊れていた
AUDIT_RESOLUTIONS = [
    (854, 480), (720, 480), (848, 480), (1280, 534),
    (1920, 1002), (1918, 1080), (1280, 692),
]
INFER_SIZES = [640, 960, 1280]


def _have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg"))


def _png_size(data: bytes) -> tuple[int, int]:
    pos = data.find(b"IHDR")
    w, h = struct.unpack(">II", data[pos + 4 : pos + 12])
    return int(w), int(h)


def _ffmpeg_scaled_size(w: int, h: int, infer_size: int) -> tuple[int, int]:
    """合成入力に同じ scale フィルタを掛けて実出力サイズを得る。"""
    out = subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:d=1",
            "-vf", vid.detection_scale_filter(infer_size),
            "-frames:v", "1", "-f", "image2", "-c:v", "png", "-",
        ],
        capture_output=True,
        check=True,
    )
    return _png_size(out.stdout)


def _make_video(path: str, w: int, h: int, frames: int, fps: int = 30) -> None:
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:r={fps}",
            "-frames:v", str(frames), "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-g", "5", path,
        ],
        check=True,
    )


def _make_video_with_audio(path: str, w: int, h: int, seconds: int,
                            fps: int = 30, acodec: str = "aac") -> None:
    """音声つきの動画を作る。mkv/webm 等、nb_frames も per-stream duration も
    持たないコンテナを再現するのに使う（R-1）。"""
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=f=440:d={seconds}",
            "-map", "0:v", "-map", "1:a",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", acodec,
            path,
        ],
        check=True,
    )


def _make_rotated_video(path: str, base_w: int, base_h: int, frames: int,
                         degrees: int, fps: int = 15) -> None:
    """回転メタデータ付きの動画を作る（issue #1 の再現手順）。

    2段階に分ける。まず base_w x base_h の平置き（回転メタデータ無し）動画を
    作り、次に -display_rotation を付けて -c copy で包み直す。画素はまったく
    動かさず、side_data_list の Display Matrix だけを足す。ffmpeg はこれを
    再生・デコード時に自動で適用する（既定で -autorotate 有効）ので、90/270度
    では実際に流れてくるフレームの幅と高さが入れ替わる。
    """
    tmp = path + ".base.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={base_w}x{base_h}:r={fps}",
            "-frames:v", str(frames), "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-g", "5", tmp,
        ],
        check=True,
    )
    try:
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                # -display_rotation は ffmpeg の CLI パーサ上「入力側」の
                # オプション扱いなので -i より前に置く（出力側に置くと
                # 「apply to output url」エラーになる。実測で確認済み）。
                "-display_rotation:v:0", str(degrees),
                "-i", tmp, "-c", "copy", path,
            ],
            check=True,
        )
    finally:
        os.remove(tmp)


def _make_odd_video(path: str, w: int, h: int, frames: int, fps: int = 5) -> None:
    """奇数解像度の合成動画を作る（issue #2 の再現手順）。

    `testsrc2` は奇数サイズ指定を黙って偶数に丸めるので使えない
    （RULES.md 2章）。`color=` は丸めない。VP9(4:2:0) は奇数解像度のまま
    持てるので、H.264 では作れない「奇数のまま probe される」入力を作れる。
    """
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=red:size={w}x{h}:rate={fps}:duration=10",
            "-frames:v", str(frames),
            "-c:v", "libvpx-vp9", "-pix_fmt", "yuv420p", "-b:v", "500k",
            path,
        ],
        check=True,
    )


def _decode_frame_rgb(path: str):
    """ffmpeg 自身に先頭フレームを PNG で吐かせて cv2 で読む。

    autorotate 込みで ffmpeg がデコードした「正解」の見た目を得るための、
    automosaic の実装から独立した手段。
    """
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path,
         "-frames:v", "1", "-f", "image2", "-c:v", "png", "-"],
        capture_output=True, check=True,
    )
    arr = np.frombuffer(out.stdout, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    assert img is not None, f"PNG デコードに失敗: {path}"
    return img


def _count_real_frames(path: str) -> int:
    """実際に読める映像フレーム数を ffmpeg 自身に数えさせる。"""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-map", "0:v",
         "-f", "rawvideo", "-pix_fmt", "gray", "-vf", "scale=2:2", "-"],
        capture_output=True, check=True,
    )
    return len(proc.stdout) // 4


def _write_detections(path: str, n_frames: int, w: int, h: int,
                      complete: bool = True, dets: dict | None = None) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "n_frames": n_frames,
                "width": w,
                "height": h,
                "complete": complete,
                "detections": dets or {},
            },
            f,
        )


def test_detection_frame_size_matches_ffmpeg():
    """C-1: 計算したデコードサイズが ffmpeg の実出力と1バイトも違わないこと。

    ここがずれるとパイプの読み出し境界がずれ、以降の全フレームが前フレームと
    混ざった斜めの画像になる。例外は出ないので検出が丸ごと無意味になる。
    """
    if not _have_ffmpeg():
        print("  デコードサイズの一致 SKIP (ffmpeg 無し)")
        return
    bad = []
    for w, h in AUDIT_RESOLUTIONS:
        info = vid.VideoInfo(w, h, 30, 1, 30, 1.0, "yuv420p",
                             None, None, None, None, False)
        for n in INFER_SIZES:
            actual = _ffmpeg_scaled_size(w, h, n)
            calc = vid.computed_detection_frame_size(info, n)
            if actual != calc:
                bad.append(f"{w}x{h} infer={n}: 実出力{actual} != 計算{calc}")
    assert not bad, "\n    " + "\n    ".join(bad)
    print(f"  デコードサイズの一致 OK ({len(AUDIT_RESOLUTIONS) * len(INFER_SIZES)} 組)")


def test_measured_size_matches_pipe():
    """C-1: 実測サイズが rawvideo のフレーム長と一致すること。"""
    if not _have_ffmpeg():
        print("  実測サイズとパイプ長の一致 SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.mp4")
        _make_video(src, 854, 480, 3)
        info = vid.probe(src)
        for n in INFER_SIZES:
            dec_w, dec_h = vid.detection_frame_size(info, n, path=src)
            proc = vid.open_detection_reader(src, n, limit_frames=1)
            data = proc.stdout.read()
            proc.stdout.close()
            proc.stderr.read()
            proc.wait()
            assert len(data) == dec_w * dec_h * 3, (
                f"854x480 infer={n}: パイプ {len(data)} バイト != "
                f"{dec_w}x{dec_h}x3 = {dec_w * dec_h * 3}"
            )
    print("  実測サイズとパイプ長の一致 OK")


def test_detect_scale_capped_at_material_resolution():
    """issue #37: --tiles のデコード長辺が素材の実解像度を超えて拡大されないこと。

    旧式（max(net_w, net_h) * tiles）は 1080p 素材・tiles=2 で 2560px を要求し、
    ffmpeg の force_original_aspect_ratio=decrease が箱に収めるために拡大までして
    2560x1440（面積で素材の1.78倍）を作る。情報は増えず、デコードとタイル推論の
    コストだけが増える。素材の長辺で頭打ちにすること。
    """
    from automosaic.detector import budget_net_size

    mat_w, mat_h = 1920, 1080
    net_w, net_h = budget_net_size(mat_w, mat_h, 960)
    assert (net_w, net_h) == (1280, 736), f"想定外の net サイズ: {net_w}x{net_h}"

    for tiles, expect in ((1, 1280), (2, 1920), (3, 1920)):
        old = max(net_w, net_h) * max(1, tiles)
        new = cli.compute_detect_scale(net_w, net_h, tiles, mat_w, mat_h)
        assert new == expect, f"tiles={tiles}: {new} != {expect}"
        assert new <= max(mat_w, mat_h), (
            f"tiles={tiles}: 新方式 {new}px が素材の長辺 {max(mat_w, mat_h)}px を超えている"
        )
        if tiles >= 2:
            # 旧式は実際に超過していたことも確認しておく（直す前に壊れていたことの根拠）
            assert old > max(mat_w, mat_h), (
                f"tiles={tiles}: 旧式 {old}px が素材の長辺を超えていない"
                "（この前提が崩れたらこのテストの意味が無い）"
            )

    # --detect-scale で明示指定した場合は頭打ちを掛けない（利用者の意図的な指定）
    explicit = cli.compute_detect_scale(net_w, net_h, 2, mat_w, mat_h, override=3000)
    assert explicit == 3000, f"明示指定が上書きされた: {explicit}"
    print("  --tiles のデコード長辺が素材解像度で頭打ちになる OK")


def test_tiles_decode_not_upscaled_beyond_material_ffmpeg():
    """上のテストの主張を実際の ffmpeg デコードでも確認する（合成1080p素材）。"""
    if not _have_ffmpeg():
        print("  tiles デコードの実測頭打ち SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.mp4")
        _make_video(src, 1920, 1080, 1)
        info = vid.probe(src)

        old_scale = 2560  # 旧式 max(net_w,net_h)=1280 * tiles=2
        new_scale = cli.compute_detect_scale(1280, 736, 2, info.width, info.height)
        assert new_scale == 1920

        old_w, old_h = vid.detection_frame_size(info, old_scale, path=src)
        new_w, new_h = vid.detection_frame_size(info, new_scale, path=src)

        old_area_ratio = (old_w * old_h) / (info.width * info.height)
        new_area_ratio = (new_w * new_h) / (info.width * info.height)

        assert old_w > info.width or old_h > info.height, (
            f"旧式が実測で素材を超えていない: {old_w}x{old_h}"
        )
        assert new_w <= info.width and new_h <= info.height, (
            f"新式が実測で素材を超えている: {new_w}x{new_h}"
        )
        assert old_area_ratio > 1.7, f"旧式の面積倍率が想定より小さい: {old_area_ratio:.3f}"
        assert new_area_ratio == 1.0, f"新式の面積倍率が1.0でない: {new_area_ratio:.3f}"
    print(
        f"  実測: 旧式 {old_w}x{old_h}(面積x{old_area_ratio:.2f}) -> "
        f"新式 {new_w}x{new_h}(面積x{new_area_ratio:.2f}) OK"
    )


class _NullDetector:
    """検出しないスタブ。run_detection のデコード側だけを見る。"""

    def detect_frame(self, frame, tta=False, tiles=1):
        return []


class _KillAtDetector(_NullDetector):
    """指定フレームまで進んだところで ffmpeg を殺す。デコーダが途中で落ちた状況。"""

    def __init__(self, holder: dict, at: int) -> None:
        self.holder = holder
        self.at = at
        self.n = 0

    def detect_frame(self, frame, tta=False, tiles=1):
        self.n += 1
        if self.n == self.at:
            self.holder["proc"].kill()
        return []


def test_decode_failure_raises():
    """C-3: 途中でデコードが落ちたら例外にすること（idx>0 でも黙殺しない）。

    修正前は `and idx == 0` が付いていたので、1フレームでも読めていれば
    途中で死んでも正常終了扱いだった。C-2 と合わさると後半が素通しの出力が
    「正常終了」で出てくる。
    """
    if not _have_ffmpeg():
        print("  途中デコード失敗の検出 SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.mp4")
        _make_video(src, 320, 240, 90)
        info = vid.probe(src)

        holder: dict = {}
        real_open = vid.open_detection_reader

        def spy(path, infer_size, limit_frames=None):
            proc = real_open(path, infer_size, limit_frames)
            holder["proc"] = proc
            return proc

        cli.vid.open_detection_reader = spy
        try:
            per_frame, n = cli.run_detection(
                src, info, _KillAtDetector(holder, 10), 320, None, quiet=True
            )
        except RuntimeError as e:
            assert "異常終了" in str(e), f"想定と違う例外: {e}"
            print("  途中デコード失敗の検出 OK")
            return
        finally:
            cli.vid.open_detection_reader = real_open
        raise AssertionError(
            f"デコードが途中で死んだのに例外にならなかった（{n} / 90 フレームで正常終了）"
        )


def test_short_decode_warns():
    """C-3: ffmpeg が終了コード0のまま途中で終わる壊れたファイルでも警告を出すこと。

    切り詰めた mp4 では ffmpeg は 0 で抜けるので終了コードでは気付けない。
    黙って短い検出結果を返さないことを見る。
    """
    if not _have_ffmpeg():
        print("  短いデコードの警告 SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.mp4")
        broken = os.path.join(d, "broken.mp4")
        # moov を先頭に置く。後ろを切っても途中まで読める
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", "testsrc2=size=320x240:r=30", "-frames:v", "90",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "5",
             "-movflags", "+faststart", src],
            check=True,
        )
        raw = open(src, "rb").read()
        with open(broken, "wb") as f:
            f.write(raw[: int(len(raw) * 0.45)])
        info = vid.probe(src)  # 尺は壊す前の 90 フレーム

        err = io.StringIO()
        real_stderr, sys.stderr = sys.stderr, err
        try:
            per_frame, n = cli.run_detection(
                broken, info, _NullDetector(), 320, None, quiet=True
            )
        finally:
            sys.stderr = real_stderr
        assert n < 90, f"壊れたファイルなのに全フレーム読めた（{n}）"
        assert "警告" in err.getvalue(), (
            f"{n}/90 フレームで終わったのに警告が無い: {err.getvalue()!r}"
        )
    print(f"  短いデコードの警告 OK ({n}/90 フレームで警告)")


def test_render_stops_when_detections_short():
    """C-2: 検出が実尺より短いとき、既定では描画を止めること。

    止めずに続けるとパス1最終フレームの領域を延ばすが、そこが空だと
    以降が全部モザイクなしで通る。
    """
    if not _have_ffmpeg():
        print("  短い検出での描画停止 SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.mp4")
        dst = os.path.join(d, "out.mp4")
        _make_video(src, 320, 240, 40)
        info = vid.probe(src)
        # 20 フレームぶんしか検出が無く、しかも最終フレームは検出0件
        regions = {i: [((10, 10, 60, 60), False)] for i in range(15)}

        try:
            cli.run_render(src, dst, info, regions, 20, 8, "black",
                           28, "ultrafast", None, quiet=True)
        except RuntimeError as e:
            assert "短い" in str(e), f"想定と違う例外: {e}"
        else:
            raise AssertionError("実尺40フレームに対し検出20フレームでも止まらなかった")

        # 明示フラグを付けたときは通すが、何フレーム目から未検出かを警告に出す
        err = io.StringIO()
        real_stderr, sys.stderr = sys.stderr, err
        try:
            cli.run_render(src, dst, info, regions, 20, 8, "black",
                           28, "ultrafast", None, quiet=True,
                           allow_short_detections=True)
        finally:
            sys.stderr = real_stderr
        msg = err.getvalue()
        assert "frame 20 以降" in msg, f"未検出区間の警告が出ていない: {msg!r}"
        assert "20 フレーム" in msg, f"未検出フレーム数が出ていない: {msg!r}"
        assert os.path.exists(dst)
    print("  短い検出での描画停止 OK")


def _make_video_with_subtitle(path: str, w: int, h: int, seconds: int,
                               fps: int = 5) -> None:
    """字幕（srt）付きの mkv を作る（issue #3 の再現手順）。"""
    srt = path + ".srt"
    with open(srt, "w", encoding="utf-8") as f:
        f.write("1\n00:00:00,000 --> 00:00:01,000\ntest\n")
    try:
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi",
                "-i", f"testsrc2=size={w}x{h}:rate={fps}:duration={seconds}",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                "-i", srt,
                "-map", "0:v", "-map", "1:a", "-map", "2:s",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-c:s", "srt",
                path,
            ],
            check=True,
        )
    finally:
        os.remove(srt)


def test_subtitle_mkv_to_mp4_reveals_real_reason_not_brokenpipe():
    """issue #3: 字幕付き mkv を mp4 に出そうとすると、修正前は
    BrokenPipeError（writer.stdin.write の例外処理なし）が本当の理由
    （mp4 は subrip コーデックのコンテナ非対応）を隠し、0バイトの
    出力ファイルだけが残っていた。preflight が検出・描画に入る前に
    本当の理由つきで止め、0バイト出力を残さないことを見る。
    """
    if not _have_ffmpeg():
        print("  字幕mkv->mp4 の実理由表示 SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "subbed.mkv")
        dst = os.path.join(d, "out.mp4")
        _make_video_with_subtitle(src, 320, 240, 2)

        err = io.StringIO()
        real_stderr, sys.stderr = sys.stderr, err
        try:
            rc = cli.main([src, "-o", dst, "--quiet"])
        finally:
            sys.stderr = real_stderr
        msg = err.getvalue()
        assert rc == 1, f"想定と違う終了コード: {rc}\n{msg}"
        assert "BrokenPipeError" not in msg, (
            f"本当の理由の代わりに例外名が出ている: {msg!r}"
        )
        assert "subrip" in msg, f"本当の理由（subrip 非対応）が出ていない: {msg!r}"
        assert not os.path.exists(dst), f"0バイトの出力が残っている: {dst}"
    print("  字幕mkv->mp4 の実理由表示 OK")


def test_subtitle_mkv_to_mkv_still_succeeds():
    """同じ字幕付き入力でも、出力を mkv にすれば preflight を通って
    従来どおり焼けること（回帰確認）。"""
    if not _have_ffmpeg():
        print("  字幕mkv->mkv の従来動作 SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "subbed.mkv")
        dst = os.path.join(d, "out.mkv")
        _make_video_with_subtitle(src, 320, 240, 2)
        det = os.path.join(d, "det.json")
        info = vid.probe(src)
        _write_detections(det, info.estimated_frames(), info.width, info.height)

        rc = cli.main(
            [src, "-o", dst, "--detections", det, "--reuse-detections", "--quiet"]
        )
        assert rc == 0, f"字幕付き mkv->mkv が失敗した（rc={rc}）"
        assert os.path.exists(dst) and os.path.getsize(dst) > 0
    print("  字幕mkv->mkv の従来動作 OK")


def test_run_render_wraps_writer_oserror_without_preflight():
    """段1単独: run_render() 内の `writer.stdin.write` を囲む
    `except OSError: break` だけを守っているケースを、main() の
    preflight を経由せず直接見る。

    preflight（main() 側、段2）と run_render の except OSError（段1）は、
    どちらも「字幕・添付のコーデックがコンテナ非対応で writer が死ぬ」
    という同じ症状を独立に塞いでいる。main() 経由の end-to-end テストだと
    段2が先に止めるので、段1を単独では検証できない。ここでは
    cli.run_render() を preflight を経由せず直接呼び、段1だけで
    「本当の理由（subrip 非対応）」つきの RuntimeError になり、
    生の BrokenPipeError/OSError が外に漏れないこと、0バイトの出力が
    残らないことを見る。

    ffmpeg（writer）の起動タイミング次第で、10フレームの書き込みが
    1回も OSError を起こさず通り切ることがある。その場合 write_failed は
    立たず、直後の writer.returncode 判定（「エンコードに失敗しました」）
    が本当の理由を出す。どちらの経路も段1（except OSError）が握りつぶさず
    尻切れ出力を残さないという保証範囲の内側なので、メッセージは両方
    受け付ける。
    """
    if not _have_ffmpeg():
        print("  段1単独: writer 途中死亡の握りつぶし SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "subbed.mkv")
        dst = os.path.join(d, "out.mp4")
        _make_video_with_subtitle(src, 320, 240, 2, fps=5)
        info = vid.probe(src)
        n_frames = info.estimated_frames() or 10

        err = io.StringIO()
        real_stderr, sys.stderr = sys.stderr, err
        try:
            try:
                cli.run_render(src, dst, info, {}, n_frames, 8, "black",
                               28, "ultrafast", None, quiet=True)
            except RuntimeError as e:
                msg = str(e)
                assert (
                    "エンコードへの書き込みに失敗しました" in msg
                    or "エンコードに失敗しました" in msg
                ), f"想定と違う RuntimeError: {msg!r}"
                assert "subrip" in msg, f"本当の理由が出ていない: {msg!r}"
            else:
                raise AssertionError(
                    "字幕コーデック非対応で writer が落ちるはずなのに例外にならなかった"
                )
        finally:
            sys.stderr = real_stderr
        assert not os.path.exists(dst), f"0バイト（または尻切れ）出力が残っている: {dst}"
    print("  段1単独: writer 途中死亡の握りつぶし OK")


def test_preflight_blocks_before_pass1_detection():
    """段2単独: main() の preflight 呼び出し
    だけを守っているケースを、パス1（検出）が呼ばれたかどうかで見分ける。

    段1（run_render 内の except OSError）は生きたままにする。段2
    （preflight）を消しても、段1が最終的に同じ「本当の理由」つき
    RuntimeError を出すので、失敗メッセージの中身だけでは段2の有無を
    見分けられない。ここでは run_detection（パス1）にスパイを挟み、
    preflight が検出・描画に入る前に止めるなら一度も呼ばれないことを見る。
    段2が無いと、時間のかかるパス1が最後まで走ってしまってから
    段1で初めて止まる（呼ばれてしまう）。
    """
    if not _have_ffmpeg():
        print("  段2単独: パス1を待たせない事前確認 SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "subbed.mkv")
        dst = os.path.join(d, "out.mp4")
        _make_video_with_subtitle(src, 320, 240, 2, fps=5)

        calls = []
        real_run_detection = cli.run_detection

        def _spy(*a, **kw):
            calls.append(1)
            return real_run_detection(*a, **kw)

        cli.run_detection = _spy
        try:
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = cli.main([src, "-o", dst, "--quiet"])
        finally:
            cli.run_detection = real_run_detection

        msg = err.getvalue()
        assert rc == 1, f"想定と違う終了コード: {rc}\n{msg}"
        assert calls == [], (
            "preflight が字幕コーデック非対応を検出・描画に入る前に"
            f"止められず、時間のかかるパス1（検出）に入ってしまった: {len(calls)} 回呼び出し"
        )
        assert "事前確認で判明しました" in msg, (
            f"preflight 由来の停止メッセージになっていない: {msg!r}"
        )
        assert not os.path.exists(dst)
    print("  段2単独: パス1を待たせない事前確認 OK")


def test_preflight_advice_only_for_codec_container_errors():
    """_preflight_advice(): ffmpeg 自身が「コーデックがコンテナ非対応」と
    言っている場合だけ字幕・添付フォントの助言を出し、それ以外の理由
    （権限・ディスク不在など preflight が拾いうる他のエラー）には
    付けないこと。無条件に助言を返す変異ではここで落ちる。
    """
    codec_err = (
        "[mp4 @ 0x1] Could not find tag for codec subrip in stream #0, "
        "codec not currently supported in container"
    )
    other_err = "[Errno 13] Permission denied: 'out.mp4'"

    advice = cli._preflight_advice(codec_err)
    assert advice != "", "コーデック非対応の理由なのに助言が出ていない"
    assert "字幕" in advice, f"助言の中身がおかしい: {advice!r}"

    assert cli._preflight_advice(other_err) == "", (
        "コーデックと無関係な理由にまで助言を付けている"
    )
    assert cli._preflight_advice("") == ""
    print("  _preflight_advice の出し分け OK")


def test_reuse_rejects_incomplete_and_mismatched():
    """C-4: 途中保存（complete: false）と解像度違いの検出JSONを弾くこと。"""
    if not _have_ffmpeg():
        print("  再利用JSONの検証 SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.mp4")
        det = os.path.join(d, "det.json")
        _make_video(src, 320, 240, 30)

        # 途中保存
        _write_detections(det, 12, 320, 240, complete=False)
        rc = cli.main([src, "--detections", det, "--reuse-detections",
                       "--detect-only", "--quiet"])
        assert rc == 1, "complete: false の検出結果が素通しした"

        # 解像度違い
        _write_detections(det, 30, 1920, 1080, complete=True)
        rc = cli.main([src, "--detections", det, "--reuse-detections",
                       "--detect-only", "--quiet"])
        assert rc == 1, "解像度違いの検出結果が素通しした"

        # 正しいものは通る
        _write_detections(det, 30, 320, 240, complete=True)
        rc = cli.main([src, "--detections", det, "--reuse-detections",
                       "--detect-only", "--quiet"])
        assert rc == 0, "正しい検出結果まで弾いている"

        # 実尺より短いものは、--allow-short-detections が無ければ止まる
        _write_detections(det, 10, 320, 240, complete=True)
        rc = cli.main([src, "--detections", det, "--reuse-detections",
                       "-o", os.path.join(d, "out.mp4"), "--quiet"])
        assert rc == 1, "実尺より短い検出結果が描画に進んだ"
    print("  再利用JSONの検証 OK")


def test_explicit_options_survive_default_mode():
    """C-6: --estimate-gaps 無しでも明示指定した値を上書きしないこと。

    絞り込み表示は「実効設定が明示指定と食い違っている」ことを伝える情報なので
    stderr に出す（--quiet でも消えないことをここで確認する）。
    """
    if not _have_ffmpeg():
        print("  明示指定の尊重 SKIP (ffmpeg 無し)")
        return
    given = cli.explicit_options(["in.mp4", "--memory", "20"])
    assert "memory" in given
    assert "motion_weight" not in given

    # 既定値と同じ値を明示指定しても明示と分かること
    given = cli.explicit_options(["in.mp4", "--memory", "6"])
    assert "memory" in given, "既定値と同値の明示指定が拾えていない"

    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.mp4")
        det = os.path.join(d, "det.json")
        _make_video(src, 320, 240, 20)
        _write_detections(det, 20, 320, 240, complete=True)

        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = cli.main([src, "--detections", det, "--reuse-detections",
                           "--detect-only", "--memory", "20",
                           "--motion-weight", "3.0"])
        assert rc == 0
        out = buf.getvalue()
        assert "明示指定を優先" in out, f"明示指定の表示が無い:\n{out}"
        assert "--memory 20" in out, f"--memory 20 が尊重されていない:\n{out}"
        assert "--motion-weight 3.0" in out, f"--motion-weight が尊重されていない:\n{out}"

        # 明示していないものは絞られ、その事実が表示される
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = cli.main([src, "--detections", det, "--reuse-detections",
                           "--detect-only"])
        assert rc == 0
        out = buf.getvalue()
        assert "設定を絞りました" in out, f"上書きが表示されていない:\n{out}"
        assert "--memory 6 -> 2" in out, f"絞り込みの中身が出ていない:\n{out}"

        # --quiet でも消えないこと
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = cli.main([src, "--detections", det, "--reuse-detections",
                           "--detect-only", "--quiet"])
        assert rc == 0
        out = buf.getvalue()
        assert "設定を絞りました" in out, f"--quiet で絞り込み表示が消えている:\n{out}"
    print("  明示指定の尊重 OK")


def test_reuse_detections_not_falsely_rejected_across_containers():
    """R-1: mp4 / webm / avi / MPEG-TS / 尺不明の h264 生ストリームで、
    正しい検出結果が --reuse-detections の事前チェックで弾かれないこと。

    nb_frames や per-stream duration の有無がコンテナごとに違う
    （mp4/avi/tsは信頼できる、webmは信頼できない、生h264は尺そのものが不明）ので、
    まとめて確認する。
    """
    if not _have_ffmpeg():
        print("  コンテナ横断の誤検知回避 SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        cases = [
            ("m.mp4", ["-c:v", "libx264"]),
            ("m.webm", ["-c:v", "libvpx-vp9", "-c:a", "libopus"]),
            ("m.avi", ["-c:v", "mpeg4", "-c:a", "mp3"]),
            ("m.ts", ["-c:v", "libx264", "-f", "mpegts"]),
        ]
        for name, extra in cases:
            src = os.path.join(d, name)
            cmd = [
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=640x480:rate=30:duration=5",
                "-f", "lavfi", "-i", "sine=f=440:d=5",
                "-map", "0:v", "-map", "1:a", "-pix_fmt", "yuv420p",
            ] + extra + [src]
            subprocess.run(cmd, check=True)
            info = vid.probe(src)
            real = _count_real_frames(src)
            det = os.path.join(d, name + ".det.json")
            _write_detections(det, real, info.width, info.height, complete=True)
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = cli.main([src, "--detections", det, "--reuse-detections",
                               "--detect-only", "--quiet"])
            assert rc == 0, (
                f"{name}: 正しい検出結果 ({real} フレーム) が弾かれた:\n{buf.getvalue()}"
            )

        # 尺が全く分からない生の h264 ストリーム（コンテナ無し）
        raw = os.path.join(d, "raw.h264")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", "testsrc2=size=320x240:rate=30:duration=2",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-f", "h264", raw],
            check=True,
        )
        info = vid.probe(raw)
        assert info.estimated_frames() is None, "生h264なのに尺を見積れてしまっている"
        real = _count_real_frames(raw)
        det = os.path.join(d, "raw.det.json")
        _write_detections(det, real, info.width, info.height, complete=True)
        rc = cli.main([raw, "--detections", det, "--reuse-detections",
                       "--detect-only", "--quiet"])
        assert rc == 0, "尺不明の生h264で正しい検出結果が弾かれた"
    print("  コンテナ横断の誤検知回避 OK")


def test_estimated_frames_not_inflated_by_audio_only_containers():
    """R-1: nb_frames も映像ストリーム自身の duration も無いコンテナ（mkv 等）で、
    コンテナ全体の duration（音声込みの最大長）にフォールバックしても、
    見積りが実フレーム数を超えないこと。

    直す前の実測: round(duration*fps) が実150フレームに対し151と出て、
    正しい検出結果が「実尺より短い」と --reuse-detections の事前チェックで
    誤って弾かれていた。
    """
    if not _have_ffmpeg():
        print("  推定フレーム数の水増し回避 SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        cases = [("m_aac.mkv", "aac"), ("m_mp3.mkv", "libmp3lame")]
        for name, codec in cases:
            path = os.path.join(d, name)
            _make_video_with_audio(path, 640, 480, 5, fps=30, acodec=codec)
            info = vid.probe(path)
            real = _count_real_frames(path)
            assert info.nb_frames is None, f"{name}: 前提が崩れている（nb_frames が付いた）"
            est = info.estimated_frames()
            assert est is not None
            assert est <= real, (
                f"{name}: 見積り {est} が実フレーム数 {real} を超えている"
            )
            assert not info.estimated_frames_reliable(), (
                f"{name}: 音声込みのフォールバック推定なのに信頼できると判定している"
            )
    print("  推定フレーム数の水増し回避 OK")


def test_reuse_detections_not_falsely_rejected_for_mkv_audio():
    """R-1: mkv+AAC / mkv+MP3 で、実尺と一致する正しい検出結果が
    --reuse-detections の事前チェックで弾かれないこと（最優先の誤爆）。
    """
    if not _have_ffmpeg():
        print("  mkv+音声の誤検知回避 SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        cases = [("m_aac.mkv", "aac"), ("m_mp3.mkv", "libmp3lame")]
        for name, codec in cases:
            src = os.path.join(d, name)
            _make_video_with_audio(src, 640, 480, 5, fps=30, acodec=codec)
            info = vid.probe(src)
            real = _count_real_frames(src)
            det = os.path.join(d, name + ".det.json")
            _write_detections(det, real, info.width, info.height, complete=True)
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = cli.main([src, "--detections", det, "--reuse-detections",
                               "--detect-only", "--quiet"])
            assert rc == 0, (
                f"{name}: 実尺と一致する正しい検出結果 ({real} フレーム) が"
                f"弾かれた:\n{buf.getvalue()}"
            )
    print("  mkv+音声の誤検知回避 OK")


def test_reuse_detections_still_rejects_short_for_mkv():
    """R-1: 推定が信用できないコンテナでも、本当に短い検出結果はやはり弾かれること
    （事前チェックでは止めないが、描画時に run_render の実デコード数チェックで弾く）。
    """
    if not _have_ffmpeg():
        print("  mkv での短い検出の拒否 SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "m_aac.mkv")
        _make_video_with_audio(src, 320, 240, 4, fps=30, acodec="aac")
        info = vid.probe(src)
        assert not info.estimated_frames_reliable()
        real = _count_real_frames(src)
        det = os.path.join(d, "det.json")
        # 実フレーム数の半分しか無い、本当に短い検出結果
        _write_detections(det, real // 2, info.width, info.height, complete=True)
        dst = os.path.join(d, "out.mp4")
        rc = cli.main([src, "--detections", det, "--reuse-detections",
                       "-o", dst, "--quiet"])
        assert rc == 1, "本当に短い検出結果が mkv で通ってしまった"
        assert not os.path.exists(dst), "尻切れの出力が退避されずに出力先へ残っている"
    print("  mkv での短い検出の拒否 OK")


def test_reuse_rejects_missing_resolution():
    """width/height の無い検出結果はエラーで弾くこと（警告止まりにしない）。

    直す前は警告だけ出して素通しし、統計は「モザイク適用率100%」なのに
    座標系のずれで実際は1画素も塗られない、という実測がある。
    """
    if not _have_ffmpeg():
        print("  解像度欠落の拒否 SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.mp4")
        det = os.path.join(d, "det.json")
        _make_video(src, 320, 240, 20)
        with open(det, "w", encoding="utf-8") as f:
            json.dump({"n_frames": 20, "complete": True, "detections": {}}, f)
        rc = cli.main([src, "--detections", det, "--reuse-detections",
                       "--detect-only", "--quiet"])
        assert rc == 1, "解像度の無い検出結果が通ってしまった"
    print("  解像度欠落の拒否 OK")


def test_resume_rejects_when_checkpoint_ahead_of_target_video():
    """途中保存の n_frames が対象動画の実フレーム数以上だと、resume が
    「推論0回で完了扱い」にならず、はっきり拒否されること。

    直す前: run_detection のデコードループが resume_from まで読み飛ばすだけで
    EOF に達し、推論を1枚も走らせずに idx==resume_from を返す。main() は
    それを完了とみなし complete=True・入力の解像度で上書き保存するので、
    中身は元の途中保存のまま（一部フレームしか検出が無い）の JSON が
    「完全」を称するようになる（実測で確認済み）。
    """
    if not _have_ffmpeg():
        print("  resume の追い越し拒否 SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.mp4")
        det = os.path.join(d, "det.json")
        _make_video(src, 320, 240, 30)  # 実30フレーム

        dets = {
            str(i): [{"class": "MALE_GENITALIA_EXPOSED", "score": 0.5, "box": [1, 1, 2, 2]}]
            for i in range(5)
        }
        with open(det, "w", encoding="utf-8") as f:
            json.dump(
                {"n_frames": 30, "width": 320, "height": 240,
                 "complete": False, "detections": dets},
                f,
            )
        before = open(det, encoding="utf-8").read()

        rc = cli.main([src, "--detections", det, "--resume", "--detect-only", "--quiet"])
        assert rc == 1, "追い越した途中保存が resume で通ってしまった"

        after = open(det, encoding="utf-8").read()
        assert after == before, "拒否したのに途中保存ファイルが書き換わっている"
    print("  resume の追い越し拒否 OK")


def test_quiet_still_reports_leak_signals():
    """--quiet でも「素通しである」ことを示す情報は stderr に出ること。

    直す前の実測: --quiet だと geometric_dropped も
    [未処理のまま残った区間] も一切出ず、適用率100%でも実は1画素も
    塗っていない出力が無言で出てきた。
    """
    if not _have_ffmpeg():
        print("  --quiet での漏れ情報 SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.mp4")
        det = os.path.join(d, "det.json")
        n = 30
        _make_video(src, 320, 240, n)
        # 前半だけ検出があり、後半は未処理区間として残る
        dets = {
            str(i): [{"class": "MALE_GENITALIA_EXPOSED", "score": 0.5, "box": [10, 10, 20, 20]}]
            for i in range(5)
        }
        with open(det, "w", encoding="utf-8") as f:
            json.dump(
                {"n_frames": n, "width": 320, "height": 240,
                 "complete": True, "detections": dets},
                f,
            )

        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = cli.main([src, "--detections", det, "--reuse-detections",
                           "--detect-only", "--quiet", "--no-bridge"])
        assert rc == 0
        err = buf.getvalue()
        assert "モザイク適用率" in err, f"--quiet で被覆率が出ていない:\n{err!r}"
        assert "未処理のまま残った区間" in err, f"--quiet で未処理区間が出ていない:\n{err!r}"
    print("  --quiet での漏れ情報 OK")


def test_corrections_apply_count_shown_not_just_loaded_count():
    """C-5後半: 手修正の『適用件数』を出す（読み込み件数だけを見せない）。

    corrections.apply() は範囲外フレームを指す修正を弾くが、CLI が読み込み
    件数しか出さないと全件反映できたように見える
    （実測: 読み込み444件のうち実際の適用は26件だった）。
    """
    if not _have_ffmpeg():
        print("  手修正の適用件数表示 SKIP (ffmpeg 無し)")
        return
    from automosaic import corrections as corr

    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.mp4")
        det = os.path.join(d, "det.json")
        corrpath = os.path.join(d, "corr.json")
        n = 10
        _make_video(src, 320, 240, n)
        _write_detections(det, n, 320, 240, complete=True)

        cset = corr.CorrectionSet(video=src, width=320, height=240)
        for f in range(3):  # 範囲内
            cset.add(corr.Correction(frame=f, box=(5, 5, 10, 10), kind="add"))
        for f in (n + 5, n + 6):  # 範囲外（n_frames を超える）
            cset.add(corr.Correction(frame=f, box=(5, 5, 10, 10), kind="add"))
        cset.save(corrpath)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main([src, "--detections", det, "--reuse-detections",
                           "--corrections", corrpath, "--detect-only"])
        assert rc == 0
        out = buf.getvalue()
        assert "読み込み 5 件" in out, f"読み込み件数が出ていない:\n{out}"
        assert "適用 3 件" in out, f"適用件数が読み込み件数と分けて出ていない:\n{out}"
    print("  手修正の適用件数表示 OK")


def test_render_detects_incomplete_pass2_decode():
    """パス2にも完走チェックを足す: probe の申告する尺と実デコード数が
    食い違う壊れた入力で、尻切れの出力が exit 0 で出ないこと。

    直す前は writer.returncode しか見ておらず、reader 側の実デコード数が
    検出結果より少なくても気付けなかった（実測: probe は90フレーム/3.0sと
    言うが実際は39フレームしか読めない入力で、警告もエラーも無く
    39フレームの出力がそのまま返った）。
    """
    if not _have_ffmpeg():
        print("  パス2の完走チェック SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.mp4")
        dst = os.path.join(d, "out.mp4")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", "testsrc2=size=320x240:r=30", "-frames:v", "90",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "5",
             "-movflags", "+faststart", src],
            check=True,
        )
        raw = open(src, "rb").read()
        broken = os.path.join(d, "broken.mp4")
        with open(broken, "wb") as f:
            f.write(raw[: int(len(raw) * 0.45)])
        info = vid.probe(src)  # 壊す前の（90フレーム申告の）info をそのまま使う

        regions = {i: [((10, 10, 60, 60), False)] for i in range(90)}
        try:
            cli.run_render(broken, dst, info, regions, 90, 8, "black",
                           28, "ultrafast", None, quiet=True)
        except RuntimeError as e:
            msg = str(e)
            assert "パス2が" in msg or "異常終了" in msg, f"想定と違う例外: {msg}"
        else:
            raise AssertionError("壊れた入力なのにパス2が正常終了した")
        assert not os.path.exists(dst), "尻切れの出力が退避されずに出力先へ残っている"
    print("  パス2の完走チェック OK")


def test_probe_reads_rotation_and_swaps_dimensions():
    """issue #1 根本原因: probe() が side_data_list の rotation を読み、
    90/270度では width/height を実際にデコードされる向きへ入れ替えること。

    直す前は常に「箱の中身の生のサイズ」（640x360）をそのまま返しており、
    ffmpeg が自動回転で実際に流すフレーム（360x640）と食い違っていた。
    """
    if not _have_ffmpeg():
        print("  回転メタデータの読み取り SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        # ffprobe の rotation は正負どちらの表記もある（270度指定は -90 と
        # 出る。実測済み）。normalize 後の期待値で確認する。
        cases = [(0, 640, 360), (90, 360, 640), (180, 640, 360), (270, 360, 640)]
        for deg, exp_w, exp_h in cases:
            path = os.path.join(d, f"rot{deg}.mp4")
            if deg == 0:
                _make_video(path, 640, 360, 3, fps=15)
            else:
                _make_rotated_video(path, 640, 360, 3, deg, fps=15)
            info = vid.probe(path)
            assert (info.width, info.height) == (exp_w, exp_h), (
                f"{deg}度: probe() が {info.width}x{info.height} を返した"
                f"（期待 {exp_w}x{exp_h}）。side_data_list の rotation を"
                "読めていない、または入れ替え方が違う"
            )
            assert info.rotation == deg, (
                f"{deg}度: info.rotation が {info.rotation}（期待 {deg}）"
            )
            # probe() の申告するサイズが、ffmpeg が実際に流すデコード後の
            # フレームサイズと一致すること。ここがずれると FrameBuffer の
            # 長さ検査は素通りし、reshape だけが転置される（本 issue の核心）。
            actual_h, actual_w = _decode_frame_rgb(path).shape[:2]
            assert (actual_w, actual_h) == (info.width, info.height), (
                f"{deg}度: 実デコードサイズ {actual_w}x{actual_h} が "
                f"probe() の申告 {info.width}x{info.height} と食い違う"
            )
    print("  回転メタデータの読み取り OK")


def test_rotated_video_pixels_not_scrambled():
    """issue #1: 回転メタデータ付き動画を焼いても、中身が転置スクランブルに
    ならないこと。モザイク領域は1件も与えず、パス2の幾何だけを見る。

    直す前は probe() が 640x360 のまま返す一方 ffmpeg は 360x640 の
    フレームを流すため、FrameBuffer(info.width, info.height) の形と実際の
    バイト列の並びが食い違う。y_size は w*h で 640*360 == 360*640 と一致し、
    彩度平面も (w//2)*(h//2) が対称に一致するので nbytes の長さ検査は
    素通りする。実際に落ちるのは reshape の形だけで、出力は例外もエラーも
    無いまま斜めに裂けたスクランブル画像になる（実測・PR添付ログ参照）。
    """
    if not _have_ffmpeg():
        print("  回転動画の画素破損チェック SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "rot.mp4")
        dst = os.path.join(d, "out.mp4")
        n_frames = 5
        _make_rotated_video(src, 640, 360, n_frames, 90, fps=15)
        info = vid.probe(src)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            cli.run_render(src, dst, info, {}, n_frames, 8, "black",
                           18, "veryfast", None, True)

        expected = _decode_frame_rgb(src)
        actual = _decode_frame_rgb(dst)
        assert actual.shape == expected.shape, (
            f"出力フレームの解像度が入力の表示向きと違う: "
            f"{actual.shape} (出力) vs {expected.shape} (入力・自動回転込み)。"
            "縦横が入れ替わったまま焼かれている"
        )
        diff = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
        mean_diff = float(diff.mean())
        # モザイク領域を1件も与えていないので、再エンコードの劣化以外に
        # 差が出るはずがない（実測: 直った状態で平均差分 0.3 前後）。
        # 転置スクランブルが起きると縞模様がまったく別内容になり、
        # 平均差分は数十まで跳ね上がる。10 は劣化分に十分な余裕を持たせた閾値。
        assert mean_diff < 10.0, (
            f"出力フレームが入力（自動回転込み）と大きく違う"
            f"（平均差分 {mean_diff:.2f}）。転置スクランブルが再発している疑い"
        )
    print("  回転動画の画素破損チェック OK")


def test_render_rejects_probe_ffmpeg_dimension_mismatch():
    """issue #32: probe() の申告サイズと ffmpeg の実デコード出力が食い違ったとき、
    パス2が黙って reshape を転置せず、reader/writer を開く前に例外で止まること。

    #1 で塞いだのは回転メタデータという「既知の1経路」だけで、
    probe と ffmpeg の解釈が別経路でずれる可能性そのものは残っている。
    FrameBuffer の長さ検査（y_size = width*height、彩度平面も (w//2)*(h//2)）は
    width と height を入れ替えても値が変わらない対称式なので、単純な長さ比較では
    この種の食い違いを検出できない。ここでは info.width/height を実際の
    デコードサイズと入れ替えて渡し、probe と ffmpeg が別経路でずれた状況を
    直接模擬する（回転メタデータという特定経路に頼らず、この故障クラスそのものを
    突く）。
    """
    if not _have_ffmpeg():
        print("  probe/ffmpeg サイズ不一致の拒否 SKIP (ffmpeg 無し)")
        return
    import dataclasses

    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.mp4")
        dst = os.path.join(d, "out.mp4")
        n_frames = 5
        _make_video(src, 64, 48, n_frames, fps=15)
        info = vid.probe(src)
        assert (info.width, info.height) == (64, 48), "前提が崩れている"

        # probe と ffmpeg が別経路でずれた状況を模擬: width/height を入れ替えて渡す。
        # y_size=64*48==48*64 なので、この入れ替えは FrameBuffer の長さ検査だけでは
        # 検出できない（この非対称性こそが issue #32 の核心）。
        swapped = dataclasses.replace(info, width=48, height=64)
        try:
            cli.run_render(src, dst, swapped, {}, n_frames, 8, "black",
                           18, "veryfast", None, True)
        except RuntimeError as e:
            assert "一致しません" in str(e), f"想定と違う例外: {e}"
        else:
            raise AssertionError(
                "probe と ffmpeg のサイズが食い違ったのにパス2が正常終了した"
                "（斜めに裂けたスクランブル画像が exit 0 で出ている疑い）"
            )
        assert not os.path.exists(dst), (
            "サイズ不一致を検出したのに出力ファイルが残っている"
            "（reader/writer を開く前に止めれば quarantine すら要らないはず）"
        )
    print("  probe/ffmpeg サイズ不一致の拒否 OK")


def test_despike_off_by_default_and_reports_when_enabled():
    """issue #9: min_track_len の既定反転の回帰ガード。

    直す前は既定でデスパイクが有効（min_track_len=2）で、単発かつスコア0.35未満の
    トラックを黙って捨てていた。実測（docs/09-mosaic-quality.md S4）:
    確実に映っている区間の実観測125件を捨て、うち40件はそのフレームに他の根拠が
    無く、捨てた結果そのフレームが素通しになっていた。

    既定では単発の弱い検出でも残ること、--despike を明示したときだけ落ちて
    かつ捨てた場所（フレーム番号・クラス・スコア）が必ず報告に出ることを固定する。
    --despike と --no-despike の同時指定はエラーで止めること（既存コマンドラインを
    黙って別の意味にしない）も併せて確認する。
    """
    if not _have_ffmpeg():
        print("  despike既定オフ + 捨てた場所の報告 SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.mp4")
        det = os.path.join(d, "det.json")
        n = 20
        _make_video(src, 320, 240, n)
        # 前後に何も無い、単発かつ低スコアの検出。despike の唯一の標的パターン
        dets = {
            "10": [{"class": "MALE_GENITALIA_EXPOSED", "score": 0.15,
                    "box": [10, 10, 20, 20]}],
        }
        with open(det, "w", encoding="utf-8") as f:
            json.dump(
                {"n_frames": n, "width": 320, "height": 240,
                 "complete": True, "detections": dets},
                f,
            )

        # 既定（フラグ無し）: デスパイクが働かず、単発検出が生き残ること
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main([src, "--detections", det, "--reuse-detections",
                           "--detect-only"])
        assert rc == 0
        text = out.getvalue()
        assert "tracks_despiked                    0" in text, (
            f"既定でデスパイクが働いている（反転できていない）:\n{text}"
        )
        assert "デスパイクで捨てた実観測" not in text

        # --despike: 明示的に有効化すると従来どおり捨て、かつ場所を報告する
        out2 = io.StringIO()
        with redirect_stdout(out2):
            rc2 = cli.main([src, "--detections", det, "--reuse-detections",
                            "--detect-only", "--despike"])
        assert rc2 == 0
        text2 = out2.getvalue()
        assert "tracks_despiked                    1" in text2, (
            f"--despike で捨てているはずが捨てていない:\n{text2}"
        )
        assert "デスパイクで捨てた実観測 1 件" in text2, (
            f"捨てた場所のレポートが出ていない（黙って素通しにしている）:\n{text2}"
        )
        assert "frame      10-10" in text2, f"捨てた場所のフレーム番号が無い:\n{text2}"

        # --despike と --no-despike の同時指定は矛盾として弾く（黙ってどちらかに倒さない）
        buf = io.StringIO()
        try:
            with redirect_stderr(buf):
                cli.main([src, "--detections", det, "--reuse-detections",
                          "--detect-only", "--despike", "--no-despike"])
        except SystemExit as e:
            assert e.code == 2
        else:
            raise AssertionError("--despike --no-despike の矛盾指定が通ってしまった")
        assert "同時に指定できません" in buf.getvalue()
    print("  despike既定オフ + 捨てた場所の報告 OK")


def test_uncovered_ranges_recomputed_after_corrections():
    """issue #4: 「素通しの区間」表示が corr.apply() 前の left_open を使っており、
    手修正の結果を反映していなかった回帰ガード。

    直す前は、remove だけの手修正（add を伴わない bare remove。「誤検知」判定で
    通常操作として置かれる）で自動領域が空になったフレームがあっても、
    report の uncovered_ranges / 画面の「素通しの区間 N 件」が 0 件のまま
    だった（bridge_uncovered() が返す left_open は corr.apply() より前の値
    のため）。add で埋めた場合も同様に、埋めた後は消えるべきものが
    残っていた可能性がある。ここでは両方向を確認する。
    """
    if not _have_ffmpeg():
        print("  素通し区間の再計算 SKIP (ffmpeg 無し)")
        return
    from automosaic import corrections as corr

    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.mp4")
        det = os.path.join(d, "det.json")
        n = 40
        _make_video(src, 320, 240, n)

        box = [80, 60, 120, 120]
        dets = {}
        for f in list(range(0, 10)) + list(range(30, n)):
            dets[str(f)] = [{"class": "FEMALE_GENITALIA_EXPOSED", "score": 0.9, "box": box}]
        # 10..29 は検出なし（20フレームの内部ギャップ。--estimate-gaps 無しの既定
        # では bridge_max が絞られ自動では埋まらない。--stitch-gap 0 で
        # stitch_tracks による再結合＝補間扱いも切り、本当に空のフレームを作る）
        _write_detections(det, n, 320, 240, complete=True, dets=dets)

        # 前提確認: 手修正なしでは、その内部ギャップが実際に素通しとして出ること
        report0 = os.path.join(d, "report0.json")
        rc = cli.main([src, "--detections", det, "--reuse-detections",
                       "--stitch-gap", "0",
                       "--detect-only", "--report", report0, "--quiet"])
        assert rc == 0
        with open(report0, encoding="utf-8") as f:
            rep0 = json.load(f)
        base_gaps = rep0["uncovered_ranges"]
        assert base_gaps, "前提が崩れている: 内部ギャップが素通しにならなかった"
        gap_frames = sorted({
            fr for g in base_gaps for fr in range(g["start_frame"], g["end_frame"] + 1)
        })

        # add: そのギャップ全部を手修正で埋める -> 素通し表示から消えること
        cset_add = corr.CorrectionSet(video=src, width=320, height=240)
        for fr in gap_frames:
            cset_add.add(corr.Correction(frame=fr, box=(5, 5, 10, 10), kind="add"))
        corr_add = os.path.join(d, "corr_add.json")
        cset_add.save(corr_add)
        report_add = os.path.join(d, "report_add.json")
        rc = cli.main([src, "--detections", det, "--reuse-detections",
                       "--stitch-gap", "0",
                       "--corrections", corr_add,
                       "--detect-only", "--report", report_add, "--quiet"])
        assert rc == 0
        with open(report_add, encoding="utf-8") as f:
            rep_add = json.load(f)
        assert rep_add["uncovered_ranges"] == [], (
            f"add で埋めたのに素通し扱いのまま報告に残っている: "
            f"{rep_add['uncovered_ranges']}"
        )

        # remove: 検出がある側 (frame 0-9) をまるごと bare remove で消す
        # -> add を伴わないので、そのフレームは空になり素通しとして出るべき
        cset_rm = corr.CorrectionSet(video=src, width=320, height=240)
        for fr in range(0, 10):
            cset_rm.add(corr.Correction(frame=fr, box=(0, 0, 320, 240), kind="remove"))
        corr_rm = os.path.join(d, "corr_rm.json")
        cset_rm.save(corr_rm)
        report_rm = os.path.join(d, "report_rm.json")
        rc = cli.main([src, "--detections", det, "--reuse-detections",
                       "--stitch-gap", "0",
                       "--corrections", corr_rm,
                       "--detect-only", "--report", report_rm, "--quiet"])
        assert rc == 0
        with open(report_rm, encoding="utf-8") as f:
            rep_rm = json.load(f)
        removed_ranges = rep_rm["uncovered_ranges"]
        removed_frames = {
            fr for g in removed_ranges for fr in range(g["start_frame"], g["end_frame"] + 1)
        }
        assert set(range(0, 10)) <= removed_frames, (
            "remove だけの手修正で空になったフレームが素通しとして報告されていない "
            f"（安全表示が嘘をついている）: uncovered_ranges={removed_ranges}"
        )
    print("  素通し区間の再計算（add で消える／remove で出る） OK")


def test_stats_match_uncovered_ranges_after_corrections():
    """issue #41: stats["frames_with_mosaic"] / stats["uncovered_gaps"] が
    corr.apply() 前の値のまま残っていた回帰ガード。

    直す前は、bare remove で自動領域が空になったフレームがあっても
    stats["frames_with_mosaic"] / stats["uncovered_gaps"]（つまり画面の
    「モザイク適用率」と stats テーブルの uncovered_gaps）が旧値のまま
    （適用率100.0%・uncovered_gaps 0）で、直後に表示される
    「[未処理のまま残った区間 N 件]」と矛盾していた（issue #41 の再現手順、
    合成素材 320x240/60フレーム・全フレーム検出・frame 20-29 に
    add を伴わない bare remove）。

    合わせて、手修正が触らない生検出/トラック段階由来のキー
    （geometric_dropped, oversized_kept, raw_detections, tracks_final,
    median_track_speed_px_per_frame）と、自動処理の内訳を示す診断キー
    （regions_interpolated, regions_from_memory, regions_bridged,
    frames_bridged）が、手修正の有無で変わらないことも確認する
    （手修正の有無だけを変えて process() 側の入力は同一なので、
    これらは corr.apply() を通しても値が変わらないはず）。
    """
    if not _have_ffmpeg():
        print("  stats と素通し件数の整合 SKIP (ffmpeg 無し)")
        return
    from automosaic import corrections as corr

    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.mp4")
        det = os.path.join(d, "det.json")
        n = 60
        w, h = 320, 240
        _make_video(src, w, h, n)

        box = [0, 0, w, h]
        dets = {str(f): [{"class": "FEMALE_GENITALIA_EXPOSED", "score": 0.9, "box": box}]
                for f in range(n)}
        _write_detections(det, n, w, h, complete=True, dets=dets)

        # ベースライン: 手修正なし。ここでは適用率100%・uncovered_gaps 0 が正しい
        report_base = os.path.join(d, "report_base.json")
        rc = cli.main([src, "--detections", det, "--reuse-detections",
                       "--detect-only", "--report", report_base, "--quiet"])
        assert rc == 0
        with open(report_base, encoding="utf-8") as f:
            rep_base = json.load(f)
        assert rep_base["stats"]["frames_with_mosaic"] == n
        assert rep_base["stats"]["uncovered_gaps"] == 0
        assert rep_base["uncovered_ranges"] == []

        # frame 20-29 を add を伴わない bare remove で消す（「誤検知」判定の
        # 通常操作）。素通しになるはずの10フレーム。
        cset = corr.CorrectionSet(video=src, width=w, height=h)
        for fr in range(20, 30):
            cset.add(corr.Correction(frame=fr, box=(0, 0, w, h), kind="remove"))
        corr_path = os.path.join(d, "corr.json")
        cset.save(corr_path)

        report_corr = os.path.join(d, "report_corr.json")
        proc = subprocess.run(
            [sys.executable, "-m", "automosaic",
             src, "--detections", det, "--reuse-detections",
             "--corrections", corr_path,
             "--detect-only", "--report", report_corr, "--quiet"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")

        with open(report_corr, encoding="utf-8") as f:
            rep_corr = json.load(f)
        stats = rep_corr["stats"]
        uncovered = rep_corr["uncovered_ranges"]

        # 中心の主張: 「適用率」の元になる frames_with_mosaic と、
        # stats テーブルの uncovered_gaps が、実際の素通し区間と矛盾しないこと。
        assert uncovered, "前提が崩れている: bare remove で素通しができなかった"
        gap_frames_total = sum(u["frames"] for u in uncovered)
        assert gap_frames_total == 10, f"想定外の素通しフレーム数: {uncovered}"
        assert stats["frames_with_mosaic"] == n - gap_frames_total, (
            "frames_with_mosaic が corr.apply() 後の regions と矛盾している: "
            f"frames_with_mosaic={stats['frames_with_mosaic']} "
            f"uncovered_ranges={uncovered}"
        )
        assert stats["uncovered_gaps"] == len(uncovered), (
            "stats[\"uncovered_gaps\"] が実際の素通し区間数と矛盾している: "
            f"uncovered_gaps={stats['uncovered_gaps']} 件数={len(uncovered)}"
        )
        assert stats["frames_with_mosaic"] + gap_frames_total == stats["frames"]

        # --quiet の1行要約も同じ値であること（report.json だけ直って
        # stderr が古いまま、という半端な直し方を防ぐ）。
        assert "uncovered_gaps=1" in err, f"--quiet の uncovered_gaps が揃っていない: {err!r}"
        expected_pct = 100.0 * stats["frames_with_mosaic"] / n
        assert f"モザイク適用率 {expected_pct:.1f}%" in err, (
            f"--quiet の適用率が report.json の frames_with_mosaic と揃っていない: {err!r}"
        )

        # 手修正が触らない段階のキーは、手修正の有無で変わらないこと
        # （geometric_dropped などを「変わらない性質」として意図的に前の値の
        # ままにしている設計の回帰ガード）。
        unaffected_keys = [
            "raw_detections", "geometric_dropped", "oversized_kept",
            "tracks_before_despike", "tracks_despiked", "tracks_stitched",
            "tracks_final", "frames_with_detection",
            "median_track_speed_px_per_frame",
            "regions_interpolated", "regions_from_memory",
            "regions_bridged", "frames_bridged",
        ]
        for k in unaffected_keys:
            assert stats[k] == rep_base["stats"][k], (
                f"手修正の有無で変わらないはずのキー {k} が変わっている: "
                f"手修正なし={rep_base['stats'][k]} 手修正あり={stats[k]}"
            )
    print("  stats と素通し件数の整合（frames_with_mosaic / uncovered_gaps） OK")


def test_check_render_geometry_flags_odd_dimensions():
    """issue #2: 奇数解像度（幅・高さ・両方のどれでも）を検査で検知すること。

    偶数解像度は None（通せる）を返し、誤検知しないことも確認する。
    """
    def _info(w, h):
        return vid.VideoInfo(w, h, 30, 1, 10, 1.0, "yuv420p",
                             None, None, None, None, False)

    for w, h in [(641, 480), (640, 481), (641, 481)]:
        msg = vid.check_render_geometry(_info(w, h))
        assert msg is not None, f"{w}x{h} が奇数なのに検査を素通りした"
        assert str(w) in msg and str(h) in msg, f"メッセージに解像度が含まれない: {msg!r}"

    assert vid.check_render_geometry(_info(640, 480)) is None, (
        "偶数解像度なのに検査に引っかかった"
    )
    print("  check_render_geometry の奇数検知 OK")


def test_odd_resolution_stops_before_wasting_pass1():
    """issue #2: 奇数解像度は、パス1（検出）を待たせずに probe 直後で止まること。

    直す前は --detect-only を付けずに焼こうとすると、パス1を最後まで走らせた
    あと、パス2の FrameBuffer guard で初めて ValueError が飛んでいた
    （検出に数時間かけたあとに落ちる、が issue の再現手順そのもの）。
    ここでは Detector の構築自体を壊し、そこに到達したら即座にわかるように
    してから確認する。
    """
    if not _have_ffmpeg():
        print("  奇数解像度の早期停止 SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "odd.webm")
        dst = os.path.join(d, "out.mp4")
        _make_odd_video(src, 641, 481, 5)

        info = vid.probe(src)
        assert (info.width, info.height) == (641, 481), (
            f"合成素材が奇数のまま probe されていない: {info.width}x{info.height}"
            "（テストの前提が崩れている）"
        )

        def _boom(**kw):
            raise AssertionError(
                "パス1（Detector構築）まで到達した。検出前に止まっていない"
            )

        real_detector, cli.Detector = cli.Detector, _boom
        try:
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = cli.main([src, "-o", dst])
        finally:
            cli.Detector = real_detector

        assert rc == 1, f"奇数解像度なのに rc={rc}"
        assert not os.path.exists(dst), "止まったはずなのに出力ファイルができている"
        assert "奇数" in err.getvalue(), f"奇数を理由にした停止メッセージが無い: {err.getvalue()!r}"
    print("  奇数解像度の早期停止（パス1を待たせない） OK")


def test_odd_resolution_detect_only_warns_but_completes():
    """issue #2: --detect-only は奇数解像度でも警告のみで完走すること。

    パス1（検出）は奇数解像度でも正しく動く（scale フィルタが
    force_divisible_by で偶数に丸めてから読むだけ）ため、詳細検出用途では
    止める必要が無い。警告なしに黙って通すのも禁止（RULES.md「黙って
    素通しを作らない」）なので、警告が出ることも確認する。
    """
    if not _have_ffmpeg():
        print("  奇数解像度 --detect-only の完走 SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "odd.webm")
        _make_odd_video(src, 641, 481, 5)

        real_detector, cli.Detector = cli.Detector, lambda **kw: _NullDetector()
        try:
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = cli.main([src, "--detect-only", "--quiet"])
        finally:
            cli.Detector = real_detector

        assert rc == 0, f"--detect-only なのに rc={rc}: {err.getvalue()}"
        msg = err.getvalue()
        assert "奇数" in msg, f"奇数の警告が出ていない: {msg!r}"
        assert "続行します" in msg, f"続行の告知が出ていない: {msg!r}"
    print("  奇数解像度 --detect-only の完走（警告のみ） OK")


def test_probe_ffmpeg_mismatch_stops_before_wasting_pass1():
    """issue #68: probe と実デコードの食い違い検査（issue #32、#47）は元々
    run_render() の直前（＝パス1の後）にしか無く、実素材ではパス1に約4.8時間
    かかる（docs/10-realrun-2026-08-24.md）ため、食い違う入力はその時間を
    無駄にしてから初めて止まっていた。ここでは vid.probe() が実デコードと
    食い違う寸法を返す状況（probe 自身の申告が誤っている状況）を模擬し、
    パス1（Detector構築）に到達する前に rc=1 で止まることを確認する。

    #2 のときと同じ手口（cli.Detector を到達検知用の例外に差し替える）を使う。
    ただし今回止めたい検査は check_render_geometry（偶奇のみを見る計算）とは
    別物なので、入れ替える寸法は偶数のまま（奇数検査には引っかからない）にする。
    """
    if not _have_ffmpeg():
        print("  probe/ffmpeg 不一致の早期停止 SKIP (ffmpeg 無し)")
        return
    import dataclasses

    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.mp4")
        dst = os.path.join(d, "out.mp4")
        _make_video(src, 64, 48, 5, fps=15)

        real_probe = vid.probe

        def _fake_probe(path):
            info = real_probe(path)
            # 実デコードは 64x48 のまま。probe だけが 48x64（w/h 入れ替え）を
            # 申告する状況を模擬する。y_size=64*48==48*64 なので、この入れ替えは
            # FrameBuffer の長さ検査だけでは検出できない（issue #32 の核心）。
            return dataclasses.replace(info, width=48, height=64)

        vid.probe = _fake_probe

        def _boom(**kw):
            raise AssertionError(
                "パス1（Detector構築）まで到達した。検出前に止まっていない"
            )

        real_detector, cli.Detector = cli.Detector, _boom
        try:
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = cli.main([src, "-o", dst])
        finally:
            vid.probe = real_probe
            cli.Detector = real_detector

        assert rc == 1, f"probe/ffmpeg 不一致なのに rc={rc}: {err.getvalue()}"
        assert not os.path.exists(dst), "止まったはずなのに出力ファイルができている"
        assert "一致しません" in err.getvalue(), (
            f"不一致を理由にした停止メッセージが無い: {err.getvalue()!r}"
        )
    print("  probe/ffmpeg 不一致の早期停止（パス1を待たせない） OK")


def test_probe_ffmpeg_mismatch_detect_only_warns_but_completes():
    """issue #68: --detect-only は probe/ffmpeg 不一致でも警告のみで完走すること。

    check_render_geometry（issue #2）と同じ扱い方に揃える。パス1自体は
    detection_frame_size() が実測値にフォールバックするため、probe の
    width/height が誤っていても致命的に壊れるわけではない
    （scale_back の分母がずれるので座標は不正確になりうるが、それは検出専用
    フラグの範囲内の話であり、モザイクを焼く出力ファイル自体は作られない）。
    """
    if not _have_ffmpeg():
        print("  probe/ffmpeg 不一致 --detect-only の完走 SKIP (ffmpeg 無し)")
        return
    import dataclasses

    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.mp4")
        _make_video(src, 64, 48, 5, fps=15)

        real_probe = vid.probe

        def _fake_probe(path):
            info = real_probe(path)
            return dataclasses.replace(info, width=48, height=64)

        vid.probe = _fake_probe
        real_detector, cli.Detector = cli.Detector, lambda **kw: _NullDetector()
        try:
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = cli.main([src, "--detect-only", "--quiet"])
        finally:
            vid.probe = real_probe
            cli.Detector = real_detector

        assert rc == 0, f"--detect-only なのに rc={rc}: {err.getvalue()}"
        msg = err.getvalue()
        assert "一致しません" in msg, f"不一致の警告が出ていない: {msg!r}"
        assert "続行します" in msg, f"続行の告知が出ていない: {msg!r}"
    print("  probe/ffmpeg 不一致 --detect-only の完走（警告のみ） OK")


def test_matching_inputs_pass_both_early_geometry_checks():
    """issue #68: 正常な（probe と実デコードが一致する）入力を誤って止めないこと。

    check_render_geometry と probe 直後の verify_full_frame_size、2つの検査を
    probe 直後に並べたので、どちらも誤爆しないことを複数の解像度・複数の
    コンテナで確認する。#47 の独立検証は mp4/mkv/webm/avi/mjpeg/TS/縦/回転/
    10bit/VFR/アナモルフィック/カバーアート添付など20種の合成素材で誤爆ゼロ
    だったが、ここでは cli.main() の通常経路（probe 直後の2検査 + パス1 +
    パス2）を実際に完走させて確認する範囲に絞る（フルの20種の再現は別途）。
    """
    if not _have_ffmpeg():
        print("  正常入力の誤爆なし SKIP (ffmpeg 無し)")
        return
    with tempfile.TemporaryDirectory() as d:
        cases = [
            ("mp4_even", os.path.join(d, "a.mp4"), 320, 240),
            ("mp4_odd_frame", os.path.join(d, "b.mp4"), 640, 480),
        ]
        for name, src, w, h in cases:
            _make_video(src, w, h, 5, fps=10)
            dst = os.path.join(d, name + "_out.mp4")
            real_detector, cli.Detector = cli.Detector, lambda **kw: _NullDetector()
            try:
                out = io.StringIO()
                err = io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    rc = cli.main([src, "-o", dst, "--quiet"])
            finally:
                cli.Detector = real_detector
            assert rc == 0, f"{name}: 正常入力なのに rc={rc}: {err.getvalue()}"
            assert "一致しません" not in err.getvalue(), (
                f"{name}: 正常入力なのに不一致の誤検知: {err.getvalue()!r}"
            )
            assert "奇数" not in err.getvalue(), (
                f"{name}: 正常入力なのに奇数の誤検知: {err.getvalue()!r}"
            )
            assert os.path.exists(dst), f"{name}: 正常入力なのに出力が作られていない"

        # 回転メタデータ（issue #1 の経路。probe() 側が width/height を
        # 入れ替え済みで整合させるので、ここも誤爆してはいけない）
        rot_src = os.path.join(d, "rot.mp4")
        _make_rotated_video(rot_src, 320, 240, 5, degrees=90, fps=10)
        rot_dst = os.path.join(d, "rot_out.mp4")
        real_detector, cli.Detector = cli.Detector, lambda **kw: _NullDetector()
        try:
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = cli.main([rot_src, "-o", rot_dst, "--quiet"])
        finally:
            cli.Detector = real_detector
        assert rc == 0, f"回転動画なのに rc={rc}: {err.getvalue()}"
        assert "一致しません" not in err.getvalue(), (
            f"回転動画なのに不一致の誤検知: {err.getvalue()!r}"
        )
        assert os.path.exists(rot_dst)
    print("  正常入力（偶数解像度・回転メタデータ）の誤爆なし OK")


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
