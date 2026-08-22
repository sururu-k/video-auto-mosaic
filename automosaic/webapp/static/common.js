"use strict";

// 画面共通の下回り。ビルドを挟まないので、module ではなく素の script で読む。

const $ = (id) => document.getElementById(id);

// トークンは URL から拾う。サーバが Cookie にも移すので素の URL でも通るが、
// Cookie を消している端末でも動くように毎回付け直す。
const TOKEN = new URLSearchParams(location.search).get("t") || "";

function url(path, params) {
  const p = new URLSearchParams(params || {});
  if (TOKEN) p.set("t", TOKEN);
  const q = p.toString();
  return q ? path + (path.includes("?") ? "&" : "?") + q : path;
}

// 画面から画面へ移るときにトークンを落とさない。落とすと 403 になって
// 「さっきまで見えていたのに開けない」という分かりにくい詰まり方をする
function link(path) {
  return url(path);
}

async function api(path, opts) {
  const o = Object.assign({}, opts || {});
  o.headers = Object.assign({}, o.headers || {});
  if (TOKEN) o.headers["X-Review-Token"] = TOKEN;
  if (o.json !== undefined) {
    o.method = o.method || "POST";
    o.headers["Content-Type"] = "application/json";
    o.body = JSON.stringify(o.json);
    delete o.json;
  }
  const res = await fetch(url(path), o);
  if (!res.ok) {
    let msg = res.status + "";
    try {
      const d = await res.json();
      msg = d.detail || d.error || msg;
    } catch (e) { /* 本文が JSON でないことはある */ }
    throw new Error(msg);
  }
  if (res.status === 204) return null;
  return res.json();
}

function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + " " + u[i];
}

function fmtSec(s) {
  if (s == null) return "-";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}`
           : `${m}:${String(x).padStart(2, "0")}`;
}

function fmtTime(t) {
  if (!t) return "-";
  const d = new Date(t * 1000);
  const p = (v) => String(v).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

const STATUS_LABEL = {
  new: "未処理",
  queued: "起動待ち",
  running: "処理中",
  done: "完了",
  failed: "失敗",
  canceled: "中断",
  interrupted: "中断（サーバ停止）",
};

function statusBadge(status) {
  const b = document.createElement("span");
  b.className = "badge " + status;
  b.textContent = STATUS_LABEL[status] || status;
  return b;
}
