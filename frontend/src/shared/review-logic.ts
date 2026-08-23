// 検査キュー画面の判断。Preact にも DOM にも依存しない。
//
// 画面部品から切り離してあるのは、ここが間違うと「押した覚えのないものが
// 消える」類の事故になるため。node から直接動かして確かめられる状態を保つ。
// tests/test_frontend.mjs がこの中身をそのまま叩いている。

import type { Progress, Region, Verdict } from "./api.js";

/** 位置指定モードの種類。add=漏れを塞ぐ shrink=塗り過ぎを狭める erase=誤検知を消す */
export type MarkMode = "add" | "shrink" | "erase";

export interface MarkModeSpec {
  /** サーバへ送る判定名。ここに持たせておかないと確定処理がモードごとの分岐だらけになる */
  verdict: Verdict;
  title: string;
  hint: string;
  wait: string;
  confirm: string;
  confirmAll?: string;
  /** 範囲を置かせずに、いま乗っている自動領域から選ばせる */
  pick?: boolean;
}

export const MARK_MODES: Record<MarkMode, MarkModeSpec> = {
  add: {
    verdict: "fixed",
    title: "漏れている場所を指定",
    hint: "漏れている場所を、画像の上で直接タップしてください",
    wait: "画像をタップしてください",
    confirm: "この位置で確定",
  },
  shrink: {
    verdict: "toobig",
    title: "残す範囲を指定（枠内の自動領域は消えます）",
    hint: "枠で囲まれた自動領域を消して、タップした範囲だけを残します",
    wait: "残したい範囲をタップしてください",
    confirm: "この範囲にする",
  },
  // 誤検知は範囲を置かせず、いま乗っているモザイクから選ばせる。
  // 消す方向の操作なので、何が消えるのかを見せないまま確定させない
  erase: {
    verdict: "false_positive",
    title: "消すモザイクを選ぶ",
    hint: "局部ではない場所に乗っているモザイクを、枠をタップして選びます",
    wait: "消す枠をタップしてください",
    confirm: "これを消す",
    confirmAll: "このコマは無処理になる",
    pick: true,
  },
};

/** 判定済みの表示。progress の集計キーとも揃えてある */
export const VERDICT_LABEL: Record<Verdict, string> = {
  ok: "問題なし",
  fixed: "塞いだ",
  unsure: "保留",
  toobig: "範囲を狭めた",
  false_positive: "誤検知として消した",
};

/** 由来ごとの枠の色。青=自動検出、黄=補間/memory、橙=橋渡し、赤=手修正 */
export const SRC_COLOR: Record<string, string> = {
  d: "#4a9eff",
  i: "#ffd479",
  m: "#ffd479",
  b: "#ffb347",
  x: "#ff5a5a",
};

/**
 * 自動で置かれた領域だけ。手で足したもの（x）は remove の対象外なので、
 * 「でかすぎる」で狭められるのはこちらだけ。
 */
export function autoBoxes(boxes: readonly Region[] | undefined): Region[] {
  return (boxes ?? []).filter((r) => r[4] !== "x");
}

/** 先頭4つが x, y, w, h であればよい。Region も Box もこれで受かる */
export type RectLike = readonly [number, number, number, number, ...unknown[]];

/** 辺が接しているだけは重なりとみなさない。corrections.apply の判定と同じ */
export function overlaps(a: RectLike, b: RectLike): boolean {
  return !(
    a[0] + a[2] <= b[0] ||
    b[0] + b[2] <= a[0] ||
    a[1] + a[3] <= b[1] ||
    b[1] + b[3] <= a[1]
  );
}

/**
 * 誤検知を確定したときに実際に消える自動領域の番号。
 *
 * remove は「その矩形と重なる自動領域」を落とす実装なので、選んだ枠に
 * 重なっている別の枠も一緒に消える。実素材では推定どうしがほとんど
 * 重なって並ぶことがあり、1つ選んだつもりで2つ消えることが起きる。
 * 選んだものだけを描いて確定させると、消える前に気づけない。
 */
export function eraseVictims(boxes: readonly Region[], picked: readonly number[]): Set<number> {
  const chosen = picked.map((i) => boxes[i]).filter((r): r is Region => r !== undefined);
  const out = new Set(picked);
  boxes.forEach((r, i) => {
    if (chosen.some((c) => overlaps(r, c))) out.add(i);
  });
  return out;
}

/**
 * 押した点から「どの枠のことか」を決める。枠の外なら null。
 *
 * 入れ子になっている枠では小さいほうを選ぶ。大きい枠は外側をタップすれば
 * 選べるが、内側の小さい枠は中でしか選べないため。
 * 近い枠を勝手に選ばないのは、「押した覚えのないものが消える」を避けるため。
 */
export function pickIndexAt(boxes: readonly Region[], x: number, y: number): number | null {
  let best = -1;
  let bestArea = Infinity;
  boxes.forEach((r, i) => {
    if (x < r[0] || x > r[0] + r[2] || y < r[1] || y > r[1] + r[3]) return;
    const a = r[2] * r[3];
    if (a < bestArea) {
      best = i;
      bestArea = a;
    }
  });
  return best < 0 ? null : best;
}

/** 選択の入り切り。同じ枠をもう一度押したら外す */
export function togglePick(picked: readonly number[], i: number): number[] {
  const at = picked.indexOf(i);
  if (at >= 0) {
    const out = picked.slice();
    out.splice(at, 1);
    return out;
  }
  return [...picked, i];
}

export interface EraseSummary {
  /** そのコマの自動領域の数 */
  total: number;
  /** 確定したときに実際に消える数。選んだ数ではない */
  victims: number;
  /** 全部消える（そのコマが無処理になる） */
  all: boolean;
  confirmLabel: string;
  confirmDisabled: boolean;
  banner: string;
}

/**
 * 誤検知モードの確定ボタンと注意書き。
 *
 * 数えるのは「選んだ数」ではなく「消える数」。重なった枠は巻き添えで
 * 消えるので、選んだ数で案内すると実際より少なく見える。
 */
export function eraseSummary(
  boxes: readonly Region[],
  picked: readonly number[],
  span: number,
): EraseSummary {
  const total = boxes.length;
  const n = picked.length ? eraseVictims(boxes, picked).size : 0;
  const all = n > 0 && n >= total;
  const m = MARK_MODES.erase;

  if (n === 0) {
    return {
      total,
      victims: 0,
      all: false,
      confirmLabel: m.wait,
      confirmDisabled: true,
      banner: m.hint,
    };
  }
  // 何が起きるかを言葉でも出す。押す前に「消える」と読めることが要る。
  // 適用範囲を前後に広げているときは、消えるのが1コマではないことも書く
  const scope = span ? `前後 ${span} コマにも同じ領域の削除が入ります。` : "";
  const extra = n > picked.length ? "重なっている枠も一緒に消えます。" : "";
  return {
    total,
    victims: n,
    all,
    confirmLabel: all ? (m.confirmAll ?? m.confirm) : m.confirm,
    confirmDisabled: false,
    banner:
      (all
        ? "確定するとこのコマのモザイクは全部消えます（無処理になります）。"
        : `確定するとこのコマのモザイク ${n} / ${total} 個が消えます。`) +
      extra +
      scope,
  };
}

/**
 * from から後ろへ探して最初の未判定。無ければ先頭から from の手前まで。
 * どちらにも無ければ「全部見終わった」を指す位置（items.length）。
 */
export function firstUnjudged(
  items: readonly { verdict: Verdict | null }[],
  from: number,
): number {
  for (let i = from; i < items.length; i++) if (!items[i]?.verdict) return i;
  for (let i = 0; i < from; i++) if (!items[i]?.verdict) return i;
  return items.length ? items.length : 0;
}

export interface SpanOption {
  v: number;
  label: string;
}

/**
 * 適用範囲の選択肢。既定は間引き幅ぶん。
 * キューは間引いて出しているので、1コマだけ直しても隣のコマは元のまま。
 */
export function spanOptions(step: number): SpanOption[] {
  return [
    { v: 0, label: "このコマだけ" },
    { v: step, label: `前後 ${step}` },
    { v: step * 3, label: `前後 ${step * 3}` },
  ];
}

/** 進捗バーの割合 (%) */
export function progressPercent(p: Progress): number {
  return p.total ? (100 * p.done) / p.total : 0;
}

/** 画像を送ってもらう幅。原寸を投げさせると1枚に数 MB かかり、判定の手応えが消える */
export function requestWidth(videoWidth: number, screenWidth: number, dpr: number): number {
  return Math.min(videoWidth, Math.max(480, Math.round(Math.min(screenWidth, 1280) * dpr)));
}
