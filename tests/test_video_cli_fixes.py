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
