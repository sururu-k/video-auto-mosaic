// canvas への描き込み。Preact の外に置いてある。
//
// ここはフレームワークの恩恵が無い（仮想 DOM で差分を取っても canvas の
// ピクセルは減らない）ので命令的なまま。代わりに ctx 以外の依存を持たせず、
// node からそのまま呼べる形にしてある。

import type { Box, Region } from "./api.js";
import { autoBoxes, eraseVictims, SRC_COLOR } from "./review-logic.js";
import type { MarkMode } from "./review-logic.js";

/** 由来コードの表示名。タイムライン画面のラベルに使う */
export const SRC_NAME: Record<string, string> = {
  d: "検出",
  i: "補間",
  m: "memory",
  b: "橋渡し",
  x: "手修正",
};

export interface ReviewOverlayOptions {
  /** canvas の解像度。動画の解像度そのものを入れてある */
  width: number;
  height: number;
  /** そのコマの全領域（手修正を含む） */
  boxes: readonly Region[];
  /** 「モザイクの範囲を枠で示す」設定 */
  showBoxes: boolean;
  markMode: MarkMode | null;
  /** 誤検知モードで選んだ自動領域の番号（autoBoxes() の添字） */
  picked: readonly number[];
  /** 置いたがまだ確定していない矩形 */
  pending: Box | null;
  /**
   * 区間の始点として置いた矩形（issue #46）。いま見ているコマが始点その
   * ものであるときにだけ渡す（区間の全長は1コマの絵には映らないので、
   * 「始点はここだった」がその場で見えるようにする）。
   */
  startBox?: Box | null;
}

/**
 * 検査キュー画面の重ね描き。
 *
 * 「狭める」「誤検知」では枠の表示設定に関係なく自動領域を出す。何を消そうと
 * しているのか分からないまま確定させないため。
 */
export function drawReviewOverlay(
  ctx: CanvasRenderingContext2D,
  o: ReviewOverlayOptions,
): void {
  ctx.clearRect(0, 0, o.width, o.height);
  const shrink = o.markMode === "shrink";
  const erase = o.markMode === "erase";
  if (!o.showBoxes && !o.pending && !o.startBox && !shrink && !erase) return;
  // 端末では画面に対して縮んで表示されるので、線幅は解像度に比例させる
  const lw = Math.max(2, Math.round(o.width / 400));

  if (erase) {
    // 選んだものは塗りつぶし、選んでいないものは細い破線にして、
    // 「いま消えるのはこれだけ」が一目で分かるようにする
    const boxes = autoBoxes(o.boxes);
    const victims = eraseVictims(boxes, o.picked);
    boxes.forEach((r, i) => {
      const on = victims.has(i);
      const chosen = o.picked.includes(i);
      // 選んだものは実線、巻き添えで消えるものは点線。どちらも赤で描く。
      // 消えることに変わりはないので、色まで分けると見落とす
      ctx.setLineDash(on && !chosen ? [lw * 4, lw * 2] : on ? [] : [lw * 3, lw * 3]);
      ctx.lineWidth = on ? lw * 2 : lw;
      if (on) {
        ctx.fillStyle = "rgba(224, 90, 86, .30)";
        ctx.fillRect(r[0], r[1], r[2], r[3]);
      }
      ctx.strokeStyle = on ? "#ff6b66" : "#98a2b0";
      ctx.strokeRect(r[0], r[1], r[2], r[3]);
      if (chosen) {
        // 消える印。塗りだけだと「選択」と「削除」の区別がつかない
        ctx.beginPath();
        ctx.moveTo(r[0], r[1]);
        ctx.lineTo(r[0] + r[2], r[1] + r[3]);
        ctx.moveTo(r[0] + r[2], r[1]);
        ctx.lineTo(r[0], r[1] + r[3]);
        ctx.stroke();
      }
    });
    ctx.setLineDash([]);
    return;
  }

  if (shrink) {
    // 薄く塗りつぶすのは、線だけでは「どれがでかいのか」が読み取りにくいから
    ctx.lineWidth = lw * 1.5;
    for (const r of autoBoxes(o.boxes)) {
      ctx.fillStyle = "rgba(255, 165, 60, .22)";
      ctx.fillRect(r[0], r[1], r[2], r[3]);
      ctx.strokeStyle = "#ffa53c";
      ctx.strokeRect(r[0], r[1], r[2], r[3]);
    }
  } else if (o.showBoxes) {
    ctx.lineWidth = lw;
    for (const r of o.boxes) {
      ctx.strokeStyle = SRC_COLOR[r[4]] ?? "#ffffff";
      ctx.strokeRect(r[0], r[1], r[2], r[3]);
    }
  }
  if (o.pending) {
    ctx.lineWidth = lw * 1.5;
    ctx.setLineDash([lw * 4, lw * 3]);
    ctx.strokeStyle = "#ffffff";
    ctx.strokeRect(o.pending[0], o.pending[1], o.pending[2], o.pending[3]);
    ctx.setLineDash([]);
  }
  if (o.startBox) {
    // 実線・緑固定。pending（白の破線）や自動領域の色と混同しないようにする
    ctx.lineWidth = lw * 1.5;
    ctx.strokeStyle = "#5ad1a0";
    ctx.strokeRect(o.startBox[0], o.startBox[1], o.startBox[2], o.startBox[3]);
  }
}

export interface RegionOverlayOptions {
  width: number;
  height: number;
  regions: readonly Region[];
  pending: Box | null;
  /** 信頼度スライダ。検出由来のうちこれ未満は一時的に隠す */
  confMin: number;
}

/** タイムライン画面の重ね描き。由来と信頼度を文字でも出す */
export function drawRegionOverlay(
  ctx: CanvasRenderingContext2D,
  o: RegionOverlayOptions,
): void {
  ctx.clearRect(0, 0, o.width, o.height);
  ctx.lineWidth = 2;
  ctx.font = "12px sans-serif";

  for (const r of o.regions) {
    const [x, y, w, h, src, score] = r;
    if (src === "d" && score < o.confMin) continue;
    ctx.strokeStyle = SRC_COLOR[src] ?? "#ffffff";
    ctx.strokeRect(x, y, w, h);
    ctx.fillStyle = ctx.strokeStyle;
    ctx.fillText(`${SRC_NAME[src] ?? src} ${score.toFixed(2)}`, x + 2, Math.max(12, y - 3));
  }

  if (o.pending) {
    ctx.setLineDash([6, 4]);
    ctx.strokeStyle = "#ffffff";
    ctx.strokeRect(o.pending[0], o.pending[1], o.pending[2], o.pending[3]);
    ctx.setLineDash([]);
  }
}

/**
 * 1px に収まるフレーム範囲のうち、いちばん悪い被覆状況。
 *
 * 平均を取ると単発の素通しが消えて見えなくなる。0（素通し）を見つけたら
 * そこで打ち切る。それより悪いものは無い。
 */
export function worstCoverage(coverage: string, a: number, b: number, nFrames: number): number {
  let worst = 1;
  for (let f = a; f < b && f < nFrames; f++) {
    const c = coverage.charCodeAt(f) - 48;
    if (c === 0) return 0;
    if (c === 2) worst = 2;
  }
  return worst;
}

export interface TimelineBandOptions {
  width: number;
  height: number;
  coverage: string;
  nFrames: number;
  /** 手修正のあるフレーム番号 */
  correctionFrames: readonly number[];
  cur: number;
}

/** タイムラインの帯。緑=被覆あり 黄=推定のみ 赤=未処理、下段に手修正 */
export function drawTimelineBand(
  ctx: CanvasRenderingContext2D,
  o: TimelineBandOptions,
): void {
  const { width: w, height: h, coverage: cov, nFrames: n } = o;
  ctx.fillStyle = "#101216";
  ctx.fillRect(0, 0, w, h);

  const bandH = h - 10;
  for (let px = 0; px < w; px++) {
    const a = Math.floor((px * n) / w);
    const b = Math.max(a + 1, Math.floor(((px + 1) * n) / w));
    const worst = worstCoverage(cov, a, b, n);
    ctx.fillStyle = worst === 0 ? "#d0453e" : worst === 2 ? "#d9b73c" : "#3ba55d";
    ctx.fillRect(px, 0, 1, bandH);
  }

  // 手修正のあるフレームを下段に赤で立てる
  ctx.fillStyle = "#e05a5a";
  for (const f of o.correctionFrames) {
    ctx.fillRect(Math.floor((f * w) / n), bandH + 1, 2, 9);
  }

  // 現在位置
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(Math.floor((o.cur * w) / n), 0, 1, h);
}

/** 手描きモードの重ね描き。既に置いてある打点は実線、これから置くものは破線 */
export function drawHandOverlay(
  ctx: CanvasRenderingContext2D,
  o: { width: number; height: number; placed: Box | null; pending: Box | null },
): void {
  ctx.clearRect(0, 0, o.width, o.height);
  const lw = Math.max(2, Math.round(o.width / 400));
  if (o.placed) {
    ctx.lineWidth = lw;
    ctx.strokeStyle = "#ff5a5a";
    ctx.strokeRect(o.placed[0], o.placed[1], o.placed[2], o.placed[3]);
  }
  if (o.pending) {
    ctx.lineWidth = lw * 1.5;
    ctx.setLineDash([lw * 4, lw * 3]);
    ctx.strokeStyle = "#ffffff";
    ctx.strokeRect(o.pending[0], o.pending[1], o.pending[2], o.pending[3]);
    ctx.setLineDash([]);
  }
}

// ----------------------------------------------------------------------
// 検査キュー画面のトラック（issue #84）。
//
// 検査キュー画面（webapp/review.tsx）は1時間の動画で10MBを超える
// coverage 文字列や regions 全量を持たない（state を light=1 でしか
// 取らない。理由はあの画面の冒頭コメント参照）。ここは新しくその
// どちらも取りに行かない。既に毎回取っている検査キュー（QueueItem[]）
// だけから作る。
//
// QueueItem.boxes は frame_regions(frame) の結果そのもの（automosaic/
// review.py）で、そのフレームに何も塗られていなければ空配列になる。
// これは reason（"despiked" か "uncovered" か）の勝ち負けとは独立に
// 常に正しい――despike で優先度負けして reason が別の値でも、
// そのフレームに実際に塗られたものが無ければ boxes は空のまま。
// なので「未塗装かどうか」は reason 文字列ではなく boxes.length で見る。
//
// 間引いた1フレームの区間が消えないことの根拠: build_queue() の
// _sample_range() は、どんなに短い区間（1フレームでも）必ず中央値の
// 1枚を候補に入れる（それより短くしようがない）。ここが拾わなければ
// build_queue 自体がそのフレームをキューに一度も出していないということ
// で、その場合はこの画面のどんな描き方をしても直せない
// （automosaic/review.py は触らない範囲の外）。
// ----------------------------------------------------------------------

/**
 * 各画素（0..width-1）が、未塗装のフレーム標本を1つでも含むかを判定する。
 *
 * 「1画素の中に未塗装が1フレームでもあれば、その画素は未塗装ありに倒す」
 * （RULES.md 0）を素直に実装したもの。frames は昇順でなくてよい
 * （ここでソートする）。
 */
export function uncoveredPixelMask(
  frames: readonly number[],
  width: number,
  nFrames: number,
): boolean[] {
  const mask = new Array<boolean>(Math.max(0, width)).fill(false);
  if (width <= 0 || nFrames <= 0) return mask;
  const sorted = [...frames].filter((f) => f >= 0 && f < nFrames).sort((a, b) => a - b);
  let i = 0;
  for (let px = 0; px < width; px++) {
    const a = Math.floor((px * nFrames) / width);
    const b = Math.max(a + 1, Math.floor(((px + 1) * nFrames) / width));
    while (i < sorted.length && sorted[i]! < a) i++;
    mask[px] = i < sorted.length && sorted[i]! < b;
  }
  return mask;
}

/** 再生ヘッドが立つ画素。範囲外の cur は端に寄せる（画面から消さない） */
export function playheadPixel(cur: number, width: number, nFrames: number): number {
  if (width <= 0) return 0;
  if (nFrames <= 0) return 0;
  const c = Math.min(Math.max(cur, 0), nFrames - 1);
  return Math.min(width - 1, Math.max(0, Math.floor((c * width) / nFrames)));
}

/** トラック上の x 座標（画素）をフレーム番号にする。クリックでの時間移動に使う */
export function frameFromTrackX(x: number, width: number, nFrames: number): number {
  if (width <= 0 || nFrames <= 0) return 0;
  const f = Math.floor((x / width) * nFrames);
  return Math.min(nFrames - 1, Math.max(0, f));
}

export interface QueueTrackOptions {
  width: number;
  height: number;
  nFrames: number;
  /** 検査キューの項目のうち、そのフレームに塗られたものが無かったフレーム番号 */
  uncoveredFrames: readonly number[];
  /** いま見ているフレーム（プレビュー中はプレビュー先） */
  cur: number;
}

/** 検査キュー画面のトラック。緑=このキューでは未塗装が見えていない 赤=未塗装の標本あり */
export function drawQueueTrack(ctx: CanvasRenderingContext2D, o: QueueTrackOptions): void {
  const { width: w, height: h, nFrames: n } = o;
  ctx.fillStyle = "#101216";
  ctx.fillRect(0, 0, w, h);
  if (w <= 0 || n <= 0) return;

  const mask = uncoveredPixelMask(o.uncoveredFrames, w, n);
  for (let px = 0; px < w; px++) {
    ctx.fillStyle = mask[px] ? "#d0453e" : "#3ba55d";
    ctx.fillRect(px, 0, 1, h);
  }

  ctx.fillStyle = "#ffffff";
  ctx.fillRect(playheadPixel(o.cur, w, n), 0, 2, h);
}
