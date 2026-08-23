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
  if (!o.showBoxes && !o.pending && !shrink && !erase) return;
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
export {
  MARK_MODES,
  SRC_COLOR,
  SRC_NAME,
  VERDICT_LABEL,
  autoBoxes,
  correctionsAfterDrop,
  drawHandOverlay,
  drawRegionOverlay,
  drawReviewOverlay,
  drawTimelineBand,
  eraseSummary,
  eraseVictims,
  firstUnjudged,
  frameFromClient,
  framePoint,
  normFromClient,
  normPoint,
  numOr,
  overlaps,
  pickIndexAt,
  progressPercent,
  requestWidth,
  scaledSize,
  spanOptions,
  tapToBox,
  togglePick,
  worstCoverage
};
