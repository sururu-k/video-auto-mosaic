// キーと意味の対応を1か所にまとめる（issue #79）。
//
// これまでは画面ごとに `switch (ev.key)` を書いていて、同じキーが画面ごとに
// 違う意味を持っていた（← / → が timeline / framestep / draw / review で
// 3通りの動きをしていた）。ここでは「キー -> アクション名」の対応表だけを
// 持ち、DOM にもフレームワークにも依存しない。実際に何をするか（アクション名
// -> 関数）は各画面が渡す。
//
// 表を直せば全画面が変わることを、tests/test_frontend.mjs がここを直接
// 読んで確かめる（画面側の .tsx を経由しない）。

/** キーボードイベントのうち判断に要る部分だけ。実イベントからもテストからも作れる */
export interface KeyLike {
  key: string;
  shiftKey?: boolean;
  /** ev.target.tagName。無ければ入力欄でないとみなす */
  targetTag?: string | null;
  /** ev.target.isContentEditable */
  targetEditable?: boolean;
}

export interface KeyBinding {
  /** アクション名。画面側はこの名前で処理を登録する */
  action: string;
  /** このアクションを起こすキー（複数可。例: ["ArrowLeft", ","]） */
  keys: readonly string[];
  /** true なら Shift 併用時のみ。省略時（false）は Shift 無しのときだけ */
  shift?: boolean;
  /** キー一覧に出す表示 */
  label: string;
  /** 意味の説明 */
  desc: string;
}

function bind(
  action: string,
  keys: readonly string[],
  label: string,
  desc: string,
  shift = false,
): KeyBinding {
  return { action, keys, label, desc, shift };
}

// ----------------------------------------------------------------------
// 入力欄でキーを拾わない（issue #79 完了条件）。
// INPUT / SELECT は元から見ていたが、TEXTAREA と contenteditable が
// 抜けていた（自由記述欄が無い画面ばかりだったので気づかれていなかった）。
// ----------------------------------------------------------------------
const TYPING_TAGS = new Set(["INPUT", "SELECT", "TEXTAREA"]);

export function isTypingTarget(t: KeyLike): boolean {
  if (t.targetTag && TYPING_TAGS.has(t.targetTag)) return true;
  if (t.targetEditable) return true;
  return false;
}

/**
 * bindings の中から ev に対応するアクション名を1つ選ぶ。
 * 入力欄にフォーカスがあれば常に null（キーを拾わない）。
 */
export function resolveKey(bindings: readonly KeyBinding[], ev: KeyLike): string | null {
  if (isTypingTarget(ev)) return null;
  const shift = !!ev.shiftKey;
  for (const b of bindings) {
    if (!!b.shift !== shift) continue;
    if (b.keys.includes(ev.key)) return b.action;
  }
  return null;
}

/**
 * ev からアクションを引いて、対応する関数があれば呼ぶ。呼んだら true。
 * 画面側はこれを1回呼ぶだけでよく、`switch (ev.key)` を書かずに済む。
 */
export function dispatchKey(
  bindings: readonly KeyBinding[],
  ev: KeyLike,
  handlers: Partial<Record<string, () => void>>,
): boolean {
  const action = resolveKey(bindings, ev);
  if (!action) return false;
  const fn = handlers[action];
  if (!fn) return false;
  fn();
  return true;
}

// ----------------------------------------------------------------------
// 全画面共通のトランスポート（動画編集ソフトの標準に合わせる。issue #79）。
//
// ← / → と , / . は同じ意味にする（動画編集ソフトの慣習で両方効く）。
// Shift+← / Shift+→ は「大きく飛ぶ」。draw の strideN、timeline / framestep /
// review の固定ジャンプ幅がここに乗る。
// J / K / L はシャトル（逆再生 / 停止 / 順再生、連打で加速）。
// 再生できない画面（draw / review）では、これらのハンドラが
// 「できない」ことを伝える案内を出す（無反応にしない）。
// ----------------------------------------------------------------------
export const STEP_BACK = bind("stepBack", ["ArrowLeft", ","], "← / ,", "1フレーム戻る");
export const STEP_FWD = bind("stepForward", ["ArrowRight", "."], "→ / .", "1フレーム進む");
export const JUMP_BACK = bind("jumpBack", ["ArrowLeft"], "Shift+←", "大きく戻る", true);
export const JUMP_FWD = bind("jumpForward", ["ArrowRight"], "Shift+→", "大きく進む", true);
export const PLAY_TOGGLE = bind("playToggle", [" "], "Space", "再生 / 停止");
export const GO_HOME = bind("goHome", ["Home"], "Home", "先頭のフレームへ");
export const GO_END = bind("goEnd", ["End"], "End", "末尾のフレームへ");
export const SHUTTLE_REV = bind("shuttleReverse", ["j", "J"], "J", "逆再生（連打で加速）");
export const SHUTTLE_STOP = bind("shuttleStop", ["k", "K"], "K", "停止");
export const SHUTTLE_FWD = bind("shuttleForward", ["l", "L"], "L", "順再生（連打で加速）");
export const HELP = bind("help", ["?"], "?", "このキー一覧を出す");

/** どの画面にも乗る土台。Shift+矢印は画面ごとにジャンプ幅の意味が違うので別枠 */
export const CORE_TRANSPORT: readonly KeyBinding[] = [
  STEP_BACK,
  STEP_FWD,
  PLAY_TOGGLE,
  GO_HOME,
  GO_END,
  SHUTTLE_REV,
  SHUTTLE_STOP,
  SHUTTLE_FWD,
  HELP,
];

export const JUMP: readonly KeyBinding[] = [JUMP_BACK, JUMP_FWD];

// ----------------------------------------------------------------------
// timeline 固有
// ----------------------------------------------------------------------
export const TL_ADD_MODE = bind("addMode", ["m", "M"], "M", "追加モード（矩形を置く）");
// timeline の 1 / 2 は「pending をこのフレームだけ / span 分適用」で、review の
// 判定キー 1〜5 とは別画面ながら同じキーが衝突していた。判定側（review）は
// RULES 0 により動かせないので、timeline 側をここで動かす。矩形を置いたあとの
// 確定操作という点は draw.tsx の Enter（タップした点を確定する）と同じ性質
// なので、そちらに合わせる。span 分の適用は Shift+Enter（「大きい範囲を確定」
// という点で Shift+矢印の「大きく飛ぶ」と語感を揃えた）。
export const TL_APPLY_FRAME = bind("applyFrame", ["Enter"], "Enter", "置いた矩形をこのフレームだけに適用");
export const TL_APPLY_SPAN = bind(
  "applySpan",
  ["Enter"],
  "Shift+Enter",
  "置いた矩形を指定フレーム数ぶん適用",
  true,
);
export const TL_DELETE_HERE = bind("deleteHere", ["d", "D"], "D", "カーソル下の手修正を削除");
export const TL_NEXT_ESTIMATED = bind("nextEstimated", ["g", "G"], "G", "次の推定のみ区間へ");
export const TL_SIZE_SMALLER = bind("sizeSmaller", ["["], "[", "矩形を縮小");
export const TL_SIZE_BIGGER = bind("sizeBigger", ["]"], "]", "矩形を拡大");

export const TIMELINE_KEYS: readonly KeyBinding[] = [
  ...CORE_TRANSPORT,
  ...JUMP,
  TL_ADD_MODE,
  TL_APPLY_FRAME,
  TL_APPLY_SPAN,
  TL_DELETE_HERE,
  TL_NEXT_ESTIMATED,
  TL_SIZE_SMALLER,
  TL_SIZE_BIGGER,
];

// ----------------------------------------------------------------------
// framestep 固有（フレーム移動と再生しかない画面）
// ----------------------------------------------------------------------
export const FRAMESTEP_KEYS: readonly KeyBinding[] = [...CORE_TRANSPORT, ...JUMP];

// ----------------------------------------------------------------------
// draw 固有。旧: ← / → が strideN フレーム移動、, / . が1フレーム移動
// だった。全画面統一のため ← / → も , / . と同じ1フレーム移動にし、
// strideN は Shift+← / Shift+→ （「大きく飛ぶ」）へ移す。
// ----------------------------------------------------------------------
export const DRAW_CONFIRM = bind("confirmTap", ["Enter"], "Enter", "タップした位置に打点を置く");
export const DRAW_ABSENT = bind("markAbsent", ["n", "N"], "N", "「ここには無い」として記録");

export const DRAW_KEYS: readonly KeyBinding[] = [
  ...CORE_TRANSPORT,
  ...JUMP,
  DRAW_CONFIRM,
  DRAW_ABSENT,
];

// ----------------------------------------------------------------------
// review 固有。
//
// 旧: ← / → が「検査キューの次の項目へ」だった（フレームは動かない）。
// 全画面統一のため ← / → は1フレーム移動（プレビュー）にし、キュー送りは
// PageUp / PageDown へ移す。根拠:
//   - J/K/L/Space はシャトル・再生に予約済み（review に再生機能は無いので
//     「できない」案内に使う。キュー送りの新居にはできない）
//   - [ / ] は timeline が「矩形の拡大縮小」に使っており、同じキーに
//     また別の意味を足すと今回直したい問題を作り直すことになる
//   - PageUp / PageDown は「一覧の前後の項目へ」という意味で他の多くの
//     アプリケーションに共通する慣習があり、「1フレーム」より大きい単位
//     （キューの1項目）の移動であることとも合う
//
// 1〜5（判定）はここでは動かさない（RULES 0）。プレビュー中（1フレーム
// 移動でキュー項目のフレームから離れているとき）は、判定キーが
// 「見えている絵と違うフレームを判定してしまう」事故を防ぐため、画面側が
// 判定不可の案内を出す（このモジュールはその判断はしない。画面側の状態
// （プレビュー位置とキュー項目のフレームが一致するか）を見て決める）。
// ----------------------------------------------------------------------
export const RV_JUDGE_OK = bind("judgeOk", ["1"], "1", "問題なし");
export const RV_JUDGE_ADD = bind("judgeAdd", ["2"], "2", "漏れている（追加モード）");
export const RV_JUDGE_UNSURE = bind("judgeUnsure", ["3"], "3", "判断できない");
export const RV_JUDGE_SHRINK = bind("judgeShrink", ["4"], "4", "でかすぎる");
export const RV_JUDGE_ERASE = bind("judgeErase", ["5"], "5", "誤検知");
export const RV_UNDO = bind("undo", ["u", "U"], "U", "ひとつ戻す");
export const RV_CANCEL = bind("cancel", ["Escape"], "Esc", "モードをやめる");
export const RV_QUEUE_PREV = bind("queuePrev", ["PageUp"], "PageUp", "検査キューの前の項目へ");
export const RV_QUEUE_NEXT = bind("queueNext", ["PageDown"], "PageDown", "検査キューの次の項目へ");

export const REVIEW_KEYS: readonly KeyBinding[] = [
  ...CORE_TRANSPORT,
  ...JUMP,
  RV_JUDGE_OK,
  RV_JUDGE_ADD,
  RV_JUDGE_UNSURE,
  RV_JUDGE_SHRINK,
  RV_JUDGE_ERASE,
  RV_UNDO,
  RV_CANCEL,
  RV_QUEUE_PREV,
  RV_QUEUE_NEXT,
];

// 区間追従（issue #46）。webapp/review.tsx にはあるが review/app.tsx には
// まだ移植していない機能（issue #79 の完了条件は「両方が新しいキーマップを
// 使うこと」であって「両方が同じ機能を持つこと」ではない。移植するかどうか
// は別途 issue に判断を書く）。REVIEW_KEYS 本体には含めず、対応する画面だけ
// これを足す。
export const RV_INTERVAL_START = bind("intervalStart", ["i", "I"], "I", "区間の始点を置く");
export const RV_INTERVAL_END = bind("intervalEnd", ["o", "O"], "O", "区間の終点（確定）");
export const REVIEW_INTERVAL_KEYS: readonly KeyBinding[] = [RV_INTERVAL_START, RV_INTERVAL_END];

// ----------------------------------------------------------------------
// キー一覧の自動生成（issue #79: 「割り当ての1か所から自動で作ること」）
// ----------------------------------------------------------------------

/** 表示用の1行。同じ action が重複していれば1回にまとめる */
export interface HelpRow {
  label: string;
  desc: string;
}

export function helpRows(bindings: readonly KeyBinding[]): HelpRow[] {
  const seen = new Set<string>();
  const rows: HelpRow[] = [];
  for (const b of bindings) {
    const k = b.action + (b.shift ? "!shift" : "");
    if (seen.has(k)) continue;
    seen.add(k);
    rows.push({ label: b.label, desc: b.desc });
  }
  return rows;
}

/** 画面下部などに1行で出す用。"← / , 1フレーム戻る ・ ..." の形 */
export function helpLine(bindings: readonly KeyBinding[]): string {
  return helpRows(bindings)
    .map((r) => `${r.label} ${r.desc}`)
    .join(" ・ ");
}
