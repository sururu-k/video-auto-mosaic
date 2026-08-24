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
    budget_net_size,
)
from . import corrections as corr
from .render import FrameBuffer, apply_regions, default_block_size
from .temporal import (
    TemporalConfig,
    estimated_only_ranges,
    frames_with_mosaic_count,
    process,
    review_flags,
    uncovered_ranges,
)
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


def load_partial(
    path: str, target_info: vid.VideoInfo | None = None
) -> tuple[dict[int, list[Detection]], int]:
    """途中保存された検出結果を読む。戻り値は (検出, 済んだフレーム数)。

    target_info を渡すと、これから検出を再開する動画に対してその途中保存が
    妥当かを検証し、おかしければ RuntimeError を出す（省略時は検証しない。
    tests/test_render.py の既存呼び出しは省略のままなので挙動は変わらない）。

    検証する2点:
      - 解像度が入力と違う: 座標系が別物の途中保存を取り違えている
      - n_frames（＝済んだとされるフレーム数）が入力の推定尺以上: これ以上
        「途中から再開」しようがない。resume_from が実フレーム数以上だと、
        run_detection のデコードループは推論を1枚も走らせずにそのまま
        EOF まで読み飛ばして「済み」扱いになり、そのまま complete=True・
        入力の解像度で上書き保存されてしまう（中身は元の途中保存のままなのに
        「完了」を称する壊れたJSONができる＝実測で再現済み）。
    """
    if not path or not os.path.exists(path):
        return {}, 0
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    per_frame = {
        int(k): [Detection.from_dict(x) for x in v] for k, v in d["detections"].items()
    }
    n_frames = int(d.get("n_frames", 0))

    if target_info is not None:
        jw, jh = d.get("width"), d.get("height")
        if jw is not None and jh is not None and (int(jw), int(jh)) != (
            target_info.width, target_info.height
        ):
            raise RuntimeError(
                f"途中保存 {path} の解像度が入力と違います: "
                f"JSON {jw}x{jh} / 入力 {target_info.width}x{target_info.height}。"
                "別の動画の途中保存を取り違えている可能性が高いため再開できません。"
            )
        # 推定値が信用できるとき（R-1: nb_frames か映像ストリーム自身の duration が
        # ある）だけ判断に使う。信用できない推定（コンテナ全体のduration頼み）を
        # 根拠に弾くと、こちらも誤爆でまともな再開を止めてしまう。
        if target_info.estimated_frames_reliable():
            expected = target_info.estimated_frames()
            if expected and n_frames >= expected:
                raise RuntimeError(
                    f"途中保存 {path} の済みフレーム数（{n_frames}）が入力の推定"
                    f"フレーム数（約 {expected}）以上です。この入力に対しては"
                    "再開できる続きがありません。別の（より長い）動画の途中保存を"
                    "取り違えている可能性が高いため中止しました。"
                )

    return per_frame, n_frames


def compute_detect_scale(
    net_w: int,
    net_h: int,
    tiles: int,
    mat_w: int,
    mat_h: int,
    override: int | None = None,
) -> int:
    """パス1のデコード長辺（detect_scale）を決める。

    タイル分割時は「ネット入力の長辺 * タイル数」を基準にしてきたが、これは
    素材の実解像度を無視する。素材より大きくデコードしても画素は補間で
    埋まるだけで情報は増えないのに、デコードと各タイルの推論コストだけが
    増える（issue #37）。素材の長辺で頭打ちにする。

    `--detect-scale` で明示指定された場合（override）は、利用者の意図的な
    指定なのでそのまま通す（頭打ちを掛けない）。
    """
    if override:
        return override
    return min(max(net_w, net_h) * max(1, tiles), max(mat_w, mat_h))


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
    # サイズは計算せず ffmpeg に実際に出させて測る。1バイトずれると以降の全フレームが
    # 前フレームと混ざった斜めの画像になり、例外も出ないまま検出が丸ごと無意味になる。
    dec_w, dec_h = vid.detection_frame_size(info, detect_scale, path=src)
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
    tail = 0
    try:
        while True:
            raw = proc.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                tail = len(raw)
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
    # 1フレームでも読めていれば見逃す、はしない。途中で止まったパス1をそのまま通すと、
    # 止まった以降が丸ごと未検出の状態でパス2に渡る（＝後半が素通しの出力）。
    if proc.returncode not in (0, None):
        raise RuntimeError(
            f"パス1のデコードが異常終了しました（{idx} フレーム目で停止 / "
            f"終了コード {proc.returncode}）:\n" + "\n".join(err[-10:])
        )
    # 末尾が半端に切れている = フレーム境界が合っていない。読み進めていたぶんは
    # 前フレームと混ざっている可能性があるので、結果を使わせない。
    if tail:
        raise RuntimeError(
            f"デコード出力がフレーム境界で終わっていません"
            f"（末尾 {tail} / {frame_bytes} バイト, デコードサイズ {dec_w}x{dec_h}）。"
            "検出結果が信用できないため中止します。"
        )
    # 壊れたファイルだと ffmpeg は終了コード0のまま途中でフレームを出し終える。
    # 尺は推定値なので止めはしないが、黙って短い検出結果を渡さない。
    expected = info.estimated_frames()
    if limit_frames:
        expected = min(expected, limit_frames) if expected else limit_frames
    if expected and idx < expected:
        print(
            f"\n警告: パス1が {idx} フレームで終わりました（この動画は約 {expected} "
            "フレームのはずです）。入力が壊れている可能性があります。"
            "この検出結果のまま描画すると不足ぶんが未検出のままになります。",
            file=sys.stderr,
        )
    # 終了コードが0でも、フレーム数が満額なら上の3つのチェックはどれも発火しない。
    # だが ffmpeg は壊れたフレームをエラーとして stderr に書きつつ、そのまま
    # デコードを継続して欠けたフレーム数を埋めることがある
    # （例: 「error while decoding MB」を出しつつ復帰する）。その場合ここまで
    # 気付けないので、rc=0 でも stderr にエラーらしき出力があれば必ず警告する。
    if err:
        print(
            "\n警告: パス1のデコード中に ffmpeg がエラーを出力していました"
            "（終了コードは正常でも、そのフレームは壊れている可能性があります）:\n  "
            + "\n  ".join(err[-10:]),
            file=sys.stderr,
        )
    return per_frame, idx


def _quarantine_incomplete_output(dst: str) -> None:
    """ガード発動などで尻切れのまま確定した出力を、完成品と紛れないよう退避する。

    書きかけでも ffmpeg 側は stdin の EOF を受けて正常にコンテナを閉じるため、
    そのまま置いておくと「短いだけの再生できる mp4」が完成品の顔をして残る。
    既存の完成品を同じ出力先に書いていた場合は、それを尻切れ版で上書きしてしまう。
    """
    if not dst or not os.path.exists(dst):
        return
    incomplete = dst + ".incomplete"
    try:
        if os.path.exists(incomplete):
            os.remove(incomplete)
        os.replace(dst, incomplete)
        print(
            f"  途中までの出力は {incomplete} に退避しました"
            "（完成品と紛れないよう出力先には残しません）。",
            file=sys.stderr,
        )
    except OSError as e:
        print(f"  警告: 途中までの出力の退避に失敗しました（{dst}）: {e}", file=sys.stderr)


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
    allow_short_detections: bool = False,
) -> None:
    pix_fmt = vid.detect_pix_fmt(info)
    ten_bit = pix_fmt.endswith("10le")
    # issue #32: FrameBuffer の長さ検査は width/height の入れ替えに対して
    # 不変なので、reader を開く前に実デコードサイズを測って突き合わせる。
    # 食い違えば reader/writer を一切開かずに例外で止める（出力ファイルを
    # 作らない。quarantine の必要すら無い）。
    vid.verify_full_frame_size(info, src)
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
    short_from: int | None = None
    hit_short_guard = False
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
                # パス1より短い検出しかない。ここから先は「判断できない」ではなく
                # 「何も知らない」なので、last_boxes を延ばしても塞げる保証がない
                # （最終フレームが空なら以降は全部素通しになる）。既定では止める。
                # ここで即 raise せず break するのは、finally でのクリーンアップ
                # （reader/writer を閉じて確実に終わらせる）を通したあとに
                # 尻切れ出力の退避までやってから例外にするため。
                if not allow_short_detections:
                    hit_short_guard = True
                    break
                if short_from is None:
                    short_from = idx
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

    if hit_short_guard:
        _quarantine_incomplete_output(dst)
        raise RuntimeError(
            f"検出結果が実尺より短いです（検出 {n_frames} フレーム / "
            f"入力は {idx + 1} フレーム目以降も続く）。"
            f"{idx} フレーム目以降を塞ぐ根拠が無いため中止しました"
            f"（出力 {dst} は書き出していません）。\n"
            "  検出をやり直すか、--limit-frames での試写など未検出区間を"
            "承知のうえで描画する場合は --allow-short-detections を付けてください。"
        )

    # パス1（run_detection）には「デコードが途中で死んでいないか」
    # 「実デコード数が期待どおりか」の照合があるが、パス2にはこれまで無かった。
    # probe が申告する尺と実デコード数が食い違う壊れた入力だと、期待フレーム数に
    # 届かないまま reader が正常終了したように見え、尻切れの出力が exit 0 で
    # 出てしまう（実測: probeは90フレーム/3.0sと言うが実際は39フレームしか
    # 読めない入力で、警告もエラーも無く39フレームの出力が返った）。
    if reader.returncode not in (0, None):
        _quarantine_incomplete_output(dst)
        raise RuntimeError(
            f"パス2のデコードが異常終了しました（{idx} フレーム目で停止 / "
            f"終了コード {reader.returncode}）:\n" + "\n".join(rerr[-10:])
        )
    if idx < total:
        _quarantine_incomplete_output(dst)
        raise RuntimeError(
            f"パス2が {idx} フレームで終わりました（検出結果は {n_frames} フレーム分、"
            f"期待していたのは {total} フレームです）。"
            f"入力の実デコード数が想定より少なく、出力 {dst} は書き出していません"
            "（probe の申告する尺が実デコード数と食い違っている可能性があります）。"
        )
    # フレーム数の照合を通っても、rc=0 のまま ffmpeg がエラーを出しつつ
    # 内部で復帰してフレーム数だけ帳尻を合わせることがある（例: 壊れた中間バイトで
    # 「error while decoding MB」を出しつつデコードを続ける）。この場合は
    # フレーム数の一致だけでは気付けないので、rc に関わらず出力する。
    if rerr:
        print(
            "\n警告: パス2のデコード中に ffmpeg がエラーを出力していました"
            "（終了コードもフレーム数も正常ですが、該当フレームが壊れている"
            "可能性があります）:\n  " + "\n  ".join(rerr[-10:]),
            file=sys.stderr,
        )

    if writer.returncode not in (0, None):
        _quarantine_incomplete_output(dst)
        raise RuntimeError("エンコードに失敗しました:\n" + "\n".join(werr[-15:]))
    # --quiet でも必ず出す。素通しの可能性がある区間を黙って渡さない。
    if short_from is not None:
        n_short = idx - short_from
        if last_boxes:
            print(
                f"\n警告: frame {short_from} 以降 {n_short} フレーム"
                f"（{short_from / info.fps:.2f}s 以降）は検出結果がありません。"
                f"直前フレームの領域 {len(last_boxes)} 個をそのまま延ばしただけで、"
                "実際に塞げている保証はありません。目視確認が必須です。",
                file=sys.stderr,
            )
        else:
            print(
                f"\n警告: frame {short_from} 以降 {n_short} フレーム"
                f"（{short_from / info.fps:.2f}s 以降）は検出結果がありません。"
                "直前フレームにも領域が無かったため、この区間はモザイクが"
                "一切かかっていません（完全な素通しです）。必ず目視確認してください。",
                file=sys.stderr,
            )


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
        help="推論の画素予算（この値の2乗が目安）。素材の縦横比なりの非正方"
        "ネット解像度に配分する（issue #8。正方の黒帯レターボックスは廃止）。"
        "実素材で 640->960 にすると検出フレームが約4割増えた。"
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
        help="フレーム面積に対するこの比を超える検出を大きすぎるとして数える（落とさない）",
    )
    t.add_argument(
        "--despike",
        action="store_true",
        help="デスパイクを有効にする（既定は無効）。単発かつスコア0.35未満のトラックを"
        "丸ごと捨てる。塗り過ぎ側は許容という設計原則と噛み合わないため既定を切ってある"
        "（docs/09-mosaic-quality.md S4: 確実に映っている区間の実観測125件を捨て、"
        "うち40件はそのフレームが素通しになっていた）。それでも有効にした場合、"
        "捨てた場所（フレーム番号・クラス・最大スコア）は必ず表示・レポートに出す",
    )
    t.add_argument(
        "--no-despike",
        action="store_true",
        help="[後方互換のため残置。既定が無効になったので通常は不要] "
        "デスパイクを無効にする。--despike と同時指定はエラー",
    )
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
    m.add_argument(
        "--allow-short-detections",
        action="store_true",
        help="検出結果が実尺より短くても描画を続ける。不足区間は直前フレームの領域を"
        "延ばすだけで、塞げている保証はない（--limit-frames の試写を焼くとき用）。"
        "既定では実尺より短い検出はエラーで止める",
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


def explicit_options(argv: list[str] | None) -> set[str]:
    """コマンドラインで実際に書かれたオプション名を返す。

    既定値と同じ値を明示指定することもあるので、値の比較では判別できない。
    既定値を全部 None にしたパーサでもう一度読み、埋まったものだけを拾う。
    """
    p = build_parser()
    for action in p._actions:
        action.default = None
    try:
        parsed = p.parse_args(argv)
    except SystemExit:
        return set()
    return {k for k, v in vars(parsed).items() if v is not None}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    given = explicit_options(argv)

    if args.list_providers:
        print("利用可能なプロバイダ:")
        for p in available_providers():
            print(f"  {p}")
        return 0

    if not args.input:
        build_parser().error("入力動画を指定してください")

    if args.despike and args.no_despike:
        build_parser().error(
            "--despike と --no-despike は同時に指定できません（矛盾する指定を黙って"
            "どちらかに倒さない）"
        )

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

    # issue #2: 奇数解像度は検出（パス1）だけは通ってしまい、描画（パス2）で
    # 初めて FrameBuffer の guard に当たって落ちる。数時間かけた検出が無駄に
    # なる壊れ方なので、無駄にする前にここで検査する。--detect-only は
    # パス1しか実行しない（＝奇数のままでも正しく動く）ので警告に留めて進める。
    geometry_problem = vid.check_render_geometry(info)
    if geometry_problem:
        if args.detect_only:
            print(f"警告: {geometry_problem} --detect-only のため続行します。", file=sys.stderr)
        else:
            print(geometry_problem, file=sys.stderr)
            return 1

    block = args.block or default_block_size(info.long_edge)

    if info.rotation and not args.quiet:
        print(
            f"入力に回転メタデータがあります（{info.rotation}度）。"
            f"表示向きの {info.width}x{info.height} として扱います。",
            file=sys.stderr,
        )

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

        # 途中保存を本番の検出として使わせない。後半が丸ごと未検出のまま焼ける。
        complete = data.get("complete")
        if complete is False:
            if args.resume:
                # --reuse-detections と --resume の同時指定。"--resume を付けて"は
                # 案内として噛み合わない（既に付いている）ので分けて案内する。
                print(
                    f"検出結果が途中保存です（complete: false）: {args.detections}\n"
                    "  --reuse-detections と --resume は同時に使えません。"
                    "  まず --resume だけを付けて検出を最後まで終わらせてから、"
                    "  改めて --reuse-detections を付けて描画してください。",
                    file=sys.stderr,
                )
            else:
                print(
                    f"検出結果が途中保存です（complete: false）: {args.detections}\n"
                    "  検出が最後まで終わっていないので、そのまま描画すると後半が素通しになります。"
                    "  --reuse-detections を外し、代わりに --resume を付けて検出を"
                    "  続けてから、改めて --reuse-detections で描画してください。",
                    file=sys.stderr,
                )
            return 1
        if complete is None:
            print(
                f"警告: {args.detections} に complete フラグがありません。"
                "古い形式か手書きの可能性があります。完了済みとして扱います。",
                file=sys.stderr,
            )

        # 解像度違いの JSON を流用すると領域が別の場所に載る。幾何フィルタでも
        # 検知できないので、ここで弾く。width/height 欠落も同様に弾く
        # （画枠外に出た矩形は render.py の描画クリップで塗られず、それでも
        # 統計上は「被覆した」と数えられて適用率100%なのに1画素も塗らない
        # 出力になった実測がある。手元の実データ8本は全て width/height を
        # 持つので、この判定を厳格化しても既存データは壊さない）。
        jw, jh = data.get("width"), data.get("height")
        if jw is None or jh is None:
            print(
                f"検出結果に解像度が記録されていません: {args.detections}\n"
                f"  座標が入力 {info.width}x{info.height} に対応しているか確認できないため"
                "中止します。検出をやり直すか、width/height を記録している検出結果を"
                "使ってください。",
                file=sys.stderr,
            )
            return 1
        if (int(jw), int(jh)) != (info.width, info.height):
            print(
                f"検出結果の解像度が入力と違います: "
                f"JSON {jw}x{jh} / 入力 {info.width}x{info.height}（{args.detections}）\n"
                "  座標がそのまま別の位置に載るため使えません。"
                "  その解像度用の検出結果を指定するか、検出をやり直してください。",
                file=sys.stderr,
            )
            return 1

        n_frames = data["n_frames"]
        per_frame = {
            int(k): [Detection.from_dict(d) for d in v]
            for k, v in data["detections"].items()
        }
        loaded = True
        if not args.quiet:
            print(f"検出結果を再利用: {args.detections}（{n_frames} フレーム）")

        # 実尺より短い検出は、描画に入る前に弾く。ここで気付けば数十分無駄にしない。
        # ただし estimated_frames() は duration からの見積りに過ぎず、nb_frames も
        # 映像ストリーム自身の duration も持たないコンテナ（mkv 等）だと、
        # コンテナ全体の duration（音声込みの最大長）にフォールバックする。
        # これは音声コーデックの priming/padding 分だけ実フレーム数より大きく出る
        # ことがあり（mkv+AAC/MP3 実測: 実150フレームに対し見積り151）、
        # ここで return 1 すると健全な検出結果が常用不能になり、案内に従った
        # 利用者が --allow-short-detections を常用するようになる
        # （それは本物のガード=run_render の実デコード数チェックまで無効化する
        # ので、この誤検知が最も危険）。
        # estimated_frames_reliable() が False のときはこの見積りを弾く根拠に
        # 使わず、情報として出すに留める。本当に短い検出は、描画に進めば
        # run_render が実デコード数で改めて弾く（そちらは推定に頼らない）。
        expected = info.estimated_frames()
        expected_reliable = info.estimated_frames_reliable()
        if args.limit_frames:
            if expected is None or args.limit_frames < expected:
                # --limit-frames 自体は利用者の明示指定なので信頼できる
                expected = args.limit_frames
                expected_reliable = True
        if expected and n_frames < expected and not args.detect_only:
            msg = (
                f"検出結果が実尺より短いです: 検出 {n_frames} フレーム / "
                f"入力は約 {expected} フレーム（{args.detections}）"
            )
            if not expected_reliable:
                if not args.quiet:
                    print(
                        msg + "\n  ただしこの入力は nb_frames も映像ストリームの"
                        "duration も持たないため、この見積りは音声込みの長さからの"
                        "概算で実フレーム数より多めに出ることがあります。"
                        "ここでは止めず、描画時に実デコード数で改めて確認します。",
                        file=sys.stderr,
                    )
            elif not args.allow_short_detections:
                print(
                    msg + "\n  不足ぶんを塞ぐ根拠が無いため中止します。"
                    "  検出をやり直すか、承知のうえで焼くなら --allow-short-detections。",
                    file=sys.stderr,
                )
                return 1
            else:
                print(
                    f"警告: {msg}\n"
                    f"  frame {n_frames} 以降は直前フレームの領域を延ばすだけです。",
                    file=sys.stderr,
                )

    if not loaded:
        # issue #8: 正方レターボックスは入力の縦横比を捨て、黒帯に画素予算を食わせる
        # （16:9 素材で44%）。同じ画素予算(infer_size**2)を入力の縦横比なりに
        # 配分し直した非正方のネット解像度を使う。素材の縦横比は info（動画その
        # ものの縦横比）から決める。タイルは NxN に割っても縦横比は変わらないので
        # 同じ net_w/net_h をそのまま使い回せる。
        net_w, net_h = budget_net_size(info.width, info.height, args.infer_size)
        det = Detector(
            model_path=args.model,
            infer_size=(net_w, net_h),
            conf=args.conf,
            nms_iou=args.nms_iou,
            provider=args.provider,
            intra_threads=args.threads,
            device_id=args.device_id,
            merge_mode=args.merge,
        )
        # タイル分割するときは、タイル1枚が推論解像度を埋められるだけデコードする。
        # 旧: infer_size(正方の辺) * タイル数。新: net の長辺 * タイル数
        # （decode 自体は正方 box への fit なので、box の長辺だけ渡せば足りる。
        # box が正方でも中身は縦横比なりに縮む＝黒帯にはならない）。
        # ただし素材の実解像度を超える拡大は情報を増やさない（issue #37）ので
        # 素材の長辺で頭打ちにする。
        detect_scale = compute_detect_scale(
            net_w, net_h, args.tiles, info.width, info.height, args.detect_scale
        )
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
                f"検出設定   conf {args.conf}  ネット入力 {net_w}x{net_h}px"
                f"  デコード長辺 {detect_scale}px"
                + (f"  {' + '.join(extra)}" if extra else "")
            )
        resume_dets: dict[int, list[Detection]] = {}
        resume_from = 0
        if args.resume and args.detections:
            try:
                resume_dets, resume_from = load_partial(args.detections, target_info=info)
            except RuntimeError as e:
                print(str(e), file=sys.stderr)
                return 1
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
        # ただし明示指定された値は上書きしない。黙って別の設定で走らせない。
        narrowed = [
            ("memory", "--memory", args.memory, min(args.memory, 2)),
            ("memory_before", "--memory-before",
             args.memory_before, min(args.memory_before or 2, 2)),
            ("bridge_max", "--bridge-max", args.bridge_max, 0),
            ("hold_growth", "--hold-growth", args.hold_growth, 0.0),
            ("motion_weight", "--motion-weight",
             args.motion_weight, min(args.motion_weight, 1.0)),
        ]
        applied: list[str] = []
        kept: list[str] = []
        for name, flag, old, new in narrowed:
            if name in given:
                if old != new:
                    kept.append(f"{flag} {old}")
                continue
            if old != new:
                setattr(args, name, new)
                applied.append(f"{flag} {old} -> {new}")

        # C-6: 実効設定が明示指定と違う状態で走ることがあるので、--quiet でも出す。
        if applied:
            print(
                "--estimate-gaps 無しのため推定で広げる設定を絞りました: "
                + ", ".join(applied),
                file=sys.stderr,
            )
        if kept:
            print("明示指定を優先: " + ", ".join(kept), file=sys.stderr)

    cfg = TemporalConfig(
        max_gap=args.max_gap,
        memory=args.memory,
        margin_scale=args.margin_scale,
        max_area_ratio=args.max_area_ratio,
        min_track_len=2 if args.despike else 0,
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
    despiked_ranges = stats.pop("_despiked_ranges", [])

    # 手修正は自動処理の後段に置く。検出をやり直しても修正が生き残るし、
    # ここで反映しておけば以降のレポートも修正後の状態を映す
    # （手で足した領域は実観測扱いなので「推定のみ区間」から外れる）。
    if args.corrections:
        cset = corr.CorrectionSet.load(args.corrections)
        if cset.items:
            # stats を受け取って実際に反映できた件数を出す。読み込み件数だけを
            # 出すと「全件反映できた」ように見えるが、範囲外フレームを指す修正は
            # apply() 内で黙って（stdoutには出さず）弾かれるため、実際の適用数は
            # それより少ないことがある（実測: 読み込み444件 / 適用26件）。
            corr_stats: dict = {}
            regions = corr.apply(regions, cset, stats=corr_stats)
            if not args.quiet:
                dropped = corr_stats.get("dropped_out_of_range", 0)
                print(
                    f"手修正を反映: {args.corrections}"
                    f"（読み込み {corr_stats.get('total', len(cset.items))} 件 / "
                    f"適用 {corr_stats.get('applied', 0)} 件"
                    f" [add {corr_stats.get('applied_add', 0)}"
                    f" / remove {corr_stats.get('applied_remove', 0)}]"
                    + (f" / 範囲外で不適用 {dropped} 件" if dropped else "")
                    + "）"
                )
        elif not args.quiet:
            print(f"手修正ファイルに項目がありません: {args.corrections}")

    # issue #4: 「素通しの区間」は corr.apply() 済みの regions から数え直す。
    # ここより上で left_open に入れていた stats["_left_open"] は corr.apply() を
    # 通す前の値で、add で埋まった区間も remove で空になった区間も反映されない
    # （bare remove だけの手修正が「誤検知」判定の通常操作としてこの壊れ方に
    # 到達する）。手修正が無い場合も regions は process() の出力そのものなので
    # 結果は変わらない。以降の表示・レポートは必ずこの left_open を使うこと。
    left_open = uncovered_ranges(regions, n_frames)

    # issue #41: stats["frames_with_mosaic"] / stats["uncovered_gaps"] は
    # process() が返した corr.apply() より前の値のままだった。left_open だけを
    # 数え直しても、同じ画面に出る「モザイク適用率」と stats テーブルの
    # uncovered_gaps は古い値のままなので、「適用率100.0%・uncovered_gaps 0」と
    # 「素通し区間 N 件」が同時に表示される（実測。issue #41 の再現手順）。
    # 上の left_open（数え直し済み）と整合するよう、この2キーだけをここで
    # 上書きする。他のキーを post-correction で数え直すべきかは per-key で判断
    # した（詳細は PR 本文）。手修正は process() が触らない生検出/トラック段階
    # には作用しないので、そこ由来のキー（geometric_dropped、tracks_*、
    # median_track_speed_px_per_frame など）や、自動処理の内訳を示す診断値
    # （regions_interpolated / regions_from_memory / regions_bridged /
    # frames_bridged。「自動処理が何をしたか」を表す値であり「最終出力に何が
    # 残っているか」ではないため）は前のままにしている。
    stats["frames_with_mosaic"] = frames_with_mosaic_count(regions, n_frames)
    stats["uncovered_gaps"] = len(left_open)

    covered = stats["frames_with_mosaic"]
    coverage_pct = 100.0 * covered / n_frames
    if not args.quiet:
        print("\n[検出統計]")
        for k, v in stats.items():
            print(f"  {k:34s} {v}")
        print(f"  モザイク適用率{'':21s} {coverage_pct:.1f}%")
    else:
        # --quiet でも「漏れている/素通しである」ことを示す数値は落とさない。
        # 進捗や内訳の全項目は抑制してよいが、被覆率と幾何フィルタで丸ごと
        # 捨てた件数（geometric_dropped）は素通しの直接の指標なので出す
        # （実測: --quiet だと「適用率100.0%」が実は1画素も塗っていない
        # ケースでも一切表示されず気付けなかった）。
        print(
            f"[検出統計] モザイク適用率 {coverage_pct:.1f}%  "
            f"geometric_dropped={stats.get('geometric_dropped', 0)}  "
            f"uncovered_gaps={stats.get('uncovered_gaps', 0)}",
            file=sys.stderr,
        )

    # 埋めなかった未処理区間は必ず表に出す。黙って素通しにしない（--quiet でも）。
    if left_open:
        fps = info.fps
        out = sys.stderr if args.quiet else sys.stdout
        print(f"\n[未処理のまま残った区間 {len(left_open)} 件]", file=out)
        for start, end in left_open[:20]:
            print(
                f"  frame {start:>7}-{end - 1:<7} "
                f"({start / fps:7.2f}s - {(end - 1) / fps:7.2f}s)  {end - start} フレーム",
                file=out,
            )
        if len(left_open) > 20:
            print(f"  ... 他 {len(left_open) - 20} 件", file=out)

    # --despike で捨てた場所は必ず表に出す。「判断できない区間を黙って素通しに
    # しない」という設計原則は、デスパイクで捨てるという判断自体にも適用される
    # （--quiet でも出す。素通しの直接の指標なので落とさない）。
    if despiked_ranges:
        fps = info.fps
        out = sys.stderr if args.quiet else sys.stdout
        print(f"\n[デスパイクで捨てた実観測 {len(despiked_ranges)} 件]", file=out)
        print(
            "  --despike は単発かつ低スコアのトラックを丸ごと捨てる。捨てた場所は"
            "目視で確認すること",
            file=out,
        )
        for start, end, cls, score in despiked_ranges[:20]:
            print(
                f"  frame {start:>7}-{end:<7} "
                f"({start / fps:7.2f}s - {end / fps:7.2f}s)  {cls}  最大score={score:.3f}",
                file=out,
            )
        if len(despiked_ranges) > 20:
            print(f"  ... 他 {len(despiked_ranges) - 20} 件", file=out)

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
                    "despiked_ranges": [
                        {
                            "start_frame": s_,
                            "end_frame": e_,
                            "start_sec": round(s_ / info.fps, 3),
                            "end_sec": round(e_ / info.fps, 3),
                            "class": cls,
                            "max_score": round(sc, 4),
                        }
                        for s_, e_, cls, sc in despiked_ranges
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
    # run_render は「検出結果が実尺より短い」等をここまで来て初めて実デコード数で
    # 検知することがある（R-1: 事前チェックは推定値ベースで信用できないことがあり、
    # そのぶんの本当の判定はここに委ねている）。例外を素通しさせず、他の中止経路と
    # 同じくメッセージを出して rc=1 で終える。
    try:
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
            allow_short_detections=args.allow_short_detections,
        )
    except RuntimeError as e:
        print(f"\n{e}", file=sys.stderr)
        return 1

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
