"""ffmpeg / ffprobe のラッパー。

方針は docs/01-technical-design.md の「手法E」。
Python 側でフレームを合成し rawvideo を ffmpeg に pipe する。中間ファイルは作らない。
音声・字幕・チャプタ・メタデータは、元ファイルを2番目の入力として渡して
そこから stream copy することで無劣化のまま持ってくる。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass


class FFmpegNotFound(RuntimeError):
    pass


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise FFmpegNotFound(
            f"{tool} が PATH に見つかりません。"
            " winget install Gyan.FFmpeg の後にシェルを開き直してください。"
        )
    return path


@dataclass
class VideoInfo:
    width: int
    height: int
    fps_num: int
    fps_den: int
    nb_frames: int | None
    duration: float | None
    pix_fmt: str
    color_primaries: str | None
    color_trc: str | None
    colorspace: str | None
    color_range: str | None
    has_audio: bool

    @property
    def fps(self) -> float:
        return self.fps_num / self.fps_den

    @property
    def fps_str(self) -> str:
        return f"{self.fps_num}/{self.fps_den}"

    @property
    def long_edge(self) -> int:
        return max(self.width, self.height)

    def estimated_frames(self) -> int | None:
        if self.nb_frames:
            return self.nb_frames
        if self.duration:
            return int(round(self.duration * self.fps))
        return None


def probe(path: str) -> VideoInfo:
    ffprobe = _require("ffprobe")
    out = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_streams",
            "-show_format",
            "-of", "json",
            path,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    data = json.loads(out.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError(f"映像ストリームが見つかりません: {path}")

    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/0"
    num, den = (int(v) for v in rate.split("/"))
    if den == 0 or num == 0:
        rate = video.get("r_frame_rate", "30/1")
        num, den = (int(v) for v in rate.split("/"))
    if den == 0:
        num, den = 30, 1

    nb = video.get("nb_frames")
    duration = video.get("duration") or data.get("format", {}).get("duration")

    return VideoInfo(
        width=int(video["width"]),
        height=int(video["height"]),
        fps_num=num,
        fps_den=den,
        nb_frames=int(nb) if nb and str(nb).isdigit() else None,
        duration=float(duration) if duration else None,
        pix_fmt=video.get("pix_fmt", "yuv420p"),
        color_primaries=video.get("color_primaries"),
        color_trc=video.get("color_transfer"),
        colorspace=video.get("color_space"),
        color_range=video.get("color_range"),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def detect_pix_fmt(info: VideoInfo) -> str:
    """処理に使う raw のピクセルフォーマット。10bit は維持する。"""
    if info.pix_fmt.endswith(("10le", "10be")):
        return "yuv420p10le"
    return "yuv420p"


def open_detection_reader(
    path: str, infer_size: int, limit_frames: int | None = None
) -> subprocess.Popen:
    """パス1用。長辺を infer_size に縮めた BGR フレームを流す。

    検出器はどのみち infer_size に縮小するので、ここで先に縮めても検出精度は落ちず、
    デコードと転送のコストだけが大きく下がる。縮小は cv2 より品質の良い ffmpeg 側で行う。
    """
    ffmpeg = _require("ffmpeg")
    cmd = [
        ffmpeg,
        "-v", "error",
        "-i", path,
        "-vf", f"scale=w={infer_size}:h={infer_size}:force_original_aspect_ratio=decrease:flags=bicubic",
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
    ]
    if limit_frames:
        cmd += ["-frames:v", str(limit_frames)]
    cmd += ["-"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def detection_frame_size(info: VideoInfo, infer_size: int) -> tuple[int, int]:
    """パス1のデコード後サイズ。ffmpeg の scale と同じ計算をする。"""
    scale = infer_size / info.long_edge
    w = max(1, int(info.width * scale))
    h = max(1, int(info.height * scale))
    # ffmpeg の scale フィルタは既定で偶数に丸める
    return w - (w % 2), h - (h % 2)


def open_full_reader(
    path: str, pix_fmt: str, limit_frames: int | None = None
) -> subprocess.Popen:
    """パス2用。原寸の YUV を流す。RGB に変換しないので色劣化が入らない。"""
    ffmpeg = _require("ffmpeg")
    cmd = [ffmpeg, "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", pix_fmt]
    if limit_frames:
        cmd += ["-frames:v", str(limit_frames)]
    cmd += ["-"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def open_writer(
    src_path: str,
    dst_path: str,
    info: VideoInfo,
    pix_fmt: str,
    crf: int = 16,
    preset: str = "slow",
    limit_frames: int | None = None,
) -> subprocess.Popen:
    """rawvideo を受け取って書き出す。

    元ファイルを2番目の入力として渡し、音声・字幕・添付・チャプタ・メタデータを
    そこから stream copy する。これが音声無劣化・字幕保持の最も確実な方法。
    色空間タグは rawvideo 経由で失われるので、probe で読んだ値を明示的に付け直す。
    """
    ffmpeg = _require("ffmpeg")
    cmd = [
        ffmpeg,
        "-v", "error",
        "-y",
        "-f", "rawvideo",
        "-pix_fmt", pix_fmt,
        "-s", f"{info.width}x{info.height}",
        "-r", info.fps_str,
        "-i", "-",
        "-i", src_path,
        "-map", "0:v",
    ]

    # limit_frames 指定時は尺が合わないので音声等は載せない（動作確認用）
    if not limit_frames:
        cmd += ["-map", "1:a?", "-map", "1:s?", "-map", "1:t?"]
        cmd += ["-map_metadata", "1", "-map_chapters", "1"]
        cmd += ["-c:a", "copy", "-c:s", "copy"]
    else:
        cmd += ["-map_chapters", "-1"]

    cmd += [
        "-c:v", "libx264",
        "-crf", str(crf),
        "-preset", preset,
        "-pix_fmt", pix_fmt,
    ]

    # 元の色空間タグをそのまま引き継ぐ。未取得なら触らない（誤ったタグを付けない）。
    if info.color_primaries:
        cmd += ["-color_primaries", info.color_primaries]
    if info.color_trc:
        cmd += ["-color_trc", info.color_trc]
    if info.colorspace:
        cmd += ["-colorspace", info.colorspace]
    if info.color_range:
        cmd += ["-color_range", info.color_range]

    if dst_path.lower().endswith(".mp4"):
        cmd += ["-movflags", "+faststart"]

    cmd += [dst_path]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
