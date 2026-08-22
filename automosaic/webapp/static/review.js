"use strict";

// 検査キュー画面。既存レビュー UI（automosaic/web/app.js）と同じ操作系を
// ジョブ単位の API に載せ替えたもの。
//
// 「どのフレームを見るか」はサーバが決める。画面はそれを1枚ずつ出して、
// 押された判定を返すだけにしてある。判定の記録もサーバ持ちなので、
// 端末を閉じても、別の端末で開き直しても続きから再開できる。
//
// 操作は指だけで完結させる。キーボードの割り当ては残してあるが、
// それが無いと出来ないことは1つも作らない。

const JOB = location.pathname.split("/").pop();
const API = "/api/jobs/" + JOB;

const el = {
  pos: $("pos"), reason: $("reason"), save: $("save"), fill: $("progress-fill"),
  shot: $("shot"), ov: $("ov"), banner: $("banner"),
  judge: $("judge"), mark: $("mark"), size: $("size"), sizeLabel: $("size-label"),
  spanRow: $("span-row"), confirm: $("btn-confirm"),
  sheet: $("sheet"), sheetInfo: $("sheet-info"),
  optClass: $("opt-class"), optStep: $("opt-step"),
};

const S = {
  state: null, items: [], idx: 0, version: 0,
  sizePct: 100, span: 0,
  pending: null,  // 置いた矩形 [x, y, w, h]（動画座標）
  tap: null,      // 正規化タップ座標。サーバへはこちらを送る
  marking: false, busy: false, imgWidth: 720,
};

const SRC_COLOR = { d: "#4a9eff", i: "#ffd479", m: "#ffd479", b: "#ffb347", x: "#ff5a5a" };

$("back").href = link("/job/" + JOB);

// --------------------------------------------------------------------
// 起動
// --------------------------------------------------------------------

async function boot() {
  // light=1 で全フレームぶんの矩形と被覆文字列を落としてもらう。
  // この画面が使うのは解像度・クラス・既定サイズだけで、あれは1時間の
  // 動画だと 10MB を超える。端末の回線では起動しなくなる
  S.state = await api(API + "/state?light=1");
  const q = await api(API + "/queue");
  S.version = q.version;
  S.items = q.items;

  el.ov.width = S.state.width;
  el.ov.height = S.state.height;

  // 送ってもらう画像の幅。原寸を投げさせると1枚に数 MB かかり、
  // 判定の手応えが消える。画面に映る以上の解像度は要らない
  const dpr = window.devicePixelRatio || 1;
  S.imgWidth = Math.min(
    S.state.width,
    Math.max(480, Math.round(Math.min(window.screen.width, 1280) * dpr))
  );

  for (const name of S.state.classes) {
    const o = document.createElement("option");
    o.value = name;
    o.textContent = name;
    if (name === S.state.default_class) o.selected = true;
    el.optClass.appendChild(o);
  }
  el.optStep.value = q.step;
  $("opt-all").checked = q.all_frames;

  buildSpanButtons(q.step);
  updateSize();
  updateProgress(q.progress);
  S.idx = firstUnjudged(0);
  show();
}

// --------------------------------------------------------------------
// 表示
// --------------------------------------------------------------------

function cur() { return S.items[S.idx] || null; }

function frameUrl(frame, width) {
  const p = { n: frame, fmt: "jpg", w: width, v: S.version };
  if ($("opt-raw").checked) p.raw = 1;
  return url(API + "/frame", p);
}

function show() {
  const it = cur();
  if (!it) {
    el.shot.removeAttribute("src");
    el.reason.textContent = "";
    el.pos.textContent = S.items.length ? "全部見終わりました" : "対象がありません";
    banner(S.items.length ? "すべて判定済みです。設定から間隔や対象を変えられます" : "");
    clearOverlay();
    return;
  }
  el.shot.src = frameUrl(it.frame, S.imgWidth);
  el.reason.textContent = `${it.label}  frame ${it.frame}`;
  el.reason.className = "tag p" + it.priority;
  el.pos.textContent = `${S.idx + 1} / ${S.items.length}`;
  drawBoxes();
  prefetch();
  banner(it.verdict
    ? { ok: "判定済み: 問題なし", fixed: "判定済み: 塞いだ", unsure: "判定済み: 保留" }[it.verdict]
    : "");
}

function prefetch() {
  // 次の2枚を先読みする。/frame は世代番号付きの URL だけキャッシュ可なので、
  // 修正が入れば URL ごと変わり、古い絵が残ることはない
  for (let k = 1; k <= 2; k++) {
    const it = S.items[S.idx + k];
    if (it) new Image().src = frameUrl(it.frame, S.imgWidth);
  }
}

function clearOverlay() {
  el.ov.getContext("2d").clearRect(0, 0, el.ov.width, el.ov.height);
}

function drawBoxes() {
  clearOverlay();
  if (!$("opt-boxes").checked && !S.pending) return;
  const ctx = el.ov.getContext("2d");
  // 端末では画面に対して縮んで表示されるので、線幅は解像度に比例させる
  const lw = Math.max(2, Math.round(S.state.width / 400));
  if ($("opt-boxes").checked) {
    ctx.lineWidth = lw;
    for (const r of (cur() && cur().boxes) || []) {
      ctx.strokeStyle = SRC_COLOR[r[4]] || "#ffffff";
      ctx.strokeRect(r[0], r[1], r[2], r[3]);
    }
  }
  if (S.pending) {
    ctx.lineWidth = lw * 1.5;
    ctx.setLineDash([lw * 4, lw * 3]);
    ctx.strokeStyle = "#ffffff";
    ctx.strokeRect(S.pending[0], S.pending[1], S.pending[2], S.pending[3]);
    ctx.setLineDash([]);
  }
}

function banner(text) {
  el.banner.textContent = text || "";
  el.banner.classList.toggle("hidden", !text);
}

function setSave(kind, text) {
  el.save.className = "save " + kind;
  el.save.textContent = text;
}

function updateProgress(p) {
  if (!p) return;
  el.fill.style.width = (p.total ? (100 * p.done) / p.total : 0).toFixed(1) + "%";
  el.sheetInfo.textContent =
    `${p.total} 枚中 ${p.done} 枚判定済み（残り ${p.remaining}）` +
    `  問題なし ${p.counts.ok} / 塞いだ ${p.counts.fixed} / 保留 ${p.counts.unsure}`;
  $("btn-undo").disabled = !p.can_undo;
  $("btn-undo2").disabled = !p.can_undo;
}

// --------------------------------------------------------------------
// 移動
// --------------------------------------------------------------------

function firstUnjudged(from) {
  for (let i = from; i < S.items.length; i++) if (!S.items[i].verdict) return i;
  for (let i = 0; i < from; i++) if (!S.items[i].verdict) return i;
  return S.items.length ? S.items.length : 0;
}

function goto(i) {
  cancelMark();
  S.idx = Math.max(0, Math.min(S.items.length, i));
  show();
}

function advance() { goto(firstUnjudged(S.idx + 1)); }

// --------------------------------------------------------------------
// 判定
// --------------------------------------------------------------------

async function judge(verdict, extra) {
  const it = cur();
  if (!it || S.busy) return;
  S.busy = true;
  setSave("busy", "保存中");
  try {
    const d = await api(API + "/mark", {
      json: Object.assign({ frame: it.frame, verdict }, extra || {}),
    });
    it.verdict = verdict;
    // 修正で領域が変わったら、その枚の矩形と画像の世代番号を入れ替える
    it.boxes = d.regions;
    S.version = d.version;
    updateProgress(d.progress);
    setSave("ok", `保存済 ${d.n_corrections}`);
    advance();
  } catch (e) {
    setSave("err", "保存できません");
    banner("保存できませんでした: " + e.message);
  } finally {
    S.busy = false;
  }
}

async function undo() {
  if (S.busy) return;
  S.busy = true;
  setSave("busy", "戻しています");
  try {
    const d = await api(API + "/undo", { method: "POST" });
    if (!d.ok) { setSave("ok", "保存済"); banner(d.error || "戻せません"); return; }
    S.version = d.version;
    const i = S.items.findIndex((x) => x.frame === d.frame);
    if (i >= 0) {
      S.items[i].verdict = null;
      S.items[i].boxes = d.regions;
      S.idx = i;
    }
    updateProgress(d.progress);
    setSave("ok", `保存済 ${d.n_corrections}`);
    cancelMark();
    show();
    banner(`frame ${d.frame} の判定を取り消しました`);
  } catch (e) {
    setSave("err", "戻せません");
    banner("取り消せませんでした: " + e.message);
  } finally {
    S.busy = false;
  }
}

// --------------------------------------------------------------------
// 位置の指定
// --------------------------------------------------------------------

function startMark() {
  if (!cur()) return;
  S.marking = true;
  S.pending = null;
  S.tap = null;
  el.judge.classList.add("hidden");
  el.mark.classList.remove("hidden");
  el.confirm.disabled = true;
  el.confirm.textContent = "画像をタップしてください";
  banner("漏れている場所を、画像の上で直接タップしてください");
  drawBoxes();
}

function cancelMark() {
  if (!S.marking) return;
  S.marking = false;
  S.pending = null;
  S.tap = null;
  el.judge.classList.remove("hidden");
  el.mark.classList.add("hidden");
  banner("");
  drawBoxes();
}

function boxSize() {
  const [w, h] = S.state.default_size;
  return [
    Math.max(8, Math.round((w * S.sizePct) / 100)),
    Math.max(8, Math.round((h * S.sizePct) / 100)),
  ];
}

function placeFromTap(nx, ny) {
  // サーバ側の review.tap_to_box と同じ規則で置く。ここで違う計算をすると
  // 「見えている枠」と「実際に塞がれる場所」がずれる
  const [bw, bh] = boxSize();
  const W = S.state.width, H = S.state.height;
  const w = Math.max(4, Math.min(W, bw));
  const h = Math.max(4, Math.min(H, bh));
  const cx = Math.min(Math.max(nx, 0), 1) * W;
  const cy = Math.min(Math.max(ny, 0), 1) * H;
  S.tap = [nx, ny];
  S.pending = [
    Math.min(Math.max(cx - w / 2, 0), W - w),
    Math.min(Math.max(cy - h / 2, 0), H - h),
    w, h,
  ];
  el.confirm.disabled = false;
  el.confirm.textContent = "この位置で確定";
  banner("大きさは下のスライダで調整できます");
  drawBoxes();
}

function updateSize() {
  const [w, h] = boxSize();
  el.sizeLabel.textContent = `${w}x${h}px`;
  if (S.tap) placeFromTap(S.tap[0], S.tap[1]);
}

function buildSpanButtons(step) {
  const opts = [
    { v: 0, label: "このコマだけ" },
    { v: step, label: `前後 ${step}` },
    { v: step * 3, label: `前後 ${step * 3}` },
  ];
  S.span = opts[1].v;
  el.spanRow.innerHTML = "";
  for (const o of opts) {
    const b = document.createElement("button");
    b.className = "span-btn" + (o.v === S.span ? " on" : "");
    b.textContent = o.label;
    b.onclick = () => {
      S.span = o.v;
      for (const c of el.spanRow.children) c.classList.remove("on");
      b.classList.add("on");
    };
    el.spanRow.appendChild(b);
  }
}

function tapAt(ev) {
  const t = ev.changedTouches ? ev.changedTouches[0] : ev;
  const r = el.ov.getBoundingClientRect();
  // 画面ピクセルではなく比率で持つ。端末の拡大率や回転で意味が変わらない
  return [(t.clientX - r.left) / r.width, (t.clientY - r.top) / r.height];
}

el.ov.addEventListener("pointerdown", (ev) => {
  if (!S.marking) return;
  ev.preventDefault();
  const [nx, ny] = tapAt(ev);
  placeFromTap(nx, ny);
});

// --------------------------------------------------------------------
// キューの組み直し
// --------------------------------------------------------------------

async function reloadQueue(params) {
  setSave("busy", "作り直し中");
  try {
    // トークンは api() が付ける。ここで url() を通すと t が二重に載る
    const qs = new URLSearchParams(Object.assign({ rebuild: 1 }, params || {}));
    const q = await api(API + "/queue?" + qs.toString());
    S.items = q.items;
    S.version = q.version;
    el.optStep.value = q.step;
    buildSpanButtons(q.step);
    updateProgress(q.progress);
    setSave("ok", "保存済");
    S.idx = firstUnjudged(0);
    show();
  } catch (e) {
    setSave("err", "作り直せません");
    banner(e.message);
  }
}

// --------------------------------------------------------------------
// 入力
// --------------------------------------------------------------------

$("btn-ok").onclick = () => judge("ok");
$("btn-unsure").onclick = () => judge("unsure");
$("btn-ng").onclick = startMark;
$("btn-undo").onclick = undo;
$("btn-undo2").onclick = undo;
$("btn-cancel").onclick = cancelMark;

el.confirm.onclick = () => {
  if (!S.tap) return;
  const [w, h] = boxSize();
  const payload = {
    x: S.tap[0], y: S.tap[1], w, h,
    span: S.span,
    class: el.optClass.value || S.state.default_class,
  };
  cancelMark();
  judge("fixed", payload);
};

el.size.oninput = () => { S.sizePct = +el.size.value; updateSize(); };
$("btn-minus").onclick = () => { el.size.value = Math.max(+el.size.min, S.sizePct - 15); el.size.oninput(); };
$("btn-plus").onclick = () => { el.size.value = Math.min(+el.size.max, S.sizePct + 15); el.size.oninput(); };

$("btn-menu").onclick = () => el.sheet.classList.remove("hidden");
$("btn-close-sheet").onclick = () => el.sheet.classList.add("hidden");

$("opt-raw").onchange = show;
$("opt-boxes").onchange = () => drawBoxes();
$("opt-all").onchange = () => reloadQueue({ all: $("opt-all").checked ? 1 : 0 });
$("btn-rebuild").onclick = () =>
  reloadQueue({ step: Math.max(1, +el.optStep.value || 5), all: $("opt-all").checked ? 1 : 0 });
$("btn-unjudged").onclick = () => { el.sheet.classList.add("hidden"); goto(firstUnjudged(0)); };
$("btn-prev-item").onclick = () => goto(S.idx - 1);
$("btn-next-item").onclick = () => goto(S.idx + 1);

document.addEventListener("keydown", (ev) => {
  if (ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT") return;
  switch (ev.key) {
    case "1": judge("ok"); break;
    case "2": startMark(); break;
    case "3": judge("unsure"); break;
    case "u": case "U": undo(); break;
    case "ArrowLeft": goto(S.idx - 1); break;
    case "ArrowRight": goto(S.idx + 1); break;
    case "Escape": cancelMark(); break;
    default: return;
  }
  ev.preventDefault();
});

boot().catch((e) => {
  setSave("err", "起動に失敗");
  banner("起動に失敗しました: " + e.message);
});
