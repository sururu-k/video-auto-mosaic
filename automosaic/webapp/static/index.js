"use strict";

// ジョブ一覧とアップロード。
//
// アップロードは XMLHttpRequest で送る。fetch でも送れるが、
// 数 GB の素材で「いま何 % 送れたか」が出ないと、固まったのか進んで
// いるのか区別できない。upload.onprogress が要る。
// 本文は File をそのまま PUT する。multipart にすると、サーバ側で
// 一度テンポラリに落としてから複製することになる。

let timer = null;

function upload(file) {
  $("up").classList.remove("hidden");
  $("upmsg").textContent = `${file.name}（${fmtBytes(file.size)}）を送っています`;
  const xhr = new XMLHttpRequest();
  xhr.open("PUT", url("/api/upload", { name: file.name }));
  if (TOKEN) xhr.setRequestHeader("X-Review-Token", TOKEN);
  xhr.upload.onprogress = (ev) => {
    if (!ev.lengthComputable) return;
    const pct = (100 * ev.loaded) / ev.total;
    $("upfill").style.width = pct.toFixed(1) + "%";
    $("upmsg").textContent =
      `${file.name}  ${fmtBytes(ev.loaded)} / ${fmtBytes(ev.total)}（${pct.toFixed(1)}%）`;
  };
  xhr.onload = () => {
    if (xhr.status >= 200 && xhr.status < 300) {
      const d = JSON.parse(xhr.responseText);
      $("upmsg").textContent = "受け取りました。設定へ移ります";
      location.href = link("/job/" + d.id);
    } else {
      $("upmsg").textContent = "失敗しました: " + xhr.status + " " + xhr.responseText;
    }
  };
  xhr.onerror = () => { $("upmsg").textContent = "通信に失敗しました"; };
  xhr.send(file);
}

$("btn-pick").onclick = () => $("file").click();
$("file").onchange = () => { if ($("file").files[0]) upload($("file").files[0]); };

const drop = $("drop");
for (const ev of ["dragenter", "dragover"]) {
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); });
}
for (const ev of ["dragleave", "drop"]) {
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); });
}
drop.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files[0];
  if (f) upload(f);
});

// --------------------------------------------------------------------

function jobRow(j) {
  const el = document.createElement("div");
  el.className = "job";

  const left = document.createElement("div");
  const name = document.createElement("div");
  name.className = "name";
  name.textContent = j.name;
  const meta = document.createElement("div");
  meta.className = "meta";
  const bits = [j.id, fmtBytes(j.size_bytes)];
  if (j.width) bits.push(`${j.width}x${j.height}`);
  if (j.n_frames) bits.push(`${j.n_frames} フレーム`);
  if (j.n_corrections) bits.push(`手修正 ${j.n_corrections} 件`);
  if (j.elapsed_sec) bits.push(`処理 ${fmtSec(j.elapsed_sec)}`);
  meta.textContent = bits.join("  /  ");
  left.appendChild(name);
  left.appendChild(meta);

  // 実行中は、その場で進捗が見えないと一覧に戻る意味がない
  const p = j.progress || {};
  const active = p.pass2 || p.pass1;
  if (j.status === "running" && active) {
    const bar = document.createElement("div");
    bar.className = "bar";
    bar.style.width = "220px";
    bar.style.marginTop = "6px";
    const i = document.createElement("i");
    i.style.width = (active.percent || 0) + "%";
    bar.appendChild(i);
    left.appendChild(bar);
    const t = document.createElement("div");
    t.className = "meta";
    t.textContent = `${active.label} ${active.n}/${active.total || "?"}` +
      (active.eta ? `  残り ${active.eta}` : "");
    left.appendChild(t);
  }

  el.appendChild(left);
  el.appendChild(statusBadge(j.status));

  const act = document.createElement("div");
  act.className = "actions";
  const add = (label, href, cls) => {
    const a = document.createElement("a");
    a.className = "btn" + (cls ? " " + cls : "");
    a.textContent = label;
    a.href = href;
    act.appendChild(a);
  };
  add("開く", link("/job/" + j.id));
  if (j.has_detections) add("検査", link("/review/" + j.id));
  add("手描き", link("/draw/" + j.id));
  if (j.has_output) add("DL", link(`/api/jobs/${j.id}/download`), "primary");
  el.appendChild(act);
  return el;
}

async function refresh() {
  try {
    const d = await api("/api/jobs");
    $("libpath").textContent = d.library;
    const list = $("list");
    list.innerHTML = "";
    if (!d.jobs.length) {
      list.innerHTML = '<p class="dim">まだありません。上から動画を追加してください</p>';
    } else {
      for (const j of d.jobs) list.appendChild(jobRow(j));
    }
    // 走っているものがあるときだけ短い間隔で見に行く。何も無いときに
    // 2秒ごとに叩き続けても意味がない
    const wait = d.active.length ? 2000 : 10000;
    clearTimeout(timer);
    timer = setTimeout(refresh, wait);
  } catch (e) {
    $("list").innerHTML = '<p class="dim">読み込めません: ' + e.message + "</p>";
    clearTimeout(timer);
    timer = setTimeout(refresh, 5000);
  }
}

refresh();
