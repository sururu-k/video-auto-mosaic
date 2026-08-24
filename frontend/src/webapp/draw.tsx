// 手描きモード。検出をかけずに、空の状態から矩形を置く。
//
// 全フレームに打つのは現実的でないので、数フレームおきに打った点の
// あいだを補間で埋める。補間の規則はサーバ側で
// tools/annotations_to_corrections.py の build() をそのまま呼んでいる。
// 「ここには無い」を打てるようにしてあるのは、対象が画面から消えた後も
// モザイクが伸び続けるのを止めるため。

import { render } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";

import type { Annotation, AnnotationsPayload, ExpandResponse, StateLight } from "../shared/api.js";
import { drawHandOverlay } from "../shared/canvas-draw.js";
import { normFromClient, scaledSize, tapToBox } from "../shared/geom.js";
import type { NormPoint } from "../shared/geom.js";
import { dispatchKey, DRAW_KEYS, helpRows } from "../shared/keymap.js";
import type { KeyLike } from "../shared/keymap.js";
import { numOr, requestWidth } from "../shared/review-logic.js";
import { api, errText, link, url } from "../shared/webapp-net.js";

const JOB = location.pathname.split("/").pop() ?? "";
const API = "/api/jobs/" + JOB;

interface SaveState {
  kind: "ok" | "busy" | "err";
  text: string;
}

function App() {
  const [state, setState] = useState<StateLight | null>(null);
  const [frame, setFrame] = useState(0);
  const [points, setPoints] = useState<Annotation[]>([]);
  const [sizePct, setSizePct] = useState(100);
  const [tap, setTap] = useState<NormPoint | null>(null);
  const [imgWidth, setImgWidth] = useState(720);
  const [busy, setBusy] = useState(false);
  const [raw, setRaw] = useState(true);
  const [cls, setCls] = useState("");
  const [stride, setStride] = useState("10");
  const [maxInterp, setMaxInterp] = useState("20");
  const [hold, setHold] = useState("4");
  // 既定は「既存の手修正に足す」。切ると展開が置き換えになり、検査キューで
  // 積んだ修正（漏れを塞いだ矩形・狭めた矩形・誤検知の打ち消し）が手描きを
  // 一度使うだけで全部消える。handdraw.expand の既定と揃えてある
  const [merge, setMerge] = useState(true);
  const [expandMsg, setExpandMsg] = useState("");
  const [banner, setBanner] = useState("好きなフレームで画像をタップすると、そこに矩形を置きます");
  const [save, setSave] = useState<SaveState>({ kind: "ok", text: "-" });
  const [helpOpen, setHelpOpen] = useState(false);

  const ovRef = useRef<HTMLCanvasElement>(null);
  // 「1つ進んでから置く」のように同じ処理で位置を2回動かす経路があるので ref でも持つ
  const frameRef = useRef(0);
  frameRef.current = frame;

  const boxSize = state ? scaledSize(state.default_size, sizePct) : ([64, 64] as [number, number]);
  const pending = tap && state ? tapToBox(tap, boxSize, state.width, state.height) : null;
  const placed = points.find((p) => p.frame === frame) ?? null;
  const strideN = Math.max(1, numOr(stride, 10));

  useEffect(() => {
    void (async () => {
      try {
        const st = await api<StateLight>(API + "/state?light=1");
        setState(st);
        setCls(st.default_class);
        setImgWidth(requestWidth(st.width, window.screen.width, window.devicePixelRatio || 1));
        const d = await api<AnnotationsPayload>(API + "/annotations");
        setPoints(d.annotations);
        setSave({ kind: "ok", text: `打点 ${d.annotations.length}` });
      } catch (e) {
        setSave({ kind: "err", text: "起動に失敗" });
        setBanner("起動に失敗しました: " + errText(e));
      }
    })();
  }, []);

  useEffect(() => {
    const ctx = ovRef.current?.getContext("2d");
    if (!ctx || !state) return;
    drawHandOverlay(ctx, {
      width: state.width,
      height: state.height,
      placed: placed?.box ?? null,
      pending,
    });
  }, [state, placed, pending]);

  function frameUrl(n: number): string {
    const p: Record<string, string | number> = { n, fmt: "jpg", w: imgWidth };
    if (raw) p["raw"] = 1;
    return url(API + "/frame", p);
  }

  function goto(n: number) {
    if (!state) return;
    const v = Math.max(0, Math.min(state.n_frames - 1, Math.round(n)));
    frameRef.current = v;
    setFrame(v);
    setTap(null);
  }

  async function send(body: unknown) {
    if (busy) return;
    setBusy(true);
    setSave({ kind: "busy", text: "保存中" });
    try {
      const d = await api<AnnotationsPayload>(API + "/annotations", { json: body });
      setPoints(d.annotations);
      setSave({ kind: "ok", text: `打点 ${d.annotations.length}` });
      setTap(null);
    } catch (e) {
      setSave({ kind: "err", text: "保存できません" });
      setBanner(errText(e));
    } finally {
      setBusy(false);
    }
  }

  // この画面に動画再生は無い（1フレームずつ打点する道具）。Space / J / K / L
  // を押しても無反応にはせず、できないことを banner に出す（issue #79）
  function noPlayback() {
    setBanner("この画面に連続再生はありません（1コマずつ打点する画面です）");
  }

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      const t = ev.target as HTMLElement | null;
      const like: KeyLike = {
        key: ev.key,
        shiftKey: ev.shiftKey,
        targetTag: t?.tagName ?? null,
        targetEditable: !!t?.isContentEditable,
      };
      const handled = dispatchKey(DRAW_KEYS, like, {
        // ← / → / , / . はどれも1フレーム移動（全画面で揃える。issue #79）。
        // 旧: ← / → だけ strideN フレーム移動だったのを Shift+← / Shift+→ へ移した
        stepBack: () => goto(frameRef.current - 1),
        stepForward: () => goto(frameRef.current + 1),
        jumpBack: () => goto(frameRef.current - strideN),
        jumpForward: () => goto(frameRef.current + strideN),
        goHome: () => goto(0),
        goEnd: () => goto((state?.n_frames ?? 1) - 1),
        playToggle: noPlayback,
        shuttleReverse: noPlayback,
        shuttleStop: noPlayback,
        shuttleForward: noPlayback,
        help: () => setHelpOpen((v) => !v),
        confirmTap: () => {
          if (tap && state) {
            void send({
              frame: frameRef.current, x: tap[0], y: tap[1],
              w: boxSize[0], h: boxSize[1], class: cls,
            });
          }
        },
        markAbsent: () => void send({ frame: frameRef.current, absent: true }),
      });
      if (handled) ev.preventDefault();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  const mark = placed ? (placed.box ? "打点あり" : "「ここには無い」") : "打点なし";

  return (
    <>
      <header class="top">
        <a id="back" class="btn" href={link("/job/" + JOB)}>戻る</a>
        <span id="pos">
          {state ? `frame ${frame} / ${state.n_frames - 1}  (${mark})` : "読み込み中"}
        </span>
        <span class="spacer" />
        <span id="save" class={"save " + save.kind}>{save.text}</span>
      </header>

      {state?.effective_check.restored && (
        <div id="effective-restored" class="restored-note">
          設定を復元して表示中: {state.effective_check.note}
        </div>
      )}

      <div id="stage">
        <div id="imgwrap">
          <img id="shot" alt="対象のフレーム" src={state ? frameUrl(frame) : undefined} />
          <canvas id="ov" ref={ovRef}
                  width={state?.width ?? 0} height={state?.height ?? 0}
                  onPointerDown={(ev) => {
                    ev.preventDefault();
                    const cv = ovRef.current;
                    if (!cv) return;
                    const touches = (ev as PointerEvent & { changedTouches?: TouchList }).changedTouches;
                    const t = touches ? touches[0]! : ev;
                    setTap(normFromClient(cv.getBoundingClientRect(), t.clientX, t.clientY));
                  }} />
        </div>
      </div>
      <div id="banner">{banner}</div>

      {/* 割り当て表（shared/keymap.ts の DRAW_KEYS）から自動で作る。
          ? キーで開閉する（issue #79） */}
      {helpOpen && (
        <div class="card" style={{ margin: "8px 0" }}>
          <h2>キー</h2>
          <ul>
            {helpRows(DRAW_KEYS).map((r) => (
              <li key={r.label}><b>{r.label}</b> {r.desc}</li>
            ))}
          </ul>
        </div>
      )}

      <div class="pad">
        <div class="row">
          <button id="b-help" onClick={() => setHelpOpen((v) => !v)}>キー一覧 (?)</button>
          <button id="b-first" onClick={() => goto(0)}>≪</button>
          <button id="b-back10" onClick={() => goto(frame - strideN)}>-10</button>
          <button id="b-back" onClick={() => goto(frame - 1)}>-1</button>
          <input id="seek" type="range" min="0" max={state ? state.n_frames - 1 : 0} value={frame}
                 style={{ flex: 1 }} aria-label="フレーム位置"
                 onInput={(e) => goto(Number(e.currentTarget.value))} />
          <button id="b-fwd" onClick={() => goto(frame + 1)}>+1</button>
          <button id="b-fwd10" onClick={() => goto(frame + strideN)}>+10</button>
          <button id="b-last" onClick={() => goto((state?.n_frames ?? 1) - 1)}>≫</button>
        </div>
        <div class="row" style={{ marginTop: "8px" }}>
          <label>移動幅
            <input id="stride" type="number" min="1" max="300" value={stride}
                   style={{ width: "70px" }} onInput={(e) => setStride(e.currentTarget.value)} />
          </label>
          <button id="b-prev-pt"
                  onClick={() => {
                    const prev = points.filter((p) => p.frame < frame).pop();
                    if (prev) goto(prev.frame);
                  }}>前の打点</button>
          <button id="b-next-pt"
                  onClick={() => {
                    const next = points.find((p) => p.frame > frame);
                    if (next) goto(next.frame);
                  }}>次の打点</button>
          <label>
            <input id="opt-raw" type="checkbox" checked={raw}
                   onChange={(e) => setRaw(e.currentTarget.checked)} />
            {" 原画で見る"}
          </label>
        </div>

        <div class="row" style={{ marginTop: "10px" }}>
          <button id="b-minus" onClick={() => setSizePct((v) => Math.max(20, v - 15))}>−</button>
          <input id="size" type="range" min="20" max="400" step="5" value={sizePct}
                 style={{ flex: 1 }} aria-label="矩形の大きさ"
                 onInput={(e) => setSizePct(Number(e.currentTarget.value))} />
          <button id="b-plus" onClick={() => setSizePct((v) => Math.min(400, v + 15))}>＋</button>
          <span id="size-label" class="dim mono">{`${boxSize[0]}x${boxSize[1]}px`}</span>
        </div>

        <div class="row" style={{ marginTop: "8px" }}>
          <button id="b-put" class="big ok half" disabled={!tap}
                  onClick={() => {
                    if (!tap) return;
                    void send({
                      frame, x: tap[0], y: tap[1], w: boxSize[0], h: boxSize[1], class: cls,
                    });
                  }}>ここに置く</button>
          <button id="b-absent" class="half"
                  onClick={() => void send({ frame, absent: true })}>ここには無い</button>
          <button id="b-del" class="half" disabled={!placed}
                  onClick={() => {
                    void (async () => {
                      try {
                        const d = await api<AnnotationsPayload>(`${API}/annotations/${frame}`, {
                          method: "DELETE",
                        });
                        setPoints(d.annotations);
                        setSave({ kind: "ok", text: `打点 ${d.annotations.length}` });
                      } catch (e) {
                        setBanner(errText(e));
                      }
                    })();
                  }}>この打点を消す</button>
        </div>

        <div class="card" style={{ marginTop: "12px" }}>
          <h2>補間して反映する</h2>
          <div class="row">
            <label>補間する最大間隔
              <input id="max_interp" type="number" min="1" max="300" value={maxInterp}
                     style={{ width: "80px" }} onInput={(e) => setMaxInterp(e.currentTarget.value)} />
            </label>
            <label>点の後ろに持たせる
              <input id="hold" type="number" min="0" max="60" value={hold}
                     style={{ width: "70px" }} onInput={(e) => setHold(e.currentTarget.value)} />
              {" フレーム"}
            </label>
            <label>クラス
              <select id="opt-class" value={cls} onChange={(e) => setCls(e.currentTarget.value)}>
                {(state?.classes ?? []).map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
            <label>
              <input id="merge" type="checkbox" checked={merge}
                     onChange={(e) => setMerge(e.currentTarget.checked)} />
              {" 既存の手修正に足す"}
            </label>
          </div>
          <div class="row" style={{ marginTop: "8px" }}>
            <button id="b-expand" class="primary"
                    onClick={() => {
                      setExpandMsg("展開しています");
                      void (async () => {
                        try {
                          const d = await api<ExpandResponse>(API + "/annotations/expand", {
                            json: {
                              max_interp: numOr(maxInterp, 20),
                              // 空欄を 0 と読まない。0 だと打点の後ろの
                              // モザイクが黙って消える（漏れる方向）
                              hold: numOr(hold, 4),
                              class: cls,
                              merge,
                            },
                          });
                          setExpandMsg(
                            `打点 ${d.points} 個 -> 矩形 ${d.expanded} 件（corrections 計 ${d.corrections} 件）` +
                              (d.frame_range ? `  フレーム ${d.frame_range[0]}〜${d.frame_range[1]}` : "") +
                              "  ジョブ画面の「検出を再利用して焼き直す」で反映されます",
                          );
                        } catch (e) {
                          setExpandMsg("展開できません: " + errText(e));
                        }
                      })();
                    }}>補間して corrections に展開</button>
            <a id="go-job" class="btn" href={link("/job/" + JOB)}>ジョブ画面へ（焼き直し）</a>
          </div>
          <p id="expand-msg" class="dim">{expandMsg}</p>
        </div>

        <div class="card">
          <h2>打点</h2>
          <div id="points" class="mono dim">
            {points.length ? (
              points.map((p) => (
                <a key={p.frame} href="#" style={{ marginRight: "10px" }}
                   onClick={(e) => { e.preventDefault(); goto(p.frame); }}>
                  {p.box
                    ? `${p.frame}: [${p.box.map((v) => Math.round(v)).join(",")}]`
                    : `${p.frame}: 無し`}
                </a>
              ))
            ) : (
              "まだありません"
            )}
          </div>
        </div>
      </div>
    </>
  );
}

document.body.textContent = "";
render(<App />, document.body);
