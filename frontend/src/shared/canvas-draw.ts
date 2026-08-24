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
