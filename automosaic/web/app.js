"use strict";

// 検査キュー画面。
//
// 「どのフレームを見るか」はサーバが決める。画面はそれを1枚ずつ出して、
// 押された判定を返すだけにしてある。判定の記録もサーバ持ちなので、
// 端末を閉じても、別の端末で開き直しても続きから再開できる。
//
// 操作は指だけで完結させる。キーボードの割り当ては残してあるが、
// それが無いと出来ないことは1つも作らない。

const $ = (id) => document.getElementById(id);

// トークンは URL から拾う。サーバが Cookie にも移すので画像は素の URL でも
// 通るが、Cookie が消えている端末でも動くように毎回付け直す。
const TOKEN = new URLSearchParams(location.search).get("t") || "";

function url(path, params) {
  const p = new URLSearchParams(params || {});
  if (TOKEN) p.set("t", TOKEN);
  const q = p.toString();
  return q ? path + "?" + q : path;
}

const el = {
  pos: $("pos"),
  reason: $("reason"),
  save: $("save"),
  fill: $("progress-fill"),
  shot: $("shot"),
  ov: $("ov"),
  banner: $("banner"),
  judge: $("judge"),
  mark: $("mark"),
  size: $("size"),
  sizeLabel: $("size-label"),
  spanRow: $("span-row"),
  markTitle: $("mark-title"),
  confirm: $("btn-confirm"),
  sheet: $("sheet"),
  sheetInfo: $("sheet-info"),
  optClass: $("opt-class"),
  optStep: $("opt-step"),
};

const S = {
  state: null,
  items: [],
  idx: 0,
  version: 0,
  sizePct: 100,
  span: 0,
  pending: null,   // 置いた矩形 [x, y, w, h]（動画座標）
  tap: null,       // 正規化タップ座標。サーバへはこちらを送る
  picked: [],      // 「誤検知」で選んだ自動領域の番号（autoBoxes() の添字）
  marking: false,
  // 位置指定モードの種類。"add" は漏れを塞ぐ、"shrink" は塗り過ぎを狭める、
  // "erase" は誤検知を消す。画面部品は共用なので、どのつもりの操作かをここで持つ
  markMode: null,
  busy: false,
  imgWidth: 720,
};

const SRC_COLOR = { d: "#4a9eff", i: "#ffd479", m: "#ffd479", b: "#ffb347", x: "#ff5a5a" };

// 位置指定モードごとの文言と、サーバへ送る判定名。
// verdict をここに持たせておかないと、確定処理がモードごとの分岐だらけになる
const MARK_MODES = {
  add: {
    verdict: "fixed",
    title: "漏れている場所を指定",
    hint: "漏れている場所を、画像の上で直接タップしてください",
    wait: "画像をタップしてください",
    confirm: "この位置で確定",
  },
  shrink: {
    verdict: "toobig",
    title: "残す範囲を指定（枠内の自動領域は消えます）",
    hint: "枠で囲まれた自動領域を消して、タップした範囲だけを残します",
    wait: "残したい範囲をタップしてください",
    confirm: "この範囲にする",
  },
  // 誤検知は範囲を置かせず、いま乗っているモザイクから選ばせる。
  // 消す方向の操作なので、何が消えるのかを見せないまま確定させない
  erase: {
    verdict: "false_positive",
    title: "消すモザイクを選ぶ",
    hint: "局部ではない場所に乗っているモザイクを、枠をタップして選びます",
    wait: "消す枠をタップしてください",
    confirm: "これを消す",
    confirmAll: "このコマは無処理になる",
    pick: true,
  },
};

// 判定済みの表示。progress の集計キーとも揃えてある
const VERDICT_LABEL = {
  ok: "問題なし",
  fixed: "塞いだ",
  unsure: "保留",
  toobig: "範囲を狭めた",
  false_positive: "誤検知として消した",
};

// --------------------------------------------------------------------
// 起動
// --------------------------------------------------------------------

async function get(path, params) {
  const res = await fetch(url(path, params));
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

async function post(path, body) {
  const res = await fetch(url(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

async function boot() {
  // light=1 で全フレームぶんの矩形と被覆文字列を落としてもらう。
  // この画面が使うのは解像度・クラス・既定サイズだけで、あれは1時間の
  // 動画だと 10MB を超える。端末の回線では起動しなくなる
  S.state = await get("/api/state", { light: 1 });
  const q = await get("/api/queue");
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
  $("link-timeline").href = url("/timeline");

  buildSpanButtons(q.step);
  updateSize();
  updateProgress(q.progress);
  S.idx = firstUnjudged(0);
  show();
}

// --------------------------------------------------------------------
// 表示
// --------------------------------------------------------------------

function cur() {
  return S.items[S.idx] || null;
}

function frameUrl(frame, width) {
  const p = { n: frame, fmt: "jpg", w: width, v: S.version };
  if ($("opt-raw").checked) p.raw = 1;
  return url("/frame", p);
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
  el.pos.title = `frame ${it.frame}`;
  drawBoxes();
  prefetch();
  if (it.verdict) {
    banner("判定済み: " + (VERDICT_LABEL[it.verdict] || it.verdict));
  } else {
    banner("");
  }
}

function prefetch() {
  // 次の2枚を先読みする。/frame は世代番号付きの URL だけキャッシュ可なので、
  // 修正が入れば URL ごと変わり、古い絵が残ることはない
  for (let k = 1; k <= 2; k++) {
    const it = S.items[S.idx + k];
    if (it) new Image().src = frameUrl(it.frame, S.imgWidth);
  }
}

function autoBoxes() {
  // 自動で置かれた領域だけ。手で足したもの（x）は remove の対象外なので、
  // 「でかすぎる」で狭められるのはこちらだけ
  const it = cur();
  return ((it && it.boxes) || []).filter((r) => r[4] !== "x");
}

function overlaps(a, b) {
  // 辺が接しているだけは重なりとみなさない。corrections.apply の判定と同じ
  return !(a[0] + a[2] <= b[0] || b[0] + b[2] <= a[0] ||
           a[1] + a[3] <= b[1] || b[1] + b[3] <= a[1]);
}

// 誤検知を確定したときに実際に消える自動領域の番号。
//
// remove は「その矩形と重なる自動領域」を落とす実装なので、選んだ枠に
// 重なっている別の枠も一緒に消える。実素材では推定どうしがほとんど
// 重なって並ぶことがあり、1つ選んだつもりで2つ消えることが起きる。
// 選んだものだけを描いて確定させると、消える前に気づけない
function eraseVictims() {
  const boxes = autoBoxes();
  const chosen = S.picked.map((i) => boxes[i]);
  const out = new Set(S.picked);
  boxes.forEach((r, i) => {
    if (chosen.some((c) => overlaps(r, c))) out.add(i);
  });
  return out;
}

function clearOverlay() {
  const ctx = el.ov.getContext("2d");
  ctx.clearRect(0, 0, el.ov.width, el.ov.height);
}

function drawBoxes() {
  clearOverlay();
  const shrink = S.markMode === "shrink";
  const erase = S.markMode === "erase";
  if (!$("opt-boxes").checked && !S.pending && !shrink && !erase) return;
  const ctx = el.ov.getContext("2d");
  // 端末では画面に対して縮んで表示されるので、線幅は解像度に比例させる
  const lw = Math.max(2, Math.round(S.state.width / 400));

  if (erase) {
    // 誤検知モードでは、枠の表示設定に関係なく自動領域を全部出す。
    // どれを消すのかが見えないまま消させてはいけない。
    // 選んだものは塗りつぶし、選んでいないものは細い破線にして、
    // 「いま消えるのはこれだけ」が一目で分かるようにする
    const boxes = autoBoxes();
    const victims = eraseVictims();
    boxes.forEach((r, i) => {
      const on = victims.has(i);
      const chosen = S.picked.includes(i);
      // 選んだものは実線、巻き添えで消えるものは点線。どちらも赤で描く。
      // 消えることに変わりはないので、色まで分けると見落とす
      ctx.setLineDash(on && !chosen ? [lw * 4, lw * 2] : on ? [] : [lw * 3, lw * 3]);
      ctx.lineWidth = on ? lw * 2 : lw;
      if (on) {
        ctx.fillStyle = "rgba(224, 90, 86, .30)";
        ctx.fillRect(r[0], r[1], r[2], r[3]);
      }
      ctx.strokeStyle = on ? "#ff6b66" : "#98a2b0";
      ctx.strokeRect(r[0], r[1], r[2], r[3]);
      if (chosen) {
        // 消える印。塗りだけだと「選択」と「削除」の区別がつかない
        ctx.beginPath();
        ctx.moveTo(r[0], r[1]);
        ctx.lineTo(r[0] + r[2], r[1] + r[3]);
        ctx.moveTo(r[0] + r[2], r[1]);
        ctx.lineTo(r[0], r[1] + r[3]);
        ctx.stroke();
      }
    });
    ctx.setLineDash([]);
    return;
  }

  if (shrink) {
    // 狭めるモードでは枠の表示設定に関係なく自動領域を出す。何を狭めようと
    // しているのか分からないまま範囲を置かせると、逆に広げる操作になる。
    // 薄く塗りつぶすのは、線だけでは「どれがでかいのか」が読み取りにくいから
    ctx.lineWidth = lw * 1.5;
    for (const r of autoBoxes()) {
      ctx.fillStyle = "rgba(255, 165, 60, .22)";
      ctx.fillRect(r[0], r[1], r[2], r[3]);
      ctx.strokeStyle = "#ffa53c";
      ctx.strokeRect(r[0], r[1], r[2], r[3]);
    }
  } else if ($("opt-boxes").checked) {
    const it = cur();
    ctx.lineWidth = lw;
    for (const r of (it && it.boxes) || []) {
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
  const pct = p.total ? (100 * p.done) / p.total : 0;
  el.fill.style.width = pct.toFixed(1) + "%";
  el.sheetInfo.textContent =
    `${p.total} 枚中 ${p.done} 枚判定済み（残り ${p.remaining}）` +
    `  問題なし ${p.counts.ok} / 塞いだ ${p.counts.fixed}` +
    ` / 狭めた ${p.counts.toobig || 0} / 誤検知 ${p.counts.false_positive || 0}` +
    ` / 保留 ${p.counts.unsure}`;
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

function advance() {
  goto(firstUnjudged(S.idx + 1));
}

// --------------------------------------------------------------------
// 判定
// --------------------------------------------------------------------

async function judge(verdict, extra) {
  const it = cur();
  if (!it || S.busy) return;
  S.busy = true;
  setSave("busy", "保存中");
  try {
    const d = await post("/api/mark", Object.assign({ frame: it.frame, verdict }, extra || {}));
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
    const d = await post("/api/undo");
    if (!d.ok) {
      setSave("ok", "保存済");
      banner(d.error || "戻せません");
      return;
    }
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

function startMark(mode) {
  if (!cur()) return;
  const m = MARK_MODES[mode];
  if (!m) return;
  const autos = autoBoxes();
  if ((mode === "shrink" || mode === "erase") && !autos.length) {
    // 消す相手がいないのに範囲だけ置かせると、ただ塗る範囲が増える。
    // どちらも自動領域が前提の操作なので、無いなら入らせない
    banner("このコマには自動で塗った領域がありません");
    return;
  }
  S.marking = true;
  S.markMode = mode;
  S.pending = null;
  S.tap = null;
  S.picked = [];
  document.body.classList.add("marking");
  document.body.classList.toggle("shrinking", mode === "shrink");
  document.body.classList.toggle("erasing", mode === "erase");
  el.judge.classList.add("hidden");
  el.mark.classList.remove("hidden");
  el.markTitle.textContent = m.title;
  el.markTitle.classList.toggle("danger", mode === "erase");
  el.confirm.disabled = true;
  el.confirm.textContent = m.wait;
  banner(m.hint);
  if (mode === "erase") {
    // 1つしかないなら選びようがない。それでも確定は押させる（消えるのが
    // 見えてから確定する、という手順自体は省かない）
    if (autos.length === 1) S.picked = [0];
    updateErase();
  }
  drawBoxes();
}

function cancelMark() {
  if (!S.marking) return;
  S.marking = false;
  S.markMode = null;
  S.pending = null;
  S.tap = null;
  S.picked = [];
  document.body.classList.remove("marking");
  document.body.classList.remove("shrinking");
  document.body.classList.remove("erasing");
  document.body.classList.remove("erase-all");
  el.markTitle.classList.remove("danger");
  el.judge.classList.remove("hidden");
  el.mark.classList.add("hidden");
  banner("");
  drawBoxes();
}

// 誤検知モードの確定ボタンと注意書きを、いまの選択に合わせて書き換える
function updateErase() {
  const total = autoBoxes().length;
  // 数えるのは「選んだ数」ではなく「消える数」。重なった枠は巻き添えで
  // 消えるので、選んだ数で案内すると実際より少なく見える
  const n = S.picked.length ? eraseVictims().size : 0;
  const all = n > 0 && n >= total;
  const m = MARK_MODES.erase;

  document.body.classList.toggle("erase-all", all);
  el.confirm.disabled = n === 0;
  el.confirm.textContent = n === 0 ? m.wait : all ? m.confirmAll : m.confirm;

  if (n === 0) {
    banner(m.hint);
    return;
  }
  // 何が起きるかを言葉でも出す。押す前に「消える」と読めることが要る。
  // 適用範囲を前後に広げているときは、消えるのが1コマではないことも書く
  const scope = S.span ? `前後 ${S.span} コマにも同じ領域の削除が入ります。` : "";
  const extra = n > S.picked.length ? "重なっている枠も一緒に消えます。" : "";
  banner(
    (all
      ? "確定するとこのコマのモザイクは全部消えます（無処理になります）。"
      : `確定するとこのコマのモザイク ${n} / ${total} 個が消えます。`) +
      extra +
      scope
  );
}

function boxSize() {
  const [w, h] = S.state.default_size;
  return [
    Math.max(8, Math.round((w * S.sizePct) / 100)),
    Math.max(8, Math.round((h * S.sizePct) / 100)),
  ];
}

function placeFromTap(nx, ny) {
  // サーバ側の tap_to_box と同じ規則で置く。ここで違う計算をすると
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
    w,
    h,
  ];
  el.confirm.disabled = false;
  el.confirm.textContent = (MARK_MODES[S.markMode] || MARK_MODES.add).confirm;
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
    b.className = "mid span-btn" + (o.v === S.span ? " on" : "");
    b.textContent = o.label;
    b.onclick = () => {
      S.span = o.v;
      for (const c of el.spanRow.children) c.classList.remove("on");
      b.classList.add("on");
      // 誤検知の注意書きは「何コマぶん消えるか」を含む。ここでも書き直す
      if (S.markMode === "erase") updateErase();
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

// 誤検知モードのタップ。押した点から「どの枠のことか」を決める。
// 入れ子になっている枠では小さいほうを選ぶ。大きい枠は外側をタップすれば
// 選べるが、内側の小さい枠は中でしか選べないため
function pickAt(nx, ny) {
  const boxes = autoBoxes();
  const x = nx * S.state.width;
  const y = ny * S.state.height;
  let best = -1;
  let bestArea = Infinity;
  boxes.forEach((r, i) => {
    if (x < r[0] || x > r[0] + r[2] || y < r[1] || y > r[1] + r[3]) return;
    const a = r[2] * r[3];
    if (a < bestArea) { best = i; bestArea = a; }
  });
  if (best < 0) {
    // 枠の外。近い枠を勝手に選ぶと「押した覚えのないものが消える」ので、
    // 何もせずに押す場所だけ教える
    banner("消したい枠の中をタップしてください");
    return;
  }
  const at = S.picked.indexOf(best);
  if (at >= 0) S.picked.splice(at, 1);
  else S.picked.push(best);
  updateErase();
  drawBoxes();
}

el.ov.addEventListener("pointerdown", (ev) => {
  if (!S.marking) return;
  ev.preventDefault();
  const [nx, ny] = tapAt(ev);
  if (S.markMode === "erase") pickAt(nx, ny);
  else placeFromTap(nx, ny);
});

// --------------------------------------------------------------------
// キューの組み直し
// --------------------------------------------------------------------

async function reloadQueue(params) {
  setSave("busy", "作り直し中");
  try {
    const q = await get("/api/queue", Object.assign({ rebuild: 1 }, params || {}));
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
$("btn-ng").onclick = () => startMark("add");
$("btn-big").onclick = () => startMark("shrink");
$("btn-fp").onclick = () => startMark("erase");
$("btn-undo").onclick = undo;
$("btn-undo2").onclick = undo;
$("btn-cancel").onclick = cancelMark;

el.confirm.onclick = () => {
  const m = MARK_MODES[S.markMode];
  if (!m) return;
  const cls = el.optClass.value || S.state.default_class;
  let payload;
  if (m.pick) {
    // 選ばれていなければ何もしない。誤検知は「選んだ枠だけ」を消す操作なので、
    // 選択なしで通すと何が消えたのか誰にも分からない修正になる
    if (!S.picked.length) return;
    const boxes = autoBoxes();
    payload = {
      pick: S.picked.map((i) => boxes[i].slice(0, 4)),
      span: S.span,
      class: cls,
    };
  } else {
    // 範囲が置かれていなければ何もしない。「でかすぎる」で範囲なしを通すと、
    // 自動領域を消すだけの修正になり、そのコマが素通しになる
    if (!S.tap) return;
    const [w, h] = boxSize();
    payload = { x: S.tap[0], y: S.tap[1], w, h, span: S.span, class: cls };
  }
  cancelMark();
  judge(m.verdict, payload);
};

el.size.oninput = () => {
  S.sizePct = +el.size.value;
  updateSize();
};
$("btn-minus").onclick = () => {
  el.size.value = Math.max(+el.size.min, S.sizePct - 15);
  el.size.oninput();
};
$("btn-plus").onclick = () => {
  el.size.value = Math.min(+el.size.max, S.sizePct + 15);
  el.size.oninput();
};

$("btn-menu").onclick = () => el.sheet.classList.remove("hidden");
$("btn-close-sheet").onclick = () => el.sheet.classList.add("hidden");
el.sheet.onclick = (ev) => { if (ev.target === el.sheet) el.sheet.classList.add("hidden"); };

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
    case "2": startMark("add"); break;
    case "3": judge("unsure"); break;
    case "4": startMark("shrink"); break;
    case "5": startMark("erase"); break;
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
