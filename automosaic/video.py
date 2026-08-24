"""ffmpeg / ffprobe のラッパー。

方針は docs/01-technical-design.md の「手法E」。
Python 側でフレームを合成し rawvideo を ffmpeg に pipe する。中間ファイルは作らない。
音声・字幕・チャプタ・メタデータは、元ファイルを2番目の入力として渡して
そこから stream copy することで無劣化のまま持ってくる。
"""

from __future__ import annotations

import json
import os
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


def check_render_geometry(info: VideoInfo) -> str | None:
    """パス2（描画）まで通せる解像度かどうかを検査する。通せないなら理由の
    メッセージを返す。通せるなら None を返す。

    奇数解像度は2枚の壁で原理的に描画できない（issue #2 実測）。
      1. ffmpeg の 4:2:0 彩度平面は ceil(w/2) x ceil(h/2) だが、
         `render.FrameBuffer` は width//2（切り捨て）で組んでいるため、
         guard を外してもバイト数がずれて全フレームが斜めに壊れる
         （guard を外すという直し方は採らない）。
      2. libx264 は 4:2:0 の奇数解像度を 8bit/10bit とも拒否するため、
         奇数のまま焼く経路はそもそも存在しない。

    一方でパス1（検出）は奇数解像度でも正しく動く（scale フィルタが
    force_divisible_by で偶数に丸めてから読むだけ）。そのため検出だけに
    数時間かけたあとパス2で初めて落ちる、という壊れ方をする
    （--detect-only は通るのに描画で落ちる）。呼び出し側は probe 直後に
    これを呼び、--detect-only のときは警告に留めてパス1へ進めてよい。
    """
    if info.width % 2 or info.height % 2:
        return (
            f"入力の解像度が奇数です（{info.width}x{info.height}）。"
            "モザイクの描画（パス2）は 4:2:0 の偶数解像度が前提で、"
            "この解像度のまま焼く経路は存在しません"
            "（libx264 が 4:2:0 の奇数解像度を拒否するため）。"
        )
    return None


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


def _measure_png_frame_size(
    stdout: bytes, stderr: bytes, fail_prefix: str
) -> tuple[int, int]:
    """ffmpeg に `-f image2 -c:v png` で吐かせた1枚の PNG の IHDR からサイズを読む。

    measure_detection_frame_size（`-vf` あり）と measure_full_frame_size
    （`-vf` なし）の唯一の違いはデコードコマンドの `-vf` の有無で、
    IHDR の解析は完全に同一だった（PR #47 のレビュー指摘）。ここに1つに
    まとめる。将来どちらかだけ直して片方が古いままになる事故を防ぐ。
    """
    pos = stdout.find(b"IHDR")
    if pos < 0 or len(stdout) < pos + 12:
        err = stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"{fail_prefix}:\n{err}")
    w, h = struct.unpack(">II", stdout[pos + 4 : pos + 12])
    return int(w), int(h)


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
    return _measure_png_frame_size(
        out.stdout, out.stderr, f"デコードサイズの実測に失敗しました（{path}）"
    )


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


def measure_full_frame_size(path: str) -> tuple[int, int]:
    """パス2用。原寸デコードの実際のサイズを ffmpeg 自身に1フレーム出させて実測する。

    open_full_reader と同じ入力・同じデフォルトの自動回転挙動で先頭フレームを
    デコードし、PNG の IHDR からサイズを読む。probe() が申告する width/height
    と一致するかどうかは verify_full_frame_size() で突き合わせる。
    """
    ffmpeg = _require("ffmpeg")
    out = subprocess.run(
        [
            ffmpeg,
            "-v", "error",
            "-i", path,
            "-frames:v", "1",
            "-f", "image2",
            "-c:v", "png",
            "-",
        ],
        capture_output=True,
    )
    return _measure_png_frame_size(
        out.stdout, out.stderr, f"パス2のデコードサイズの実測に失敗しました（{path}）"
    )


def verify_full_frame_size(info: VideoInfo, path: str) -> None:
    """probe() の申告サイズと ffmpeg の実デコード出力を突き合わせる。

    issue #32: `FrameBuffer` の長さ検査（`y_size = width*height` ほか）は
    width と height を入れ替えても値が変わらない対称式なので、probe() と
    ffmpeg の実デコードが別経路でずれても検査を素通りし、reshape だけが
    転置されて全フレームが斜めに裂けたスクランブル画像になる（issue #1 で
    塞いだ回転メタデータの経路がまさにこれだった）。パス1の
    detection_frame_size() は食い違いを警告して実測値へフォールバックするが、
    ここが食い違うのはフォールバックする根拠が無い（FrameBuffer をどちらの
    サイズで作っても、以降の座標系のどこかがずれる）ので、警告ではなく
    例外で止める。

    issue #68: 呼び出し場所は2か所ある。probe() 直後（cli.py の main()、
    パス1に入る前）と run_render() の先頭（パス2の reader/writer を開く前）。
    前者は実素材で3.2fps・約4.8時間かかるパス1を無駄にする前に止めるため、
    後者はパス1の間に入力ファイルが差し替わるなど状況が変わりうるために
    残してある。判明済みの発生経路は、回転メタデータの未知パターン・
    映像ストリームが複数あるコンテナで ffmpeg の既定選択と probe() の選択が
    ずれる場合・先頭フレームが壊れているファイルなど（PR #47 の独立検証で
    実測）。
    """
    measured = measure_full_frame_size(path)
    if measured != (info.width, info.height):
        raise RuntimeError(
            f"probe の申告サイズ {info.width}x{info.height} が"
            f" ffmpeg の実デコード出力 {measured[0]}x{measured[1]} と"
            "一致しません（issue #32）。FrameBuffer の長さ検査は幅と高さの"
            "入れ替えに対して不変なため、ここで止めないと例外もエラーも"
            "出ないまま全フレームが斜めに裂けたスクランブル画像がモザイクの"
            "描画（パス2）から exit 0 で出ます。この入力は今のところ処理できません"
            "（原因の実例: 回転メタデータの未知パターン、映像ストリームが複数ある"
            "コンテナでのストリーム選択のずれ、先頭フレームが壊れたファイル）。"
            "ffprobe -show_streams の出力を添えて調査を依頼してください。"
        )


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


def _writer_cmd(
    src_path: str,
    dst_path: str,
    info: VideoInfo,
    pix_fmt: str,
    crf: int,
    preset: str,
    limit_frames: int | None,
    zero_frames: bool = False,
) -> list[str]:
    """本番の writer と preflight の writer、両方が同じコマンドを組み立てるための共通部。

    preflight（zero_frames=True）だけを別のコマンドで代用すると、preflight を
    通っても本番側だけコンテナ/コーデックの組み合わせが違うということが起こりうる。
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

    if zero_frames:
        # 出力尺を0にする。付けないと、映像フレームを1枚も渡していなくても
        # 音声・字幕はコンテナの尺なりに丸ごとコピーされてしまい、preflight のはずが
        # 長尺素材で本番同然の時間がかかる（実測で確認済み）。
        cmd += ["-t", "0"]

    cmd += [dst_path]
    return cmd


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
    cmd = _writer_cmd(src_path, dst_path, info, pix_fmt, crf, preset, limit_frames)
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)


# --------------------------------------------------------------------------
# 確認用プロキシ動画（issue #18）
# --------------------------------------------------------------------------

#: 長辺のピクセル数。実測（30分・1920x1080素材、data/library の実ジョブ）:
#: 640px 全Iフレームで 270MB（実測 2m15s）。現行の /frame（1枚JPEG）を
#: 55,303枚集めると 7.2GB になるのに対し、この規模まで落ちる。
PROXY_LONG_EDGE = 640
PROXY_CRF = 26
PROXY_PRESET = "veryfast"
#: 全フレームをIフレームにする。シークのたびに直前のキーフレームまで
#: 遡ってデコードする必要が無くなり、どのフレームへの移動も1枚のデコードで
#: 済む（issue #18 の実測: 640px 全Iフレームの55,303フレームを 7.43秒で
#: 全デコード = 約7,437fps）。GOPを開けるとサイズは1/3程度に縮むが、
#: シークのたびにGOP先頭まで遡ってデコードする経路が発生する。#19
#: （フレーム厳密なコマ送り）の土台にするため、既定は確実な全Iフレームにする。
PROXY_GOP = 1


def proxy_scale_filter(long_edge: int = PROXY_LONG_EDGE) -> str:
    """プロキシ用の縮小フィルタ。縦横どちらが長辺でも long_edge に収める。

    detection_scale_filter と同じ形（force_original_aspect_ratio=decrease +
    force_divisible_by）にしてあるのは、libx264 が偶数サイズしか受けないため。
    """
    return (
        f"scale=w={long_edge}:h={long_edge}"
        ":force_original_aspect_ratio=decrease"
        f":force_divisible_by={DETECTION_DIVISIBLE_BY}"
        ":flags=bicubic"
    )


def generate_proxy(
    src_path: str,
    dst_path: str,
    long_edge: int = PROXY_LONG_EDGE,
    gop: int = PROXY_GOP,
    crf: int = PROXY_CRF,
    preset: str = PROXY_PRESET,
) -> None:
    """確認用プロキシ動画を作る。1本のffmpeg呼び出しで完結させる。

    src_path には必ず焼き上がった output.mp4（モザイク済み）を渡すこと。
    原画から作ると、モザイクをかける前のフレームがプロキシという形で
    端末のディスクに残ってしまい、「原画を端末に残さない」という前提
    （webapp/app.py の /frame が raw=1 のときキャッシュさせない理由と同じ）が崩れる。

    h264_amf は -g 1 を受け付けない（実機で確認: Task finished with error
    code: -22）ため、常に libx264 を使う。速度差は実測でほぼ無い。

    失敗時は中間ファイル（.tmp）を残さない。中断された動画が「プロキシが
    ある」ように見えると、シークしたときだけ壊れて気づく道具になる。
    """
    ffmpeg = _require("ffmpeg")
    tmp = dst_path + ".tmp"
    cmd = [
        ffmpeg,
        "-v", "error",
        "-y",
        "-i", src_path,
        "-vf", proxy_scale_filter(long_edge),
        "-an",
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-g", str(max(1, gop)),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        # 出力先は "proxy.mp4.tmp"（拡張子が .tmp）。ffmpeg は出力形式を
        # ファイル名の拡張子から推定するので、.tmp のままだと
        # 「形式を選べない」で失敗する（実測）。-f で明示する
        "-f", "mp4",
        tmp,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True)
    except OSError as e:
        raise RuntimeError(f"プロキシ生成を起動できません: {e}")
    if proc.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"プロキシ生成に失敗しました（ffmpeg 終了コード {proc.returncode}）:\n{err}")
    os.replace(tmp, dst_path)


def nb_frames(path: str) -> int | None:
    """コンテナのヘッダから読めるフレーム数だけを返す（全デコードしない）。

    mp4 の nb_frames はコンテナのヘッダ（stss/stsz）由来で、フルデコードせず
    速く読める。実測（このリポジトリの実ジョブ、55,303フレーム）:
    ヘッダ読み取りだけで 0.1秒、ffmpeg 側の全デコードで数えた値と完全一致した。
    プロキシとoutput.mp4のフレーム数照合はこちらを使う。
    """
    return probe(path).nb_frames


def preflight_writer(
    src_path: str,
    dst_path: str,
    info: VideoInfo,
    pix_fmt: str,
    crf: int = 16,
    preset: str = "slow",
    limit_frames: int | None = None,
) -> tuple[bool, str]:
    """本番と同じ writer コマンドを0フレームだけ実行し、ヘッダ書き込み時点で
    分かる失敗（字幕・添付のコーデックがコンテナに非対応、出力先の書き込み権限
    など）を、時間のかかる検出・描画に入る前に見つける。

    limit_frames は本番の open_writer に渡すのと同じ値を渡すこと。指定の有無で
    音声・字幕・添付をマッピングするかどうかが変わるので、揃えないと preflight が
    本番と違う組み合わせをテストしてしまう。

    stdin には何も渡さない（rawvideo は -s/-pix_fmt/-r で仕様が確定しているので
    ffmpeg はヘッダ確定に実データを必要としない）。所要は実測 0.1 秒未満。
    戻り値は (成功したか, stderr の生テキスト)。
    """
    cmd = _writer_cmd(
        src_path, dst_path, info, pix_fmt, crf, preset, limit_frames, zero_frames=True
    )
    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    ok = proc.returncode in (0, None)
    return ok, proc.stderr.decode("utf-8", "replace")
