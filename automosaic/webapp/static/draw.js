"use strict";

// 手描きモード。検出をかけずに、空の状態から矩形を置く。
//
// 全フレームに打つのは現実的でないので、数フレームおきに打った点の
// あいだを補間で埋める。補間の規則はサーバ側で
// tools/annotations_to_corrections.py の build() をそのまま呼んでいる。
// 「ここには無い」を打てるようにしてあるのは、対象が画面から消えた後も
// モザイクが伸び続けるのを止めるため。

const JOB = location.pathname.split("/").pop();
const API = "/api/jobs/" + JOB;

const S = {
  state: null, frame: 0, points: [], sizePct: 100,
  pending: null, tap: null, imgWidth: 720, busy: false,
};

$("back").href = link("/job/" + JOB);
$("go-job").href = link("/job/" + JOB);

function ptAt(frame) {
  return S.points.find((p) => p.frame === frame) || null;
}

function frameUrl(n) {
  const p = { n, fmt: "jpg", w: S.imgWidth };
  if ($("opt-raw").checked) p.raw = 1;
  return url(API + "/frame", p);
}

function boxSize() {
  const [w, h] = S.state.default_size;
  return [
    Math.max(8, Math.round((w * S.sizePct) / 100)),
    Math.max(8, Math.round((h * S.sizePct) / 100)),
  ];
}

function draw() {
  const ov = $("ov");
  const ctx = ov.getContext("2d");
  ctx.clearRect(0, 0, ov.width, ov.height);
  const lw = Math.max(2, Math.round(S.state.width / 400));

  // 既に置いてある打点は実線、これから置くものは破線。
  // 見分けがつかないと、押したのか押していないのかが分からない
  const cur = ptAt(S.frame);
  if (cur && cur.box) {
    ctx.lineWidth = lw;
    ctx.strokeStyle = "#ff5a5a";
    ctx.strokeRect(cur.box[0], cur.box[1], cur.box[2], cur.box[3]);
  }
  if (S.pending) {
    ctx.lineWidth = lw * 1.5;
    ctx.setLineDash([lw * 4, lw * 3]);
    ctx.strokeStyle = "#ffffff";
    ctx.strokeRect(S.pending[0], S.pending[1], S.pending[2], S.pending[3]);
    ctx.setLineDash([]);
  }
}

function show() {
  $("shot").src = frameUrl(S.frame);
  $("seek").value = S.frame;
  const p = ptAt(S.frame);
  const mark = p ? (p.box ? "打点あり" : "「ここには無い」") : "打点なし";
  $("pos").textContent = `frame ${S.frame} / ${S.state.n_frames - 1}  (${mark})`;
  $("b-del").disabled = !p;
  draw();
}

function goto(n) {
  S.frame = Math.max(0, Math.min(S.state.n_frames - 1, Math.round(n)));
  S.pending = null;
  S.tap = null;
  $("b-put").disabled = true;
  show();
}

function renderPoints() {
  const d = $("points");
  if (!S.points.length) { d.textContent = "まだありません"; return; }
  d.innerHTML = "";
  for (const p of S.points) {
    const a = document.createElement("a");
    a.href = "#";
    a.style.marginRight = "10px";
    a.textContent = p.box
      ? `${p.frame}: [${p.box.map((v) => Math.round(v)).join(",")}]`
      : `${p.frame}: 無し`;
    a.onclick = (e) => { e.preventDefault(); goto(p.frame); };
    d.appendChild(a);
  }
}

// --------------------------------------------------------------------

async function boot() {
  S.state = await api(API + "/state?light=1");
  const ov = $("ov");
  ov.width = S.state.width;
  ov.height = S.state.height;
  const dpr = window.devicePixelRatio || 1;
  S.imgWidth = Math.min(
    S.state.width,
    Math.max(480, Math.round(Math.min(window.screen.width, 1280) * dpr))
  );
  $("seek").max = S.state.n_frames - 1;

  for (const name of S.state.classes) {
    const o = document.createElement("option");
    o.value = name;
    o.textContent = name;
    if (name === S.state.default_class) o.selected = true;
    $("opt-class").appendChild(o);
  }

  const d = await api(API + "/annotations");
  S.points = d.annotations;
  renderPoints();
  updateSize();
  goto(0);
  $("save").textContent = `打点 ${S.points.length}`;
}

function updateSize() {
  const [w, h] = boxSize();
  $("size-label").textContent = `${w}x${h}px`;
  if (S.tap) place(S.tap[0], S.tap[1]);
}

function place(nx, ny) {
  // サーバ側の review.tap_to_box と同じ規則。ここで違う計算をすると
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
  $("b-put").disabled = false;
  draw();
}

$("ov").addEventListener("pointerdown", (ev) => {
  ev.preventDefault();
  const t = ev.changedTouches ? ev.changedTouches[0] : ev;
  const r = $("ov").getBoundingClientRect();
  place((t.clientX - r.left) / r.width, (t.clientY - r.top) / r.height);
});

async function send(body) {
  if (S.busy) return;
  S.busy = true;
  $("save").className = "save busy";
  $("save").textContent = "保存中";
  try {
    const d = await api(API + "/annotations", { json: body });
    S.points = d.annotations;
    renderPoints();
    $("save").className = "save ok";
    $("save").textContent = `打点 ${S.points.length}`;
    S.pending = null;
    S.tap = null;
    $("b-put").disabled = true;
    show();
  } catch (e) {
    $("save").className = "save err";
    $("save").textContent = "保存できません";
    $("banner").textContent = e.message;
  } finally {
    S.busy = false;
  }
}

$("b-put").onclick = () => {
  if (!S.tap) return;
  const [w, h] = boxSize();
  send({ frame: S.frame, x: S.tap[0], y: S.tap[1], w, h, class: $("opt-class").value });
};
$("b-absent").onclick = () => send({ frame: S.frame, absent: true });
$("b-del").onclick = async () => {
  try {
    const d = await api(`${API}/annotations/${S.frame}`, { method: "DELETE" });
    S.points = d.annotations;
    renderPoints();
    $("save").textContent = `打点 ${S.points.length}`;
    show();
  } catch (e) { $("banner").textContent = e.message; }
};

const stride = () => Math.max(1, +$("stride").value || 10);
$("b-first").onclick = () => goto(0);
$("b-last").onclick = () => goto(S.state.n_frames - 1);
$("b-back").onclick = () => goto(S.frame - 1);
$("b-fwd").onclick = () => goto(S.frame + 1);
$("b-back10").onclick = () => goto(S.frame - stride());
$("b-fwd10").onclick = () => goto(S.frame + stride());
$("seek").oninput = () => goto(+$("seek").value);
$("opt-raw").onchange = show;

$("b-prev-pt").onclick = () => {
  const prev = S.points.filter((p) => p.frame < S.frame).pop();
  if (prev) goto(prev.frame);
};
$("b-next-pt").onclick = () => {
  const next = S.points.find((p) => p.frame > S.frame);
  if (next) goto(next.frame);
};

$("size").oninput = () => { S.sizePct = +$("size").value; updateSize(); };
$("b-minus").onclick = () => { $("size").value = Math.max(+$("size").min, S.sizePct - 15); $("size").oninput(); };
$("b-plus").onclick = () => { $("size").value = Math.min(+$("size").max, S.sizePct + 15); $("size").oninput(); };

$("b-expand").onclick = async () => {
  $("expand-msg").textContent = "展開しています";
  try {
    const d = await api(API + "/annotations/expand", {
      json: {
        max_interp: +$("max_interp").value || 20,
        hold: +$("hold").value || 0,
        class: $("opt-class").value,
        merge: $("merge").checked,
      },
    });
    $("expand-msg").textContent =
      `打点 ${d.points} 個 -> 矩形 ${d.expanded} 件（corrections 計 ${d.corrections} 件）` +
      (d.frame_range ? `  フレーム ${d.frame_range[0]}〜${d.frame_range[1]}` : "") +
      "  ジョブ画面の「検出を再利用して焼き直す」で反映されます";
  } catch (e) {
    $("expand-msg").textContent = "展開できません: " + e.message;
  }
};

document.addEventListener("keydown", (ev) => {
  if (ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT") return;
  switch (ev.key) {
    case ",": goto(S.frame - 1); break;
    case ".": goto(S.frame + 1); break;
    case "ArrowLeft": goto(S.frame - stride()); break;
    case "ArrowRight": goto(S.frame + stride()); break;
    case "Enter": if (!$("b-put").disabled) $("b-put").click(); break;
    case "n": case "N": $("b-absent").click(); break;
    default: return;
  }
  ev.preventDefault();
});

boot().catch((e) => {
  $("save").className = "save err";
  $("save").textContent = "起動に失敗";
  $("banner").textContent = "起動に失敗しました: " + e.message;
});
