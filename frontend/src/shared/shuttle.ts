// J / K / L のシャトル速度（issue #79: 「連打で速くなる。標準的なシャトル」）。
//
// <video> の playbackRate は負の値を無視するブラウザが多く、逆再生に使えない。
// timeline / framestep はどちらも「1フレームだけ進める／戻す」処理を既に
// 持っている（コマ送り・コマ戻し）ので、シャトルはその処理を一定間隔で
// 繰り返して実現する。ここでは「今の速度と押されたキーの向きから、次の
// 速度がいくつになるか」という判断だけを DOM から切り離して置く。
// 実際にタイマーを回すのは画面側（useEffect + setInterval）。

/** -1=逆再生 / 0=停止 / 1=順再生 */
export type ShuttleDir = -1 | 0 | 1;

/** これ以上は速くしない。速すぎるとサーバへのフレーム要求が追いつかない */
export const SHUTTLE_MAX = 8;

/**
 * 次の速度（signed。絶対値が倍率、符号が向き）。
 *
 * - K（dir=0）は常に停止。
 * - 止まっているところから J / L を押すと1倍で動き出す。
 * - 同じ向きを連打すると倍々で加速し、SHUTTLE_MAX で頭打ち。
 * - 動いている向きと逆を押すと、減速ではなく逆向きの1倍から入り直す
 *   （「反対を1回押しただけで一気に逆最大速度」を避ける）。
 */
export function nextShuttleSpeed(cur: number, dir: ShuttleDir): number {
  if (dir === 0) return 0;
  if (cur === 0) return dir;
  if (Math.sign(cur) !== dir) return dir;
  const doubled = Math.abs(cur) * 2;
  return dir * Math.min(doubled, SHUTTLE_MAX);
}
