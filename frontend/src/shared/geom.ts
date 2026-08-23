// 座標まわり。
//
// 画面には2つの座標系が混ざっている。どちらも数値2つの組なので、
// 取り違えても JS では何も起きず、モザイクの位置だけが静かにずれる。
// 別の型として区別して、代入の時点で止まるようにしてある。
//
//   NormPoint   正規化タップ座標 (0..1)。端末の画面幅・回転・拡大率に
//               左右されない。サーバへ送るのはこちら
//   FramePoint  フレーム座標（動画そのもののピクセル）。canvas への描画と
//               当たり判定に使う

import type { Box } from "./api.js";

declare const NORM: unique symbol;
declare const FRAME: unique symbol;

/** 正規化タップ座標 (0..1)。中身は [x, y] の並びのまま */
export type NormPoint = readonly [number, number] & { readonly [NORM]: true };

/** フレーム座標のピクセル。中身は [x, y] の並びのまま */
export type FramePoint = readonly [number, number] & { readonly [FRAME]: true };

/** 矩形の大きさ [w, h]。フレーム座標のピクセル */
export type Size = readonly [number, number];

export function normPoint(x: number, y: number): NormPoint {
  return [x, y] as unknown as NormPoint;
}

export function framePoint(x: number, y: number): FramePoint {
  return [x, y] as unknown as FramePoint;
}

/**
 * 既定サイズを百分率で伸縮させる。
 * 8px を下限にしてあるのは、これより小さいとモザイクのブロック1つ分にも
 * 満たず、置いても隠れないため。
 */
export function scaledSize(base: Size, pct: number): [number, number] {
  return [
    Math.max(8, Math.round((base[0] * pct) / 100)),
    Math.max(8, Math.round((base[1] * pct) / 100)),
  ];
}

/**
 * 正規化タップ座標を、そこを中心とする矩形に直す。
 *
 * review.py の tap_to_box と同じ規則で置く。ここで違う計算をすると
 * 「見えている枠」と「実際に塞がれる場所」がずれる。端に寄ったタップでは
 * 矩形を切り詰めずに内側へ押し戻す（切り詰めると画面端の対象を覆いきれない）。
 */
export function tapToBox(p: NormPoint, size: Size, width: number, height: number): Box {
  const w = Math.max(4, Math.min(width, size[0]));
  const h = Math.max(4, Math.min(height, size[1]));
  const cx = Math.min(Math.max(p[0], 0), 1) * width;
  const cy = Math.min(Math.max(p[1], 0), 1) * height;
  return [
    Math.min(Math.max(cx - w / 2, 0), width - w),
    Math.min(Math.max(cy - h / 2, 0), height - h),
    w,
    h,
  ];
}

/**
 * 要素上のポインタ位置を正規化座標にする。
 * 画面ピクセルではなく比率で持つので、端末の拡大率や回転で意味が変わらない。
 */
export function normFromClient(
  rect: { left: number; top: number; width: number; height: number },
  clientX: number,
  clientY: number,
): NormPoint {
  return normPoint((clientX - rect.left) / rect.width, (clientY - rect.top) / rect.height);
}

/** 要素上のポインタ位置をフレーム座標にする */
export function frameFromClient(
  rect: { left: number; top: number; width: number; height: number },
  clientX: number,
  clientY: number,
  width: number,
  height: number,
): FramePoint {
  return framePoint(
    ((clientX - rect.left) / rect.width) * width,
    ((clientY - rect.top) / rect.height) * height,
  );
}
