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
from . import corrections as corr
from .render import FrameBuffer, apply_regions, default_block_size
from .temporal import TemporalConfig, estimated_only_ranges, process, review_flags
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


def save_detections(
    path: str,
    per_frame: dict[int, list[Detection]],
    n_frames: int,
    info: vid.VideoInfo,
    complete: bool = True,
) -> None:
    """検出結果を保存する。書き込み途中で落ちても壊れないよう一時ファイル経由で置換する。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {
                "n_frames": n_frames,
                "width": info.width,
                "height": info.height,
                "complete": complete,
                "detections": {
                    str(k): [d.as_dict() for d in v] for k, v in per_frame.items() if v
                },
            },
            f,
            ensure_ascii=False,
        )
    os.replace(tmp, path)


def load_partial(path: str) -> tuple[dict[int, list[Detection]], int]:
    """途中保存された検出結果を読む。戻り値は (検出, 済んだフレーム数)。"""
    if not path or not os.path.exists(path):
        return {}, 0
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    per_frame = {
        int(k): [Detection.from_dict(x) for x in v] for k, v in d["detections"].items()
    }
    return per_frame, int(d.get("n_frames", 0))


def run_detection(
    src: str,
    info: vid.VideoInfo,
    det: Detector,
    detect_scale: int,
    limit_frames: int | None,
    quiet: bool,
    frame_step: int = 1,
    tta: bool = False,
    tiles: int = 1,
    checkpoint_path: str | None = None,
    checkpoint_every: int = 2000,
    resume_from: int = 0,
    resume_dets: dict[int, list[Detection]] | None = None,
) -> tuple[dict[int, list[Detection]], int]:
    dec_w, dec_h = vid.detection_frame_size(info, detect_scale)
    frame_bytes = dec_w * dec_h * 3
    # デコード後フレーム座標 -> 元動画座標 の倍率
    scale_back = info.width / dec_w

    proc = vid.open_detection_reader(src, detect_scale, limit_frames)
    err: list[str] = []
    _drain(proc.stderr, err)

    total = info.estimated_frames()
    if limit_frames:
        total = min(total, limit_frames) if total else limit_frames
    prog = None if quiet else Progress(total, "パス1 検出")

    per_frame: dict[int, list[Detection]] = dict(resume_dets or {})
    idx = 0
    try:
        while True:
            raw = proc.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            # 再開時は、済んだフレームのデコードだけ流して推論を飛ばす。
            # デコードは推論に比べて桁違いに安いので、正確なシークを作るより
            # 読み飛ばすほうが簡単で確実。
            if idx < resume_from:
                idx += 1
                if prog:
                    prog.update(idx)
                continue
            if frame_step <= 1 or idx % frame_step == 0:
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(dec_h, dec_w, 3)
                dets = det.detect_frame(frame, tta=tta, tiles=tiles)
                per_frame[idx] = [
                    Detection(
                        d.cls,
                        d.score,
                        (
                            int(round(d.box[0] * scale_back)),
                            int(round(d.box[1] * scale_back)),
                            int(round(d.box[2] * scale_back)),
                            int(round(d.box[3] * scale_back)),
                        ),
                    )
                    for d in dets
                ]
            else:
                # 間引いたフレームは空にしておく。補間とmemoryが埋める。
                per_frame[idx] = []
            idx += 1
            if prog:
                prog.update(idx)
            # 途中保存。長尺は数時間かかるので、落ちたときに全損しないようにする
            if checkpoint_path and checkpoint_every > 0 and idx % checkpoint_every == 0:
                save_detections(checkpoint_path, per_frame, idx, info, complete=False)
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()

    if prog:
        prog.done(idx)
    if proc.returncode not in (0, None) and idx == 0:
        raise RuntimeError('デコードに失敗しました:\n' + '\n'.join(err[-10:]))
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
    g.add_argument(
        "--infer-size",
        type=int,
        default=960,
        help="推論解像度。実素材で 640->960 にすると検出フレームが約4割増えた。"
        "1280 まで上げても伸びはわずかで時間が1.7倍になる",
    )
    g.add_argument(
        "--conf",
        type=float,
        default=0.06,
        help="信頼度しきい値。このモデルは実写でスコアが低く出るため大きく下げてある。"
        "既定の 0.2 相当だと実素材で半分近く取りこぼす",
    )
    g.add_argument("--nms-iou", type=float, default=0.45, help="重複判定の IoU しきい値")
    g.add_argument(
        "--merge",
        default="union",
        choices=["union", "nms"],
        help="重複検出のまとめ方。union は外接矩形に統合（被覆を減らさない）、"
        "nms は最高スコアの1個だけ残す",
    )
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
        "--tta",
        action="store_true",
        help="水平反転した推論結果もマージする。推論回数2倍。取りこぼしが減る",
    )
    g.add_argument(
        "--tiles",
        type=int,
        default=1,
        help="フレームを NxN のタイルに割って各タイルも推論する。"
        "小さく写る対象に効く。推論回数は (1 + N*N) 倍",
    )
    g.add_argument(
        "--detect-scale",
        type=int,
        default=0,
        help="パス1のデコード長辺 px（0で自動: タイル数に応じて決める）",
    )
    g.add_argument(
        "--frame-step",
        type=int,
        default=1,
        help="Nフレームおきに検出する。間は補間で埋める。速度と引き換えに、"
        "数フレームしか映らない対象を取りこぼす危険が上がる",
    )

    t = p.add_argument_group("時間方向")
    t.add_argument("--max-gap", type=int, default=12, help="トラック継続を許す欠損フレーム数")
    t.add_argument("--memory", type=int, default=6, help="トラック終了後へ保持するフレーム数")
    t.add_argument(
        "--memory-before",
        type=int,
        default=0,
        help="トラック開始前へ遡って保持するフレーム数（0で --memory と同じ）。"
        "検出が遅れて始まる分を先回りして塞ぐ",
    )
    t.add_argument(
        "--stitch-gap",
        type=int,
        default=90,
        help="途切れたトラック同士を繋ぐ最大フレーム間隔（0で無効）。"
        "位置と大きさが近ければ1本に繋ぎ、あいだは補間で埋める",
    )
    t.add_argument(
        "--stitch-dist",
        type=float,
        default=0.12,
        help="繋ぐ条件: 中心間距離が画面対角のこの比以内",
    )
    t.add_argument(
        "--margin-scale",
        type=float,
        default=0.35,
        help="膨張マージンの倍率。大きいと潰しすぎ、小さいと輪郭が出る",
    )
    t.add_argument(
        "--margin-cap",
        type=float,
        default=16.0,
        help="膨張マージンの絶対上限 px（0で無効）。潰しすぎを直接抑える",
    )
    t.add_argument(
        "--motion-weight",
        type=float,
        default=2.0,
        help="局所速度に掛ける係数。速く動く対象への追従遅れを吸収する。"
        "この分は --margin-cap の外なので、静止時の大きさは変わらない",
    )
    t.add_argument(
        "--motion-cap",
        type=float,
        default=60.0,
        help="動き由来のマージンの上限 px",
    )
    t.add_argument(
        "--hold-growth",
        type=float,
        default=0.5,
        help="実観測から1フレーム離れるごとに領域を広げる割合（局所速度に対する比）。"
        "memory や補間で推定している区間の不確かさを覆う",
    )
    t.add_argument(
        "--estimated-factor",
        type=float,
        default=1.3,
        help="補間/memory/橋渡し由来の領域に掛けるマージン倍率",
    )
    t.add_argument(
        "--max-area-ratio",
        type=float,
        default=0.35,
        help="フレーム面積に対するこの比を超える検出は誤検出として落とす",
    )
    t.add_argument("--no-despike", action="store_true", help="デスパイクを無効にする")
    t.add_argument(
        "--track-min-peak",
        type=float,
        default=0.0,
        help="トラック内の最大スコアがこれ未満なら丸ごと捨てる（2閾値ヒステリシス）。"
        "0で無効。--conf を大きく下げたときの誤検出対策",
    )
    t.add_argument(
        "--bridge-max",
        type=int,
        default=150,
        help="前後が覆われている未処理区間を埋める最大フレーム数",
    )
    t.add_argument(
        "--estimate-gaps",
        action="store_true",
        help="検出が途切れた区間を推定で埋める。memory・橋渡し・不確かさ膨張が有効になり、"
        "取りこぼしは減るが位置が当てずっぽうの領域が増えて塗り過ぎになる。"
        "既定は無効で、実際に検出できた箇所と、検出と検出のあいだの補間だけを塗る",
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
        "--checkpoint-every",
        type=int,
        default=2000,
        help="検出結果を何フレームごとに途中保存するか（0で無効）。"
        "長尺は数時間かかるので、落ちたときに全損しないようにする",
    )
    m.add_argument(
        "--resume",
        action="store_true",
        help="--detections に途中保存があれば、その続きから検出を再開する",
    )
    m.add_argument(
        "--reuse-detections",
        action="store_true",
        help="--detections の JSON があればパス1を飛ばす",
    )
    m.add_argument("--report", help="統計とレビュー対象フレームの JSON 出力先")
    m.add_argument(
        "--corrections",
        help="人手レビュー（python -m automosaic.review）で作った修正 JSON。"
        "時間方向の処理を通したあとに反映してから焼く",
    )
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
            merge_mode=args.merge,
        )
        # タイル分割するときは、タイル1枚が推論解像度を埋められるだけデコードする
        detect_scale = args.detect_scale or args.infer_size * max(1, args.tiles)
        if not args.quiet:
            print(f"プロバイダ {det.active_provider}")
            extra = []
            if args.tta:
                extra.append("TTA(水平反転)")
            if args.tiles > 1:
                extra.append(f"{args.tiles}x{args.tiles} タイル")
            if args.frame_step > 1:
                extra.append(f"{args.frame_step}フレームおき")
            print(
                f"検出設定   conf {args.conf}  デコード長辺 {detect_scale}px"
                + (f"  {' + '.join(extra)}" if extra else "")
            )
        resume_dets: dict[int, list[Detection]] = {}
        resume_from = 0
        if args.resume and args.detections:
            resume_dets, resume_from = load_partial(args.detections)
            if resume_from and not args.quiet:
                print(f"途中保存から再開: {resume_from} フレームまで済み")

        per_frame, n_frames = run_detection(
            src, info, det, detect_scale, args.limit_frames, args.quiet,
            frame_step=max(1, args.frame_step),
            tta=args.tta,
            tiles=max(1, args.tiles),
            checkpoint_path=args.detections,
            checkpoint_every=args.checkpoint_every,
            resume_from=resume_from,
            resume_dets=resume_dets,
        )
        if args.detections:
            save_detections(args.detections, per_frame, n_frames, info, complete=True)
            if not args.quiet:
                print(f"検出結果を保存: {args.detections}")

    if n_frames == 0:
        print("フレームを1枚も読めませんでした。", file=sys.stderr)
        return 1

    # ---- 時間方向の安定化 ----
    if not args.estimate_gaps:
        # 既定は「実際に検出できた箇所だけ」。推定で広げる要素を落とし、
        # 検出と検出のあいだの補間だけ残す。塗り過ぎを避けるための方針。
        args.memory = min(args.memory, 2)
        args.memory_before = min(args.memory_before or 2, 2)
        args.bridge_max = 0
        args.hold_growth = 0.0
        args.motion_weight = min(args.motion_weight, 1.0)

    cfg = TemporalConfig(
        max_gap=args.max_gap,
        memory=args.memory,
        margin_scale=args.margin_scale,
        max_area_ratio=args.max_area_ratio,
        min_track_len=0 if args.no_despike else 2,
        bridge_max=0 if args.no_bridge else args.bridge_max,
        frame_step=max(1, args.frame_step),
        track_min_peak=args.track_min_peak,
        memory_before=args.memory_before,
        stitch_max_gap=args.stitch_gap,
        stitch_dist_ratio=args.stitch_dist,
        margin_cap_px=args.margin_cap,
        motion_weight=args.motion_weight,
        hold_growth=args.hold_growth,
        motion_cap=args.motion_cap,
        estimated_factor=args.estimated_factor,
    )
    regions, stats = process(
        per_frame, n_frames, info.width, info.height, classes, cfg
    )
    left_open = stats.pop("_left_open", [])

    # 手修正は自動処理の後段に置く。検出をやり直しても修正が生き残るし、
    # ここで反映しておけば以降のレポートも修正後の状態を映す
    # （手で足した領域は実観測扱いなので「推定のみ区間」から外れる）。
    if args.corrections:
        cset = corr.CorrectionSet.load(args.corrections)
        if cset.items:
            regions = corr.apply(regions, cset)
            if not args.quiet:
                print(f"手修正を反映: {args.corrections}（{len(cset.items)} 件）")
        elif not args.quiet:
            print(f"手修正ファイルに項目がありません: {args.corrections}")

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

    est_only = estimated_only_ranges(regions, n_frames)
    if est_only and not args.quiet:
        fps = info.fps
        total_est = sum(e - s + 1 for s, e, _ in est_only)
        print()
        print(
            f"[推定のみで覆っている区間 {len(est_only)} 件 / 計 {total_est} フレーム]"
        )
        print("  検出器が効いておらず位置が当てずっぽうに近い。人手レビューの最優先対象")
        for s_, e_, peak in sorted(est_only, key=lambda t: -(t[1] - t[0]))[:15]:
            print(
                f"  frame {s_:>7}-{e_:<7} ({s_ / fps:7.2f}s - {e_ / fps:7.2f}s)  "
                f"{e_ - s_ + 1:>5} フレーム  最大 hold {peak}"
            )
        if len(est_only) > 15:
            print(f"  ... 他 {len(est_only) - 15} 件")

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
                    "estimated_only_ranges": [
                        {
                            "start_frame": s_,
                            "end_frame": e_,
                            "start_sec": round(s_ / info.fps, 3),
                            "end_sec": round(e_ / info.fps, 3),
                            "frames": e_ - s_ + 1,
                            "max_hold": peak,
                        }
                        for s_, e_, peak in est_only
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
