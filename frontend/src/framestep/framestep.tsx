// automosaic フレーム厳密確認ビュー（issue #19）。
//
// <video> のシークは秒単位で、フレーム番号を指定できない（issue #19 本文参照）。
// ここでは WebCodecs（mediabunny 経由）でコンテナのフレームを直接デコードし、
// 「n 番目のフレーム」を表引きで確定させたうえで、コマ送り・コマ戻し・
// N へのジャンプ・再生/停止を提供する。
//
// WebCodecs が使えないブラウザでは、黙って劣化させずに <video> へフォールバックし、
// 「フレーム厳密ではない」ことを画面に明示する。
//
// 入口: /framestep?src=<動画URL>&label=<表示名>
//   src は同一オリジンの URL を渡す（/api/jobs/{id}/video や /static/... など）。
//   このページ自体はジョブに紐付かない、独立した確認ビュー。

import { render } from "preact";
import { useEffect, useMemo, useRef, useState } from "preact/hooks";

import { dispatchKey, FRAMESTEP_KEYS, helpRows } from "../shared/keymap.js";
import type { KeyLike } from "../shared/keymap.js";
import { nextShuttleSpeed } from "../shared/shuttle.js";
import { FrameStepPlayer, webCodecsSupported } from "./player.js";
import type { FrameStepPlayerInfo } from "./player.js";

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; info: FrameStepPlayerInfo }
  | { kind: "error"; message: string };

// Shift+← / Shift+→（大きく飛ぶ）のジャンプ幅。固定値（issue #79。timeline と揃えた）
const JUMP_STEP = 10;
const SHUTTLE_TICK_MS = 120;

function App() {
  const params = new URLSearchParams(location.search);
  const src = params.get("src") ?? "";
  const label = params.get("label") ?? src;

  const supported = useMemo(webCodecsSupported, []);
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [cur, setCur] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [frameInput, setFrameInput] = useState("0");
  const [busy, setBusy] = useState(false);
  // 「押したのに何も起きない」を避けるための案内（issue #79）。
  // 再生できない状態（索引作成中・エラー）で Space / J / K / L を押したときに出す
  const [notice, setNotice] = useState("");
  const [helpOpen, setHelpOpen] = useState(false);

  const playerRef = useRef<FrameStepPlayer | null>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const curRef = useRef(0);
  const playingRef = useRef(false);
  const playTimer = useRef<number | null>(null);
  // シャトル（J/K/L）。Space の等速再生とは別の仕組みにしてある。
  // 逆再生は setInterval で1コマずつ戻すしかなく、Space 側の
  // 「fps ぴったりの間隔で+1」という設計とは要件が違うため
  const shuttleSpeed = useRef(0);
  const shuttleTimer = useRef<number | null>(null);

  useEffect(() => {
    curRef.current = cur;
    setFrameInput(String(cur));
  }, [cur]);

  useEffect(() => {
    playingRef.current = playing;
  }, [playing]);

  // WebCodecs 非対応なら索引読みを試みない。フォールバックの <video> だけ出す。
  useEffect(() => {
    if (!supported || !src) return;
    let cancelled = false;
    const player = new FrameStepPlayer(src);
    playerRef.current = player;
    player
      .load()
      .then((info) => {
        if (cancelled) return;
        setState({ kind: "ready", info });
        draw(0);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setState({ kind: "error", message: String(err instanceof Error ? err.message : err) });
      });
    return () => {
      cancelled = true;
      player.dispose();
    };
    // eslint-disable-next-line
  }, [src, supported]);

  async function draw(n: number) {
    const player = playerRef.current;
    const canvas = canvasRef.current;
    if (!player || !canvas) return;
    setBusy(true);
    try {
      const sample = await player.getFrame(n);
      canvas.width = sample.displayWidth;
      canvas.height = sample.displayHeight;
      const ctx = canvas.getContext("2d");
      if (ctx) sample.draw(ctx, 0, 0, sample.displayWidth, sample.displayHeight);
      sample.close();
      setCur(n);
    } catch (err) {
      setState({ kind: "error", message: String(err instanceof Error ? err.message : err) });
    } finally {
      setBusy(false);
    }
  }

  function stopPlay() {
    if (playTimer.current !== null) {
      window.clearInterval(playTimer.current);
      playTimer.current = null;
    }
    setPlaying(false);
  }

  function stopShuttle() {
    shuttleSpeed.current = 0;
    if (shuttleTimer.current !== null) {
      window.clearInterval(shuttleTimer.current);
      shuttleTimer.current = null;
    }
  }

  function togglePlay() {
    if (state.kind !== "ready") return;
    stopShuttle();
    if (playingRef.current) {
      stopPlay();
      return;
    }
    setPlaying(true);
    const intervalMs = state.info.fps > 0 ? 1000 / state.info.fps : 1000 / 30;
    playTimer.current = window.setInterval(() => {
      const next = curRef.current + 1;
      if (next >= state.info.frameCount) {
        stopPlay();
        return;
      }
      void draw(next);
    }, intervalMs);
  }

  /** J/K/L のシャトル。dir=0（K）は常に停止 */
  function shuttle(dir: -1 | 0 | 1) {
    if (state.kind !== "ready") {
      setNotice(
        state.kind === "loading"
          ? "索引を作成中です。読み込みが終わってから動かせます"
          : "この動画は読み込みに失敗しています",
      );
      return;
    }
    setNotice("");
    stopPlay();
    const total = state.info.frameCount;
    const next = nextShuttleSpeed(shuttleSpeed.current, dir);
    shuttleSpeed.current = next;
    if (next === 0) {
      stopShuttle();
      return;
    }
    if (shuttleTimer.current !== null) return; // 既に回っている。速度だけ変わる
    shuttleTimer.current = window.setInterval(() => {
      const before = curRef.current;
      const n = Math.max(0, Math.min(total - 1, before + shuttleSpeed.current));
      if (n === before) {
        stopShuttle();
        return;
      }
      void draw(n);
    }, SHUTTLE_TICK_MS);
  }

  useEffect(() => () => { stopPlay(); stopShuttle(); }, []);

  /** 「できない」ことが分かるようにしてから、条件を満たせば n へ動かす */
  function stepTo(n: number) {
    if (state.kind !== "ready") {
      setNotice(
        state.kind === "loading"
          ? "索引を作成中です。読み込みが終わってから動かせます"
          : "この動画は読み込みに失敗しています",
      );
      return;
    }
    setNotice("");
    stopPlay();
    stopShuttle();
    void draw(Math.max(0, Math.min(state.info.frameCount - 1, n)));
  }

  function onKey(e: KeyboardEvent) {
    const t = e.target as HTMLElement | null;
    const like: KeyLike = {
      key: e.key,
      shiftKey: e.shiftKey,
      targetTag: t?.tagName ?? null,
      targetEditable: !!t?.isContentEditable,
    };
    const handled = dispatchKey(FRAMESTEP_KEYS, like, {
      stepBack: () => stepTo(curRef.current - 1),
      stepForward: () => stepTo(curRef.current + 1),
      jumpBack: () => stepTo(curRef.current - JUMP_STEP),
      jumpForward: () => stepTo(curRef.current + JUMP_STEP),
      playToggle: () => {
        if (shuttleSpeed.current !== 0) { stopShuttle(); return; }
        if (state.kind !== "ready") {
          setNotice(
            state.kind === "loading"
              ? "索引を作成中です。読み込みが終わってから再生できます"
              : "この動画は読み込みに失敗しています",
          );
          return;
        }
        setNotice("");
        togglePlay();
      },
      goHome: () => stepTo(0),
      goEnd: () => stepTo(state.kind === "ready" ? state.info.frameCount - 1 : 0),
      shuttleReverse: () => shuttle(-1),
      shuttleStop: () => shuttle(0),
      shuttleForward: () => shuttle(1),
      help: () => setHelpOpen((v) => !v),
    });
    if (handled) e.preventDefault();
  }

  useEffect(() => {
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line
  }, [state]);

  if (!src) {
    return (
      <main>
        <div class="card">
          <h2>フレーム厳密確認ビュー</h2>
          <p>URL に <span class="mono">?src=&lt;動画URL&gt;</span> を指定してください。</p>
        </div>
      </main>
    );
  }

  if (!supported) {
    return (
      <main>
        <div class="card">
          <h2 class="warn">WebCodecs 非対応</h2>
          <p>このブラウザは WebCodecs（<span class="mono">VideoDecoder</span>）に対応していません。
             フレーム厳密なコマ送りはできません。以下は通常の &lt;video&gt; 再生です
             （<b>フレーム番号は目安であり厳密ではありません</b>）。</p>
        </div>
        {/* biome-ignore: フォールバック専用の <video> */}
        <video src={src} controls style={{ maxWidth: "100%" }} />
      </main>
    );
  }

  return (
    <main>
      <header>
        <div class="titles">
          <span id="video-name">{label}</span>
          {state.kind === "ready" && (
            <span class="sep">|</span>
          )}
          {state.kind === "ready" && (
            <span id="video-meta">
              {`${state.info.width}x${state.info.height}  ${state.info.fps.toFixed(3)} fps  ${state.info.frameCount} フレーム`}
            </span>
          )}
        </div>
      </header>

      {state.kind === "loading" && <p class="dim">索引を作成中（全フレームの提示時刻を1回走査しています）...</p>}
      {state.kind === "error" && <p class="warn">{state.message}</p>}
      {notice && <p class="warn">{notice}</p>}

      {/* 割り当て表（shared/keymap.ts の FRAMESTEP_KEYS）から自動で作る。
          ? キーで開閉する（issue #79） */}
      {helpOpen && (
        <div class="card" style={{ margin: "8px 0" }}>
          <h2>キー</h2>
          <ul>
            {helpRows(FRAMESTEP_KEYS).map((r) => (
              <li key={r.label}><b>{r.label}</b> {r.desc}</li>
            ))}
          </ul>
        </div>
      )}

      <section class="stage">
        <canvas ref={canvasRef} style={{ maxWidth: "100%", background: "#000" }} />

        {state.kind === "ready" && (
          <div class="controls">
            <button onClick={togglePlay}>{playing ? "停止" : "再生"} (Space)</button>
            <button disabled={busy} onClick={() => stepTo(cur - 1)}>
              &lt; コマ戻し (←/,)
            </button>
            <button disabled={busy} onClick={() => stepTo(cur + 1)}>
              コマ送り (→/.) &gt;
            </button>
            <span class="field">
              フレーム
              <input
                type="number"
                min="0"
                max={state.info.frameCount - 1}
                value={frameInput}
                onInput={(e) => setFrameInput(e.currentTarget.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    stopPlay();
                    stopShuttle();
                    const n = Number(frameInput);
                    if (Number.isFinite(n)) void draw(Math.max(0, Math.min(state.info.frameCount - 1, Math.trunc(n))));
                  }
                }}
              />
              <span>/ {state.info.frameCount - 1}</span>
              <button
                disabled={busy}
                onClick={() => {
                  stopPlay();
                  stopShuttle();
                  const n = Number(frameInput);
                  if (Number.isFinite(n)) void draw(Math.max(0, Math.min(state.info.frameCount - 1, Math.trunc(n))));
                }}
              >
                移動
              </button>
            </span>
            <span class="field mono">t = {state.kind === "ready" ? playerRef.current?.timestampOf(cur).toFixed(4) : ""} s</span>
            <button class="dim" onClick={() => setHelpOpen((v) => !v)}>キー一覧 (?)</button>
          </div>
        )}
      </section>
    </main>
  );
}

document.body.textContent = "";
render(<App />, document.body);
