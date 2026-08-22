"use strict";

// ジョブ1件の画面。設定して起動し、進捗を見て、完成品を受け取る。
//
// 進捗は SSE で受ける。3〜4時間かかる処理なので、途中で切れることを
// 前提にしてある。EventSource は自前で繋ぎ直すが、その間に来た進捗を
// 取りこぼしても、繋ぎ直した時点でサーバが現状を1件目として送るので
// 追いつける。EventSource が使えない場合のために取り直し口も残してある。

const JOB = location.pathname.split("/").pop();
const FIELDS = ["infer_size", "conf", "classes", "mode", "block", "crf", "limit_frames", "provider"];
const FLAGS = ["tta", "estimate_gaps", "detect_only"];

let es = null;
let poller = null;

$("back").href = link("/");
$("dl").href = link(`/api/jobs/${JOB}/download`);
$("go-review").href = link(`/review/${JOB}`);
$("go-draw").href = link(`/draw/${JOB}`);

function settings() {
  const s = {};
  for (const f of FIELDS) {
    const v = $(f).value;
    if (v !== "" && v !== null) s[f] = v;
  }
  // 0 は「指定しない」の意味にする。--block 0 は CLI 側の自動と同義だが、
  // --limit-frames 0 は「0フレーム処理」になってしまう
  if (+s.block === 0) delete s.block;
  if (+s.limit_frames === 0) delete s.limit_frames;
  for (const f of FLAGS) s[f] = $(f).checked;
  return s;
}

function applySettings(s) {
  for (const f of FIELDS) if (s[f] !== undefined && s[f] !== null) $(f).value = s[f];
  for (const f of FLAGS) $(f).checked = !!s[f];
}

function setProgress(key, p) {
  const bar = $(key === "pass1" ? "p1" : "p2");
  const txt = $(key === "pass1" ? "p1text" : "p2text");
  if (!p) { bar.style.width = "0"; txt.textContent = "-"; return; }
  bar.style.width = (p.percent != null ? p.percent : 0) + "%";
  txt.textContent =
    `${p.n}${p.total ? " / " + p.total : ""}` +
    (p.percent != null ? `  (${p.percent}%)` : "") +
    `  ${p.fps.toFixed(1)} fps` +
    (p.eta ? `  残り ${p.eta}` : "");
}

function renderStatus(status, error) {
  $("status").innerHTML = "";
  $("status").appendChild(statusBadge(status));
  const running = status === "running" || status === "queued";
  $("btn-start").disabled = running;
  $("btn-rerender").disabled = running;
  $("btn-cancel").disabled = !running;
  $("hint").textContent = error || "";
}

function appendLog(lines) {
  if (!lines || !lines.length) return;
  const el = $("log");
  const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
  for (const l of lines) el.textContent += (l.text !== undefined ? l.text : l) + "\n";
  if (stick) el.scrollTop = el.scrollHeight;
}

async function loadDetail() {
  const d = await api(`/api/jobs/${JOB}`);
  $("title").textContent = d.name;
  const bits = [d.id, fmtBytes(d.size_bytes)];
  if (d.width) bits.push(`${d.width}x${d.height}`);
  if (d.fps) bits.push(`${d.fps} fps`);
  if (d.n_frames) bits.push(`${d.n_frames} フレーム`);
  if (d.duration_sec) bits.push(fmtSec(d.duration_sec));
  bits.push("作成 " + fmtTime(d.created_at));
  $("info").textContent = bits.join("  /  ");
  if (Object.keys(d.settings || {}).length) applySettings(d.settings);
  renderStatus(d.status, d.error);
  setProgress("pass1", (d.progress || {}).pass1);
  setProgress("pass2", (d.progress || {}).pass2);
  $("dl").classList.toggle("hidden", !d.has_output);
  $("go-review").classList.toggle("hidden", !d.has_detections);
  if (d.has_output) {
    $("preview").src = link(`/api/jobs/${JOB}/video`);
    $("preview").classList.remove("hidden");
  }
  return d;
}

async function loadLog() {
  try {
    const d = await api(`/api/jobs/${JOB}/log?tail=120`);
    $("log").textContent = d.lines.join("\n") + (d.lines.length ? "\n" : "");
    $("log").scrollTop = $("log").scrollHeight;
  } catch (e) { /* まだ1度も走らせていないジョブではログが無い */ }
}

// --------------------------------------------------------------------
// 進捗の受信
// --------------------------------------------------------------------

function connect() {
  if (es) { es.close(); es = null; }
  if (typeof EventSource === "undefined") { startPolling(); return; }
  es = new EventSource(url(`/api/jobs/${JOB}/events`));
  es.addEventListener("progress", (ev) => {
    $("conn").textContent = "";
    const d = JSON.parse(ev.data);
    setProgress("pass1", (d.progress || {}).pass1);
    setProgress("pass2", (d.progress || {}).pass2);
    renderStatus(d.status, d.error);
    appendLog(d.log);
  });
  es.addEventListener("end", (ev) => {
    const d = JSON.parse(ev.data);
    renderStatus(d.status, d.error);
    es.close();
    es = null;
    loadDetail();
  });
  es.onerror = () => {
    // EventSource は自分で繋ぎ直す。切れているあいだも状態が分かるように
    // しておかないと、終わったのか落ちたのか区別できない
    $("conn").textContent = "接続が切れました。繋ぎ直しています";
  };
}

function startPolling() {
  clearInterval(poller);
  poller = setInterval(async () => {
    try {
      const d = await api(`/api/jobs/${JOB}/progress`);
      setProgress("pass1", (d.progress || {}).pass1);
      setProgress("pass2", (d.progress || {}).pass2);
      renderStatus(d.status, d.error);
    } catch (e) { /* 一時的な失敗で止めない */ }
  }, 2000);
}

// --------------------------------------------------------------------
// 操作
// --------------------------------------------------------------------

async function start(reuse) {
  $("hint").textContent = "起動しています";
  try {
    await api(`/api/jobs/${JOB}/start`, { json: { settings: settings(), reuse } });
    $("log").textContent = "";
    renderStatus("running", "");
    connect();
  } catch (e) {
    $("hint").textContent = "起動できません: " + e.message;
  }
}

$("btn-start").onclick = () => start(false);
$("btn-rerender").onclick = () => start(true);
$("btn-cancel").onclick = async () => {
  try { await api(`/api/jobs/${JOB}/cancel`, { method: "POST" }); }
  catch (e) { $("hint").textContent = e.message; }
};

$("btn-dataset").onclick = async () => {
  $("dsmsg").textContent = "書き出しています";
  try {
    const d = await api(`/api/jobs/${JOB}/dataset`, { method: "POST" });
    $("dsmsg").textContent = `${d.frames} フレームを ${d.dir} に書き出しました`;
  } catch (e) {
    $("dsmsg").textContent = "書き出せません: " + e.message;
  }
};

$("btn-delete").onclick = async () => {
  if (!confirm("素材ごと消えます。よろしいですか")) return;
  try {
    await api(`/api/jobs/${JOB}`, { method: "DELETE" });
    location.href = link("/");
  } catch (e) { $("hint").textContent = e.message; }
};

loadDetail().then(loadLog).then(connect).catch((e) => {
  $("hint").textContent = "読み込めません: " + e.message;
});
