// 検査キュー画面（automosaic/web/index.html）。
//
// 「どのフレームを見るか」はサーバが決める。画面はそれを1枚ずつ出して、
// 押された判定を返すだけにしてある。判定の記録もサーバ持ちなので、
// 端末を閉じても、別の端末で開き直しても続きから再開できる。
//
// 操作は指だけで完結させる。キーボードの割り当ては残してあるが、
// それが無いと出来ないことは1つも作らない。
//
// 画面部品は Preact、画像に重ねる枠は canvas 直描き。canvas は仮想 DOM の
// 恩恵が無いので、描画そのものは shared/canvas-draw.ts の純粋な関数に置いて
// useEffect から呼ぶだけにしてある。
//
// issue #79: ← / → は旧「検査キューの次の項目へ」から「1フレームだけ
// プレビュー」へ変わった（全画面で ← / → の意味を統一するため）。プレビューは
// 表示だけを動かし、判定の対象（cur.frame。サーバへ送る frame）は動かさない。
// プレビュー中は判定キー・判定ボタンを止める（RULES 0: 判定の誤発火防止）。
// 検査キューの項目送り（旧 ← / →）は PageUp / PageDown に移した。
// この画面には frontend/src/webapp/review.tsx にある区間追従（issue #46。
// I / O キー）がまだ無い。移植するかは別途 issue の判断に委ねる（issue #79）。

import { render } from "preact";
import { useEffect, useMemo, useRef, useState } from "preact/hooks";

import type { Progress, QueuePayload, QueueItem, StateLight, MarkRequest, MarkResponse, UndoResponse } from "../shared/api.js";
import { drawReviewOverlay } from "../shared/canvas-draw.js";
import { normFromClient, scaledSize, tapToBox } from "../shared/geom.js";
import type { NormPoint } from "../shared/geom.js";
import { dispatchKey, helpRows, REVIEW_KEYS } from "../shared/keymap.js";
import type { KeyLike } from "../shared/keymap.js";
import {
  MARK_MODES,
  VERDICT_LABEL,
  autoBoxes,
  eraseSummary,
  firstUnjudged,
  pickIndexAt,
  progressPercent,
  requestWidth,
  spanOptions,
  togglePick,
} from "../shared/review-logic.js";
import type { MarkMode } from "../shared/review-logic.js";
import { errText, get, post, url } from "../shared/review-net.js";

// Shift+← / Shift+→（大きく飛ぶ）のプレビュー移動幅。固定値（timeline と揃えた）
const JUMP_STEP = 10;

interface SaveState {
  kind: "ok" | "busy" | "err";
  text: string;
}

/** そのコマの既定の案内。判定済みならそれを出す */
function bannerFor(items: readonly QueueItem[], idx: number): string {
  const it = items[idx];
  if (!it) {
    return items.length ? "すべて判定済みです。設定から間隔や対象を変えられます" : "";
  }
  return it.verdict ? "判定済み: " + (VERDICT_LABEL[it.verdict] ?? it.verdict) : "";
}

function App() {
  const [state, setState] = useState<StateLight | null>(null);
  const [items, setItems] = useState<QueueItem[]>([]);
  const [idx, setIdx] = useState(0);
  const [version, setVersion] = useState(0);
  const [progress, setProgress] = useState<Progress | null>(null);

  const [step, setStep] = useState(5);
  const [stepInput, setStepInput] = useState("5");
  const [allFrames, setAllFrames] = useState(false);
  const [optRaw, setOptRaw] = useState(false);
  const [optBoxes, setOptBoxes] = useState(true);
  const [cls, setCls] = useState("");

  const [sizePct, setSizePct] = useState(100);
  const [span, setSpan] = useState(0);
  const [markMode, setMarkMode] = useState<MarkMode | null>(null);
  // 正規化タップ座標。サーバへはこちらを送る。フレーム座標との取り違えは型で止まる
  const [tap, setTap] = useState<NormPoint | null>(null);
  // 「誤検知」で選んだ自動領域の番号（autoBoxes() の添字）
  const [picked, setPicked] = useState<number[]>([]);
  // 1フレームだけのプレビュー（issue #79）。null なら判定対象のフレーム
  // （cur.frame）をそのまま見ている。判定は常に cur.frame に対して行うので、
  // これが cur.frame と食い違っている間は判定を止める（誤って違うフレームを
  // 判定してしまう事故を防ぐ。RULES 0）
  const [previewFrame, setPreviewFrame] = useState<number | null>(null);

  const [busy, setBusy] = useState(false);
  const [imgWidth, setImgWidth] = useState(720);
  // 判定の案内より優先して出す一時的な文言。移動すると消える
  const [notice, setNotice] = useState("");
  const [save, setSave] = useState<SaveState>({ kind: "ok", text: "保存済" });
  const [sheetOpen, setSheetOpen] = useState(false);

  const ovRef = useRef<HTMLCanvasElement>(null);

  const cur = items[idx] ?? null;
  const displayFrame = previewFrame ?? cur?.frame ?? 0;
  const previewing = previewFrame !== null && !!cur && previewFrame !== cur.frame;
  const autos = useMemo(() => autoBoxes(cur?.boxes), [cur]);
  const boxSize = state ? scaledSize(state.default_size, sizePct) : ([64, 64] as [number, number]);
  const pending =
    tap && state && markMode !== "erase" ? tapToBox(tap, boxSize, state.width, state.height) : null;
  const erase = markMode === "erase" ? eraseSummary(autos, picked, span) : null;

  // ----------------------------------------------------------------
  // 起動
  // ----------------------------------------------------------------
  useEffect(() => {
    void (async () => {
      try {
        // light=1 で全フレームぶんの矩形と被覆文字列を落としてもらう。
        // この画面が使うのは解像度・クラス・既定サイズだけで、あれは1時間の
        // 動画だと 10MB を超える。端末の回線では起動しなくなる
        const st = await get<StateLight>("/api/state", { light: 1 });
        const q = await get<QueuePayload>("/api/queue");
        setState(st);
        setCls(st.default_class);
        setItems(q.items);
        setVersion(q.version);
        setStep(q.step);
        setStepInput(String(q.step));
        setAllFrames(q.all_frames);
        setSpan(q.step);
        setProgress(q.progress);
        // 送ってもらう画像の幅。原寸を投げさせると1枚に数 MB かかり、
        // 判定の手応えが消える。画面に映る以上の解像度は要らない
        setImgWidth(requestWidth(st.width, window.screen.width, window.devicePixelRatio || 1));
        setIdx(firstUnjudged(q.items, 0));
      } catch (e) {
        setSave({ kind: "err", text: "起動に失敗" });
        setNotice("起動に失敗しました: " + errText(e));
      }
    })();
  }, []);

  // ----------------------------------------------------------------
  // 画像と重ね描き
  // ----------------------------------------------------------------
  function frameUrl(frame: number, width: number): string {
    const p: Record<string, string | number> = { n: frame, fmt: "jpg", w: width, v: version };
    if (optRaw) p["raw"] = 1;
    return url("/frame", p);
  }

  // 次の2枚を先読みする。/frame は世代番号付きの URL だけキャッシュ可なので、
  // 修正が入れば URL ごと変わり、古い絵が残ることはない
  useEffect(() => {
    for (let k = 1; k <= 2; k++) {
      const it = items[idx + k];
      if (it) new Image().src = frameUrl(it.frame, imgWidth);
    }
  }, [items, idx, imgWidth, version, optRaw]);

  useEffect(() => {
    const cv = ovRef.current;
    const ctx = cv?.getContext("2d");
    if (!cv || !ctx || !state) return;
    drawReviewOverlay(ctx, {
      width: state.width,
      height: state.height,
      // プレビュー中は検出枠を出さない。cur.boxes は cur.frame の検出結果で、
      // いま見ている絵（displayFrame）のものではないため
      boxes: previewing ? [] : (cur?.boxes ?? []),
      showBoxes: optBoxes,
      markMode,
      picked,
      pending,
    });
  }, [state, cur, previewing, optBoxes, markMode, picked, pending]);

  // 位置指定モードは CSS 側でもカーソルとボタンの見た目を変える。
  // どのつもりの操作かが画面全体で分かるようにしておく
  useEffect(() => {
    const c = document.body.classList;
    c.toggle("marking", markMode !== null);
    c.toggle("shrinking", markMode === "shrink");
    c.toggle("erasing", markMode === "erase");
    c.toggle("erase-all", erase?.all ?? false);
  }, [markMode, erase?.all]);

  // ----------------------------------------------------------------
  // 移動と判定
  // ----------------------------------------------------------------
  function cancelMark() {
    setMarkMode(null);
    setTap(null);
    setPicked([]);
  }

  function goto(i: number, list: QueueItem[] = items) {
    cancelMark();
    setPreviewFrame(null); // 新しい項目に来たら、プレビューはその項目の frame に戻す
    setIdx(Math.max(0, Math.min(list.length, i)));
    setNotice("");
  }

  /** 1フレームのプレビュー移動（issue #79）。cur（判定対象）は動かさない */
  function stepPreview(delta: number) {
    if (!state || !cur) return;
    cancelMark();
    const base = previewFrame ?? cur.frame;
    setPreviewFrame(Math.max(0, Math.min(state.n_frames - 1, base + delta)));
    setNotice("");
  }

  function jumpPreviewTo(frame: number) {
    if (!state || !cur) return;
    cancelMark();
    setPreviewFrame(Math.max(0, Math.min(state.n_frames - 1, frame)));
    setNotice("");
  }

  /** プレビュー中は判定を止める。見えている絵と違う frame を判定する事故を防ぐ（RULES 0） */
  function requireOnItem(): boolean {
    if (previewing) {
      setNotice(
        `プレビュー中です（frame ${displayFrame}）。判定するには ← / → で frame ${cur?.frame} まで戻ってください`,
      );
      return false;
    }
    return true;
  }

  function noPlayback() {
    setNotice("この画面に連続再生はありません（1枚ずつ判定する画面です）");
  }

  async function judge(verdict: MarkRequest["verdict"], extra?: Partial<MarkRequest>) {
    const it = cur;
    if (!it || busy) return;
    if (!requireOnItem()) return;
    setBusy(true);
    setSave({ kind: "busy", text: "保存中" });
    try {
      const d = await post<MarkResponse>("/api/mark", { frame: it.frame, verdict, ...extra });
      // 修正で領域が変わったら、その枚の矩形と画像の世代番号を入れ替える
      const next = items.map((x, i) =>
        i === idx ? { ...x, verdict, boxes: d.regions } : x,
      );
      setItems(next);
      setVersion(d.version);
      setProgress(d.progress);
      setSave({ kind: "ok", text: `保存済 ${d.n_corrections}` });
      goto(firstUnjudged(next, idx + 1), next);
    } catch (e) {
      setSave({ kind: "err", text: "保存できません" });
      setNotice("保存できませんでした: " + errText(e));
    } finally {
      setBusy(false);
    }
  }

  async function undo() {
    if (busy) return;
    setBusy(true);
    setSave({ kind: "busy", text: "戻しています" });
    try {
      const d = await post<UndoResponse>("/api/undo");
      if (!d.ok) {
        setSave({ kind: "ok", text: "保存済" });
        setNotice(d.error || "戻せません");
        return;
      }
      setVersion(d.version);
      const i = items.findIndex((x) => x.frame === d.frame);
      if (i >= 0) {
        setItems(items.map((x, j) => (j === i ? { ...x, verdict: null, boxes: d.regions } : x)));
        setIdx(i);
      }
      setProgress(d.progress);
      setSave({ kind: "ok", text: `保存済 ${d.n_corrections}` });
      cancelMark();
      setNotice(`frame ${d.frame} の判定を取り消しました`);
    } catch (e) {
      setSave({ kind: "err", text: "戻せません" });
      setNotice("取り消せませんでした: " + errText(e));
    } finally {
      setBusy(false);
    }
  }

  // ----------------------------------------------------------------
  // 位置の指定
  // ----------------------------------------------------------------
  function startMark(mode: MarkMode) {
    if (!cur) return;
    if (!requireOnItem()) return;
    if ((mode === "shrink" || mode === "erase") && !autos.length) {
      // 消す相手がいないのに範囲だけ置かせると、ただ塗る範囲が増える。
      // どちらも自動領域が前提の操作なので、無いなら入らせない
      setNotice("このコマには自動で塗った領域がありません");
      return;
    }
    setMarkMode(mode);
    setTap(null);
    // 1つしかないなら選びようがない。それでも確定は押させる（消えるのが
    // 見えてから確定する、という手順自体は省かない）
    setPicked(mode === "erase" && autos.length === 1 ? [0] : []);
    setNotice("");
  }

  function onCanvasPointerDown(ev: PointerEvent) {
    if (!markMode || !state) return;
    ev.preventDefault();
    const cv = ovRef.current;
    if (!cv) return;
    // タッチ経由で呼ばれても動くようにしてある（pointerdown には changedTouches は無い）
    const touches = (ev as PointerEvent & { changedTouches?: TouchList }).changedTouches;
    const t = touches ? touches[0]! : ev;
    const p = normFromClient(cv.getBoundingClientRect(), t.clientX, t.clientY);
    if (markMode === "erase") {
      const i = pickIndexAt(autos, p[0] * state.width, p[1] * state.height);
      if (i === null) {
        // 枠の外。近い枠を勝手に選ぶと「押した覚えのないものが消える」ので、
        // 何もせずに押す場所だけ教える
        setNotice("消したい枠の中をタップしてください");
        return;
      }
      setNotice("");
      setPicked((prev) => togglePick(prev, i));
    } else {
      setTap(p);
    }
  }

  function confirmMark() {
    if (!markMode || !state) return;
    const m = MARK_MODES[markMode];
    const useClass = cls || state.default_class;
    let payload: Partial<MarkRequest>;
    if (m.pick) {
      // 選ばれていなければ何もしない。誤検知は「選んだ枠だけ」を消す操作なので、
      // 選択なしで通すと何が消えたのか誰にも分からない修正になる
      if (!picked.length) return;
      payload = {
        pick: picked.map((i) => autos[i]!.slice(0, 4) as [number, number, number, number]),
        span,
        class: useClass,
      };
    } else {
      // 範囲が置かれていなければ何もしない。「でかすぎる」で範囲なしを通すと、
      // 自動領域を消すだけの修正になり、そのコマが素通しになる
      if (!tap) return;
      payload = { x: tap[0], y: tap[1], w: boxSize[0], h: boxSize[1], span, class: useClass };
    }
    cancelMark();
    void judge(m.verdict, payload);
  }

  // ----------------------------------------------------------------
  // キューの組み直し
  // ----------------------------------------------------------------
  async function reloadQueue(params: Record<string, string | number>) {
    setSave({ kind: "busy", text: "作り直し中" });
    try {
      const q = await get<QueuePayload>("/api/queue", { rebuild: 1, ...params });
      setItems(q.items);
      setVersion(q.version);
      setStep(q.step);
      setStepInput(String(q.step));
      setSpan(q.step);
      setProgress(q.progress);
      setSave({ kind: "ok", text: "保存済" });
      goto(firstUnjudged(q.items, 0), q.items);
    } catch (e) {
      setSave({ kind: "err", text: "作り直せません" });
      setNotice(errText(e));
    }
  }

  // ----------------------------------------------------------------
  // キー
  // ----------------------------------------------------------------
  // キーは shared/keymap.ts の REVIEW_KEYS が唯一の割り当て表。
  // ここでは「アクション名 -> 何をするか」だけを持つ（issue #79）。
  // webapp/review.tsx と同じ表を使うが、この画面には区間追従（I/O）が
  // まだ無いので REVIEW_KEYS だけを使う（REVIEW_INTERVAL_KEYS は使わない）
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      const t = ev.target as HTMLElement | null;
      const like: KeyLike = {
        key: ev.key,
        shiftKey: ev.shiftKey,
        targetTag: t?.tagName ?? null,
        targetEditable: !!t?.isContentEditable,
      };
      const handled = dispatchKey(REVIEW_KEYS, like, {
        // 判定（1〜5）は動かしていない（RULES 0）
        judgeOk: () => void judge("ok"),
        judgeAdd: () => startMark("add"),
        judgeUnsure: () => void judge("unsure"),
        judgeShrink: () => startMark("shrink"),
        judgeErase: () => startMark("erase"),
        undo: () => void undo(),
        cancel: () => { cancelMark(); setPreviewFrame(null); setNotice(""); },
        // ← / → はキュー送りから1フレームのプレビューへ変わった（issue #79）。
        // キュー送りは PageUp / PageDown に移した
        stepBack: () => stepPreview(-1),
        stepForward: () => stepPreview(1),
        jumpBack: () => stepPreview(-JUMP_STEP),
        jumpForward: () => stepPreview(JUMP_STEP),
        goHome: () => jumpPreviewTo(0),
        goEnd: () => jumpPreviewTo((state?.n_frames ?? 1) - 1),
        queuePrev: () => goto(idx - 1),
        queueNext: () => goto(idx + 1),
        // この画面に連続再生は無い（1枚ずつ判定する画面）
        playToggle: noPlayback,
        shuttleReverse: noPlayback,
        shuttleStop: noPlayback,
        shuttleForward: noPlayback,
        help: () => setSheetOpen(true),
      });
      if (handled) ev.preventDefault();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  // ----------------------------------------------------------------
  // 表示
  // ----------------------------------------------------------------
  const spec = markMode ? MARK_MODES[markMode] : null;
  const confirmLabel = erase
    ? erase.confirmLabel
    : spec
      ? tap
        ? spec.confirm
        : spec.wait
      : "";
  const confirmDisabled = erase ? erase.confirmDisabled : !tap;
  // プレビュー中（issue #79）は、判定の案内より先に「今は判定対象を見ていない」
  // ことを出す。それが分からないまま判定キーを押すと、なぜ止まったのか分からない
  const previewBanner = previewing
    ? `プレビュー中: frame ${displayFrame}（判定対象は frame ${cur?.frame}。← / → で戻れます）`
    : "";
  const bannerText = spec
    ? erase
      ? erase.banner
      : tap
        ? "大きさは下のスライダで調整できます"
        : spec.hint
    : notice || previewBanner || bannerFor(items, idx);
  const posText = cur
    ? `${idx + 1} / ${items.length}`
    : items.length
      ? "全部見終わりました"
      : "対象がありません";
  const pct = progress ? progressPercent(progress) : 0;

  return (
    <>
      <header id="bar">
        <div id="progress">
          <div id="progress-fill" style={{ width: pct.toFixed(1) + "%" }} />
        </div>
        <div id="bar-row">
          <span id="pos" title={cur ? `frame ${cur.frame}` : undefined}>
            {state ? posText : "読み込み中"}
          </span>
          <span id="reason" class={cur ? "tag p" + cur.priority : "tag"}>
            {cur ? `${cur.label}  frame ${cur.frame}` : ""}
          </span>
          <span id="save" class={"save " + save.kind}>{save.text}</span>
          {/* ひとつ戻すは上に置く。判定が5つになって下段が埋まったのもあるが、
              取り消しは頻度が低いわりに押し間違えると判定が1つ消える。
              判定ボタンの並びから離しておくほうが安全 */}
          <button id="btn-undo" class="icon" aria-label="ひとつ戻す"
                  disabled={progress ? !progress.can_undo : false} onClick={() => void undo()}>戻す</button>
          <button id="btn-menu" class="icon" aria-label="設定"
                  onClick={() => setSheetOpen(true)}>設定</button>
        </div>
      </header>

      <main id="stage">
        <div id="imgwrap">
          <img id="shot" alt={previewing ? "プレビュー中のフレーム" : "判定対象のフレーム"}
               src={cur ? frameUrl(displayFrame, imgWidth) : undefined} />
          <canvas id="ov" ref={ovRef}
                  width={state?.width ?? 0} height={state?.height ?? 0}
                  onPointerDown={onCanvasPointerDown} />
        </div>
        <div id="banner" class={bannerText ? "" : "hidden"}>{bannerText}</div>
      </main>

      <footer id="pad">
        {/* 判定モード。ここだけで1枚が終わるのが普通の流れ */}
        <div id="judge" class={markMode ? "hidden" : ""}>
          {/* プレビュー中は判定ボタンを止める（issue #79。RULES 0: 誤発火防止） */}
          <button id="btn-ok" class="big ok" disabled={previewing} onClick={() => void judge("ok")}>問題なし</button>
          {/* 範囲を直す2つ。縦に4つ積むと縦持ちで画像面が潰れるので横に並べる */}
          <div class="row">
            <button id="btn-ng" class="big ng" disabled={previewing} onClick={() => startMark("add")}>漏れている</button>
            <button id="btn-big" class="big warn" disabled={previewing} onClick={() => startMark("shrink")}>でかすぎる</button>
          </div>
          {/* 誤検知はモザイクを消す方向なので、色を灰にして目立たせない */}
          <div class="row">
            <button id="btn-fp" class="mid dim" disabled={previewing} onClick={() => startMark("erase")}>誤検知</button>
            <button id="btn-unsure" class="mid" disabled={previewing} onClick={() => void judge("unsure")}>判断できない</button>
          </div>
        </div>

        {/* 位置指定モード。3つの判定で共用する。操作系を分けると、
            どれかだけ手に馴染まないものになる */}
        <div id="mark" class={markMode ? "" : "hidden"}>
          <div id="mark-title" class={"mark-title" + (markMode === "erase" ? " danger" : "")}>
            {spec?.title ?? ""}
          </div>
          <div class="row size-row">
            <button id="btn-minus" class="sq"
                    onClick={() => setSizePct((v) => Math.max(20, v - 15))}>−</button>
            <input id="size" type="range" min="20" max="400" step="5" value={sizePct}
                   aria-label="矩形の大きさ"
                   onInput={(e) => setSizePct(Number(e.currentTarget.value))} />
            <button id="btn-plus" class="sq"
                    onClick={() => setSizePct((v) => Math.min(400, v + 15))}>＋</button>
            <span id="size-label" class="size-label">{`${boxSize[0]}x${boxSize[1]}px`}</span>
          </div>
          <div class="row" id="span-row">
            {spanOptions(step).map((o) => (
              <button key={o.v} class={"mid span-btn" + (o.v === span ? " on" : "")}
                      onClick={() => setSpan(o.v)}>{o.label}</button>
            ))}
          </div>
          <button id="btn-confirm" class="big ok" disabled={confirmDisabled} onClick={confirmMark}>
            {confirmLabel}
          </button>
          <div class="row">
            <button id="btn-cancel" class="mid" onClick={cancelMark}>やめる</button>
            <button id="btn-undo2" class="mid" disabled={progress ? !progress.can_undo : false}
                    onClick={() => void undo()}>ひとつ戻す</button>
          </div>
        </div>
      </footer>

      {/* 設定シート。普段は畳んでおく。ここに置くのは頻度の低い操作だけ */}
      <div id="sheet" class={sheetOpen ? "" : "hidden"}
           onClick={(e) => { if (e.target === e.currentTarget) setSheetOpen(false); }}>
        <div id="sheet-inner">
          <h2>設定</h2>

          <label class="opt">
            <input id="opt-raw" type="checkbox" checked={optRaw}
                   onChange={(e) => { setOptRaw(e.currentTarget.checked); setNotice(""); }} />
            {" 原画で確認する（モザイクを外す）"}
          </label>
          <label class="opt">
            <input id="opt-boxes" type="checkbox" checked={optBoxes}
                   onChange={(e) => setOptBoxes(e.currentTarget.checked)} />
            {" モザイクの範囲を枠で示す"}
          </label>
          <label class="opt">
            <input id="opt-all" type="checkbox" checked={allFrames}
                   onChange={(e) => {
                     const on = e.currentTarget.checked;
                     setAllFrames(on);
                     void reloadQueue({ all: on ? 1 : 0 });
                   }} />
            {" 全フレームを対象にする"}
          </label>

          <label class="opt-row">間隔
            <input id="opt-step" type="number" min="1" max="300" value={stepInput}
                   onInput={(e) => setStepInput(e.currentTarget.value)} />
            {" フレームおき"}
          </label>

          <label class="opt-row">クラス
            <select id="opt-class" value={cls} onChange={(e) => setCls(e.currentTarget.value)}>
              {(state?.classes ?? []).map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
          </label>

          <div class="opt-row">
            <button id="btn-rebuild" class="mid wide"
                    onClick={() => void reloadQueue({
                      step: Math.max(1, Number(stepInput) || 5),
                      all: allFrames ? 1 : 0,
                    })}>キューを作り直す</button>
          </div>
          <div class="opt-row">
            <button id="btn-unjudged" class="mid wide"
                    onClick={() => { setSheetOpen(false); goto(firstUnjudged(items, 0)); }}>
              未判定の先頭へ
            </button>
          </div>
          <div class="opt-row">
            <button id="btn-prev-item" class="mid" onClick={() => goto(idx - 1)}>前の1枚</button>
            <button id="btn-next-item" class="mid" onClick={() => goto(idx + 1)}>次の1枚</button>
          </div>

          <p id="sheet-info" class="info">
            {progress
              ? `${progress.total} 枚中 ${progress.done} 枚判定済み（残り ${progress.remaining}）` +
                `  問題なし ${progress.counts.ok} / 塞いだ ${progress.counts.fixed}` +
                ` / 狭めた ${progress.counts.toobig} / 誤検知 ${progress.counts.false_positive}` +
                ` / 保留 ${progress.counts.unsure}`
              : ""}
          </p>
          <p class="info">
            <a id="link-timeline" href={url("/timeline")}>タイムライン画面（PC 向け）</a>
          </p>
          {/* 割り当て表（shared/keymap.ts の REVIEW_KEYS）から自動で作る。
              手で書き写すと割り当てを直したときにここだけ古いまま腐る（issue #79）。
              ? キーでここ（設定シート）を開く */}
          <p class="info">
            キー:
            {helpRows(REVIEW_KEYS).map((r) => ` ${r.label} ${r.desc} /`).join("")}
          </p>
          <p class="info">
            キュー送り（前後の項目へ）は PageUp / PageDown。
            ← / → はこの画面では1フレームのプレビューで、判定対象は動かさない
          </p>

          <button id="btn-close-sheet" class="big" onClick={() => setSheetOpen(false)}>閉じる</button>
        </div>
      </div>
    </>
  );
}

// HTML は据え置きなので、中身を差し替えてから Preact に描かせる。
// 静的なマークアップと二重に出さないための後始末
document.body.textContent = "";
render(<App />, document.body);
