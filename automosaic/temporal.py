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

try:  # scipy があれば最適割当を使う。無くても動くようにしておく
    from scipy.optimize import linear_sum_assignment as _lsa
except Exception:  # noqa: BLE001
    _lsa = None

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
    source: str  # "detected" | "interpolated" | "memory" | "bridged" | "manual"
    speed: float = 0.0  # このフレーム付近での移動量 px/frame
    hold: int = 0       # 直近の実観測から何フレーム離れた推定か


@dataclass
class TemporalConfig:
    iou_threshold: float = 0.20
    max_gap: int = 12          # トラック継続を許す欠損フレーム数
    memory: int = 6            # トラック端を後ろへ保持するフレーム数
    memory_before: int = 0     # トラック開始前へ遡って保持するフレーム数（0でmemoryと同じ）
    stitch_max_gap: int = 90   # 途切れたトラック同士を繋ぐ最大フレーム間隔（0で無効）
    stitch_iou: float = 0.10   # 繋ぐ条件その1: 端の矩形の IoU
    stitch_dist_ratio: float = 0.12  # 繋ぐ条件その2: 中心間距離が対角のこの比以内
    stitch_size_ratio: float = 5.0   # 大きさがこの倍率を超えて違うものは繋がない
    min_track_len: int = 2     # これ未満のトラックはデスパイク対象
    despike_conf: float = 0.35 # 最大スコアがこれ未満の短いトラックだけ落とす
    track_min_peak: float = 0.0  # トラック内の最大スコアがこれ未満なら丸ごと捨てる（0で無効）
    min_area_ratio: float = 0.0000  # フレーム面積比の下限（0で無効）
    max_area_ratio: float = 0.35    # これを超える検出は誤検出とみなす
    margin_scale: float = 1.0    # 膨張マージン全体の倍率
    jpeg_margin: int = 4         # 圧縮アーティファクトの染み出し対策
    base_ratio: float = 0.15     # 基礎マージン = sqrt(面積) * この比
    base_min: float = 6.0
    base_max: float = 40.0
    score_reference: float = 0.5 # このスコアに達したら「十分に確からしい」とみなす
    confidence_weight: float = 0.5   # 低信頼のときに基礎マージンへ上乗せする割合
    estimated_factor: float = 1.3    # 補間/memory/橋渡し由来の領域に掛ける倍率
    margin_cap_ratio: float = 0.5    # マージンは sqrt(面積) のこの比までで頭打ち
    margin_cap_px: float = 0.0       # 絶対上限 px（0で無効）
    motion_weight: float = 2.0       # 局所速度に掛ける係数。動く対象の追従遅れを吸収する
    motion_cap: float = 60.0         # 動き由来のマージンの上限 px
    hold_growth: float = 0.25        # 実観測から1フレーム離れるごとに広げる割合（速度比）
    hold_cap: float = 48.0           # 不確かさ由来のマージンの上限 px
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


def _assign(cost: list[list[float]], max_cost: float) -> list[tuple[int, int]]:
    """トラックと検出の対応付けを解く。

    scipy があればハンガリアン法で全体最適に解く。IoU の大きい順に貪欲で
    取っていくと、対象が複数あるときに片方が取り違えられて軌跡が入れ替わる。
    scipy が無い環境では従来どおり貪欲で近似する。
    """
    if not cost or not cost[0]:
        return []

    pairs: list[tuple[int, int]] = []
    if _lsa is not None:
        import numpy as _np

        m = _np.asarray(cost, dtype=float)
        rows, cols = _lsa(m)
        for r, c in zip(rows, cols):
            if m[r, c] <= max_cost:
                pairs.append((int(r), int(c)))
        return pairs

    flat = sorted(
        (
            (cost[i][j], i, j)
            for i in range(len(cost))
            for j in range(len(cost[0]))
            if cost[i][j] <= max_cost
        )
    )
    used_r: set[int] = set()
    used_c: set[int] = set()
    for _, i, j in flat:
        if i in used_r or j in used_c:
            continue
        used_r.add(i)
        used_c.add(j)
        pairs.append((i, j))
    return pairs


def build_tracks(
    per_frame: dict[int, list[Detection]], n_frames: int, cfg: TemporalConfig
) -> list[Track]:
    """クラスごとにトラックを作る。

    max_gap フレームまでは検出が無くてもトラックを生かしておく。ByteTrack の
    「低スコア検出を捨てずに軌跡で救済する」考え方の簡易版。
    対応付けはクラスごとに線形割当で解く（_assign 参照）。
    """
    tracks: list[Track] = []
    active: list[tuple[Track, int, Box]] = []  # (track, last_seen_frame, last_box)

    for f in range(n_frames):
        dets = per_frame.get(f, [])

        # 期限切れのトラックを active から外す
        active = [(t, lf, lb) for (t, lf, lb) in active if f - lf <= cfg.effective_max_gap]

        matched_det: set[int] = set()

        # クラスごとに割当を解く。クラスをまたいだ対応付けはしない
        classes = {d.cls for d in dets}
        for cls in classes:
            t_idx = [i for i, (t, _, _) in enumerate(active) if t.cls == cls]
            d_idx = [i for i, d in enumerate(dets) if d.cls == cls]
            if not t_idx or not d_idx:
                continue

            cost = [
                [
                    1.0 - _iou(active[ti][2], tuple(float(v) for v in dets[di].box))
                    for di in d_idx
                ]
                for ti in t_idx
            ]
            for r, c in _assign(cost, 1.0 - cfg.iou_threshold):
                ti, di = t_idx[r], d_idx[c]
                t, _, _ = active[ti]
                box = tuple(float(v) for v in dets[di].box)
                t.obs[f] = (box, dets[di].score)
                active[ti] = (t, f, box)
                matched_det.add(di)

        # マッチしなかった検出は新規トラック
        for di, d in enumerate(dets):
            if di in matched_det:
                continue
            box = tuple(float(v) for v in d.box)
            t = Track(cls=d.cls, obs={f: (box, d.score)})
            tracks.append(t)
            active.append((t, f, box))

    return tracks


def despike(tracks: list[Track], cfg: TemporalConfig) -> tuple[list[Track], int]:
    """短命かつ低スコアのトラックを落とす。補間より前に行うこと。

    Recall 優先なので、スコアが高いものは1フレームだけでも残す。

    track_min_peak を指定すると2閾値のヒステリシスになる。
    「トラック内で一度でも track_min_peak を超えたものだけを有効とし、
    そのトラック内は検出しきい値まで拾う」という Canny と同じ発想。
    検出しきい値を極端に下げたときの誤検出を、フレーム単位でなく
    トラック単位で落とせる。ただし弱い検出しか出ない本物も落ちるので、
    実素材で効果を測ってから使うこと。既定は無効。
    """
    kept, dropped = [], 0
    for t in tracks:
        if cfg.track_min_peak > 0 and t.max_score < cfg.track_min_peak:
            dropped += 1
            continue
        if len(t) < cfg.min_track_len and t.max_score < cfg.despike_conf:
            dropped += 1
            continue
        kept.append(t)
    return kept, dropped


def stitch_tracks(
    tracks: list[Track], frame_w: int, frame_h: int, cfg: TemporalConfig
) -> tuple[list[Track], int]:
    """途切れ途切れのトラックを、位置と大きさが近ければ1本に繋ぐ。

    検出が数秒のスパンで飛び飛びになると、max_gap を超えてトラックが分断され、
    「モザイク有り・なし・有り」になる。同じ対象なら座標も大きさも似ているはずなので、
    それを手がかりに繋ぎ直す。繋がった区間は既存の線形補間が埋めるので、
    結果としてモザイクが持続する。

    繋ぐ条件は3つすべてを満たすこと:
      - 時間の隔たりが stitch_max_gap 以内
      - 端の矩形が重なっている（IoU）か、中心が近い
      - 大きさが極端に違わない
    """
    if cfg.stitch_max_gap <= 0 or len(tracks) < 2:
        return tracks, 0

    diag = math.hypot(frame_w, frame_h)
    max_dist = diag * cfg.stitch_dist_ratio

    # 開始フレーム順に見て、後ろのトラックを貪欲に吸収する
    order = sorted(range(len(tracks)), key=lambda i: tracks[i].first)
    alive = {i: tracks[i] for i in order}
    merged = 0

    for i in order:
        a = alive.get(i)
        if a is None:
            continue
        changed = True
        while changed:
            changed = False
            best = None
            for j in order:
                if j == i or j not in alive:
                    continue
                b = alive[j]
                if b.cls != a.cls:
                    continue
                gap = b.first - a.last
                if gap <= 0 or gap > cfg.stitch_max_gap:
                    continue

                box_a = a.obs[a.last][0]
                box_b = b.obs[b.first][0]

                area_a = max(1.0, box_a[2] * box_a[3])
                area_b = max(1.0, box_b[2] * box_b[3])
                ratio = max(area_a, area_b) / min(area_a, area_b)
                if ratio > cfg.stitch_size_ratio:
                    continue

                ca, cb = _center(box_a), _center(box_b)
                dist = math.hypot(cb[0] - ca[0], cb[1] - ca[1])
                if _iou(box_a, box_b) < cfg.stitch_iou and dist > max_dist:
                    continue

                # 隔たりが小さいものから繋ぐ
                if best is None or gap < best[0]:
                    best = (gap, j)

            if best is not None:
                _, j = best
                b = alive.pop(j)
                a.obs.update(b.obs)
                merged += 1
                changed = True

    return list(alive.values()), merged


def _lerp_box(a: Box, b: Box, w: float) -> Box:
    return tuple(a[i] + (b[i] - a[i]) * w for i in range(4))  # type: ignore[return-value]


def densify(
    tracks: list[Track], n_frames: int, cfg: TemporalConfig
) -> dict[int, list[Region]]:
    """補間と frame memory でトラックを連続化し、フレームごとの領域に落とす。"""
    per_frame: dict[int, list[Region]] = {i: [] for i in range(n_frames)}

    for t in tracks:
        frames = sorted(t.obs)
        local = _local_speeds(t, frames)

        # 観測フレームをそのまま置く
        for f in frames:
            box, score = t.obs[f]
            per_frame[f].append(Region(box, t.cls, score, "detected", local[f]))

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
                        max(local[a], local[b]),
                        min(f - a, b - f),
                    )
                )

        # トラック端を前後に保持する（frame memory）。
        # 計算コストほぼゼロで検出漏れを潰せるので最初に入れるべき機構。
        step = max(1, cfg.frame_step)
        mem_after = cfg.memory * step
        mem_before = (cfg.memory_before or cfg.memory) * step
        # memory 区間は矩形を固定してはいけない。対象が動いていると置いていかれる。
        # 端点の速度で外挿し、観測から離れた分は hold として持たせて、
        # expand 側で不確かさぶんを広げる。
        if mem_before > 0:
            head_box, head_score = t.obs[frames[0]]
            vx, vy = _endpoint_velocity(t, frames, at_start=True)
            for f in range(max(0, frames[0] - mem_before), frames[0]):
                d = frames[0] - f
                per_frame[f].append(
                    Region(
                        _shift_box(head_box, -vx * d, -vy * d),
                        t.cls,
                        head_score,
                        "memory",
                        local[frames[0]],
                        d,
                    )
                )
        if mem_after > 0:
            tail_box, tail_score = t.obs[frames[-1]]
            vx, vy = _endpoint_velocity(t, frames, at_start=False)
            for f in range(frames[-1] + 1, min(n_frames, frames[-1] + mem_after + 1)):
                d = f - frames[-1]
                per_frame[f].append(
                    Region(
                        _shift_box(tail_box, vx * d, vy * d),
                        t.cls,
                        tail_score,
                        "memory",
                        local[frames[-1]],
                        d,
                    )
                )

    return per_frame


def _local_speeds(track: Track, frames: list[int]) -> dict[int, float]:
    """観測フレームごとの局所的な移動量 px/frame。

    全トラック共通の中央値を使うと、ピストン運動のように速く動く対象に
    マージンが全く足りなくなる。逆に静止している対象は無駄に膨らむ。
    前後の観測との変位から、そのフレーム付近での実際の速さを出す。
    """
    if len(frames) < 2:
        return {f: 0.0 for f in frames}

    out: dict[int, float] = {}
    for i, f in enumerate(frames):
        vals = []
        if i > 0:
            a, b = frames[i - 1], f
            ca, cb = _center(track.obs[a][0]), _center(track.obs[b][0])
            vals.append(math.hypot(cb[0] - ca[0], cb[1] - ca[1]) / max(1, b - a))
        if i < len(frames) - 1:
            a, b = f, frames[i + 1]
            ca, cb = _center(track.obs[a][0]), _center(track.obs[b][0])
            vals.append(math.hypot(cb[0] - ca[0], cb[1] - ca[1]) / max(1, b - a))
        out[f] = max(vals) if vals else 0.0
    return out


def _endpoint_velocity(track: Track, frames: list[int], at_start: bool) -> tuple[float, float]:
    """トラック端での速度ベクトル px/frame。memory 区間の外挿に使う。

    端の数観測から求める。1点しかなければ 0（外挿せず固定）。
    """
    if len(frames) < 2:
        return (0.0, 0.0)
    if at_start:
        a, b = frames[0], frames[min(2, len(frames) - 1)]
    else:
        a, b = frames[max(0, len(frames) - 3)], frames[-1]
    dt = b - a
    if dt <= 0:
        return (0.0, 0.0)
    ca, cb = _center(track.obs[a][0]), _center(track.obs[b][0])
    return ((cb[0] - ca[0]) / dt, (cb[1] - ca[1]) / dt)


def _shift_box(box: Box, dx: float, dy: float) -> Box:
    return (box[0] + dx, box[1] + dy, box[2], box[3])


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

        score = min(
            min((r.score for r in before), default=0.0),
            min((r.score for r in after), default=0.0),
        )
        cls = before[0].cls if before else after[0].cls

        # 前後それぞれの代表矩形を線形に繋ぐ。外接矩形で塞ぐと、対象が大きく
        # 動いた区間で矩形が画面の大半を覆ってしまう。補間なら必要な範囲に収まる。
        box_a = _union_box([r.box for r in before]) if before else None
        box_b = _union_box([r.box for r in after]) if after else None
        span = end - start + 1

        for i in range(start, end):
            if box_a is not None and box_b is not None:
                w = (i - start + 1) / span
                box = _lerp_box(box_a, box_b, w)
            else:
                box = box_a or box_b
            speed = max(
                (r.speed for r in before), default=0.0
            )
            speed = max(speed, max((r.speed for r in after), default=0.0))
            per_frame[i].append(
                Region(box, cls, score, "bridged", speed, min(i - start + 1, end - i))
            )
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
) -> Box:
    """膨張マージンを乗せる。

    margin = base + motion + confidence + jpeg
      base       面積依存の基礎マージン
      motion     速く動くほど厚く（検出漏れは動きの速い場面で起きる）
      confidence 低信頼ほど厚く
      jpeg       圧縮アーティファクトの染み出し対策

    信頼度の項は素の (1 - score) を使ってはいけない。このモデルはスコアが
    全体的に低く、(1 - score) がほぼ常に1になって基礎マージンを何倍にも
    膨らませてしまう。score_reference で正規化し、上限も設ける。

    最終的なマージンは対象の大きさに対する比で頭打ちにする。小さい対象を
    画面いっぱいに潰さないための歯止め。
    """
    x, y, w, h = region.box
    area = max(1.0, w * h)
    scale = math.sqrt(area)

    base = min(max(cfg.base_ratio * scale, cfg.base_min), cfg.base_max)
    score_norm = min(1.0, max(0.0, region.score) / max(1e-6, cfg.score_reference))
    confidence = (1.0 - score_norm) * base * cfg.confidence_weight

    # 大きさに由来する分。ここは「潰しすぎ」に直結するので上限で抑える
    static = (base + confidence + cfg.jpeg_margin) * cfg.margin_scale
    if region.source != "detected":
        static *= cfg.estimated_factor
    static = min(static, cfg.margin_cap_ratio * scale + cfg.base_min)
    if cfg.margin_cap_px > 0:
        static = min(static, cfg.margin_cap_px)

    # 動きに由来する分は上限の外に出す。追従の遅れを吸収するためのもので、
    # ここを削ると速く動く場面で対象の先端がモザイクから飛び出す。
    # 静止している対象には乗らないので、全体が太る心配はない。
    motion = min(
        region.speed * cfg.motion_weight * max(1, cfg.frame_step),
        cfg.motion_cap,
    )

    # 実観測から離れた推定ほど、対象がどこにいるか分からない。
    # 外挿しても誤差は溜まるので、離れた分だけ覆う範囲を広げる。
    uncertainty = min(
        region.speed * cfg.hold_growth * region.hold,
        cfg.hold_cap,
    )

    margin = static + motion + uncertainty

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
    # 結合を先に、デスパイクを後に行う。順序が逆だと、単発の低スコア検出が
    # 「短命かつ低スコア」として先に捨てられ、結合処理がそれを見る前に消える。
    # 実際、位置が15pxしか離れていない検出が繋がらずに1.8秒の穴になっていた。
    # 先に繋いでおけば、まとまったトラックとして評価されるので生き残る。
    tracks, n_stitched = stitch_tracks(tracks, frame_w, frame_h, cfg)
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
        out[f] = [(expand(r, frame_w, frame_h, cfg), r) for r in regions]

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
        "tracks_stitched": n_stitched,
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


def estimated_only_ranges(
    regions_per_frame: dict[int, list[tuple[Box, Region]]],
    n_frames: int,
    min_len: int = 5,
) -> list[tuple[int, int, float]]:
    """実観測が1つも無く、推定だけで覆っているフレームの連続区間。

    ここは検出器が実際に効いていない区間で、モザイクの位置は当てずっぽうに近い。
    自動処理の限界がそのまま出るので、人手レビューの最優先対象になる。
    戻り値は (開始, 終了, 区間内の最大 hold)。
    """
    out: list[tuple[int, int, float]] = []
    start = None
    peak = 0
    for f in range(n_frames):
        regs = regions_per_frame.get(f, [])
        has_real = any(r.source == "detected" or r.source == "manual" for _, r in regs)
        if regs and not has_real:
            if start is None:
                start, peak = f, 0
            peak = max(peak, max((r.hold for _, r in regs), default=0))
        else:
            if start is not None and f - start >= min_len:
                out.append((start, f - 1, peak))
            start = None
    if start is not None and n_frames - start >= min_len:
        out.append((start, n_frames - 1, peak))
    return out


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
