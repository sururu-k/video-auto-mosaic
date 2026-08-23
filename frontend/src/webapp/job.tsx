// ジョブ1件の画面。設定して起動し、進捗を見て、完成品を受け取る。
//
// 進捗は SSE で受ける。3〜4時間かかる処理なので、途中で切れることを
// 前提にしてある。EventSource は自前で繋ぎ直すが、その間に来た進捗を
// 取りこぼしても、繋ぎ直した時点でサーバが現状を1件目として送るので
// 追いつける。EventSource が使えない場合のために取り直し口も残してある。

import { render } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";

import type {
  JobProgress,
  JobSettings,
  JobState,
  JobStatus,
  LogPayload,
  PassProgress,
  ProgressSnapshot,
  SettingField,
  SettingFlag,
} from "../shared/api.js";
import { SETTING_FIELDS, SETTING_FLAGS } from "../shared/api.js";
import { api, errText, fmtBytes, fmtSec, fmtTime, link, url } from "../shared/webapp-net.js";
import { StatusBadge } from "./StatusBadge.js";

const JOB = location.pathname.split("/").pop() ?? "";

/** 入力欄の初期値。job.html の selected / value と揃えてある */
const FIELD_DEFAULTS: Record<SettingField, string> = {
  infer_size: "960",
  conf: "0.06",
  classes: "default",
  mode: "pixelize",
  block: "0",
  crf: "16",
  limit_frames: "0",
  provider: "auto",
};

function passText(p: PassProgress): string {
  return (
    `${p.n}${p.total ? " / " + p.total : ""}` +
    (p.percent != null ? `  (${p.percent}%)` : "") +
    `  ${p.fps.toFixed(1)} fps` +
    (p.eta ? `  残り ${p.eta}` : "")
  );
}

function Pass({ label, id, p }: { label: string; id: string; p: PassProgress | undefined }) {
  return (
    <div style={id === "p1" ? { marginBottom: "8px" } : undefined}>
      <div class="dim" id={id + "label"}>{label}</div>
      <div class="bar"><i id={id} style={{ width: p ? (p.percent ?? 0) + "%" : "0" }} /></div>
      <div class="dim mono" id={id + "text"}>{p ? passText(p) : "-"}</div>
    </div>
  );
}

function App() {
  const [detail, setDetail] = useState<JobState | null>(null);
  const [status, setStatus] = useState<JobStatus>("new");
  const [progress, setProgress] = useState<JobProgress>({});
  const [fields, setFields] = useState<Record<SettingField, string>>({ ...FIELD_DEFAULTS });
  const [flags, setFlags] = useState<Record<SettingFlag, boolean>>({
    tta: false,
    estimate_gaps: false,
    detect_only: false,
  });
  const [hint, setHint] = useState("");
  const [conn, setConn] = useState("");
  const [dsmsg, setDsmsg] = useState("");
  const [log, setLog] = useState("");

  const logRef = useRef<HTMLDivElement>(null);
  const es = useRef<EventSource | null>(null);
  const poller = useRef<ReturnType<typeof setInterval> | null>(null);
  // 追記の直前に「いちばん下を見ていたか」を控える。見ていたときだけ追従する
  const stick = useRef(true);

  function settings(): JobSettings {
    const s: JobSettings = {};
    for (const f of SETTING_FIELDS) {
      const v = fields[f];
      if (v !== "") s[f] = v;
    }
    // 0 は「指定しない」の意味にする。--block 0 は CLI 側の自動と同義だが、
    // --limit-frames 0 は「0フレーム処理」になってしまう
    if (Number(s.block) === 0) delete s.block;
    if (Number(s.limit_frames) === 0) delete s.limit_frames;
    for (const f of SETTING_FLAGS) s[f] = flags[f];
    return s;
  }

  function applySettings(s: JobSettings) {
    setFields((prev) => {
      const next = { ...prev };
      for (const f of SETTING_FIELDS) {
        const v = s[f];
        if (v !== undefined && v !== null) next[f] = String(v);
      }
      return next;
    });
    setFlags({
      tta: !!s.tta,
      estimate_gaps: !!s.estimate_gaps,
      detect_only: !!s.detect_only,
    });
  }

  function appendLog(lines: readonly { text: string }[]) {
    if (!lines.length) return;
    const el = logRef.current;
    stick.current = el ? el.scrollTop + el.clientHeight >= el.scrollHeight - 20 : true;
    setLog((prev) => prev + lines.map((l) => l.text).join("\n") + "\n");
  }

  useEffect(() => {
    const el = logRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [log]);

  async function loadDetail(): Promise<JobState> {
    const d = await api<JobState>(`/api/jobs/${JOB}`);
    setDetail(d);
    if (Object.keys(d.settings).length) applySettings(d.settings);
    setStatus(d.status);
    setHint(d.error ?? "");
    setProgress(d.progress);
    return d;
  }

  async function loadLog() {
    try {
      const d = await api<LogPayload>(`/api/jobs/${JOB}/log?tail=120`);
      stick.current = true;
      setLog(d.lines.join("\n") + (d.lines.length ? "\n" : ""));
    } catch {
      /* まだ1度も走らせていないジョブではログが無い */
    }
  }

  // ----------------------------------------------------------------
  // 進捗の受信
  // ----------------------------------------------------------------
  function startPolling() {
    if (poller.current) clearInterval(poller.current);
    poller.current = setInterval(() => {
      void (async () => {
        try {
          const d = await api<ProgressSnapshot>(`/api/jobs/${JOB}/progress`);
          setProgress(d.progress);
          setStatus(d.status);
          setHint(d.error ?? "");
        } catch {
          /* 一時的な失敗で止めない */
        }
      })();
    }, 2000);
  }

  function connect() {
    if (es.current) {
      es.current.close();
      es.current = null;
    }
    if (typeof EventSource === "undefined") {
      startPolling();
      return;
    }
    const src = new EventSource(url(`/api/jobs/${JOB}/events`));
    es.current = src;
    src.addEventListener("progress", (ev) => {
      setConn("");
      const d = JSON.parse((ev as MessageEvent<string>).data) as ProgressSnapshot;
      setProgress(d.progress);
      setStatus(d.status);
      setHint(d.error ?? "");
      appendLog(d.log);
    });
    src.addEventListener("end", (ev) => {
      const d = JSON.parse((ev as MessageEvent<string>).data) as ProgressSnapshot;
      setStatus(d.status);
      setHint(d.error ?? "");
      src.close();
      es.current = null;
      void loadDetail();
    });
    src.onerror = () => {
      // EventSource は自分で繋ぎ直す。切れているあいだも状態が分かるように
      // しておかないと、終わったのか落ちたのか区別できない
      setConn("接続が切れました。繋ぎ直しています");
    };
  }

  useEffect(() => {
    void loadDetail()
      .then(loadLog)
      .then(connect)
      .catch((e: unknown) => setHint("読み込めません: " + errText(e)));
    return () => {
      es.current?.close();
      if (poller.current) clearInterval(poller.current);
    };
  }, []);

  // ----------------------------------------------------------------
  // 操作
  // ----------------------------------------------------------------
  async function start(reuse: boolean) {
    setHint("起動しています");
    try {
      await api(`/api/jobs/${JOB}/start`, { json: { settings: settings(), reuse } });
      setLog("");
      setStatus("running");
      setHint("");
      connect();
    } catch (e) {
      setHint("起動できません: " + errText(e));
    }
  }

  const running = status === "running" || status === "queued";
  const d = detail;
  const infoBits: string[] = [];
  if (d) {
    infoBits.push(d.id, fmtBytes(d.size_bytes));
    if (d.width) infoBits.push(`${d.width}x${d.height}`);
    if (d.fps) infoBits.push(`${d.fps} fps`);
    if (d.n_frames) infoBits.push(`${d.n_frames} フレーム`);
    if (d.duration_sec) infoBits.push(fmtSec(d.duration_sec));
    infoBits.push("作成 " + fmtTime(d.created_at));
  }

  const field = (f: SettingField) => ({
    id: f,
    value: fields[f],
    onInput: (e: Event) => {
      const v = (e.currentTarget as HTMLInputElement | HTMLSelectElement).value;
      setFields((prev) => ({ ...prev, [f]: v }));
    },
    onChange: (e: Event) => {
      const v = (e.currentTarget as HTMLInputElement | HTMLSelectElement).value;
      setFields((prev) => ({ ...prev, [f]: v }));
    },
  });

  const flag = (f: SettingFlag) => ({
    id: f,
    type: "checkbox" as const,
    checked: flags[f],
    onChange: (e: Event) =>
      setFlags((prev) => ({ ...prev, [f]: (e.currentTarget as HTMLInputElement).checked })),
  });

  return (
    <>
      <header class="top">
        <a id="back" class="btn" href={link("/")}>一覧</a>
        <h1 id="title">{d?.name ?? "ジョブ"}</h1>
        <span class="spacer" />
        <span id="status"><StatusBadge status={status} /></span>
      </header>

      <main>
        <div class="card">
          <h2>素材</h2>
          <p id="info" class="dim mono">{infoBits.join("  /  ")}</p>
        </div>

        <div class="card">
          <h2>設定</h2>
          <div class="grid">
            <label>推論解像度
              <select {...field("infer_size")}>
                <option value="640">640（速い）</option>
                <option value="960">960（既定・費用対効果のピーク）</option>
                <option value="1280">1280（遅い）</option>
              </select>
            </label>
            <label>信頼度しきい値 <input {...field("conf")} type="number" step="0.01" min="0.01" max="0.9" /></label>
            <label>対象クラス
              <select {...field("classes")}>
                <option value="default">default（露出のみ）</option>
                <option value="conservative">conservative（COVERED も含む）</option>
              </select>
            </label>
            <label>モザイク
              <select {...field("mode")}>
                <option value="pixelize">pixelize</option>
                <option value="black">black</option>
              </select>
            </label>
            <label>ブロック px（0で自動） <input {...field("block")} type="number" min="0" max="200" /></label>
            <label>CRF <input {...field("crf")} type="number" min="10" max="30" /></label>
            <label>先頭 N フレームだけ（0で全部） <input {...field("limit_frames")} type="number" min="0" /></label>
            <label>プロバイダ
              <select {...field("provider")}>
                <option value="auto">auto</option>
                <option value="dml">dml</option>
                <option value="cpu">cpu</option>
                <option value="cuda">cuda</option>
              </select>
            </label>
          </div>
          <div class="row" style={{ marginTop: "8px" }}>
            <label><input {...flag("tta")} /> TTA（推論2倍・検出+16%）</label>
            <label><input {...flag("estimate_gaps")} /> 途切れた区間を推定で埋める</label>
            <label><input {...flag("detect_only")} /> 検出だけ（描画しない）</label>
          </div>
          <div class="row" style={{ marginTop: "10px" }}>
            <button id="btn-start" class="primary" disabled={running}
                    onClick={() => void start(false)}>処理を始める</button>
            <button id="btn-rerender" disabled={running}
                    onClick={() => void start(true)}>検出を再利用して焼き直す</button>
            <button id="btn-cancel" class="danger" disabled={!running}
                    onClick={() => {
                      void (async () => {
                        try {
                          await api(`/api/jobs/${JOB}/cancel`, { method: "POST" });
                        } catch (e) {
                          setHint(errText(e));
                        }
                      })();
                    }}>中断</button>
          </div>
          <p id="hint" class="dim">{hint}</p>
        </div>

        <div class="card">
          <h2>進捗</h2>
          <Pass label="パス1 検出" id="p1" p={progress.pass1} />
          <Pass label="パス2 描画" id="p2" p={progress.pass2} />
          <p id="conn" class="dim">{conn}</p>
        </div>

        <div class="card">
          <h2>完成品</h2>
          <div class="row">
            <a id="dl" class={"btn primary" + (d?.has_output ? "" : " hidden")}
               href={link(`/api/jobs/${JOB}/download`)}>完成品をダウンロード</a>
            <a id="go-review" class={"btn" + (d?.has_detections ? "" : " hidden")}
               href={link(`/review/${JOB}`)}>検査キューを開く</a>
            <a id="go-draw" class="btn" href={link(`/draw/${JOB}`)}>手描きモード</a>
            <button id="btn-dataset"
                    onClick={() => {
                      setDsmsg("書き出しています");
                      void (async () => {
                        try {
                          const r = await api<{ frames: number; dir: string }>(
                            `/api/jobs/${JOB}/dataset`, { method: "POST" });
                          setDsmsg(`${r.frames} フレームを ${r.dir} に書き出しました`);
                        } catch (e) {
                          setDsmsg("書き出せません: " + errText(e));
                        }
                      })();
                    }}>学習データを書き出す</button>
            <button id="btn-delete" class="danger"
                    onClick={() => {
                      if (!confirm("素材ごと消えます。よろしいですか")) return;
                      void (async () => {
                        try {
                          await api(`/api/jobs/${JOB}`, { method: "DELETE" });
                          location.href = link("/");
                        } catch (e) {
                          setHint(errText(e));
                        }
                      })();
                    }}>ジョブを消す</button>
          </div>
          <p id="dsmsg" class="dim">{dsmsg}</p>
          <video id="preview" class={d?.has_output ? "" : "hidden"} controls playsInline
                 src={d?.has_output ? link(`/api/jobs/${JOB}/video`) : undefined}
                 style={{ maxWidth: "100%", marginTop: "10px" }} />
        </div>

        <div class="card">
          <h2>ログ</h2>
          <div id="log" ref={logRef}>{log}</div>
        </div>
      </main>
    </>
  );
}

document.body.textContent = "";
render(<App />, document.body);
