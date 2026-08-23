// automosaic タイムライン画面（PC 向け）。
//
// 動画をまとめて見たいとき用の従来画面。既定の入口は検査キュー側で、
// こちらは「通しで見て帯の色を確かめる」ためだけに残してある。
// 全フレームを送るのは被覆状況の文字列と矩形リストだけで、
// 絵は必要なフレームだけ取りに行く。
//
// 帯と重ね枠は canvas 直描き（shared/canvas-draw.ts）。それ以外は Preact。

import { render } from "preact";
import { useEffect, useMemo, useRef, useState } from "preact/hooks";

import type { Box, Correction, CorrectionsPayload, FrameRange, StateFull, UpdatePayload } from "../shared/api.js";
import { drawRegionOverlay, drawTimelineBand } from "../shared/canvas-draw.js";
import { frameFromClient } from "../shared/geom.js";
import type { FramePoint } from "../shared/geom.js";
import { errText, url as u } from "../shared/review-net.js";

interface SaveStatus {
  kind: "saved" | "dirty" | "error";
  text: string;
}

const HINT_DEFAULT =
  "M で追加モード → 動画上をクリックして矩形を置く → 1 でこのフレームだけ / 2 で以降に適用";

function App() {
  const [state, setState] = useState<StateFull | null>(null);
  // 手修正の全リスト。これをそのまま POST する
  const [corrections, setCorrections] = useState<Correction[]>([]);
  const [cur, setCur] = useState(0);
  const [addMode, setAddMode] = useState(false);
  // 置いたがまだ適用していない矩形
  const [pending, setPending] = useState<Box | null>(null);
  const [size, setSize] = useState<[number, number]>([64, 64]);
  const [playing, setPlaying] = useState(false);
  const [confMin, setConfMin] = useState(0);
  const [rawStill, setRawStill] = useState(false);
  const [spanInput, setSpanInput] = useState("30");
  const [cls, setCls] = useState("");
  const [hint, setHint] = useState(HINT_DEFAULT);
  const [hintActive, setHintActive] = useState(false);
  const [save, setSave] = useState<SaveStatus>({ kind: "saved", text: "保存済み" });
  const [stillSrc, setStillSrc] = useState<string | null>(null);
  const [stillShown, setStillShown] = useState(false);
  const [resizeTick, setResizeTick] = useState(0);

  const videoRef = useRef<HTMLVideoElement>(null);
  const overlayRef = useRef<HTMLCanvasElement>(null);
  const bandRef = useRef<HTMLCanvasElement>(null);
  const listRefs = useRef<HTMLUListElement[]>([]);
  // 動画座標系のカーソル位置。D の対象判定に使う。描画に効かないので ref
  const mouse = useRef<FramePoint | null>(null);
  // 古いフレーム画像の到着で上書きされるのを防ぐ
  const stillToken = useRef(0);
  // 再生中の位置合わせで使う。setFrame を通さずに読むため
  const playingRef = useRef(false);
  playingRef.current = playing;
  // 「止めてから1コマ戻る」のように、同じ処理の中で現在位置を2回動かす経路がある。
  // state の更新は次の描画までは反映されないので、位置だけは ref でも持つ
  const curRef = useRef(0);
  // クラス選択。applyPending から読むだけなので ref にも写しておく
  const clsRef = useRef("");
  clsRef.current = cls || state?.default_class || "";

  const regions = state?.regions[String(cur)] ?? [];

  // ----------------------------------------------------------------
  // 読み込み
  // ----------------------------------------------------------------
  useEffect(() => {
    void (async () => {
      try {
        const st = (await (await fetch(u("/api/state"))).json()) as StateFull;
        const c = (await (await fetch(u("/api/corrections"))).json()) as CorrectionsPayload;
        setState(st);
        setSize([st.default_size[0], st.default_size[1]]);
        setCls(st.default_class);
        setCorrections(c.corrections ?? []);
      } catch (e) {
        setSave({ kind: "error", text: "起動に失敗: " + errText(e) });
      }
    })();
  }, []);

  // ----------------------------------------------------------------
  // フレーム移動
  // ----------------------------------------------------------------
  function loadStill(n: number, raw = rawStill) {
    const token = ++stillToken.current;
    const img = new Image();
    img.onload = () => {
      if (token !== stillToken.current) return; // 古い要求。捨てる
      setStillSrc(img.src);
      setStillShown(true);
    };
    img.src = raw ? u("/frame", { n, raw: 1 }) : u("/frame", { n });
  }

  function hideStill() {
    stillToken.current++;
    setStillShown(false);
  }

  function setFrame(n: number, force = false) {
    const st = state;
    if (!st) return;
    const v = Math.max(0, Math.min(st.n_frames - 1, Math.round(n)));
    if (v === curRef.current && !force) return;
    curRef.current = v;
    setCur(v);
    if (!playingRef.current) {
      // フレーム中央の時刻を狙う。境界ちょうどだと前後どちらが出るか不定
      if (st.has_video && videoRef.current) videoRef.current.currentTime = (v + 0.5) / st.fps;
      loadStill(v);
    }
  }

  // 再生中はフレーム番号を動画の再生位置から拾い直す
  useEffect(() => {
    if (!playing || !state?.has_video) return;
    let live = true;
    const tick = () => {
      if (!live) return;
      const vid = videoRef.current;
      if (vid) {
        const n = Math.round(vid.currentTime * state.fps);
        if (n !== curRef.current) {
          curRef.current = n;
          setCur(n);
        }
      }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
    return () => {
      live = false;
    };
  }, [playing, state]);

  function play() {
    if (!state?.has_video) return;
    playingRef.current = true;
    setPlaying(true);
    hideStill();
    void videoRef.current?.play();
  }

  function pause() {
    setPlaying(false);
    playingRef.current = false;
    const vid = videoRef.current;
    vid?.pause();
    if (vid && state) setFrame(Math.round(vid.currentTime * state.fps), true);
  }

  function togglePlay() {
    if (playing) pause();
    else play();
  }

  /** 再生中に押されたら止めてから動く。原稿の各所にあった前置き */
  function pausedThen(fn: () => void) {
    if (playingRef.current) pause();
    fn();
  }

  // ----------------------------------------------------------------
  // 重ね描きと帯
  // ----------------------------------------------------------------
  useEffect(() => {
    const ctx = overlayRef.current?.getContext("2d");
    if (!ctx || !state) return;
    drawRegionOverlay(ctx, {
      width: state.width,
      height: state.height,
      regions,
      pending,
      confMin,
    });
  }, [state, regions, pending, confMin]);

  const correctionFrames = useMemo(() => corrections.map((c) => c.frame), [corrections]);

  useEffect(() => {
    const cv = bandRef.current;
    const ctx = cv?.getContext("2d");
    if (!cv || !ctx || !state) return;
    const w = cv.clientWidth;
    if (cv.width !== w) cv.width = w;
    drawTimelineBand(ctx, {
      width: w,
      height: cv.height,
      coverage: state.coverage,
      nFrames: state.n_frames,
      correctionFrames,
      cur,
    });
  }, [state, correctionFrames, cur, resizeTick]);

  useEffect(() => {
    const onResize = () => setResizeTick((v) => v + 1);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // 現在位置の項目を見えるところへ持ってくる。再生中はやらない
  // （毎フレーム動くとリストが流れ続けて読めない）
  useEffect(() => {
    if (playing) return;
    for (const ul of listRefs.current) {
      if (!ul) continue;
      const li = ul.querySelector("li.current");
      li?.scrollIntoView({ block: "nearest" });
    }
  }, [cur, playing, corrections, state]);

  // ----------------------------------------------------------------
  // 修正
  // ----------------------------------------------------------------
  async function pushCorrections(items: Correction[]) {
    setCorrections(items);
    setSave({ kind: "dirty", text: "保存中" });
    try {
      const res = await fetch(u("/api/corrections"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ corrections: items }),
      });
      if (!res.ok) throw new Error(await res.text());
      const d = (await res.json()) as UpdatePayload;
      // 手修正は実観測扱いなので、帯の色も推定のみ区間も変わる。丸ごと差し替える
      setState((prev) =>
        prev
          ? {
              ...prev,
              coverage: d.coverage,
              regions: d.regions,
              estimated_only_ranges: d.estimated_only_ranges,
              uncovered_ranges: d.uncovered_ranges,
            }
          : prev,
      );
      setSave({ kind: "saved", text: `保存済み (${d.n_corrections} 件)` });
      if (!playingRef.current) loadStill(curRef.current); // モザイクが乗った絵に差し替える
    } catch (e) {
      setSave({ kind: "error", text: "保存失敗: " + errText(e) });
    }
  }

  function placePending(p: FramePoint) {
    const [w, h] = size;
    setPending([Math.round(p[0] - w / 2), Math.round(p[1] - h / 2), w, h]);
    setHintActive(false);
    setHint("矩形を置きました。1 でこのフレームだけ / 2 でここから N フレームに適用");
    setAddMode(false);
  }

  function applyPending(nFrames: number) {
    if (!pending || !state) {
      setHint("先に M を押して動画上をクリックし、矩形を置いてください");
      setHintActive(true);
      return;
    }
    const cls = clsRef.current;
    const at = curRef.current;
    const last = Math.min(state.n_frames - 1, at + nFrames - 1);
    const added: Correction[] = [];
    for (let f = at; f <= last; f++) {
      added.push({ frame: f, box: [...pending], class: cls, kind: "add" });
    }
    setPending(null);
    setHint(`frame ${at}-${last} に ${cls} を追加しました`);
    void pushCorrections([...corrections, ...added]);
  }

  function deleteUnderCursor() {
    const m = mouse.current;
    if (!m) return;
    // 後ろから見て最初に当たった1件だけ消す（corrections.remove_at と同じ規則）
    const at = curRef.current;
    for (let i = corrections.length - 1; i >= 0; i--) {
      const c = corrections[i]!;
      if (c.frame !== at) continue;
      const [x, y, w, h] = c.box;
      if (m[0] >= x && m[0] <= x + w && m[1] >= y && m[1] <= y + h) {
        const next = corrections.slice();
        next.splice(i, 1);
        setHint(`frame ${at} の手修正を1件消しました`);
        void pushCorrections(next);
        return;
      }
    }
    setHint("カーソルの下に手修正がありません");
  }

  function undoLast() {
    if (!corrections.length) return;
    void pushCorrections(corrections.slice(0, -1));
  }

  function nextEstimatedRange() {
    const ranges = state?.estimated_only_ranges ?? [];
    for (const r of ranges) {
      if (r.start > curRef.current) {
        setFrame(r.start, true);
        return;
      }
    }
    // 末尾まで来たら先頭へ戻る
    if (ranges[0]) setFrame(ranges[0].start, true);
  }

  function scaleSize(k: number) {
    const next: [number, number] = [
      Math.max(8, Math.round(size[0] * k)),
      Math.max(8, Math.round(size[1] * k)),
    ];
    setSize(next);
    if (pending) {
      // 置いた矩形は中心を保ったまま伸縮させる
      const cx = pending[0] + pending[2] / 2;
      const cy = pending[1] + pending[3] / 2;
      setPending([Math.round(cx - next[0] / 2), Math.round(cy - next[1] / 2), next[0], next[1]]);
    }
  }

  // ----------------------------------------------------------------
  // 入力
  // ----------------------------------------------------------------
  function canvasPos(ev: MouseEvent): FramePoint | null {
    const cv = overlayRef.current;
    if (!cv || !state) return null;
    return frameFromClient(cv.getBoundingClientRect(), ev.clientX, ev.clientY, state.width, state.height);
  }

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      const t = ev.target as HTMLElement;
      if (t.tagName === "INPUT" || t.tagName === "SELECT") return;
      const k = ev.key;
      if (k === " ") { ev.preventDefault(); togglePlay(); return; }
      if (k === ",") { pausedThen(() => setFrame(curRef.current - 1, true)); return; }
      if (k === ".") { pausedThen(() => setFrame(curRef.current + 1, true)); return; }
      if (k === "[") { scaleSize(1 / 1.15); return; }
      if (k === "]") { scaleSize(1.15); return; }
      if (k === "m" || k === "M") {
        pausedThen(() => {
          setAddMode(true);
          setHintActive(true);
          setHint("追加モード: 動画上をクリックして矩形を置いてください");
        });
        return;
      }
      if (k === "1") { pausedThen(() => applyPending(1)); return; }
      if (k === "2") {
        pausedThen(() => applyPending(Math.max(1, parseInt(spanInput, 10) || 30)));
        return;
      }
      if (k === "d" || k === "D") { deleteUnderCursor(); return; }
      if (k === "g" || k === "G") { pausedThen(nextEstimatedRange); return; }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  // ----------------------------------------------------------------
  // 表示
  // ----------------------------------------------------------------
  const st = state;
  const estFrames = (st?.estimated_only_ranges ?? []).reduce((a, r) => a + r.frames, 0);
  const uncFrames = (st?.uncovered_ranges ?? []).reduce((a, r) => a + r.frames, 0);

  // 手修正はフレームごとにまとめる。連番で置くと1件ずつでは読めない
  const byFrame = useMemo(() => {
    const m = new Map<number, number>();
    for (const c of corrections) m.set(c.frame, (m.get(c.frame) ?? 0) + 1);
    return m;
  }, [corrections]);
  const manFrames = useMemo(() => [...byFrame.keys()].sort((a, b) => a - b), [byFrame]);

  const rangeList = (ranges: readonly FrameRange[], slot: number) => (
    <ul ref={(el) => { if (el) listRefs.current[slot] = el; }}>
      {ranges.map((r) => (
        <li key={`${r.start}-${r.end}`}
            class={cur >= r.start && cur <= r.end ? "current" : ""}
            onClick={() => setFrame(r.start, true)}>
          <span>
            {`${r.start} - ${r.end}  (${(r.start / (st?.fps ?? 1)).toFixed(2)}s - ${(r.end / (st?.fps ?? 1)).toFixed(2)}s)`}
          </span>
          <span class="len">{`${r.frames}f`}</span>
        </li>
      ))}
    </ul>
  );

  return (
    <>
      <header>
        <div class="titles">
          <a id="link-queue" href={u("/")}>検査キューへ戻る</a>
          <span class="sep">|</span>
          <span id="video-name">{st ? st.video : "読み込み中"}</span>
          <span class="sep">|</span>
          <span id="video-meta">
            {st
              ? `${st.width}x${st.height}  ${st.fps.toFixed(3)} fps  ${st.n_frames} フレーム` +
                `  /  再生: ${st.rendered ?? "なし"}`
              : ""}
          </span>
        </div>
        <div id="save-status" class={save.kind}>{save.text}</div>
      </header>

      <main>
        <section class="stage">
          <div id="player"
               style={{
                 width: st ? st.width + "px" : undefined,
                 height: st && !st.has_video ? st.height + "px" : undefined,
               }}>
            <video id="video" ref={videoRef} preload="metadata"
                   src={st?.has_video ? u("/video") : undefined}
                   style={{ display: st && !st.has_video ? "none" : undefined }}
                   onPause={() => { if (playingRef.current) pause(); }}
                   onPlay={() => { playingRef.current = true; setPlaying(true); hideStill(); }}
                   onEnded={() => { playingRef.current = false; setPlaying(false); setFrame(curRef.current, true); }}
                   /* メタデータが載る前の currentTime 代入は黙って無視されるので、載ってから入れ直す */
                   onLoadedMetadata={() => { if (!playingRef.current) setFrame(curRef.current, true); }} />
            {/* <video> のシークはフレーム厳密でないので、停止中は
                サーバから取った厳密なフレーム画像を上に重ねる */}
            <img id="still" alt="" src={stillSrc ?? undefined}
                 class={stillShown ? "show" : ""}
                 style={{ aspectRatio: st ? `${st.width} / ${st.height}` : undefined }} />
            <canvas id="overlay" ref={overlayRef}
                    width={st?.width ?? 0} height={st?.height ?? 0}
                    onMouseMove={(ev) => { mouse.current = canvasPos(ev) ?? mouse.current; }}
                    onClick={(ev) => {
                      const p = canvasPos(ev);
                      if (!p) return;
                      mouse.current = p;
                      if (addMode) placePending(p);
                    }} />
          </div>

          <div class="controls">
            <button id="btn-play" onClick={togglePlay}>再生 / 停止 (Space)</button>
            <button id="btn-prev" onClick={() => pausedThen(() => setFrame(curRef.current - 1, true))}>&lt; コマ戻し (,)</button>
            <button id="btn-next" onClick={() => pausedThen(() => setFrame(curRef.current + 1, true))}>コマ送り (.) &gt;</button>
            <span class="field">フレーム
              <input id="frame-input" type="number" min="0" max={st ? st.n_frames - 1 : undefined}
                     value={cur}
                     onChange={(e) => {
                       const v = Number(e.currentTarget.value);
                       pausedThen(() => setFrame(v, true));
                     }} />
              <span id="frame-total">{st ? `/ ${st.n_frames - 1}` : ""}</span>
            </span>
            <span class="field">時刻 <span id="time-label">{(cur / (st?.fps ?? 1)).toFixed(2) + "s"}</span></span>
            <button id="btn-next-est" onClick={() => pausedThen(nextEstimatedRange)}>次の推定のみ区間 (G)</button>
          </div>

          <div class="controls">
            <span class="field">追加サイズ <span id="size-label">{`${size[0]} x ${size[1]} px`}</span>
              <button id="btn-smaller" onClick={() => scaleSize(1 / 1.15)}>[</button>
              <button id="btn-bigger" onClick={() => scaleSize(1.15)}>]</button>
            </span>
            <span class="field">クラス
              <select id="class-select" value={cls} onChange={(e) => setCls(e.currentTarget.value)}>
                {(st?.classes ?? []).map((name) => <option key={name} value={name}>{name}</option>)}
              </select>
            </span>
            <span class="field">2 の適用フレーム数
              <input id="span-input" type="number" min="1" value={spanInput}
                     onInput={(e) => setSpanInput(e.currentTarget.value)} />
            </span>
            <span class="field">信頼度で隠す
              <input id="conf-slider" type="range" min="0" max="1" step="0.01" value={confMin}
                     onInput={(e) => setConfMin(parseFloat(e.currentTarget.value))} />
              <span id="conf-label">{confMin.toFixed(2)}</span>
            </span>
            <label class="field">
              <input id="raw-toggle" type="checkbox" checked={rawStill}
                     onChange={(e) => {
                       const on = e.currentTarget.checked;
                       setRawStill(on);
                       if (!playingRef.current) loadStill(curRef.current, on);
                     }} />
              {" 原画で確認"}
            </label>
            <span class="field">修正 <span id="corr-count">{corrections.length}</span> 件</span>
            <button id="btn-undo" onClick={undoLast}>直前の修正を取り消す</button>
          </div>

          <div id="mode-hint" class={"hint" + (hintActive ? " active" : "")}>{hint}</div>
        </section>

        <section class="timeline-wrap">
          <canvas id="timeline" ref={bandRef} height={46}
                  onClick={(ev) => {
                    const cv = bandRef.current;
                    if (!cv || !st) return;
                    const r = cv.getBoundingClientRect();
                    const f = Math.floor(((ev.clientX - r.left) / r.width) * st.n_frames);
                    pausedThen(() => setFrame(f, true));
                  }} />
          <div class="legend">
            <span><i class="sw sw-real" />被覆あり（実観測）</span>
            <span><i class="sw sw-est" />推定のみ</span>
            <span><i class="sw sw-none" />未処理</span>
            <span><i class="sw sw-manual" />手修正あり</span>
          </div>
        </section>

        <section class="lists">
          <div class="list-col">
            <h2>推定のみの区間 <span id="est-count">
              {st
                ? `${st.estimated_only_ranges.length} 件 / ${estFrames} フレーム ` +
                  `(${((100 * estFrames) / st.n_frames).toFixed(1)}%)`
                : ""}
            </span></h2>
            <p class="note">検出が1つも無く、補間と memory だけで塗っている。位置は当てずっぽうに近い</p>
            {rangeList(st?.estimated_only_ranges ?? [], 0)}
          </div>
          <div class="list-col">
            <h2>未処理の区間 <span id="unc-count">
              {st ? `${st.uncovered_ranges.length} 件 / ${uncFrames} フレーム` : ""}
            </span></h2>
            <p class="note">モザイクが1つも乗っていない。素通し</p>
            {rangeList(st?.uncovered_ranges ?? [], 1)}
          </div>
          <div class="list-col">
            <h2>手修正 <span id="man-count">
              {`${corrections.length} 件 / ${manFrames.length} フレーム`}
            </span></h2>
            <p class="note">D でカーソル下の手修正を削除</p>
            <ul ref={(el) => { if (el) listRefs.current[2] = el; }}>
              {manFrames.map((f) => (
                <li key={f} class={cur === f ? "current" : ""} onClick={() => setFrame(f, true)}>
                  <span>{`frame ${f}  (${(f / (st?.fps ?? 1)).toFixed(2)}s)`}</span>
                  <span class="len">{String(byFrame.get(f))}</span>
                </li>
              ))}
            </ul>
          </div>
        </section>

        <section class="keys">
          <b>キー</b>
          {" Space 再生/停止 ・ , . コマ移動 ・ [ ] 矩形サイズ ・ " +
           "M 追加モード ・ 1 このフレームだけ ・ 2 ここから N フレーム ・ " +
           "D 削除 ・ G 次の推定のみ区間"}
        </section>
      </main>
    </>
  );
}

document.body.textContent = "";
render(<App />, document.body);
