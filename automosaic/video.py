"""ffmpeg / ffprobe のラッパー。

方針は docs/01-technical-design.md の「手法E」。
Python 側でフレームを合成し rawvideo を ffmpeg に pipe する。中間ファイルは作らない。
音声・字幕・チャプタ・メタデータは、元ファイルを2番目の入力として渡して
そこから stream copy することで無劣化のまま持ってくる。
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import sys
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
    # duration がコンテナ全体（音声込みの最大長）へのフォールバックで、
    # 映像ストリーム自身の値ではないときに True。末尾に追加した新フィールドなので、
    # 既存の位置引数呼び出し（テストなど）は既定値 False のまま動く。
    duration_from_format_only: bool = False
    # side_data_list から読んだ表示回転（0/90/180/270）。90/270 のときは
    # width/height は probe() の時点ですでに入れ替え済み（＝実際にデコード
    # される表示向きのサイズ）。ここには「入れ替えた」という事実だけを残す。
    rotation: int = 0

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
        """duration や nb_frames からの推定フレーム数。あくまで見積り。

        nb_frames が無く、映像ストリーム自身の duration も無い場合は、
        コンテナ全体の duration（音声を含む最大長）にフォールバックする。
        これは音声コーデックの priming/padding の分だけ映像の実尺より
        長く出ることがある（mkv+AAC/MP3 で実測: 実150フレームに対し
        round(duration*fps)=151）。ここで round ではなく切り捨てるのは、
        その水増し分をできるだけ吸収して「弾く」判断の誤爆を減らすため。
        それでも完全には信用できないので、判定に使ってよいかは
        estimated_frames_reliable() で別途確認すること。
        """
        if self.nb_frames:
            return self.nb_frames
        if self.duration:
            if self.duration_from_format_only:
                return int(self.duration * self.fps)  # 切り捨て
            return int(round(self.duration * self.fps))
        return None

    def estimated_frames_reliable(self) -> bool:
        """estimated_frames() を「これより短ければ弾く」判断の根拠にしてよいか。

        nb_frames はコンテナのヘッダ由来でほぼ正確。映像ストリーム自身の
        duration も同様に信用できる。どちらも無くコンテナ全体の duration
        （音声込みの最大長）にフォールバックした場合だけ False になる。
        mkv や webm はコンテナ内に per-stream の duration / nb_frames を
        持たないことが多く、この状態の推定値だけを根拠に検出結果を
        「実尺より短い」と決めつけると、健全な検出結果まで弾いてしまう。
        """
        if self.nb_frames:
            return True
        return bool(self.duration) and not self.duration_from_format_only


def _display_rotation(video: dict) -> int:
    """side_data_list から表示回転を読み、0/90/180/270 に正規化する。

    ffmpeg は再生時にこの回転を自動で適用して（既定で -autorotate 有効）、
    デコード後のフレームは回転済み・転置済みのものが出てくる。90/270 度では
    幅と高さが入れ替わって出るのに probe() がここを見ていないと、申告される
    width/height と実際にデコードされるフレームの縦横が食い違う。バイト数が
    たまたま一致すると（正方形に近い解像度など）長さ検査を素通りし、
    reshape だけが転置されて全フレームが斜めに裂けたスクランブルになる
    （このリポジトリでの実測: 640x360 に 90度回転を付けた素材で再現）。

    side_data_list を持たない（回転メタデータ無し）動画は 0 を返す。
    45度刻みでない・カメラ機器が書く古い形式の rotate タグのみで
    side_data_list が無い場合は、ここでは検出できない（未確認・別途要対応）。
    """
    for sd in video.get("side_data_list") or []:
        if sd.get("side_data_type") == "Display Matrix" and "rotation" in sd:
            try:
                deg = round(float(sd["rotation"]))
            except (TypeError, ValueError):
                continue
            norm = deg % 360
            if norm in (0, 90, 180, 270):
                return norm
    return 0


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
    # 映像ストリーム自身の duration を優先する。無い場合だけコンテナ全体の
    # duration（音声を含む最大長）に落ちる。落ちたかどうかは
    # duration_from_format_only として記録し、見積りの信用度判定に使う。
    stream_duration = video.get("duration")
    format_duration = data.get("format", {}).get("duration")
    duration = stream_duration or format_duration

    width = int(video["width"])
    height = int(video["height"])
    rotation = _display_rotation(video)
    if rotation in (90, 270):
        # ffmpeg はデコード時にこの回転を自動で適用し、90/270度では
        # 幅と高さを入れ替えたフレームを流す。width/height はここで
        # 実際にデコードされる向きに合わせておく。以降のコード（パス1の
        # scale_back、パス2の FrameBuffer など）は全部この値を前提に
        # 動くので、ここで揃えないと検出座標もフレーム内容もずれる。
        width, height = height, width

    return VideoInfo(
        width=width,
        height=height,
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
        duration_from_format_only=not stream_duration and bool(format_duration),
        rotation=rotation,
    )


def detect_pix_fmt(info: VideoInfo) -> str:
    """処理に使う raw のピクセルフォーマット。10bit は維持する。"""
    if info.pix_fmt.endswith(("10le", "10be")):
        return "yuv420p10le"
    return "yuv420p"


#: scale の force_divisible_by。奇数サイズを出させないために明示する
DETECTION_DIVISIBLE_BY = 2


def detection_scale_filter(infer_size: int) -> str:
    """パス1のデコードに使う scale フィルタ。

    force_divisible_by を明示する。既定値は 1 で、その場合 ffmpeg は奇数サイズも出す。
    実出力サイズの計算式は force_divisible_by を含む形なので、こことサイズ計算・実測は
    必ず同じフィルタ文字列を使うこと。
    """
    return (
        f"scale=w={infer_size}:h={infer_size}"
        ":force_original_aspect_ratio=decrease"
        f":force_divisible_by={DETECTION_DIVISIBLE_BY}"
        ":flags=bicubic"
    )


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
        "-vf", detection_scale_filter(infer_size),
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
    ]
    if limit_frames:
        cmd += ["-frames:v", str(limit_frames)]
    cmd += ["-"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _av_rescale(a: int, b: int, c: int) -> int:
    """ffmpeg の av_rescale(a, b, c)。四捨五入（0.5 は絶対値の大きい側）。"""
    return (a * b + c // 2) // c


def computed_detection_frame_size(info: VideoInfo, infer_size: int) -> tuple[int, int]:
    """scale の出力サイズを ffmpeg の ff_scale_adjust_dimensions と同じ式で求める。

    force_original_aspect_ratio=decrease のとき ffmpeg は、
    「force_divisible_by で割った上で四捨五入し、掛け戻す」= 2の倍数への四捨五入 をする。
    切り捨てでも切り上げでもないので、単純な int() では合わない解像度が多数ある
    （854x480 や 720x480 で実測ずれ）。指定した長辺そのものは最後に切り捨てられる。

    infer_size と入力の縦横がどちらも奇数という組み合わせだけは ffmpeg と 2px ずれる。
    実際に読むサイズは measure_detection_frame_size() の実測を使うこと。
    """
    div = DETECTION_DIVISIBLE_BY
    w = h = infer_size
    tmp_w = _av_rescale(h, info.width, info.height * div) * div
    tmp_h = _av_rescale(w, info.height, info.width * div) * div
    w = min(tmp_w, w) // div * div
    h = min(tmp_h, h) // div * div
    return max(div, w), max(div, h)


def measure_detection_frame_size(path: str, infer_size: int) -> tuple[int, int]:
    """scale 後のサイズを ffmpeg 自身に1フレーム出させて実測する。

    パス1は dec_w * dec_h * 3 バイト単位でパイプを読むので、1バイトでもずれると
    以降の全フレームが前フレームと混ざった斜めの画像になり、しかも例外が出ない。
    ffmpeg 側の丸め方はバージョンで変わりうるため、計算に頼らず実物を測る。
    """
    ffmpeg = _require("ffmpeg")
    out = subprocess.run(
        [
            ffmpeg,
            "-v", "error",
            "-i", path,
            "-vf", detection_scale_filter(infer_size),
            "-frames:v", "1",
            "-f", "image2",
            "-c:v", "png",
            "-",
        ],
        capture_output=True,
    )
    data = out.stdout
    pos = data.find(b"IHDR")
    if pos < 0 or len(data) < pos + 12:
        err = out.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(
            f"デコードサイズの実測に失敗しました（{path}）:\n{err}"
        )
    w, h = struct.unpack(">II", data[pos + 4 : pos + 12])
    return int(w), int(h)


def detection_frame_size(
    info: VideoInfo, infer_size: int, path: str | None = None
) -> tuple[int, int]:
    """パス1のデコード後サイズ。

    path を渡した場合は ffmpeg に実際に出させたサイズを返す（こちらが正）。
    計算値と食い違ったら警告する。黙って食い違いを飲み込むと全フレームがずれる。
    """
    computed = computed_detection_frame_size(info, infer_size)
    if path is None:
        return computed
    measured = measure_detection_frame_size(path, infer_size)
    if measured != computed:
        print(
            f"警告: デコードサイズの計算値 {computed[0]}x{computed[1]} が"
            f" ffmpeg の実出力 {measured[0]}x{measured[1]} と一致しません。"
            "実測値を使います（automosaic.video の計算式が ffmpeg の版とずれています）。",
            file=sys.stderr,
        )
    return measured


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
