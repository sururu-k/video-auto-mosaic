// 検査キュー画面。既存レビュー UI（automosaic/web/app.tsx）と同じ操作系を
// ジョブ単位の API に載せ替えたもの。
//
// 「どのフレームを見るか」はサーバが決める。画面はそれを1枚ずつ出して、
// 押された判定を返すだけにしてある。判定の記録もサーバ持ちなので、
// 端末を閉じても、別の端末で開き直しても続きから再開できる。
//
// 操作は指だけで完結させる。キーボードの割り当ては残してあるが、
// それが無いと出来ないことは1つも作らない。

import { render } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";

import type {
  MarkRequest,
  MarkResponse,
  Progress,
  QueueItem,
  QueuePayload,
  StateLight,
  UndoResponse,
  Verdict,
} from "../shared/api.js";
import { drawReviewOverlay } from "../shared/canvas-draw.js";
import { normFromClient, scaledSize, tapToBox } from "../shared/geom.js";
import type { NormPoint } from "../shared/geom.js";
import { firstUnjudged, numOr, progressPercent, requestWidth, spanOptions } from "../shared/review-logic.js";
import { api, errText, link, url } from "../shared/webapp-net.js";

const JOB = location.pathname.split("/").pop() ?? "";
const API = "/api/jobs/" + JOB;

/** この画面が出せる判定は3つだけ。狭める・誤検知は単体レビュー UI 側にある */
const JUDGED_LABEL: Partial<Record<Verdict, string>> = {
  ok: "判定済み: 問題なし",
  fixed: "判定済み: 塞いだ",
  unsure: "判定済み: 保留",
};

interface SaveState {
  kind: "ok" | "busy" | "err";
  text: string;
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
  const [marking, setMarking] = useState(false);
  // 正規化タップ座標。サーバへはこちらを送る
  const [tap, setTap] = useState<NormPoint | null>(null);

  const [busy, setBusy] = useState(false);
  const [imgWidth, setImgWidth] = useState(720);
  const [notice, setNotice] = useState("");
  const [save, setSave] = useState<SaveState>({ kind: "ok", text: "保存済" });
  const [sheetOpen, setSheetOpen] = useState(false);

  const ovRef = useRef<HTMLCanvasElement>(null);

  const cur = items[idx] ?? null;
  const boxSize = state ? scaledSize(state.default_size, sizePct) : ([64, 64] as [number, number]);
  const pending = tap && state ? tapToBox(tap, boxSize, state.width, state.height) : null;

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
      markMode: null,
      picked: [],
      pending,
    });
  }, [state, cur, optBoxes, pending]);

  function cancelMark() {
    setMarking(false);
    setTap(null);
  }

  function goto(i: number, list: QueueItem[] = items) {
    cancelMark();
    setIdx(Math.max(0, Math.min(list.length, i)));
    setNotice("");
  }

  async function judge(verdict: Verdict, extra?: Partial<MarkRequest>) {
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

  function startMark() {
    if (!cur) return;
    setMarking(true);
    setTap(null);
    setNotice("");
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
        case "2": startMark(); break;
        case "3": void judge("unsure"); break;
        case "u":
        case "U": void undo(); break;
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

  const bannerText = marking
    ? tap
      ? "大きさは下のスライダで調整できます"
      : "漏れている場所を、画像の上で直接タップしてください"
    : notice ||
      (cur
        ? cur.verdict
          ? (JUDGED_LABEL[cur.verdict] ?? "")
          : ""
        : items.length
          ? "すべて判定済みです。設定から間隔や対象を変えられます"
          : "");
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
                  onPointerDown={(ev) => {
                    if (!marking) return;
                    ev.preventDefault();
                    const cv = ovRef.current;
                    if (!cv) return;
                    // タッチ経由で呼ばれても動くようにしてある
                    const touches = (ev as PointerEvent & { changedTouches?: TouchList }).changedTouches;
                    const t = touches ? touches[0]! : ev;
                    setTap(normFromClient(cv.getBoundingClientRect(), t.clientX, t.clientY));
                  }} />
        </div>
      </div>
      <div id="banner" class={bannerText ? "" : "hidden"}>{bannerText}</div>

      <div class="pad">
        {/* 判定モード。ここだけで1枚が終わるのが普通の流れ */}
        <div id="judge" class={marking ? "hidden" : ""}>
          <div class="row">
            <button id="btn-ok" class="big ok half" onClick={() => void judge("ok")}>問題なし</button>
            <button id="btn-ng" class="big ng half" onClick={startMark}>漏れている</button>
          </div>
          <div class="row" style={{ marginTop: "8px" }}>
            <button id="btn-unsure" class="half" onClick={() => void judge("unsure")}>判断できない</button>
            <button id="btn-undo" class="half" disabled={progress ? !progress.can_undo : false}
                    onClick={() => void undo()}>ひとつ戻す</button>
          </div>
        </div>

        {/* 位置指定モード */}
        <div id="mark" class={marking ? "" : "hidden"}>
          <div class="row">
            <button id="btn-minus" onClick={() => setSizePct((v) => Math.max(20, v - 15))}>−</button>
            <input id="size" type="range" min="20" max="400" step="5" value={sizePct}
                   aria-label="矩形の大きさ" style={{ flex: 1 }}
                   onInput={(e) => setSizePct(Number(e.currentTarget.value))} />
            <button id="btn-plus" onClick={() => setSizePct((v) => Math.min(400, v + 15))}>＋</button>
            <span id="size-label" class="dim mono">{`${boxSize[0]}x${boxSize[1]}px`}</span>
          </div>
          <div class="row" id="span-row" style={{ marginTop: "8px" }}>
            {spanOptions(step).map((o) => (
              <button key={o.v} class={"span-btn" + (o.v === span ? " on" : "")}
                      onClick={() => setSpan(o.v)}>{o.label}</button>
            ))}
          </div>
          <button id="btn-confirm" class="big ok" style={{ marginTop: "8px" }} disabled={!tap}
                  onClick={() => {
                    if (!tap || !state) return;
                    const payload: Partial<MarkRequest> = {
                      x: tap[0], y: tap[1], w: boxSize[0], h: boxSize[1],
                      span,
                      class: cls || state.default_class,
                    };
                    cancelMark();
                    void judge("fixed", payload);
                  }}>
            {tap ? "この位置で確定" : "画像をタップしてください"}
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
              `  問題なし ${progress.counts.ok} / 塞いだ ${progress.counts.fixed} / 保留 ${progress.counts.unsure}`
            : ""}
        </p>
        <p class="dim">キー: 1 問題なし / 2 漏れている / 3 判断できない / U ひとつ戻す / ← → 前後の1枚</p>
      </div>
    </>
  );
}

document.body.textContent = "";
render(<App />, document.body);
