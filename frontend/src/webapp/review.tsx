// 検査キュー画面。既存レビュー UI（automosaic/web/app.tsx）と同じ操作系を
// ジョブ単位の API に載せ替えたもの。
//
// 「どのフレームを見るか」はサーバが決める。画面はそれを1枚ずつ出して、
// 押された判定を返すだけにしてある。判定の記録もサーバ持ちなので、
// 端末を閉じても、別の端末で開き直しても続きから再開できる。
//
// 操作は指だけで完結させる。キーボードの割り当ては残してあるが、
// それが無いと出来ないことは1つも作らない。
//
// 例外が区間追従（issue #46）。「2」でタップした位置を「I」で区間の始点に
// し、矢印キーで終点のコマへ移動してタップ、「O」で確定するとあいだを
// 補間で埋める。動画編集ソフトのイン点・アウト点と同じ操作系にしてある。
// automosaic/webapp/spans.py の interval_add_records() を review.mark_interval()
// が呼ぶ。「漏れている」（fixed）専用（review.mark_interval のドキュストリング
// 参照。「でかすぎる」の remove を区間補間で動かすのは危険）。
//
// 判定は5つ（問題なし・漏れている・判断できない・でかすぎる・誤検知）。
// 「でかすぎる」「誤検知」は自動領域を打ち消す remove を伴う（漏れる方向へ
// 倒れうる操作）ので、position placement / pick の中身は
// frontend/src/review/app.tsx（旧 UI）と同じ shared/review-logic.ts の
// 判断をそのまま使う。別実装を書くと「見えている枠と実際に塞がれる場所が
// ずれる」を作る（geom.ts 冒頭のコメント）。

import { render } from "preact";
import { useEffect, useMemo, useRef, useState } from "preact/hooks";

import type {
  MarkRequest,
  MarkResponse,
  Progress,
  QueueItem,
  QueuePayload,
  StateLight,
  UndoResponse,
} from "../shared/api.js";
import { drawReviewOverlay } from "../shared/canvas-draw.js";
import { normFromClient, scaledSize, tapToBox } from "../shared/geom.js";
import type { NormPoint } from "../shared/geom.js";
import {
  MARK_MODES,
  VERDICT_LABEL,
  autoBoxes,
  eraseSummary,
  firstUnjudged,
  intervalStatus,
  numOr,
  pickIndexAt,
  progressPercent,
  requestWidth,
  spanOptions,
  togglePick,
} from "../shared/review-logic.js";
import type { MarkMode } from "../shared/review-logic.js";
import { api, errText, link, url } from "../shared/webapp-net.js";

const JOB = location.pathname.split("/").pop() ?? "";
const API = "/api/jobs/" + JOB;

interface SaveState {
  kind: "ok" | "busy" | "err";
  text: string;
}

/**
 * 区間の始点（issue #46）。動画編集ソフトのイン点にならい、キーボードの
 * 「I」で置く。「O」を押した時点のコマが終点になり、あいだを補間で埋める。
 */
interface IntervalStartPoint {
  frame: number;
  tap: NormPoint;
  size: [number, number];
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

  const [stepInput, setStepInput] = useState("5");
  const [step, setStep] = useState(5);
  const [allFrames, setAllFrames] = useState(false);
  const [optRaw, setOptRaw] = useState(false);
  const [optBoxes, setOptBoxes] = useState(true);
  const [cls, setCls] = useState("");

  const [sizePct, setSizePct] = useState(100);
  const [span, setSpan] = useState(0);
  const [markMode, setMarkMode] = useState<MarkMode | null>(null);
  // 正規化タップ座標。サーバへはこちらを送る
  const [tap, setTap] = useState<NormPoint | null>(null);
  // 「誤検知」で選んだ自動領域の番号（autoBoxes() の添字）
  const [picked, setPicked] = useState<number[]>([]);
  // 区間の始点（issue #46）。frame をまたいで生きるので markMode/tap とは別に持つ
  const [intervalStart, setIntervalStart] = useState<IntervalStartPoint | null>(null);

  const [busy, setBusy] = useState(false);
  const [imgWidth, setImgWidth] = useState(720);
  const [notice, setNotice] = useState("");
  const [save, setSave] = useState<SaveState>({ kind: "ok", text: "保存済" });
  const [sheetOpen, setSheetOpen] = useState(false);

  const ovRef = useRef<HTMLCanvasElement>(null);

  const cur = items[idx] ?? null;
  const autos = useMemo(() => autoBoxes(cur?.boxes), [cur]);
  const boxSize = state ? scaledSize(state.default_size, sizePct) : ([64, 64] as [number, number]);
  const pending =
    tap && state && markMode !== "erase" ? tapToBox(tap, boxSize, state.width, state.height) : null;
  const erase = markMode === "erase" ? eraseSummary(autos, picked, span) : null;
  // 区間の始点を置いたコマそのものを見ているときだけ、その矩形を描く
  const startBox =
    intervalStart && cur && state && intervalStart.frame === cur.frame
      ? tapToBox(intervalStart.tap, intervalStart.size, state.width, state.height)
      : null;
  const interval = intervalStatus(intervalStart, cur?.frame ?? null, markMode === "add" && !!tap);

  useEffect(() => {
    void (async () => {
      try {
        // light=1 で全フレームぶんの矩形と被覆文字列を落としてもらう。
        // この画面が使うのは解像度・クラス・既定サイズだけで、あれは1時間の
        // 動画だと 10MB を超える。端末の回線では起動しなくなる
        const st = await api<StateLight>(API + "/state?light=1");
        const q = await api<QueuePayload>(API + "/queue");
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

  function frameUrl(frame: number, width: number): string {
    const p: Record<string, string | number> = { n: frame, fmt: "jpg", w: width, v: version };
    if (optRaw) p["raw"] = 1;
    return url(API + "/frame", p);
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
    const ctx = ovRef.current?.getContext("2d");
    if (!ctx || !state) return;
    drawReviewOverlay(ctx, {
      width: state.width,
      height: state.height,
      boxes: cur?.boxes ?? [],
      showBoxes: optBoxes,
      markMode,
      picked,
      pending,
      startBox,
    });
  }, [state, cur, optBoxes, markMode, picked, pending, startBox]);

  function cancelMark() {
    setMarkMode(null);
    setTap(null);
    setPicked([]);
    setIntervalStart(null);
  }

  function goto(i: number, list: QueueItem[] = items) {
    if (intervalStart) {
      // 区間の始点はコマをまたいで生かす。タップ位置だけ捨てて、
      // 終点のコマへ移動できるようにする（ナビゲーションは矢印キーのまま）
      setTap(null);
      setPicked([]);
    } else {
      cancelMark();
    }
    setIdx(Math.max(0, Math.min(list.length, i)));
    setNotice("");
  }

  async function judge(verdict: MarkRequest["verdict"], extra?: Partial<MarkRequest>) {
    const it = cur;
    if (!it || busy) return;
    setBusy(true);
    setSave({ kind: "busy", text: "保存中" });
    try {
      const d = await api<MarkResponse>(API + "/mark", {
        json: { frame: it.frame, verdict, ...extra },
      });
      // 修正で領域が変わったら、その枚の矩形と画像の世代番号を入れ替える
      const next = items.map((x, i) => (i === idx ? { ...x, verdict, boxes: d.regions } : x));
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
      const d = await api<UndoResponse>(API + "/undo", { method: "POST" });
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

  function startMark(mode: MarkMode) {
    if (!cur) return;
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
    // タッチ経由で呼ばれても動くようにしてある
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

  // -- 区間（issue #46） -------------------------------------------------
  // 動画編集ソフトのイン点・アウト点と同じ操作系。「2」で漏れている位置を
  // タップしたあと、「I」で始点として確定し、矢印キーで終点のコマへ移動、
  // 再びタップして「O」で確定すると、あいだを補間で埋める。マウスでしか
  // 押せない小さいボタンを増やすと編集ソフトらしさが消えるので、ここは
  // キーだけで完結させ、ボタンはその代替（同じ関数を呼ぶ）として添える。

  function markStart() {
    if (markMode !== "add" || !tap || !cur) {
      setNotice("先に「漏れている」でタップして位置を決めてから I を押してください");
      return;
    }
    setIntervalStart({ frame: cur.frame, tap, size: boxSize });
    setTap(null);
    setNotice("");
  }

  async function confirmInterval() {
    if (busy) return;
    if (!intervalStart) {
      setNotice("先に I で区間の始点を置いてください");
      return;
    }
    if (markMode !== "add" || !tap || !cur || !state) {
      setNotice("終点の位置をタップしてください");
      return;
    }
    const useClass = cls || state.default_class;
    const payload: Partial<MarkRequest> = {
      x: tap[0],
      y: tap[1],
      w: boxSize[0],
      h: boxSize[1],
      class: useClass,
      start_frame: intervalStart.frame,
      start_x: intervalStart.tap[0],
      start_y: intervalStart.tap[1],
      start_w: intervalStart.size[0],
      start_h: intervalStart.size[1],
    };
    cancelMark();
    await judge("fixed", payload);
  }

  async function reloadQueue(params: Record<string, string | number>) {
    setSave({ kind: "busy", text: "作り直し中" });
    try {
      // トークンは api() が付ける。ここで url() を通すと t が二重に載る
      const qs = new URLSearchParams();
      qs.set("rebuild", "1");
      for (const [k, v] of Object.entries(params)) qs.set(k, String(v));
      const q = await api<QueuePayload>(API + "/queue?" + qs.toString());
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

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      const t = ev.target as HTMLElement;
      if (t.tagName === "INPUT" || t.tagName === "SELECT") return;
      switch (ev.key) {
        case "1": void judge("ok"); break;
        case "2": startMark("add"); break;
        case "3": void judge("unsure"); break;
        case "4": startMark("shrink"); break;
        case "5": startMark("erase"); break;
        case "u":
        case "U": void undo(); break;
        case "i":
        case "I": markStart(); break;
        case "o":
        case "O": void confirmInterval(); break;
        case "ArrowLeft": goto(idx - 1); break;
        case "ArrowRight": goto(idx + 1); break;
        case "Escape": cancelMark(); break;
        default: return;
      }
      ev.preventDefault();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  const spec = markMode ? MARK_MODES[markMode] : null;
  const confirmLabel = erase
    ? erase.confirmLabel
    : spec
      ? tap
        ? spec.confirm
        : spec.wait
      : "";
  const confirmDisabled = erase ? erase.confirmDisabled : !tap;
  const bannerText = spec
    ? erase
      ? erase.banner
      : interval.active
        ? interval.banner
        : tap
          ? "大きさは下のスライダで調整できます。区間追従は I で始点を置けます"
          : spec.hint
    : notice || bannerFor(items, idx);
  const posText = cur
    ? `${idx + 1} / ${items.length}`
    : items.length
      ? "全部見終わりました"
      : "対象がありません";
  const pct = progress ? progressPercent(progress) : 0;

  return (
    <>
      <header class="top">
        <a id="back" class="btn" href={link("/job/" + JOB)}>戻る</a>
        <span id="pos">{state ? posText : "読み込み中"}</span>
        <span id="reason" class={cur ? "tag p" + cur.priority : "tag"}>
          {cur ? `${cur.label}  frame ${cur.frame}` : ""}
        </span>
        <span class="spacer" />
        <span id="save" class={"save " + save.kind}>{save.text}</span>
        <button id="btn-menu" onClick={() => setSheetOpen(true)}>設定</button>
      </header>

      <div class="bar"><i id="progress-fill" style={{ width: pct.toFixed(1) + "%" }} /></div>

      <div id="stage">
        <div id="imgwrap">
          <img id="shot" alt="判定対象のフレーム"
               src={cur ? frameUrl(cur.frame, imgWidth) : undefined} />
          <canvas id="ov" ref={ovRef}
                  width={state?.width ?? 0} height={state?.height ?? 0}
                  onPointerDown={onCanvasPointerDown} />
        </div>
      </div>
      <div id="banner" class={bannerText ? "" : "hidden"}>{bannerText}</div>

      <div class="pad">
        {/* 判定モード。ここだけで1枚が終わるのが普通の流れ */}
        <div id="judge" class={markMode ? "hidden" : ""}>
          <div class="row">
            <button id="btn-ok" class="big ok half" onClick={() => void judge("ok")}>問題なし</button>
            <button id="btn-ng" class="big ng half" onClick={() => startMark("add")}>漏れている</button>
          </div>
          <div class="row" style={{ marginTop: "8px" }}>
            <button id="btn-big" class="big warn half" onClick={() => startMark("shrink")}>でかすぎる</button>
            {/* 誤検知はモザイクを消す方向なので、色を灰にして目立たせない */}
            <button id="btn-fp" class="big dim half" onClick={() => startMark("erase")}>誤検知</button>
          </div>
          <div class="row" style={{ marginTop: "8px" }}>
            <button id="btn-unsure" class="half" onClick={() => void judge("unsure")}>判断できない</button>
            <button id="btn-undo" class="half" disabled={progress ? !progress.can_undo : false}
                    onClick={() => void undo()}>ひとつ戻す</button>
          </div>
        </div>

        {/* 位置指定モード。3つの判定（漏れている・でかすぎる・誤検知）で共用する */}
        <div id="mark" class={markMode ? "" : "hidden"}>
          <div id="mark-title" class={markMode === "erase" ? "mark-title danger" : "mark-title"}>
            {spec?.title ?? ""}
          </div>
          {markMode !== "erase" && (
            <div class="row">
              <button id="btn-minus" onClick={() => setSizePct((v) => Math.max(20, v - 15))}>−</button>
              <input id="size" type="range" min="20" max="400" step="5" value={sizePct}
                     aria-label="矩形の大きさ" style={{ flex: 1 }}
                     onInput={(e) => setSizePct(Number(e.currentTarget.value))} />
              <button id="btn-plus" onClick={() => setSizePct((v) => Math.min(400, v + 15))}>＋</button>
              <span id="size-label" class="dim mono">{`${boxSize[0]}x${boxSize[1]}px`}</span>
            </div>
          )}
          <div class="row" id="span-row" style={{ marginTop: "8px" }}>
            {spanOptions(step).map((o) => (
              <button key={o.v} class={"span-btn" + (o.v === span ? " on" : "")}
                      onClick={() => setSpan(o.v)}>{o.label}</button>
            ))}
          </div>
          {markMode === "add" && (
            // 区間追従（issue #46）。動画編集ソフトのイン点・アウト点と同じで、
            // タップした位置を I で始点にし、終点のコマで O を押すとあいだを埋める
            <div class="row" id="interval-row" style={{ marginTop: "8px" }}>
              <button id="btn-interval-start" class="half" disabled={!tap}
                      onClick={markStart}>
                {intervalStart
                  ? `始点を更新（I） / いま frame ${intervalStart.frame}`
                  : "ここを区間の始点にする（I）"}
              </button>
              <button id="btn-interval-confirm" class="half ok" disabled={interval.confirmDisabled}
                      onClick={() => void confirmInterval()}>
                区間をここまで塗る（O）
              </button>
            </div>
          )}
          {interval.active && (
            <div id="interval-banner" class="dim mono" style={{ marginTop: "4px" }}>
              {interval.banner}
            </div>
          )}
          <button id="btn-confirm" class={"big " + (markMode === "erase" ? "ng" : "ok")}
                  style={{ marginTop: "8px" }} disabled={confirmDisabled} onClick={confirmMark}>
            {confirmLabel}
          </button>
          <div class="row" style={{ marginTop: "8px" }}>
            <button id="btn-cancel" class="half" onClick={cancelMark}>やめる</button>
            <button id="btn-undo2" class="half" disabled={progress ? !progress.can_undo : false}
                    onClick={() => void undo()}>ひとつ戻す</button>
          </div>
        </div>
      </div>

      {/* 設定シート。普段は畳んでおく */}
      <div id="sheet" class={"card" + (sheetOpen ? "" : " hidden")}
           style={{ maxWidth: "720px", margin: "0 auto 20px" }}>
        <h2>設定</h2>
        <div class="row">
          <label>
            <input id="opt-raw" type="checkbox" checked={optRaw}
                   onChange={(e) => { setOptRaw(e.currentTarget.checked); setNotice(""); }} />
            {" 原画で確認する"}
          </label>
          <label>
            <input id="opt-boxes" type="checkbox" checked={optBoxes}
                   onChange={(e) => setOptBoxes(e.currentTarget.checked)} />
            {" モザイクの範囲を枠で示す"}
          </label>
          <label>
            <input id="opt-all" type="checkbox" checked={allFrames}
                   onChange={(e) => {
                     const on = e.currentTarget.checked;
                     setAllFrames(on);
                     void reloadQueue({ all: on ? 1 : 0 });
                   }} />
            {" 全フレームを対象にする"}
          </label>
        </div>
        <div class="row" style={{ marginTop: "8px" }}>
          <label>間隔
            <input id="opt-step" type="number" min="1" max="300" value={stepInput}
                   style={{ width: "80px" }}
                   onInput={(e) => setStepInput(e.currentTarget.value)} />
            {" フレームおき"}
          </label>
          <label>クラス
            <select id="opt-class" value={cls} onChange={(e) => setCls(e.currentTarget.value)}>
              {(state?.classes ?? []).map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
        </div>
        <div class="row" style={{ marginTop: "8px" }}>
          <button id="btn-rebuild"
                  onClick={() => void reloadQueue({
                    step: Math.max(1, numOr(stepInput, 5)),
                    all: allFrames ? 1 : 0,
                  })}>キューを作り直す</button>
          <button id="btn-unjudged"
                  onClick={() => { setSheetOpen(false); goto(firstUnjudged(items, 0)); }}>未判定の先頭へ</button>
          <button id="btn-prev-item" onClick={() => goto(idx - 1)}>前の1枚</button>
          <button id="btn-next-item" onClick={() => goto(idx + 1)}>次の1枚</button>
          <button id="btn-close-sheet" onClick={() => setSheetOpen(false)}>閉じる</button>
        </div>
        <p id="sheet-info" class="dim">
          {progress
            ? `${progress.total} 枚中 ${progress.done} 枚判定済み（残り ${progress.remaining}）` +
              `  問題なし ${progress.counts.ok} / 塞いだ ${progress.counts.fixed}` +
              ` / 狭めた ${progress.counts.toobig} / 誤検知 ${progress.counts.false_positive}` +
              ` / 保留 ${progress.counts.unsure}`
            : ""}
        </p>
        <p class="dim">
          キー: 1 問題なし / 2 漏れている / 3 判断できない / 4 でかすぎる /
          5 誤検知 / U ひとつ戻す / ← → 前後の1枚 / Esc やめる
        </p>
        <p class="dim">
          区間追従（#46）: 2 でタップして I が始点 → ← → で終点のコマへ移動して
          タップ → O で確定（あいだを補間して埋める）
        </p>
      </div>
    </>
  );
}

document.body.textContent = "";
render(<App />, document.body);
