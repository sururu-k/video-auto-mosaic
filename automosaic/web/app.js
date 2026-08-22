"use strict";

// automosaic レビュー UI。
// 見るべき区間だけを提示して、そこだけ直すための画面。全フレームを送るのは
// 被覆状況の文字列と矩形リストだけで、絵は必要なフレームだけ取りに行く。

const $ = (id) => document.getElementById(id);

const el = {
  videoName: $("video-name"),
  videoMeta: $("video-meta"),
  save: $("save-status"),
  video: $("video"),
  still: $("still"),
  overlay: $("overlay"),
  player: $("player"),
  frameInput: $("frame-input"),
  frameTotal: $("frame-total"),
  timeLabel: $("time-label"),
  sizeLabel: $("size-label"),
  spanInput: $("span-input"),
  classSelect: $("class-select"),
  confSlider: $("conf-slider"),
  confLabel: $("conf-label"),
  rawToggle: $("raw-toggle"),
  corrCount: $("corr-count"),
  hint: $("mode-hint"),
  timeline: $("timeline"),
  estList: $("est-list"),
  uncList: $("unc-list"),
  manList: $("man-list"),
  estCount: $("est-count"),
  uncCount: $("unc-count"),
  manCount: $("man-count"),
};

const S = {
  state: null,          // /api/state の中身
  corrections: [],      // 手修正の全リスト。これをそのまま POST する
  cur: 0,               // 現在フレーム
  addMode: false,       // M で入る追加モード
  pending: null,        // 置いたがまだ適用していない矩形 [x,y,w,h]
  size: [64, 64],       // 追加する矩形のサイズ
  mouse: null,          // 動画座標系のカーソル位置。D の対象判定に使う
  stillToken: 0,        // 古いフレーム画像の到着で上書きされるのを防ぐ
  playing: false,
  confMin: 0,
};

// 由来ごとの色。青=自動検出、黄=補間/memory/橋渡し、赤=手修正。
const SRC_COLOR = {
  d: "#4a9eff",
  i: "#ffd479",
  m: "#ffd479",
  b: "#ffb347",
  x: "#ff5a5a",
};
const SRC_NAME = {
  d: "検出",
  i: "補間",
  m: "memory",
  b: "橋渡し",
  x: "手修正",
};

// --------------------------------------------------------------------
// 読み込み
// --------------------------------------------------------------------

async function boot() {
  S.state = await (await fetch("/api/state")).json();
  const c = await (await fetch("/api/corrections")).json();
  S.corrections = c.corrections || [];

  const st = S.state;
  S.size = st.default_size.slice();

  el.videoName.textContent = st.video;
  el.videoMeta.textContent =
    `${st.width}x${st.height}  ${st.fps.toFixed(3)} fps  ${st.n_frames} フレーム` +
    `  /  再生: ${st.rendered || "なし"}`;
  el.frameTotal.textContent = `/ ${st.n_frames - 1}`;
  el.frameInput.max = st.n_frames - 1;

  el.overlay.width = st.width;
  el.overlay.height = st.height;
  el.player.style.width = st.width + "px";
  el.still.style.aspectRatio = `${st.width} / ${st.height}`;

  for (const name of st.classes) {
    const o = document.createElement("option");
    o.value = name;
    o.textContent = name;
    if (name === st.default_class) o.selected = true;
    el.classSelect.appendChild(o);
  }

  if (st.has_video) {
    el.video.src = "/video";
  } else {
    // 再生用の動画が無いときはコマ送りだけで運用する
    el.video.style.display = "none";
    el.player.style.height = st.height + "px";
  }

  updateSizeLabel();
  renderLists();
  setFrame(0, true);
  drawTimeline();
  tick();
}

// --------------------------------------------------------------------
// フレーム移動
// --------------------------------------------------------------------

function setFrame(n, force) {
  const st = S.state;
  n = Math.max(0, Math.min(st.n_frames - 1, Math.round(n)));
  if (n === S.cur && !force) return;
  S.cur = n;
  el.frameInput.value = n;
  el.timeLabel.textContent = (n / st.fps).toFixed(2) + "s";

  if (!S.playing) {
    // フレーム中央の時刻を狙う。境界ちょうどだと前後どちらが出るか不定
    if (st.has_video) el.video.currentTime = (n + 0.5) / st.fps;
    loadStill(n);
  }
  draw();
  drawTimeline();
  markCurrentInLists();
}

function loadStill(n) {
  const token = ++S.stillToken;
  const raw = el.rawToggle.checked ? "&raw=1" : "";
  const img = new Image();
  img.onload = () => {
    if (token !== S.stillToken) return; // 古い要求。捨てる
    el.still.src = img.src;
    el.still.classList.add("show");
  };
  img.src = `/frame?n=${n}${raw}`;
}

function hideStill() {
  S.stillToken++;
  el.still.classList.remove("show");
}

function tick() {
  if (S.playing && S.state.has_video) {
    const n = Math.round(el.video.currentTime * S.state.fps);
    if (n !== S.cur) {
      S.cur = n;
      el.frameInput.value = n;
      el.timeLabel.textContent = (n / S.state.fps).toFixed(2) + "s";
      draw();
      drawTimeline();
    }
  }
  requestAnimationFrame(tick);
}

function play() {
  if (!S.state.has_video) return;
  S.playing = true;
  hideStill();
  el.video.play();
}

function pause() {
  S.playing = false;
  el.video.pause();
  setFrame(Math.round(el.video.currentTime * S.state.fps), true);
}

function togglePlay() {
  if (S.playing) pause(); else play();
}

// --------------------------------------------------------------------
// 重ね描き
// --------------------------------------------------------------------

function regionsAt(n) {
  return (S.state.regions[String(n)] || []);
}

function draw() {
  const ctx = el.overlay.getContext("2d");
  ctx.clearRect(0, 0, el.overlay.width, el.overlay.height);
  ctx.lineWidth = 2;
  ctx.font = "12px sans-serif";

  for (const r of regionsAt(S.cur)) {
    const [x, y, w, h, src, score] = r;
    if (src === "d" && score < S.confMin) continue; // 信頼度スライダで一時的に隠す
    ctx.strokeStyle = SRC_COLOR[src] || "#ffffff";
    ctx.strokeRect(x, y, w, h);
    ctx.fillStyle = ctx.strokeStyle;
    ctx.fillText(`${SRC_NAME[src] || src} ${score.toFixed(2)}`, x + 2, Math.max(12, y - 3));
  }

  if (S.pending) {
    ctx.setLineDash([6, 4]);
    ctx.strokeStyle = "#ffffff";
    ctx.strokeRect(S.pending[0], S.pending[1], S.pending[2], S.pending[3]);
    ctx.setLineDash([]);
  }
}

// --------------------------------------------------------------------
// タイムライン
// --------------------------------------------------------------------

function drawTimeline() {
  const cv = el.timeline;
  const w = cv.clientWidth;
  if (cv.width !== w) cv.width = w;
  const h = cv.height;
  const ctx = cv.getContext("2d");
  const st = S.state;
  const cov = st.coverage;
  const n = st.n_frames;

  ctx.fillStyle = "#101216";
  ctx.fillRect(0, 0, w, h);

  const bandH = h - 10;
  for (let px = 0; px < w; px++) {
    const a = Math.floor((px * n) / w);
    const b = Math.max(a + 1, Math.floor(((px + 1) * n) / w));
    // 1px に何十フレームも入るので、その中でいちばん悪い状態を出す。
    // 平均を取ると単発の素通しが消えて見えなくなる。
    let worst = 1;
    for (let f = a; f < b && f < n; f++) {
      const c = cov.charCodeAt(f) - 48;
      if (c === 0) { worst = 0; break; }
      if (c === 2) worst = 2;
    }
    ctx.fillStyle = worst === 0 ? "#d0453e" : worst === 2 ? "#d9b73c" : "#3ba55d";
    ctx.fillRect(px, 0, 1, bandH);
  }

  // 手修正のあるフレームを下段に赤で立てる
  ctx.fillStyle = "#e05a5a";
  for (const c of S.corrections) {
    const px = Math.floor((c.frame * w) / n);
    ctx.fillRect(px, bandH + 1, 2, 9);
  }

  // 現在位置
  const cx = Math.floor((S.cur * w) / n);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(cx, 0, 1, h);
}

// --------------------------------------------------------------------
// リスト
// --------------------------------------------------------------------

function rangeItem(r, ul) {
  const li = document.createElement("li");
  const t0 = (r.start / S.state.fps).toFixed(2);
  const t1 = (r.end / S.state.fps).toFixed(2);
  const a = document.createElement("span");
  a.textContent = `${r.start} - ${r.end}  (${t0}s - ${t1}s)`;
  const b = document.createElement("span");
  b.className = "len";
  b.textContent = `${r.frames}f`;
  li.appendChild(a);
  li.appendChild(b);
  li.dataset.start = r.start;
  li.dataset.end = r.end;
  li.onclick = () => setFrame(r.start, true);
  ul.appendChild(li);
}

function renderLists() {
  const st = S.state;
  el.estList.innerHTML = "";
  el.uncList.innerHTML = "";
  el.manList.innerHTML = "";

  for (const r of st.estimated_only_ranges) rangeItem(r, el.estList);
  for (const r of st.uncovered_ranges) rangeItem(r, el.uncList);

  const estFrames = st.estimated_only_ranges.reduce((a, r) => a + r.frames, 0);
  const uncFrames = st.uncovered_ranges.reduce((a, r) => a + r.frames, 0);
  el.estCount.textContent =
    `${st.estimated_only_ranges.length} 件 / ${estFrames} フレーム ` +
    `(${(100 * estFrames / st.n_frames).toFixed(1)}%)`;
  el.uncCount.textContent = `${st.uncovered_ranges.length} 件 / ${uncFrames} フレーム`;

  // 手修正はフレームごとにまとめる。連番で置くと1件ずつでは読めない
  const byFrame = new Map();
  for (const c of S.corrections) {
    byFrame.set(c.frame, (byFrame.get(c.frame) || 0) + 1);
  }
  const frames = [...byFrame.keys()].sort((a, b) => a - b);
  for (const f of frames) {
    const li = document.createElement("li");
    const a = document.createElement("span");
    a.textContent = `frame ${f}  (${(f / st.fps).toFixed(2)}s)`;
    const b = document.createElement("span");
    b.className = "len";
    b.textContent = `${byFrame.get(f)}`;
    li.appendChild(a);
    li.appendChild(b);
    li.dataset.start = f;
    li.dataset.end = f;
    li.onclick = () => setFrame(f, true);
    el.manList.appendChild(li);
  }
  el.manCount.textContent = `${S.corrections.length} 件 / ${frames.length} フレーム`;
  el.corrCount.textContent = S.corrections.length;
  markCurrentInLists();
}

function markCurrentInLists() {
  for (const ul of [el.estList, el.uncList, el.manList]) {
    for (const li of ul.children) {
      const inside = S.cur >= +li.dataset.start && S.cur <= +li.dataset.end;
      li.classList.toggle("current", inside);
      if (inside) li.scrollIntoView({ block: "nearest" });
    }
  }
}

// --------------------------------------------------------------------
// 修正
// --------------------------------------------------------------------

function setSaveStatus(kind, text) {
  el.save.className = kind;
  el.save.textContent = text;
}

async function pushCorrections() {
  setSaveStatus("dirty", "保存中");
  try {
    const res = await fetch("/api/corrections", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ corrections: S.corrections }),
    });
    if (!res.ok) throw new Error(await res.text());
    const d = await res.json();
    // 手修正は実観測扱いなので、帯の色も推定のみ区間も変わる。丸ごと差し替える
    S.state.coverage = d.coverage;
    S.state.regions = d.regions;
    S.state.estimated_only_ranges = d.estimated_only_ranges;
    S.state.uncovered_ranges = d.uncovered_ranges;
    setSaveStatus("saved", `保存済み (${d.n_corrections} 件)`);
    renderLists();
    drawTimeline();
    draw();
    if (!S.playing) loadStill(S.cur); // モザイクが乗った絵に差し替える
  } catch (e) {
    setSaveStatus("error", "保存失敗: " + e.message);
  }
}

function placePending(cx, cy) {
  const [w, h] = S.size;
  S.pending = [
    Math.round(cx - w / 2),
    Math.round(cy - h / 2),
    w,
    h,
  ];
  el.hint.classList.remove("active");
  el.hint.textContent = "矩形を置きました。1 でこのフレームだけ / 2 でここから N フレームに適用";
  S.addMode = false;
  draw();
}

function applyPending(nFrames) {
  if (!S.pending) {
    el.hint.textContent = "先に M を押して動画上をクリックし、矩形を置いてください";
    el.hint.classList.add("active");
    return;
  }
  const cls = el.classSelect.value;
  const last = Math.min(S.state.n_frames - 1, S.cur + nFrames - 1);
  for (let f = S.cur; f <= last; f++) {
    S.corrections.push({
      frame: f,
      box: S.pending.slice(),
      class: cls,
      kind: "add",
    });
  }
  S.pending = null;
  el.hint.textContent =
    `frame ${S.cur}-${last} に ${cls} を追加しました`;
  pushCorrections();
}

function deleteUnderCursor() {
  if (!S.mouse) return;
  const [mx, my] = S.mouse;
  // 後ろから見て最初に当たった1件だけ消す（corrections.remove_at と同じ規則）
  for (let i = S.corrections.length - 1; i >= 0; i--) {
    const c = S.corrections[i];
    if (c.frame !== S.cur) continue;
    const [x, y, w, h] = c.box;
    if (mx >= x && mx <= x + w && my >= y && my <= y + h) {
      S.corrections.splice(i, 1);
      el.hint.textContent = `frame ${S.cur} の手修正を1件消しました`;
      pushCorrections();
      return;
    }
  }
  el.hint.textContent = "カーソルの下に手修正がありません";
}

function undoLast() {
  if (!S.corrections.length) return;
  S.corrections.pop();
  pushCorrections();
}

function nextEstimatedRange() {
  const ranges = S.state.estimated_only_ranges;
  for (const r of ranges) {
    if (r.start > S.cur) { setFrame(r.start, true); return; }
  }
  if (ranges.length) setFrame(ranges[0].start, true); // 末尾まで来たら先頭へ戻る
}

function updateSizeLabel() {
  el.sizeLabel.textContent = `${S.size[0]} x ${S.size[1]} px`;
}

function scaleSize(k) {
  S.size = [
    Math.max(8, Math.round(S.size[0] * k)),
    Math.max(8, Math.round(S.size[1] * k)),
  ];
  updateSizeLabel();
  if (S.pending) {
    const cx = S.pending[0] + S.pending[2] / 2;
    const cy = S.pending[1] + S.pending[3] / 2;
    placePending(cx, cy);
  }
}

// --------------------------------------------------------------------
// 入力
// --------------------------------------------------------------------

function canvasPos(ev) {
  const r = el.overlay.getBoundingClientRect();
  return [
    ((ev.clientX - r.left) / r.width) * S.state.width,
    ((ev.clientY - r.top) / r.height) * S.state.height,
  ];
}

el.overlay.addEventListener("mousemove", (ev) => {
  S.mouse = canvasPos(ev);
});

el.overlay.addEventListener("click", (ev) => {
  const [x, y] = canvasPos(ev);
  S.mouse = [x, y];
  if (S.addMode) placePending(x, y);
});

el.timeline.addEventListener("click", (ev) => {
  const r = el.timeline.getBoundingClientRect();
  const f = Math.floor(((ev.clientX - r.left) / r.width) * S.state.n_frames);
  if (S.playing) pause();
  setFrame(f, true);
});

document.addEventListener("keydown", (ev) => {
  if (ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT") return;
  const k = ev.key;
  if (k === " ") { ev.preventDefault(); togglePlay(); return; }
  if (k === ",") { if (S.playing) pause(); setFrame(S.cur - 1, true); return; }
  if (k === ".") { if (S.playing) pause(); setFrame(S.cur + 1, true); return; }
  if (k === "[") { scaleSize(1 / 1.15); return; }
  if (k === "]") { scaleSize(1.15); return; }
  if (k === "m" || k === "M") {
    if (S.playing) pause();
    S.addMode = true;
    el.hint.classList.add("active");
    el.hint.textContent = "追加モード: 動画上をクリックして矩形を置いてください";
    return;
  }
  if (k === "1") { if (S.playing) pause(); applyPending(1); return; }
  if (k === "2") {
    if (S.playing) pause();
    applyPending(Math.max(1, parseInt(el.spanInput.value, 10) || 30));
    return;
  }
  if (k === "d" || k === "D") { deleteUnderCursor(); return; }
  if (k === "g" || k === "G") { if (S.playing) pause(); nextEstimatedRange(); return; }
});

$("btn-play").onclick = togglePlay;
$("btn-prev").onclick = () => { if (S.playing) pause(); setFrame(S.cur - 1, true); };
$("btn-next").onclick = () => { if (S.playing) pause(); setFrame(S.cur + 1, true); };
$("btn-smaller").onclick = () => scaleSize(1 / 1.15);
$("btn-bigger").onclick = () => scaleSize(1.15);
$("btn-next-est").onclick = () => { if (S.playing) pause(); nextEstimatedRange(); };
$("btn-undo").onclick = undoLast;

el.frameInput.onchange = () => { if (S.playing) pause(); setFrame(+el.frameInput.value, true); };
el.rawToggle.onchange = () => { if (!S.playing) loadStill(S.cur); };
el.confSlider.oninput = () => {
  S.confMin = parseFloat(el.confSlider.value);
  el.confLabel.textContent = S.confMin.toFixed(2);
  draw();
};

el.video.addEventListener("pause", () => { if (S.playing) pause(); });
el.video.addEventListener("play", () => { S.playing = true; hideStill(); });
el.video.addEventListener("ended", () => { S.playing = false; setFrame(S.cur, true); });
// メタデータが載る前の currentTime 代入は黙って無視されるので、載ってから入れ直す
el.video.addEventListener("loadedmetadata", () => { if (!S.playing) setFrame(S.cur, true); });
window.addEventListener("resize", drawTimeline);

boot().catch((e) => setSaveStatus("error", "起動に失敗: " + e.message));
