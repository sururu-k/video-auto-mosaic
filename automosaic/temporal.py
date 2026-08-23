"""時間方向の安定化。

実装の処理順（process() の呼び出し順がこれ）:
    幾何フィルタ -> トラッキング -> 結合 -> デスパイク
    -> 補間 + frame memory -> 橋渡し -> 膨張

docs/01-technical-design.md の設計は「幾何フィルタ -> トラッキング -> デスパイク
-> 補間」の4段で、結合と橋渡しは後から足した段。位置は上の通り。

順序で効いているのは2つ。

「デスパイクは補間より先」。順序を逆にすると、一瞬の誤検出が補間で長い区間に
引き伸ばされる。

「結合はデスパイクより先」。逆にすると、単発の弱い検出が「短命かつ低スコア」
として先に捨てられ、結合処理がそれを見る前に消える。実素材で、位置が15pxしか
離れていない検出が繋がらずに1.8秒の穴になっていた。

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
    # フレーム面積比がこれを超える検出は「大きすぎる」として数える。捨てはしない。
    # 以前はここで捨てていたが、接写や全画面のアップは「大きく映っている＝いちばん
    # 塞ぐべき」場面で、実測で比 0.36 の検出が全フレーム落ちて被覆 0 になっていた。
    # 塗り過ぎは許容できるが漏れは許容できないので、捨てずに残して件数だけ報告する。
    max_area_ratio: float = 0.35
    # フレームをまるごと覆う検出を捨てる比の閾値（0で無効）。既定は無効。
    # 「見えている面積で比を計算する」に変えた副作用で、画面を覆い切る矩形は
    # 位置に関わらずすべて visible_ratio が厳密に 1.0 に潰れるようになった
    # （検出器は box をフレームにクランプせず外接矩形を出すので、四方どこにはみ
    # 出していても 1.0 になる）。全面ドアップは「いちばん塞ぐべき場面」であり、
    # 全画面がモザイクになるのは正しい出力。誤検出が画面全体を覆っても塗り過ぎ側
    # （許容できる）に倒れるだけなので、既定では捨てない。実測で全編ドアップの
    # 動画が 60/60 -> 0/60 まで落ちるのを確認して既定を無効に変えた。
    # 値そのものは残してあるので、位置情報を持たない矩形を明示的に弾きたい
    # 特殊な用途があれば 1.0 などを指定すればよい。
    drop_area_ratio: float = 0.0
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
) -> tuple[dict[int, list[Detection]], int, int]:
    """ありえない大きさの検出を落とす。落とした件数と、大きすぎた件数も返す。

    落とすのは次の2つだけ。どちらも「塞ぐ範囲を決められない」もの。
      - 幅か高さが 1px 以下（面積ゼロで塗りようがない）
      - 生の面積比が min_area_ratio 未満（既定0で無効。検出器のノイズ由来の
        極小矩形を想定）
      - フレームをまるごと覆う（drop_area_ratio。既定0で無効。位置の情報が無い）

    max_area_ratio を超えるだけの検出は落とさない。大きく映っている場面ほど
    塞ぐべきなので、ここで捨てると漏れる側に外れる。件数だけ数えて返す。

    min_area_ratio は「見えている面積」でなく矩形そのものの生の面積比で見る。
    可視面積で判定すると、対象自体は十分な大きさでも画面端に来た瞬間に比が
    縮んで「小さすぎる」として捨てられる方向に倒れる（漏れる側）。実測で
    (600,440,200,200)（右下にはみ出し）が生の比0.13・可視比0.005になり、
    min_area_ratio=0.01 では可視比だと drop・生の比なら keep だった。
    対象の実サイズをそのまま評価するため生の面積を使う。

    drop_area_ratio（フレーム全体を覆っているかの判定）は逆に「はみ出した分を
    除いた実際に見えている面積」で見る。はみ出した矩形の生の面積で判定すると、
    画面の一部しか覆っていない検出まで比 1.0 超と数えられて捨てられてしまう。

    戻り値: (残した検出, 落とした件数, 大きすぎた件数)
    """
    frame_area = float(frame_w * frame_h)
    out: dict[int, list[Detection]] = {}
    dropped = 0
    oversized = 0
    for f, dets in per_frame.items():
        kept = []
        for d in dets:
            if d.box[2] <= 1 or d.box[3] <= 1:
                dropped += 1
                continue
            raw_ratio = (
                (float(d.box[2]) * float(d.box[3])) / frame_area if frame_area else 0.0
            )
            if raw_ratio < cfg.min_area_ratio:
                dropped += 1
                continue
            visible_ratio = _visible_ratio(d.box, frame_w, frame_h) if frame_area else 0.0
            if cfg.drop_area_ratio > 0 and visible_ratio >= cfg.drop_area_ratio:
                dropped += 1
                continue
            if cfg.max_area_ratio > 0 and visible_ratio > cfg.max_area_ratio:
                oversized += 1
            kept.append(d)
        out[f] = kept
    return out, dropped, oversized


def _visible_ratio(box: Box, frame_w: int, frame_h: int) -> float:
    """矩形のうちフレーム内に入っている面積の、フレーム面積に対する比。"""
    frame_area = float(frame_w * frame_h)
    if frame_area <= 0:
        return 0.0
    x0 = max(0.0, float(box[0]))
    y0 = max(0.0, float(box[1]))
    x1 = min(float(frame_w), float(box[0]) + float(box[2]))
    y1 = min(float(frame_h), float(box[1]) + float(box[3]))
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0) / frame_area


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


def despike(
    tracks: list[Track], cfg: TemporalConfig
) -> tuple[list[Track], int, list[tuple[int, int, str, float]]]:
    """短命かつ低スコアのトラックを落とす。補間より前に行うこと。

    Recall 優先なので、スコアが高いものは1フレームだけでも残す。既定は無効
    （min_track_len=0）。塗り過ぎのコストは許容できるが漏れは許容できないので、
    捨てる理由がない（docs/09-mosaic-quality.md S4 の実測: 確実に映っている区間内の
    実観測125件を捨て、うち40件はそのフレームが素通しになっていた）。

    それでも --despike で明示的に有効化された場合、「判断できない区間を黙って
    素通しにしない」という設計原則を守るため、捨てた場所（開始/終了フレーム・
    クラス・最大スコア）も返す。呼び出し側はこれを必ず報告すること。

    track_min_peak を指定すると2閾値のヒステリシスになる。
    「トラック内で一度でも track_min_peak を超えたものだけを有効とし、
    そのトラック内は検出しきい値まで拾う」という Canny と同じ発想。
    検出しきい値を極端に下げたときの誤検出を、フレーム単位でなく
    トラック単位で落とせる。ただし弱い検出しか出ない本物も落ちるので、
    実素材で効果を測ってから使うこと。既定は無効。
    """
    kept, dropped = [], 0
    dropped_ranges: list[tuple[int, int, str, float]] = []
    for t in tracks:
        if cfg.track_min_peak > 0 and t.max_score < cfg.track_min_peak:
            dropped += 1
            dropped_ranges.append((t.first, t.last, t.cls, t.max_score))
            continue
        if len(t) < cfg.min_track_len and t.max_score < cfg.despike_conf:
            dropped += 1
            dropped_ranges.append((t.first, t.last, t.cls, t.max_score))
            continue
        kept.append(t)
    return kept, dropped, dropped_ranges


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

    filtered, n_geo_dropped, n_oversized = geometric_filter(
        filtered, frame_w, frame_h, cfg
    )
    tracks = build_tracks(filtered, n_frames, cfg)
    n_tracks_before = len(tracks)
    # 結合を先に、デスパイクを後に行う。順序が逆だと、単発の低スコア検出が
    # 「短命かつ低スコア」として先に捨てられ、結合処理がそれを見る前に消える。
    # 実際、位置が15pxしか離れていない検出が繋がらずに1.8秒の穴になっていた。
    # 先に繋いでおけば、まとまったトラックとして評価されるので生き残る。
    tracks, n_stitched = stitch_tracks(tracks, frame_w, frame_h, cfg)
    tracks, n_despiked, despiked_ranges = despike(tracks, cfg)

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
        "oversized_kept": n_oversized,
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
    # despike が有効（min_track_len > 0 か track_min_peak > 0）で何か捨てていたら、
    # 場所を必ず出せるように渡しておく。既定では despike 無効なので通常は空。
    stats["_despiked_ranges"] = despiked_ranges
    return out, stats


def _infer_frame_step(
    regions_per_frame: dict[int, list[tuple[Box, Region]]], n_frames: int
) -> int:
    """実観測（source=="detected"）フレームの間隔から、検出のサンプリング刻みを推す。

    --frame-step N で間引き検出した素材は、実観測が N フレームおきにしか無い。
    その隙間は「検出器が働いていない」のではなく、そもそも検出していないだけ
    なので、estimated_only_ranges から見ると N-1 フレームの推定区間が構造的に
    大量発生する。これはノイズであって漏れの情報を持たない。

    間隔は「複数回現れて初めて信用する」。1回しか現れない間隔は、たまたま
    実観測が離れて出ただけ（本物の検出漏れ）の可能性が高く、それを間引きの
    刻みとして扱うと本物の短い漏れを握りつぶしてしまう（漏れる側に外れる）。
    そのため2回以上現れる間隔の最小値だけを刻みとみなし、無ければ 1
    （＝間引きなし・フィルタしない）を返す。

    実測（data/bench3, 55303フレーム, 5フレーム刻み検出）: 実観測間隔の内訳は
    5(10800件) / 10(55件) / 15(16件) / それ以外少数、最小の再現間隔は 5 で
    実際の --frame-step と一致した。
    """
    frames = sorted(
        f
        for f in range(n_frames)
        if any(r.source == "detected" for _, r in regions_per_frame.get(f, []))
    )
    if len(frames) < 2:
        return 1
    counts: dict[int, int] = {}
    for a, b in zip(frames, frames[1:]):
        d = b - a
        if d > 0:
            counts[d] = counts.get(d, 0) + 1
    repeated = [d for d, c in counts.items() if c >= 2]
    return min(repeated) if repeated else 1


def uncovered_ranges(
    regions_per_frame: dict[int, list[tuple[Box, "Region"]]],
    n_frames: int,
) -> list[tuple[int, int]]:
    """最終的な regions を数え直して、領域が1つも無いフレームの連続区間を返す。

    issue #4: `bridge_uncovered()` が返す left_open は `corr.apply()` を通す前の
    値であり、その後 add/remove の手修正で regions が書き換わっても反映されない。
    「誤検知」判定は add を伴わない bare remove を置く操作なので、これで自動領域
    だけが残っていたフレームが空になっても left_open には現れず、安全表示
    （素通しの区間 N 件）が 0 件のまま嘘をつく（実測: remove 対象の10フレームが
    実際は素通しなのに n_uncovered_ranges=0 と表示された）。

    ここでは corr.apply() 済みの regions_per_frame を直接見て空フレームを
    数え直すので、add で埋まった分は正しく除外され、remove で空になった分は
    正しく検出される。呼び出し側（cli.py）は手修正の有無に関わらず、
    ここで数え直した結果だけを「素通しの区間」として表示・レポートすること。

    戻り値の (start, end) は bridge_uncovered() の left_open と同じ半開区間
    （[start, end)）。手修正が無ければ left_open と同じ結果になる。
    """
    out: list[tuple[int, int]] = []
    start = None
    for f in range(n_frames):
        if regions_per_frame.get(f):
            if start is not None:
                out.append((start, f))
                start = None
        else:
            if start is None:
                start = f
    if start is not None:
        out.append((start, n_frames))
    return out


def frames_with_mosaic_count(
    regions_per_frame: dict[int, list[tuple[Box, "Region"]]],
    n_frames: int,
) -> int:
    """issue #41: regions を数え直して、実際に領域が1つでもあるフレーム数を返す。

    `process()` が返す stats["frames_with_mosaic"] は corr.apply() を通す前の
    値であり、手修正（とくに add を伴わない bare remove）で regions が空になった
    フレームがあっても減らない。「モザイク適用率」はこの値から計算されるので、
    `uncovered_ranges()` で数え直した素通し区間と矛盾した数字が同じ画面に並ぶ
    （実測: 適用率100.0%・uncovered_gaps 0 と、素通し区間10フレームが同時に出た）。

    呼び出し側（cli.py）は corr.apply() 済みの regions_per_frame を渡すこと。
    `uncovered_ranges()` と対になる数え方（空でないフレームの数）なので、
    n_frames からこの戻り値を引いたものが uncovered_ranges の総フレーム数と一致する。
    """
    return sum(1 for f in range(n_frames) if regions_per_frame.get(f))


def estimated_only_ranges(
    regions_per_frame: dict[int, list[tuple[Box, Region]]],
    n_frames: int,
    min_len: int = 1,
    frame_step: int = 0,
) -> list[tuple[int, int, float]]:
    """実観測が1つも無く、推定だけで覆っているフレームの連続区間。

    ここは検出器が実際に効いていない区間で、モザイクの位置は当てずっぽうに近い。
    自動処理の限界がそのまま出るので、人手レビューの最優先対象になる。
    戻り値は (開始, 終了, 区間内の最大 hold)。

    min_len は既定で1、つまり1フレームでも報告から落とさない。以前の既定5では
    2〜4フレームの区間がまるごと消えていたが、実素材では漏れが 0.2 秒（数フレーム）
    の粒度で出入りするので、そこが見えないと意味がない。報告から消える方向は
    見逃す方向なので、短い区間ほど落としてはいけない。

    ただし --frame-step > 1 で間引き検出した素材では、min_len を単純に1に
    固定すると別の問題が出る。実観測どうしの間が構造的に N-1 フレーム空くため、
    その間引きの穴そのものが「推定のみ区間」として大量に報告され、本物の漏れが
    ノイズに埋もれる（実測: bench3 で min_len=1 だと 3524 区間のうち 2399 件が
    ちょうど間引き幅ぶんの4フレーム区間だった）。

    frame_step はここでは呼び出し側から渡さず（既定0）、regions_per_frame の
    実観測間隔から自動推定する。呼び出し元（cli.py 等）はこの関数を
    `estimated_only_ranges(regions, n_frames)` の2引数でしか呼んでおらず、
    そちらを触らずに直すためにシグネチャは後方互換を保ったまま、既定引数で
    自動推定を「吸収」している。明示的に frame_step を渡した場合はそちらを使う。

    実効的な最小長は max(min_len, 推定frame_step)。間引きなし（frame_step=1）の
    素材では従来どおり min_len=1 のまま全区間が報告される。
    """
    if frame_step <= 0:
        frame_step = _infer_frame_step(regions_per_frame, n_frames)
    effective_min_len = max(min_len, frame_step)

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
            if start is not None and f - start >= effective_min_len:
                out.append((start, f - 1, peak))
            start = None
    if start is not None and n_frames - start >= effective_min_len:
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
