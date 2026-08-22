"""時間方向の安定化。

処理順は docs/01-technical-design.md の通り:
    幾何フィルタ -> トラッキング -> デスパイク -> 補間 -> frame memory -> 膨張

「デスパイクは補間より先」が重要。順序を逆にすると、一瞬の誤検出が補間で
長い区間に引き伸ばされる。

バッチ処理なので未来フレームも使える。前後どちらにも検出があるフレームは
必ず埋められるため、単発〜数フレームの検出漏れは構造的にゼロにできる。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .detector import Detection

Box = tuple[float, float, float, float]  # x, y, w, h


def _iou(a: Box, b: Box) -> float:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ax1, ay1 = ax0 + aw, ay0 + ah
    bx1, by1 = bx0 + bw, by0 + bh
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _center(b: Box) -> tuple[float, float]:
    return b[0] + b[2] / 2, b[1] + b[3] / 2


@dataclass
class Track:
    cls: str
    obs: dict[int, tuple[Box, float]] = field(default_factory=dict)  # frame -> (box, score)

    @property
    def first(self) -> int:
        return min(self.obs)

    @property
    def last(self) -> int:
        return max(self.obs)

    @property
    def max_score(self) -> float:
        return max(s for _, s in self.obs.values())

    def __len__(self) -> int:
        return len(self.obs)


@dataclass
class Region:
    """最終的にモザイクをかける領域。"""

    box: Box
    cls: str
    score: float
    source: str  # "detected" | "interpolated" | "memory"


@dataclass
class TemporalConfig:
    iou_threshold: float = 0.20
    max_gap: int = 12          # トラック継続を許す欠損フレーム数
    memory: int = 6            # トラック端を前後に保持するフレーム数
    min_track_len: int = 2     # これ未満のトラックはデスパイク対象
    despike_conf: float = 0.35 # 最大スコアがこれ未満の短いトラックだけ落とす
    min_area_ratio: float = 0.0000  # フレーム面積比の下限（0で無効）
    max_area_ratio: float = 0.35    # これを超える検出は誤検出とみなす
    margin_scale: float = 1.0  # 膨張マージン全体の倍率
    jpeg_margin: int = 8
    bridge_max: int = 150      # 前後が覆われている未処理区間を埋める最大長（0で無効）
    frame_step: int = 1        # 検出を何フレームおきに行ったか（マージンとgapに反映）

    @property
    def effective_max_gap(self) -> int:
        """間引き検出時はトラック継続の許容ギャップを自動で広げる。

        step=3 なら検出は3フレームおきにしか来ないので、max_gap がそれ未満だと
        トラックが毎回切れてしまう。
        """
        return max(self.max_gap, self.frame_step * 4)


def filter_classes(
    per_frame: dict[int, list[Detection]], classes: set[str]
) -> dict[int, list[Detection]]:
    return {
        f: [d for d in dets if d.cls in classes] for f, dets in per_frame.items()
    }


def geometric_filter(
    per_frame: dict[int, list[Detection]],
    frame_w: int,
    frame_h: int,
    cfg: TemporalConfig,
) -> tuple[dict[int, list[Detection]], int]:
    """ありえない大きさの検出を落とす。落とした件数も返す。"""
    frame_area = float(frame_w * frame_h)
    out: dict[int, list[Detection]] = {}
    dropped = 0
    for f, dets in per_frame.items():
        kept = []
        for d in dets:
            area = d.box[2] * d.box[3]
            ratio = area / frame_area if frame_area else 0.0
            if ratio > cfg.max_area_ratio or ratio < cfg.min_area_ratio:
                dropped += 1
                continue
            if d.box[2] <= 1 or d.box[3] <= 1:
                dropped += 1
                continue
            kept.append(d)
        out[f] = kept
    return out, dropped


def build_tracks(
    per_frame: dict[int, list[Detection]], n_frames: int, cfg: TemporalConfig
) -> list[Track]:
    """クラスごとに IoU 貪欲マッチングでトラックを作る。

    max_gap フレームまでは検出が無くてもトラックを生かしておく。ByteTrack の
    「低スコア検出を捨てずに軌跡で救済する」考え方の簡易版。
    """
    tracks: list[Track] = []
    active: list[tuple[Track, int, Box]] = []  # (track, last_seen_frame, last_box)

    for f in range(n_frames):
        dets = per_frame.get(f, [])

        # 期限切れのトラックを active から外す
        active = [(t, lf, lb) for (t, lf, lb) in active if f - lf <= cfg.effective_max_gap]

        used_det: set[int] = set()
        used_track: set[int] = set()

        # 全ペアの IoU を出して、大きい順に貪欲マッチ
        pairs = []
        for ti, (t, lf, lb) in enumerate(active):
            for di, d in enumerate(dets):
                if d.cls != t.cls:
                    continue
                iou = _iou(lb, tuple(float(v) for v in d.box))
                if iou >= cfg.iou_threshold:
                    pairs.append((iou, ti, di))
        pairs.sort(reverse=True)

        for iou, ti, di in pairs:
            if ti in used_track or di in used_det:
                continue
            used_track.add(ti)
            used_det.add(di)
            t, _, _ = active[ti]
            box = tuple(float(v) for v in dets[di].box)
            t.obs[f] = (box, dets[di].score)
            active[ti] = (t, f, box)

        # マッチしなかった検出は新規トラック
        for di, d in enumerate(dets):
            if di in used_det:
                continue
            box = tuple(float(v) for v in d.box)
            t = Track(cls=d.cls, obs={f: (box, d.score)})
            tracks.append(t)
            active.append((t, f, box))

    return tracks


def despike(tracks: list[Track], cfg: TemporalConfig) -> tuple[list[Track], int]:
    """短命かつ低スコアのトラックを落とす。補間より前に行うこと。

    Recall 優先なので、スコアが高いものは1フレームだけでも残す。
    """
    kept, dropped = [], 0
    for t in tracks:
        if len(t) < cfg.min_track_len and t.max_score < cfg.despike_conf:
            dropped += 1
            continue
        kept.append(t)
    return kept, dropped


def _lerp_box(a: Box, b: Box, w: float) -> Box:
    return tuple(a[i] + (b[i] - a[i]) * w for i in range(4))  # type: ignore[return-value]


def densify(
    tracks: list[Track], n_frames: int, cfg: TemporalConfig
) -> dict[int, list[Region]]:
    """補間と frame memory でトラックを連続化し、フレームごとの領域に落とす。"""
    per_frame: dict[int, list[Region]] = {i: [] for i in range(n_frames)}

    for t in tracks:
        frames = sorted(t.obs)

        # 観測フレームをそのまま置く
        for f in frames:
            box, score = t.obs[f]
            per_frame[f].append(Region(box, t.cls, score, "detected"))

        # 観測と観測のあいだを線形補間で埋める。バッチなので未来を使える。
        for a, b in zip(frames, frames[1:]):
            gap = b - a
            if gap <= 1:
                continue
            box_a, score_a = t.obs[a]
            box_b, score_b = t.obs[b]
            for f in range(a + 1, b):
                w = (f - a) / gap
                per_frame[f].append(
                    Region(
                        _lerp_box(box_a, box_b, w),
                        t.cls,
                        min(score_a, score_b),
                        "interpolated",
                    )
                )

        # トラック端を前後に保持する（frame memory）。
        # 計算コストほぼゼロで検出漏れを潰せるので最初に入れるべき機構。
        mem = cfg.memory * max(1, cfg.frame_step)
        if mem > 0:
            head_box, head_score = t.obs[frames[0]]
            for f in range(max(0, frames[0] - mem), frames[0]):
                per_frame[f].append(Region(head_box, t.cls, head_score, "memory"))
            tail_box, tail_score = t.obs[frames[-1]]
            for f in range(frames[-1] + 1, min(n_frames, frames[-1] + mem + 1)):
                per_frame[f].append(Region(tail_box, t.cls, tail_score, "memory"))

    return per_frame


def _union_box(boxes: list[Box]) -> Box:
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return (x0, y0, x1 - x0, y1 - y0)


def bridge_uncovered(
    per_frame: dict[int, list[Region]], n_frames: int, cfg: TemporalConfig
) -> tuple[int, list[tuple[int, int]]]:
    """前後が覆われている未処理区間を、両側の領域の外接矩形で埋める。

    トラックが max_gap を超えて分断されると、frame memory で伸ばしても届かない
    フレームが素通しで残る。素通しは法的に致命的なので、「判断できない = 潰す」
    に従って両側を包む矩形で塞ぐ。

    区間が bridge_max より長い場合は埋めない。長い未処理区間は本当に対象が
    映っていない可能性が高く、そこまで潰すと過剰になるため。ただし埋めなかった
    区間は呼び出し側に返し、レビュー対象として必ず記録する。

    戻り値: (埋めたフレーム数, 埋めなかった未処理区間のリスト)
    """
    covered = [bool(per_frame.get(f)) for f in range(n_frames)]
    filled = 0
    left_open: list[tuple[int, int]] = []

    f = 0
    while f < n_frames:
        if covered[f]:
            f += 1
            continue
        start = f
        while f < n_frames and not covered[f]:
            f += 1
        end = f  # [start, end) が未処理区間

        if start == 0 or end == n_frames:
            # 動画の先頭/末尾に接する区間。片側にしか根拠がないので埋めない
            left_open.append((start, end))
            continue
        if cfg.bridge_max <= 0 or (end - start) > cfg.bridge_max:
            left_open.append((start, end))
            continue

        before = per_frame[start - 1]
        after = per_frame[end]
        boxes = [r.box for r in before] + [r.box for r in after]
        if not boxes:
            left_open.append((start, end))
            continue

        union = _union_box(boxes)
        score = min(
            min((r.score for r in before), default=0.0),
            min((r.score for r in after), default=0.0),
        )
        cls = before[0].cls if before else after[0].cls
        for i in range(start, end):
            per_frame[i].append(Region(union, cls, score, "bridged"))
            filled += 1

    return filled, left_open


def _track_speed(tracks: list[Track]) -> dict[int, float]:
    """トラックごとの平均移動量 px/frame。マージンの motion 項に使う。"""
    speeds: dict[int, float] = {}
    for i, t in enumerate(tracks):
        frames = sorted(t.obs)
        if len(frames) < 2:
            speeds[i] = 0.0
            continue
        total, steps = 0.0, 0
        for a, b in zip(frames, frames[1:]):
            ca, cb = _center(t.obs[a][0]), _center(t.obs[b][0])
            dist = math.hypot(cb[0] - ca[0], cb[1] - ca[1])
            total += dist / max(1, b - a)
            steps += 1
        speeds[i] = total / steps if steps else 0.0
    return speeds


def expand(
    region: Region,
    frame_w: int,
    frame_h: int,
    cfg: TemporalConfig,
    speed: float = 0.0,
) -> Box:
    """膨張マージンを乗せる。

    margin = base + motion + confidence + jpeg
      base       面積依存の基礎マージン
      motion     速く動くほど厚く（検出漏れは動きの速い場面で起きる）
      confidence 低信頼ほど厚く
      jpeg       圧縮アーティファクトの染み出し対策
    補間・memory 由来の領域はさらに厚くする。
    """
    x, y, w, h = region.box
    area = max(1.0, w * h)
    base = max(0.20 * math.sqrt(area), 12.0)
    # 間引き検出時は検出フレーム間で対象がより大きく動くので、その分厚く盛る
    motion = 1.5 * speed * max(1, cfg.frame_step)
    confidence = (1.0 - min(1.0, region.score)) * base * 2.0
    margin = (base + motion + confidence + cfg.jpeg_margin) * cfg.margin_scale

    if region.source != "detected":
        margin *= 2.0  # 推定で置いた領域は素直に厚く盛る

    nx = x - margin
    ny = y - margin
    nw = w + margin * 2
    nh = h + margin * 2

    nx = max(0.0, nx)
    ny = max(0.0, ny)
    nw = min(nw, frame_w - nx)
    nh = min(nh, frame_h - ny)
    return (nx, ny, nw, nh)


def process(
    per_frame_dets: dict[int, list[Detection]],
    n_frames: int,
    frame_w: int,
    frame_h: int,
    classes: set[str],
    cfg: TemporalConfig,
) -> tuple[dict[int, list[tuple[Box, Region]]], dict]:
    """検出結果を最終的な描画領域に変換する。統計も返す。"""
    filtered = filter_classes(per_frame_dets, classes)
    n_raw = sum(len(v) for v in filtered.values())

    filtered, n_geo_dropped = geometric_filter(filtered, frame_w, frame_h, cfg)
    tracks = build_tracks(filtered, n_frames, cfg)
    n_tracks_before = len(tracks)
    tracks, n_despiked = despike(tracks, cfg)

    speeds = _track_speed(tracks)
    dense = densify(tracks, n_frames, cfg)
    n_bridged, left_open = bridge_uncovered(dense, n_frames, cfg)

    # トラックと速度の対応をつけ直す（densify で Region に落ちると track が消えるため、
    # クラスと位置から引き直すのは無駄。速度はトラック単位の代表値で十分なので、
    # 全トラックの中央値を使う）。
    speed_values = sorted(speeds.values())
    median_speed = speed_values[len(speed_values) // 2] if speed_values else 0.0

    out: dict[int, list[tuple[Box, Region]]] = {}
    for f in range(n_frames):
        regions = dense.get(f, [])
        out[f] = [
            (expand(r, frame_w, frame_h, cfg, median_speed), r) for r in regions
        ]

    covered = sum(1 for f in range(n_frames) if out[f])
    detected_frames = sum(1 for f in range(n_frames) if filtered.get(f))
    n_interp = sum(
        1 for f in range(n_frames) for _, r in out[f] if r.source == "interpolated"
    )
    n_memory = sum(1 for f in range(n_frames) for _, r in out[f] if r.source == "memory")

    n_bridge_regions = sum(
        1 for f in range(n_frames) for _, r in out[f] if r.source == "bridged"
    )

    stats = {
        "frames": n_frames,
        "raw_detections": n_raw,
        "geometric_dropped": n_geo_dropped,
        "tracks_before_despike": n_tracks_before,
        "tracks_despiked": n_despiked,
        "tracks_final": len(tracks),
        "frames_with_detection": detected_frames,
        "frames_with_mosaic": covered,
        "regions_interpolated": n_interp,
        "regions_from_memory": n_memory,
        "regions_bridged": n_bridge_regions,
        "frames_bridged": n_bridged,
        "uncovered_gaps": len(left_open),
        "median_track_speed_px_per_frame": round(median_speed, 2),
    }
    stats["_left_open"] = left_open
    return out, stats


def review_flags(
    regions_per_frame: dict[int, list[tuple[Box, Region]]],
    n_frames: int,
) -> list[dict]:
    """人手レビューに回すべきフレーム区間を洗い出す。

    「判断できない = 潰す」を実行したうえで、潰した根拠が弱い箇所を記録する。
    """
    flags: list[dict] = []
    prev_area = None
    for f in range(n_frames):
        regs = regions_per_frame.get(f, [])
        reasons = []

        if any(r.source != "detected" for _, r in regs):
            reasons.append("推定領域（補間/memory）を含む")
        if regs and max(r.score for _, r in regs) < 0.30:
            reasons.append("低信頼")

        area = sum(b[2] * b[3] for b, _ in regs)
        if prev_area is not None and regs:
            if prev_area > 0 and (area > prev_area * 2 or area < prev_area / 2):
                reasons.append("面積の急変")
        prev_area = area if regs else prev_area

        if reasons:
            flags.append({"frame": f, "reasons": reasons})
    return flags
