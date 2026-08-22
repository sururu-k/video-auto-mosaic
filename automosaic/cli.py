"""CLI。動画を投げるとモザイクをかけて出す。

2パス構成:
  パス1  全フレームを検出し、座標だけを JSON に持つ（縮小フレームで推論）
  パス2  原寸 YUV を読み直してモザイクを描画し、ffmpeg に書き戻す
中間フレームをディスクに展開しないので、長尺でもディスクを食わない。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

import numpy as np

from . import __version__
from .detector import (
    CONSERVATIVE_CLASSES,
    DEFAULT_CLASSES,
    Detection,
    Detector,
    available_providers,
)
from .render import FrameBuffer, apply_regions, default_block_size
from .temporal import TemporalConfig, process, review_flags
from . import video as vid


def _drain(pipe, sink: list) -> threading.Thread:
    """ffmpeg の stderr を読み捨てる。放置するとバッファが詰まって固まる。"""

    def run():
        try:
            for line in iter(pipe.readline, b""):
                sink.append(line.decode("utf-8", "replace").rstrip())
        except Exception:
            pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _fmt_eta(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


class Progress:
    def __init__(self, total: int | None, label: str) -> None:
        self.total = total
        self.label = label
        self.start = time.time()
        self.last = 0.0

    def update(self, n: int, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last < 0.5:
            return
        self.last = now
        elapsed = now - self.start
        rate = n / elapsed if elapsed > 0 else 0.0
        if self.total:
            pct = 100.0 * n / self.total
            eta = (self.total - n) / rate if rate > 0 else 0
            msg = (
                f"\r{self.label} {n}/{self.total} ({pct:5.1f}%) "
                f"{rate:6.1f} fps  残り {_fmt_eta(eta)}   "
            )
        else:
            msg = f"\r{self.label} {n} フレーム  {rate:6.1f} fps   "
        sys.stderr.write(msg)
        sys.stderr.flush()

    def done(self, n: int) -> None:
        self.update(n, force=True)
        sys.stderr.write("\n")
        sys.stderr.flush()


def run_detection(
    src: str,
    info: vid.VideoInfo,
    det: Detector,
    infer_size: int,
    limit_frames: int | None,
    quiet: bool,
    frame_step: int = 1,
) -> tuple[dict[int, list[Detection]], int]:
    dec_w, dec_h = vid.detection_frame_size(info, infer_size)
    frame_bytes = dec_w * dec_h * 3
    # 正方フレーム座標(infer_size) -> 元動画座標 の倍率
    scale_back = info.width / dec_w * infer_size

    proc = vid.open_detection_reader(src, infer_size, limit_frames)
    err: list[str] = []
    _drain(proc.stderr, err)

    total = info.estimated_frames()
    if limit_frames:
        total = min(total, limit_frames) if total else limit_frames
    prog = None if quiet else Progress(total, "パス1 検出")

    square = np.zeros((infer_size, infer_size, 3), dtype=np.uint8)
    per_frame: dict[int, list[Detection]] = {}
    idx = 0
    try:
        while True:
            raw = proc.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(dec_h, dec_w, 3)
            if frame_step <= 1 or idx % frame_step == 0:
                # 右下パディングで正方に（nudenet の前処理と同じ規約）
                square[:] = 0
                square[:dec_h, :dec_w] = frame
                per_frame[idx] = det.detect_square(square, scale_back)
            else:
                # 間引いたフレームは空にしておく。補間とmemoryが埋める。
                per_frame[idx] = []
            idx += 1
            if prog:
                prog.update(idx)
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()

    if prog:
        prog.done(idx)
    if proc.returncode not in (0, None) and idx == 0:
        raise RuntimeError("デコードに失敗しました:\n" + "\n".join(err[-10:]))
    return per_frame, idx


def run_render(
    src: str,
    dst: str,
    info: vid.VideoInfo,
    regions_per_frame: dict,
    n_frames: int,
    block: int,
    mode: str,
    crf: int,
    preset: str,
    limit_frames: int | None,
    quiet: bool,
) -> None:
    pix_fmt = vid.detect_pix_fmt(info)
    ten_bit = pix_fmt.endswith("10le")
    fb = FrameBuffer(info.width, info.height, ten_bit=ten_bit)

    reader = vid.open_full_reader(src, pix_fmt, limit_frames)
    writer = vid.open_writer(src, dst, info, pix_fmt, crf, preset, limit_frames)
    rerr: list[str] = []
    werr: list[str] = []
    _drain(reader.stderr, rerr)
    _drain(writer.stderr, werr)

    total = min(n_frames, limit_frames) if limit_frames else n_frames
    prog = None if quiet else Progress(total, "パス2 描画")

    idx = 0
    last_boxes: list = []
    try:
        while True:
            raw = reader.stdout.read(fb.nbytes)
            if len(raw) < fb.nbytes:
                break
            y, u, v = fb.wrap(raw)

            if idx < n_frames:
                boxes = [b for b, _ in regions_per_frame.get(idx, [])]
                last_boxes = boxes
            else:
                # パス1より長い場合。素通しはしない（判断できない = 潰す）
                boxes = last_boxes

            apply_regions(y, u, v, boxes, block, mode=mode, ten_bit=ten_bit)
            writer.stdin.write(fb.pack(y, u, v))
            idx += 1
            if prog:
                prog.update(idx)
    finally:
        try:
            reader.stdout.close()
        except Exception:
            pass
        reader.wait()
        try:
            writer.stdin.close()
        except Exception:
            pass
        writer.wait()

    if prog:
        prog.done(idx)
    if writer.returncode not in (0, None):
        raise RuntimeError("エンコードに失敗しました:\n" + "\n".join(werr[-15:]))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="automosaic",
        description="実写動画の局部を自動検出してモザイクをかける",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("input", nargs="?", help="入力動画")
    p.add_argument("-o", "--output", help="出力動画（既定: <入力>_mosaic.mp4）")

    g = p.add_argument_group("検出")
    g.add_argument(
        "--model",
        default=os.path.join(os.path.dirname(os.path.dirname(__file__)), "weights", "640m.onnx"),
        help="ONNX モデル",
    )
    g.add_argument("--infer-size", type=int, default=640, help="推論解像度")
    g.add_argument(
        "--conf",
        type=float,
        default=0.12,
        help="信頼度しきい値。Recall 優先なので既定より下げてある",
    )
    g.add_argument("--nms-iou", type=float, default=0.45, help="NMS の IoU しきい値")
    g.add_argument(
        "--classes",
        default="default",
        help="default(露出のみ) / conservative(COVERED も含む) / カンマ区切りの明示指定",
    )
    g.add_argument(
        "--provider",
        default="auto",
        choices=["auto", "cpu", "dml", "cuda"],
        help="ONNX Runtime の実行プロバイダ",
    )
    g.add_argument("--threads", type=int, default=0, help="CPU 推論のスレッド数（0で自動）")
    g.add_argument("--device-id", type=int, default=1, help="DirectML のアダプタ番号")
    g.add_argument(
        "--frame-step",
        type=int,
        default=1,
        help="Nフレームおきに検出する。間は補間で埋める。速度と引き換えに、"
        "数フレームしか映らない対象を取りこぼす危険が上がる",
    )

    t = p.add_argument_group("時間方向")
    t.add_argument("--max-gap", type=int, default=12, help="トラック継続を許す欠損フレーム数")
    t.add_argument("--memory", type=int, default=6, help="トラック端を前後に保持するフレーム数")
    t.add_argument("--margin-scale", type=float, default=1.0, help="膨張マージンの倍率")
    t.add_argument(
        "--max-area-ratio",
        type=float,
        default=0.35,
        help="フレーム面積に対するこの比を超える検出は誤検出として落とす",
    )
    t.add_argument("--no-despike", action="store_true", help="デスパイクを無効にする")
    t.add_argument(
        "--bridge-max",
        type=int,
        default=150,
        help="前後が覆われている未処理区間を埋める最大フレーム数",
    )
    t.add_argument(
        "--no-bridge",
        action="store_true",
        help="未処理区間の穴埋めを無効にする（素通しフレームが残る）",
    )

    r = p.add_argument_group("描画・出力")
    r.add_argument("--block", type=int, default=0, help="モザイクのブロックサイズ px（0で自動）")
    r.add_argument("--mode", default="pixelize", choices=["pixelize", "black"])
    r.add_argument("--crf", type=int, default=16, help="x264 CRF。16〜18 が視覚的に無劣化")
    r.add_argument("--preset", default="slow", help="x264 preset")

    m = p.add_argument_group("運用")
    m.add_argument("--detections", help="検出結果 JSON の保存先／再利用元")
    m.add_argument(
        "--reuse-detections",
        action="store_true",
        help="--detections の JSON があればパス1を飛ばす",
    )
    m.add_argument("--report", help="統計とレビュー対象フレームの JSON 出力先")
    m.add_argument("--limit-frames", type=int, help="先頭 N フレームだけ処理（動作確認用）")
    m.add_argument("--detect-only", action="store_true", help="パス1だけ実行して統計を出す")
    m.add_argument("--quiet", action="store_true")
    m.add_argument("--list-providers", action="store_true", help="利用可能なプロバイダを表示して終了")
    m.add_argument("--version", action="version", version=f"automosaic {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_providers:
        print("利用可能なプロバイダ:")
        for p in available_providers():
            print(f"  {p}")
        return 0

    if not args.input:
        build_parser().error("入力動画を指定してください")

    src = args.input
    if not os.path.exists(src):
        print(f"入力が見つかりません: {src}", file=sys.stderr)
        return 1

    dst = args.output
    if not dst:
        stem, _ = os.path.splitext(src)
        dst = f"{stem}_mosaic.mp4"
    if os.path.abspath(dst) == os.path.abspath(src):
        print("出力が入力と同じパスです。上書きは行いません。", file=sys.stderr)
        return 1

    if args.classes == "default":
        classes = set(DEFAULT_CLASSES)
    elif args.classes == "conservative":
        classes = set(CONSERVATIVE_CLASSES)
    else:
        classes = {c.strip() for c in args.classes.split(",") if c.strip()}

    info = vid.probe(src)
    block = args.block or default_block_size(info.long_edge)

    if not args.quiet:
        print(f"入力      {src}")
        print(f"          {info.width}x{info.height}  {info.fps:.3f} fps  {info.pix_fmt}")
        est = info.estimated_frames()
        print(f"          推定 {est} フレーム" if est else "          フレーム数不明")
        print(f"対象クラス {', '.join(sorted(classes))}")
        print(f"ブロック   {block} px  モード {args.mode}")

    # ---- パス1: 検出 ----
    per_frame: dict[int, list[Detection]] = {}
    n_frames = 0
    loaded = False

    if args.reuse_detections and args.detections and os.path.exists(args.detections):
        with open(args.detections, encoding="utf-8") as f:
            data = json.load(f)
        n_frames = data["n_frames"]
        per_frame = {
            int(k): [Detection.from_dict(d) for d in v]
            for k, v in data["detections"].items()
        }
        loaded = True
        if not args.quiet:
            print(f"検出結果を再利用: {args.detections}（{n_frames} フレーム）")

    if not loaded:
        det = Detector(
            model_path=args.model,
            infer_size=args.infer_size,
            conf=args.conf,
            nms_iou=args.nms_iou,
            provider=args.provider,
            intra_threads=args.threads,
            device_id=args.device_id,
        )
        if not args.quiet:
            print(f"プロバイダ {det.active_provider}")
        per_frame, n_frames = run_detection(
            src, info, det, args.infer_size, args.limit_frames, args.quiet,
            frame_step=max(1, args.frame_step),
        )
        if args.detections:
            os.makedirs(os.path.dirname(os.path.abspath(args.detections)), exist_ok=True)
            with open(args.detections, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "n_frames": n_frames,
                        "width": info.width,
                        "height": info.height,
                        "detections": {
                            str(k): [d.as_dict() for d in v]
                            for k, v in per_frame.items()
                            if v
                        },
                    },
                    f,
                    ensure_ascii=False,
                )
            if not args.quiet:
                print(f"検出結果を保存: {args.detections}")

    if n_frames == 0:
        print("フレームを1枚も読めませんでした。", file=sys.stderr)
        return 1

    # ---- 時間方向の安定化 ----
    cfg = TemporalConfig(
        max_gap=args.max_gap,
        memory=args.memory,
        margin_scale=args.margin_scale,
        max_area_ratio=args.max_area_ratio,
        min_track_len=0 if args.no_despike else 2,
        bridge_max=0 if args.no_bridge else args.bridge_max,
        frame_step=max(1, args.frame_step),
    )
    regions, stats = process(
        per_frame, n_frames, info.width, info.height, classes, cfg
    )
    left_open = stats.pop("_left_open", [])

    if not args.quiet:
        print("\n[検出統計]")
        for k, v in stats.items():
            print(f"  {k:34s} {v}")
        covered = stats["frames_with_mosaic"]
        print(f"  モザイク適用率{'':21s} {100.0 * covered / n_frames:.1f}%")

    # 埋めなかった未処理区間は必ず表に出す。黙って素通しにしない。
    if left_open and not args.quiet:
        fps = info.fps
        print(f"\n[未処理のまま残った区間 {len(left_open)} 件]")
        for start, end in left_open[:20]:
            print(
                f"  frame {start:>7}-{end - 1:<7} "
                f"({start / fps:7.2f}s - {(end - 1) / fps:7.2f}s)  {end - start} フレーム"
            )
        if len(left_open) > 20:
            print(f"  ... 他 {len(left_open) - 20} 件")

    flags = review_flags(regions, n_frames)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "stats": stats,
                    "uncovered_ranges": [
                        {
                            "start_frame": s,
                            "end_frame": e - 1,
                            "start_sec": round(s / info.fps, 3),
                            "end_sec": round((e - 1) / info.fps, 3),
                            "frames": e - s,
                        }
                        for s, e in left_open
                    ],
                    "review_frames": flags,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        if not args.quiet:
            print(f"\nレポートを保存: {args.report}（要レビュー {len(flags)} フレーム）")

    if args.detect_only:
        return 0

    # ---- パス2: 描画 ----
    run_render(
        src,
        dst,
        info,
        regions,
        n_frames,
        block,
        args.mode,
        args.crf,
        args.preset,
        args.limit_frames,
        args.quiet,
    )

    if not args.quiet:
        size_mb = os.path.getsize(dst) / (1024 * 1024)
        print(f"\n出力      {dst}  ({size_mb:.1f} MB)")
        print(
            "\n出力は必ず目視確認してください。"
            "検出漏れは自動では保証されません。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
