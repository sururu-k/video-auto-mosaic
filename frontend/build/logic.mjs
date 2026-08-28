// src/shared/review-logic.ts
var MARK_MODES = {
  add: {
    verdict: "fixed",
    title: "漏れている場所を指定",
    hint: "漏れている場所を、画像の上で直接タップしてください",
    wait: "画像をタップしてください",
    confirm: "この位置で確定"
  },
  shrink: {
    verdict: "toobig",
    title: "残す範囲を指定（枠内の自動領域は消えます）",
    hint: "枠で囲まれた自動領域を消して、タップした範囲だけを残します",
    wait: "残したい範囲をタップしてください",
    confirm: "この範囲にする"
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
    pick: true
  }
};
var VERDICT_LABEL = {
  ok: "問題なし",
  fixed: "塞いだ",
  unsure: "保留",
  toobig: "範囲を狭めた",
  false_positive: "誤検知として消した"
};
var SRC_COLOR = {
  d: "#4a9eff",
  i: "#ffd479",
  m: "#ffd479",
  b: "#ffb347",
  x: "#ff5a5a"
};
function autoBoxes(boxes) {
  return (boxes ?? []).filter((r) => r[4] !== "x");
}
function overlaps(a, b) {
  return !(a[0] + a[2] <= b[0] || b[0] + b[2] <= a[0] || a[1] + a[3] <= b[1] || b[1] + b[3] <= a[1]);
}
function eraseVictims(boxes, picked) {
  const chosen = picked.map((i) => boxes[i]).filter((r) => r !== void 0);
  const out = new Set(picked);
  boxes.forEach((r, i) => {
    if (chosen.some((c) => overlaps(r, c))) out.add(i);
  });
  return out;
}
function pickIndexAt(boxes, x, y) {
  let best = -1;
  let bestArea = Infinity;
  boxes.forEach((r, i) => {
    if (x < r[0] || x > r[0] + r[2] || y < r[1] || y > r[1] + r[3]) return;
    const a = r[2] * r[3];
    if (a < bestArea) {
      best = i;
      bestArea = a;
    }
  });
  return best < 0 ? null : best;
}
function togglePick(picked, i) {
  const at = picked.indexOf(i);
  if (at >= 0) {
    const out = picked.slice();
    out.splice(at, 1);
    return out;
  }
  return [...picked, i];
}
function eraseSummary(boxes, picked, span) {
  const total = boxes.length;
  const n = picked.length ? eraseVictims(boxes, picked).size : 0;
  const all = n > 0 && n >= total;
  const m = MARK_MODES.erase;
  if (n === 0) {
    return {
      total,
      victims: 0,
      all: false,
      confirmLabel: m.wait,
      confirmDisabled: true,
      banner: m.hint
    };
  }
  const scope = span ? `前後 ${span} コマにも同じ領域の削除が入ります。` : "";
  const extra = n > picked.length ? "重なっている枠も一緒に消えます。" : "";
  return {
    total,
    victims: n,
    all,
    confirmLabel: all ? m.confirmAll ?? m.confirm : m.confirm,
    confirmDisabled: false,
    banner: (all ? "確定するとこのコマのモザイクは全部消えます（無処理になります）。" : `確定するとこのコマのモザイク ${n} / ${total} 個が消えます。`) + extra + scope
  };
}
function pairPartner(items, i) {
  const c = items[i];
  if (!c) return null;
  if (c.kind === "add") {
    const prev = items[i - 1];
    return prev && prev.kind === "remove" && prev.frame === c.frame ? i - 1 : null;
  }
  const next = items[i + 1];
  return next && next.kind === "add" && next.frame === c.frame ? i + 1 : null;
}
function correctionsAfterDrop(items, drop) {
  const gone = /* @__PURE__ */ new Set();
  for (const i of drop) {
    if (i < 0 || i >= items.length) continue;
    gone.add(i);
    const p = pairPartner(items, i);
    if (p !== null) gone.add(p);
  }
  if (!gone.size) return items.slice();
  const frames = /* @__PURE__ */ new Set();
  for (const i of gone) frames.add(items[i].frame);
  for (const f of frames) {
    const hadAdd = items.some((c) => c.frame === f && c.kind === "add");
    const rest = items.filter((c, i) => c.frame === f && !gone.has(i));
    if (!hadAdd || !rest.length || rest.some((c) => c.kind === "add")) continue;
    items.forEach((c, i) => {
      if (c.frame === f) gone.add(i);
    });
  }
  return items.filter((_, i) => !gone.has(i));
}
function firstUnjudged(items, from) {
  for (let i = from; i < items.length; i++) if (!items[i]?.verdict) return i;
  for (let i = 0; i < from; i++) if (!items[i]?.verdict) return i;
  return items.length ? items.length : 0;
}
function spanOptions(step) {
  return [
    { v: 0, label: "このコマだけ" },
    { v: step, label: `前後 ${step}` },
    { v: step * 3, label: `前後 ${step * 3}` }
  ];
}
function numOr(v, fallback) {
  const t = v.trim();
  if (t === "") return fallback;
  const n = Number(t);
  return Number.isFinite(n) ? n : fallback;
}
function progressPercent(p) {
  return p.total ? 100 * p.done / p.total : 0;
}
function requestWidth(videoWidth, screenWidth, dpr) {
  return Math.min(videoWidth, Math.max(480, Math.round(Math.min(screenWidth, 1280) * dpr)));
}
function intervalStatus(start, curFrame, endTapPlaced) {
  if (!start) {
    return { active: false, onStartFrame: false, banner: "", confirmDisabled: true };
  }
  const onStartFrame = curFrame !== null && curFrame === start.frame;
  const banner = endTapPlaced ? `区間: frame ${start.frame} 〜 frame ${curFrame}。O で確定（Esc でやめる）` : `始点は frame ${start.frame}。終点のコマへ移動してタップし、O で確定（Esc でやめる）`;
  return { active: true, onStartFrame, banner, confirmDisabled: !endTapPlaced };
}
function dragToInterval(startFrame, endFrame, markMode, hasTap) {
  const isDrag = startFrame !== endFrame;
  if (isDrag && markMode === "add" && hasTap) {
    return { intervalStart: { frame: startFrame }, previewFrame: endFrame };
  }
  return { intervalStart: null, previewFrame: endFrame };
}

// src/shared/geom.ts
function normPoint(x, y) {
  return [x, y];
}
function framePoint(x, y) {
  return [x, y];
}
function scaledSize(base, pct) {
  return [
    Math.max(8, Math.round(base[0] * pct / 100)),
    Math.max(8, Math.round(base[1] * pct / 100))
  ];
}
function tapToBox(p, size, width, height) {
  const w = Math.max(4, Math.min(width, size[0]));
  const h = Math.max(4, Math.min(height, size[1]));
  const cx = Math.min(Math.max(p[0], 0), 1) * width;
  const cy = Math.min(Math.max(p[1], 0), 1) * height;
  return [
    Math.min(Math.max(cx - w / 2, 0), width - w),
    Math.min(Math.max(cy - h / 2, 0), height - h),
    w,
    h
  ];
}
function normFromClient(rect, clientX, clientY) {
  return normPoint((clientX - rect.left) / rect.width, (clientY - rect.top) / rect.height);
}
function frameFromClient(rect, clientX, clientY, width, height) {
  return framePoint(
    (clientX - rect.left) / rect.width * width,
    (clientY - rect.top) / rect.height * height
  );
}

// src/shared/canvas-draw.ts
var SRC_NAME = {
  d: "検出",
  i: "補間",
  m: "memory",
  b: "橋渡し",
  x: "手修正"
};
function drawReviewOverlay(ctx, o) {
  ctx.clearRect(0, 0, o.width, o.height);
  const shrink = o.markMode === "shrink";
  const erase = o.markMode === "erase";
  if (!o.showBoxes && !o.pending && !o.startBox && !shrink && !erase) return;
  const lw = Math.max(2, Math.round(o.width / 400));
  if (erase) {
    const boxes = autoBoxes(o.boxes);
    const victims = eraseVictims(boxes, o.picked);
    boxes.forEach((r, i) => {
      const on = victims.has(i);
      const chosen = o.picked.includes(i);
      ctx.setLineDash(on && !chosen ? [lw * 4, lw * 2] : on ? [] : [lw * 3, lw * 3]);
      ctx.lineWidth = on ? lw * 2 : lw;
      if (on) {
        ctx.fillStyle = "rgba(224, 90, 86, .30)";
        ctx.fillRect(r[0], r[1], r[2], r[3]);
      }
      ctx.strokeStyle = on ? "#ff6b66" : "#98a2b0";
      ctx.strokeRect(r[0], r[1], r[2], r[3]);
      if (chosen) {
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
    ctx.lineWidth = lw * 1.5;
    for (const r of autoBoxes(o.boxes)) {
      ctx.fillStyle = "rgba(255, 165, 60, .22)";
      ctx.fillRect(r[0], r[1], r[2], r[3]);
      ctx.strokeStyle = "#ffa53c";
      ctx.strokeRect(r[0], r[1], r[2], r[3]);
    }
  } else if (o.showBoxes) {
    ctx.lineWidth = lw;
    for (const r of o.boxes) {
      ctx.strokeStyle = SRC_COLOR[r[4]] ?? "#ffffff";
      ctx.strokeRect(r[0], r[1], r[2], r[3]);
    }
  }
  if (o.pending) {
    ctx.lineWidth = lw * 1.5;
    ctx.setLineDash([lw * 4, lw * 3]);
    ctx.strokeStyle = "#ffffff";
    ctx.strokeRect(o.pending[0], o.pending[1], o.pending[2], o.pending[3]);
    ctx.setLineDash([]);
  }
  if (o.startBox) {
    ctx.lineWidth = lw * 1.5;
    ctx.strokeStyle = "#5ad1a0";
    ctx.strokeRect(o.startBox[0], o.startBox[1], o.startBox[2], o.startBox[3]);
  }
}
function drawRegionOverlay(ctx, o) {
  ctx.clearRect(0, 0, o.width, o.height);
  ctx.lineWidth = 2;
  ctx.font = "12px sans-serif";
  for (const r of o.regions) {
    const [x, y, w, h, src, score] = r;
    if (src === "d" && score < o.confMin) continue;
    ctx.strokeStyle = SRC_COLOR[src] ?? "#ffffff";
    ctx.strokeRect(x, y, w, h);
    ctx.fillStyle = ctx.strokeStyle;
    ctx.fillText(`${SRC_NAME[src] ?? src} ${score.toFixed(2)}`, x + 2, Math.max(12, y - 3));
  }
  if (o.pending) {
    ctx.setLineDash([6, 4]);
    ctx.strokeStyle = "#ffffff";
    ctx.strokeRect(o.pending[0], o.pending[1], o.pending[2], o.pending[3]);
    ctx.setLineDash([]);
  }
}
function worstCoverage(coverage, a, b, nFrames) {
  let worst = 1;
  for (let f = a; f < b && f < nFrames; f++) {
    const c = coverage.charCodeAt(f) - 48;
    if (c === 0) return 0;
    if (c === 2) worst = 2;
  }
  return worst;
}
function drawTimelineBand(ctx, o) {
  const { width: w, height: h, coverage: cov, nFrames: n } = o;
  ctx.fillStyle = "#101216";
  ctx.fillRect(0, 0, w, h);
  const bandH = h - 10;
  for (let px = 0; px < w; px++) {
    const a = Math.floor(px * n / w);
    const b = Math.max(a + 1, Math.floor((px + 1) * n / w));
    const worst = worstCoverage(cov, a, b, n);
    ctx.fillStyle = worst === 0 ? "#d0453e" : worst === 2 ? "#d9b73c" : "#3ba55d";
    ctx.fillRect(px, 0, 1, bandH);
  }
  ctx.fillStyle = "#e05a5a";
  for (const f of o.correctionFrames) {
    ctx.fillRect(Math.floor(f * w / n), bandH + 1, 2, 9);
  }
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(Math.floor(o.cur * w / n), 0, 1, h);
}
function drawHandOverlay(ctx, o) {
  ctx.clearRect(0, 0, o.width, o.height);
  const lw = Math.max(2, Math.round(o.width / 400));
  if (o.placed) {
    ctx.lineWidth = lw;
    ctx.strokeStyle = "#ff5a5a";
    ctx.strokeRect(o.placed[0], o.placed[1], o.placed[2], o.placed[3]);
  }
  if (o.pending) {
    ctx.lineWidth = lw * 1.5;
    ctx.setLineDash([lw * 4, lw * 3]);
    ctx.strokeStyle = "#ffffff";
    ctx.strokeRect(o.pending[0], o.pending[1], o.pending[2], o.pending[3]);
    ctx.setLineDash([]);
  }
}
function pixelSampleMaskInRange(frames, width, rangeStart, rangeEnd) {
  const mask = new Array(Math.max(0, width)).fill(false);
  const n = rangeEnd - rangeStart;
  if (width <= 0 || n <= 0) return mask;
  const sorted = [...frames].filter((f) => f >= rangeStart && f < rangeEnd).sort((a, b) => a - b);
  let i = 0;
  for (let px = 0; px < width; px++) {
    const aRel = Math.floor(px * n / width);
    const bRel = Math.max(aRel + 1, Math.floor((px + 1) * n / width));
    const a = rangeStart + aRel;
    const b = rangeStart + bRel;
    while (i < sorted.length && sorted[i] < a) i++;
    mask[px] = i < sorted.length && sorted[i] < b;
  }
  return mask;
}
function uncoveredPixelMask(frames, width, nFrames) {
  return pixelSampleMaskInRange(frames, width, 0, nFrames);
}
function playheadPixelInRange(cur, width, rangeStart, rangeEnd) {
  if (width <= 0) return 0;
  const n = rangeEnd - rangeStart;
  if (n <= 0) return 0;
  const c = Math.min(Math.max(cur, rangeStart), rangeEnd - 1);
  return Math.min(width - 1, Math.max(0, Math.floor((c - rangeStart) * width / n)));
}
function playheadPixel(cur, width, nFrames) {
  return playheadPixelInRange(cur, width, 0, nFrames);
}
function frameFromTrackXInRange(x, width, rangeStart, rangeEnd) {
  const n = rangeEnd - rangeStart;
  if (width <= 0 || n <= 0) return rangeStart;
  const f = rangeStart + Math.floor(x / width * n);
  return Math.min(rangeEnd - 1, Math.max(rangeStart, f));
}
function frameFromTrackX(x, width, nFrames) {
  return frameFromTrackXInRange(x, width, 0, nFrames);
}
function fullViewport(nFrames) {
  return { start: 0, end: Math.max(0, nFrames) };
}
function clampViewportStart(start, len, nFrames) {
  const maxStart = Math.max(0, nFrames - len);
  return Math.min(maxStart, Math.max(0, start));
}
function zoomLen(curLen, factor, nFrames) {
  if (nFrames <= 0) return 0;
  const len = Math.round(curLen * factor);
  return Math.min(nFrames, Math.max(1, len));
}
function zoomViewport(vp, factor, cur, nFrames) {
  const curLen = vp.end - vp.start;
  const len = zoomLen(curLen, factor, nFrames);
  const start = clampViewportStart(cur - Math.floor(len / 2), len, nFrames);
  return { start, end: start + len };
}
function followPlayhead(vp, cur, nFrames) {
  const len = vp.end - vp.start;
  if (len <= 0) return vp;
  if (cur >= vp.start && cur < vp.end) return vp;
  const start = clampViewportStart(cur - Math.floor(len / 2), len, nFrames);
  return { start, end: start + len };
}
function frameRangeToPixels(a, b, width, viewStart, viewEnd) {
  if (width <= 0 || viewEnd <= viewStart) return null;
  const lo = Math.min(a, b);
  const hi = Math.max(a, b);
  if (hi < viewStart || lo >= viewEnd) return null;
  const clo = Math.max(lo, viewStart);
  const chi = Math.min(hi, viewEnd - 1);
  const n = viewEnd - viewStart;
  const px0 = Math.floor((clo - viewStart) * width / n);
  const px1 = Math.max(px0 + 1, Math.floor((chi - viewStart + 1) * width / n));
  return [Math.max(0, px0), Math.min(width, px1)];
}
function drawQueueTrack(ctx, o) {
  const { width: w, height: h, nFrames: n } = o;
  ctx.fillStyle = "#101216";
  ctx.fillRect(0, 0, w, h);
  if (w <= 0 || n <= 0) return;
  const start = o.viewStart ?? 0;
  const end = o.viewEnd ?? n;
  if (end <= start) return;
  const mask = pixelSampleMaskInRange(o.uncoveredFrames, w, start, end);
  for (let px = 0; px < w; px++) {
    ctx.fillStyle = mask[px] ? "#d0453e" : "#3ba55d";
    ctx.fillRect(px, 0, 1, h);
  }
  if (o.correctionFrames?.length) {
    const cmask = pixelSampleMaskInRange(o.correctionFrames, w, start, end);
    ctx.fillStyle = "#5ad1a0";
    for (let px = 0; px < w; px++) {
      if (cmask[px]) ctx.fillRect(px, Math.max(0, h - 6), 1, Math.min(6, h));
    }
  }
  if (o.interval) {
    const rng = frameRangeToPixels(o.interval.start, o.interval.end ?? o.interval.start, w, start, end);
    if (rng) {
      ctx.fillStyle = "rgba(90, 209, 160, .35)";
      ctx.fillRect(rng[0], 0, rng[1] - rng[0], h);
    }
  }
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(playheadPixelInRange(o.cur, w, start, end), 0, 2, h);
}
function drawOverviewTrack(ctx, o) {
  const { width: w, height: h, nFrames: n } = o;
  ctx.fillStyle = "#101216";
  ctx.fillRect(0, 0, w, h);
  if (w <= 0 || n <= 0) return;
  const mask = uncoveredPixelMask(o.uncoveredFrames, w, n);
  for (let px = 0; px < w; px++) {
    ctx.fillStyle = mask[px] ? "#d0453e" : "#3ba55d";
    ctx.fillRect(px, 0, 1, h);
  }
  const rng = frameRangeToPixels(o.viewStart, o.viewEnd - 1, w, 0, n);
  if (rng) {
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 1;
    const rw = Math.max(1, rng[1] - rng[0] - 1);
    ctx.strokeRect(rng[0] + 0.5, 0.5, rw, Math.max(1, h - 1));
  }
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(playheadPixel(o.cur, w, n), 0, 1, h);
}

// src/shared/job-logic.ts
function proxyLabel(status) {
  switch (status) {
    case "generating":
      return "生成中";
    case "done":
      return "完成";
    case "failed":
      return "失敗";
    default:
      return "未生成";
  }
}

// src/shared/keymap.ts
function bind(action, keys, label, desc, shift = false) {
  return { action, keys, label, desc, shift };
}
var TYPING_TAGS = /* @__PURE__ */ new Set(["INPUT", "SELECT", "TEXTAREA"]);
function isTypingTarget(t) {
  if (t.targetTag && TYPING_TAGS.has(t.targetTag)) return true;
  if (t.targetEditable) return true;
  return false;
}
function resolveKey(bindings, ev) {
  if (isTypingTarget(ev)) return null;
  const shift = !!ev.shiftKey;
  for (const b of bindings) {
    if (!!b.shift !== shift) continue;
    if (b.keys.includes(ev.key)) return b.action;
  }
  return null;
}
function dispatchKey(bindings, ev, handlers) {
  const action = resolveKey(bindings, ev);
  if (!action) return false;
  const fn = handlers[action];
  if (!fn) return false;
  fn();
  return true;
}
var STEP_BACK = bind("stepBack", ["ArrowLeft", ","], "← / ,", "1フレーム戻る");
var STEP_FWD = bind("stepForward", ["ArrowRight", "."], "→ / .", "1フレーム進む");
var JUMP_BACK = bind("jumpBack", ["ArrowLeft"], "Shift+←", "大きく戻る", true);
var JUMP_FWD = bind("jumpForward", ["ArrowRight"], "Shift+→", "大きく進む", true);
var PLAY_TOGGLE = bind("playToggle", [" "], "Space", "再生 / 停止");
var GO_HOME = bind("goHome", ["Home"], "Home", "先頭のフレームへ");
var GO_END = bind("goEnd", ["End"], "End", "末尾のフレームへ");
var SHUTTLE_REV = bind("shuttleReverse", ["j", "J"], "J", "逆再生（連打で加速）");
var SHUTTLE_STOP = bind("shuttleStop", ["k", "K"], "K", "停止");
var SHUTTLE_FWD = bind("shuttleForward", ["l", "L"], "L", "順再生（連打で加速）");
var HELP = bind("help", ["?"], "?", "このキー一覧を出す");
var CORE_TRANSPORT = [
  STEP_BACK,
  STEP_FWD,
  PLAY_TOGGLE,
  GO_HOME,
  GO_END,
  SHUTTLE_REV,
  SHUTTLE_STOP,
  SHUTTLE_FWD,
  HELP
];
var JUMP = [JUMP_BACK, JUMP_FWD];
var TL_ADD_MODE = bind("addMode", ["m", "M"], "M", "追加モード（矩形を置く）");
var TL_APPLY_FRAME = bind("applyFrame", ["Enter"], "Enter", "置いた矩形をこのフレームだけに適用");
var TL_APPLY_SPAN = bind(
  "applySpan",
  ["Enter"],
  "Shift+Enter",
  "置いた矩形を指定フレーム数ぶん適用",
  true
);
var TL_DELETE_HERE = bind("deleteHere", ["d", "D"], "D", "カーソル下の手修正を削除");
var TL_NEXT_ESTIMATED = bind("nextEstimated", ["g", "G"], "G", "次の推定のみ区間へ");
var TL_SIZE_SMALLER = bind("sizeSmaller", ["["], "[", "矩形を縮小");
var TL_SIZE_BIGGER = bind("sizeBigger", ["]"], "]", "矩形を拡大");
var TIMELINE_KEYS = [
  ...CORE_TRANSPORT,
  ...JUMP,
  TL_ADD_MODE,
  TL_APPLY_FRAME,
  TL_APPLY_SPAN,
  TL_DELETE_HERE,
  TL_NEXT_ESTIMATED,
  TL_SIZE_SMALLER,
  TL_SIZE_BIGGER
];
var FRAMESTEP_KEYS = [...CORE_TRANSPORT, ...JUMP];
var DRAW_CONFIRM = bind("confirmTap", ["Enter"], "Enter", "タップした位置に打点を置く");
var DRAW_ABSENT = bind("markAbsent", ["n", "N"], "N", "「ここには無い」として記録");
var DRAW_KEYS = [
  ...CORE_TRANSPORT,
  ...JUMP,
  DRAW_CONFIRM,
  DRAW_ABSENT
];
var RV_JUDGE_OK = bind("judgeOk", ["1"], "1", "問題なし");
var RV_JUDGE_ADD = bind("judgeAdd", ["2"], "2", "漏れている（追加モード）");
var RV_JUDGE_UNSURE = bind("judgeUnsure", ["3"], "3", "判断できない");
var RV_JUDGE_SHRINK = bind("judgeShrink", ["4"], "4", "でかすぎる");
var RV_JUDGE_ERASE = bind("judgeErase", ["5"], "5", "誤検知");
var RV_UNDO = bind("undo", ["u", "U"], "U", "ひとつ戻す");
var RV_CANCEL = bind("cancel", ["Escape"], "Esc", "モードをやめる");
var RV_QUEUE_PREV = bind("queuePrev", ["PageUp"], "PageUp", "検査キューの前の項目へ");
var RV_QUEUE_NEXT = bind("queueNext", ["PageDown"], "PageDown", "検査キューの次の項目へ");
var REVIEW_KEYS = [
  ...CORE_TRANSPORT,
  ...JUMP,
  RV_JUDGE_OK,
  RV_JUDGE_ADD,
  RV_JUDGE_UNSURE,
  RV_JUDGE_SHRINK,
  RV_JUDGE_ERASE,
  RV_UNDO,
  RV_CANCEL,
  RV_QUEUE_PREV,
  RV_QUEUE_NEXT
];
var TRACK_ZOOM_IN = bind("trackZoomIn", ["=", "+"], "+", "トラックを拡大");
var TRACK_ZOOM_OUT = bind("trackZoomOut", ["-", "_"], "-", "トラックを縮小");
var TRACK_ZOOM_FIT = bind("trackZoomFit", ["0"], "0", "トラックの拡大を戻す（全体表示）");
var TRACK_ZOOM_KEYS = [
  TRACK_ZOOM_IN,
  TRACK_ZOOM_OUT,
  TRACK_ZOOM_FIT
];
var RV_INTERVAL_START = bind("intervalStart", ["i", "I"], "I", "区間の始点を置く");
var RV_INTERVAL_END = bind("intervalEnd", ["o", "O"], "O", "区間の終点（確定）");
var REVIEW_INTERVAL_KEYS = [RV_INTERVAL_START, RV_INTERVAL_END];
function helpRows(bindings) {
  const seen = /* @__PURE__ */ new Set();
  const rows = [];
  for (const b of bindings) {
    const k = b.action + (b.shift ? "!shift" : "");
    if (seen.has(k)) continue;
    seen.add(k);
    rows.push({ label: b.label, desc: b.desc });
  }
  return rows;
}
function helpLine(bindings) {
  return helpRows(bindings).map((r) => `${r.label} ${r.desc}`).join(" ・ ");
}

// src/shared/shuttle.ts
var SHUTTLE_MAX = 8;
function nextShuttleSpeed(cur, dir) {
  if (dir === 0) return 0;
  if (cur === 0) return dir;
  if (Math.sign(cur) !== dir) return dir;
  const doubled = Math.abs(cur) * 2;
  return dir * Math.min(doubled, SHUTTLE_MAX);
}

// src/shared/web-build.ts
var BUILD_ID = /^[0-9a-f]{64}$/;
function shownBuildId(value) {
  return typeof value === "string" && BUILD_ID.test(value) ? value.slice(0, 12) : "取得不能";
}
function webBuildProblem(frontendId, serverId) {
  if (typeof frontendId === "string" && typeof serverId === "string" && BUILD_ID.test(frontendId) && frontendId === serverId) {
    return null;
  }
  return `画面とサーバのバージョンが一致しないか、確認できません（画面 ${shownBuildId(frontendId)} / サーバ ${shownBuildId(serverId)}）。誤った API へ操作を送らないため、この画面の操作を停止しました。処理中のジョブが無いことを確認してからサーバを再起動し、画面を再読み込みしてください。`;
}
async function withMatchingWebBuild(problem, operation) {
  const message = await problem;
  if (message) throw new Error(message);
  return await operation();
}
export {
  CORE_TRANSPORT,
  DRAW_ABSENT,
  DRAW_CONFIRM,
  DRAW_KEYS,
  FRAMESTEP_KEYS,
  GO_END,
  GO_HOME,
  HELP,
  JUMP,
  JUMP_BACK,
  JUMP_FWD,
  MARK_MODES,
  PLAY_TOGGLE,
  REVIEW_INTERVAL_KEYS,
  REVIEW_KEYS,
  RV_CANCEL,
  RV_INTERVAL_END,
  RV_INTERVAL_START,
  RV_JUDGE_ADD,
  RV_JUDGE_ERASE,
  RV_JUDGE_OK,
  RV_JUDGE_SHRINK,
  RV_JUDGE_UNSURE,
  RV_QUEUE_NEXT,
  RV_QUEUE_PREV,
  RV_UNDO,
  SHUTTLE_FWD,
  SHUTTLE_MAX,
  SHUTTLE_REV,
  SHUTTLE_STOP,
  SRC_COLOR,
  SRC_NAME,
  STEP_BACK,
  STEP_FWD,
  TIMELINE_KEYS,
  TL_ADD_MODE,
  TL_APPLY_FRAME,
  TL_APPLY_SPAN,
  TL_DELETE_HERE,
  TL_NEXT_ESTIMATED,
  TL_SIZE_BIGGER,
  TL_SIZE_SMALLER,
  TRACK_ZOOM_FIT,
  TRACK_ZOOM_IN,
  TRACK_ZOOM_KEYS,
  TRACK_ZOOM_OUT,
  VERDICT_LABEL,
  autoBoxes,
  clampViewportStart,
  correctionsAfterDrop,
  dispatchKey,
  dragToInterval,
  drawHandOverlay,
  drawOverviewTrack,
  drawQueueTrack,
  drawRegionOverlay,
  drawReviewOverlay,
  drawTimelineBand,
  eraseSummary,
  eraseVictims,
  firstUnjudged,
  followPlayhead,
  frameFromClient,
  frameFromTrackX,
  frameFromTrackXInRange,
  framePoint,
  frameRangeToPixels,
  fullViewport,
  helpLine,
  helpRows,
  intervalStatus,
  isTypingTarget,
  nextShuttleSpeed,
  normFromClient,
  normPoint,
  numOr,
  overlaps,
  pickIndexAt,
  pixelSampleMaskInRange,
  playheadPixel,
  playheadPixelInRange,
  progressPercent,
  proxyLabel,
  requestWidth,
  resolveKey,
  scaledSize,
  spanOptions,
  tapToBox,
  togglePick,
  uncoveredPixelMask,
  webBuildProblem,
  withMatchingWebBuild,
  worstCoverage,
  zoomLen,
  zoomViewport
};
