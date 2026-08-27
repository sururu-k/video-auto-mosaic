// ジョブ一覧とアップロード。
//
// アップロードは XMLHttpRequest で送る。fetch でも送れるが、
// 数 GB の素材で「いま何 % 送れたか」が出ないと、固まったのか進んで
// いるのか区別できない。upload.onprogress が要る。
// 本文は File をそのまま PUT する。multipart にすると、サーバ側で
// 一度テンポラリに落としてから複製することになる。

import { render } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";

import type { JobDetail, JobSummary, JobsPayload } from "../shared/api.js";
import {
  TOKEN,
  api,
  errText,
  fmtBytes,
  fmtSec,
  link,
  url,
  withCurrentWebBuild,
} from "../shared/webapp-net.js";
import { StatusBadge } from "./StatusBadge.js";

interface UploadState {
  active: boolean;
  msg: string;
  pct: number;
}

function JobRow({ job }: { job: JobSummary }) {
  const bits: string[] = [job.id, fmtBytes(job.size_bytes)];
  if (job.width) bits.push(`${job.width}x${job.height}`);
  if (job.n_frames) bits.push(`${job.n_frames} フレーム`);
  if (job.n_corrections) bits.push(`手修正 ${job.n_corrections} 件`);
  if (job.elapsed_sec) bits.push(`処理 ${fmtSec(job.elapsed_sec)}`);

  // 実行中は、その場で進捗が見えないと一覧に戻る意味がない
  const active = job.progress.pass2 ?? job.progress.pass1;
  const showBar = job.status === "running" && active;

  return (
    <div class="job">
      <div>
        <div class="name">{job.name}</div>
        <div class="meta">{bits.join("  /  ")}</div>
        {showBar && active ? (
          <>
            <div class="bar" style={{ width: "220px", marginTop: "6px" }}>
              <i style={{ width: (active.percent ?? 0) + "%" }} />
            </div>
            <div class="meta">
              {`${active.label} ${active.n}/${active.total || "?"}` +
                (active.eta ? `  残り ${active.eta}` : "")}
            </div>
          </>
        ) : null}
      </div>
      <StatusBadge status={job.status} />
      <div class="actions">
        <a class="btn" href={link("/job/" + job.id)}>開く</a>
        {job.has_detections ? <a class="btn" href={link("/review/" + job.id)}>検査</a> : null}
        <a class="btn" href={link("/draw/" + job.id)}>手描き</a>
        {job.has_output ? (
          <a class="btn primary" href={link(`/api/jobs/${job.id}/download`)}>DL</a>
        ) : null}
      </div>
    </div>
  );
}

function App() {
  const [payload, setPayload] = useState<JobsPayload | null>(null);
  const [listError, setListError] = useState("");
  const [up, setUp] = useState<UploadState>({ active: false, msg: "", pct: 0 });
  const [over, setOver] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  async function upload(file: File) {
    try {
      await withCurrentWebBuild(() => {
        setUp({ active: true, pct: 0, msg: `${file.name}（${fmtBytes(file.size)}）を送っています` });
        const xhr = new XMLHttpRequest();
        xhr.open("PUT", url("/api/upload", { name: file.name }));
        if (TOKEN) xhr.setRequestHeader("X-Review-Token", TOKEN);
        xhr.upload.onprogress = (ev) => {
          if (!ev.lengthComputable) return;
          const pct = (100 * ev.loaded) / ev.total;
          setUp({
            active: true,
            pct,
            msg: `${file.name}  ${fmtBytes(ev.loaded)} / ${fmtBytes(ev.total)}（${pct.toFixed(1)}%）`,
          });
        };
        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            const d = JSON.parse(xhr.responseText) as JobDetail;
            setUp((s) => ({ ...s, msg: "受け取りました。設定へ移ります" }));
            location.href = link("/job/" + d.id);
          } else {
            setUp((s) => ({ ...s, msg: "失敗しました: " + xhr.status + " " + xhr.responseText }));
          }
        };
        xhr.onerror = () => setUp((s) => ({ ...s, msg: "通信に失敗しました" }));
        xhr.send(file);
      });
    } catch (e) {
      setUp({ active: false, pct: 0, msg: errText(e) });
    }
  }

  useEffect(() => {
    let live = true;
    const refresh = async () => {
      try {
        const d = await api<JobsPayload>("/api/jobs");
        if (!live) return;
        setPayload(d);
        setListError("");
        // 走っているものがあるときだけ短い間隔で見に行く。何も無いときに
        // 2秒ごとに叩き続けても意味がない
        timer.current = setTimeout(() => void refresh(), d.active.length ? 2000 : 10000);
      } catch (e) {
        if (!live) return;
        setListError(errText(e));
        timer.current = setTimeout(() => void refresh(), 5000);
      }
    };
    void refresh();
    return () => {
      live = false;
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);

  const stop = (e: Event) => e.preventDefault();

  return (
    <>
      <header class="top">
        <h1>automosaic</h1>
        <span class="spacer" />
        <span id="libpath" class="dim mono">{payload?.library ?? ""}</span>
      </header>

      <main>
        <div class="card">
          <h2>素材を追加する</h2>
          <div id="drop" class={"drop" + (over ? " over" : "")}
               onDragEnter={(e) => { stop(e); setOver(true); }}
               onDragOver={(e) => { stop(e); setOver(true); }}
               onDragLeave={(e) => { stop(e); setOver(false); }}
               onDrop={(e) => {
                 stop(e);
                 setOver(false);
                 const f = e.dataTransfer?.files[0];
                 if (f) upload(f);
               }}>
            ここに動画をドロップ、または
            <button id="btn-pick" onClick={() => fileRef.current?.click()}>ファイルを選ぶ</button>
            <input id="file" ref={fileRef} type="file" accept="video/*" class="hidden"
                   onChange={(e) => {
                     const f = e.currentTarget.files?.[0];
                     if (f) upload(f);
                   }} />
          </div>
          <div id="up" class={up.active ? "" : "hidden"} style={{ marginTop: "10px" }}>
            <div class="bar"><i id="upfill" style={{ width: up.pct.toFixed(1) + "%" }} /></div>
            <p id="upmsg" class="dim">{up.msg}</p>
          </div>
          {/* JS を切っていても投稿できる経路は noscript 側にある。
              ここが動いている時点で JS は生きているので出さない */}
        </div>

        <div class="card">
          <h2>ジョブ</h2>
          <div id="list">
            {listError ? (
              <p class="dim">{"読み込めません: " + listError}</p>
            ) : !payload ? (
              <p class="dim">読み込み中</p>
            ) : !payload.jobs.length ? (
              <p class="dim">まだありません。上から動画を追加してください</p>
            ) : (
              payload.jobs.map((j) => <JobRow key={j.id} job={j} />)
            )}
          </div>
        </div>
      </main>
    </>
  );
}

document.body.textContent = "";
render(<App />, document.body);
