// 画面ロジックの検査。ブラウザが使えないので node から直接動かす。
//
//   node tests/test_frontend.mjs
//
// 見ているのは frontend/build/logic.mjs。DOM にもフレームワークにも
// 依存しない部分だけを束ねたもので、npm が無くても回る（ビルド結果を
// リポジトリに入れてある）。画面部品は Preact に載っているが、
// 「押した結果として何が選ばれ、確定ボタンに何が出るか」はこちらにある。
//
// 誤検知モードを重点的に見ているのは、消す方向の操作だからで、
// 「押した覚えのないものが消える」が起きると気づけないため。

import { fileURLToPath } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const LOGIC = path.resolve(HERE, "..", "frontend", "build", "logic.mjs");

const L = await import("file://" + LOGIC.replace(/\\/g, "/"));

let fails = 0;
let count = 0;
function ok(cond, msg) {
  count++;
  if (cond) {
    console.log("  OK   " + msg);
  } else {
    console.log("  FAIL " + msg);
    fails++;
  }
}
function section(name) {
  console.log("\n" + name);
}

// 動画は 640x480、既定の矩形は 64x64 とする
const W = 640;
const H = 480;

// --------------------------------------------------------------------
section("自動領域の絞り込み");
// --------------------------------------------------------------------
{
  const boxes = [
    [100, 100, 50, 50, "d", 0.9],
    [300, 300, 50, 50, "i", 0.4],
    [10, 10, 20, 20, "x", 1.0],
  ];
  const autos = L.autoBoxes(boxes);
  ok(autos.length === 2, "手修正（x）は自動領域に数えない");
  ok(L.autoBoxes(undefined).length === 0, "矩形が無いコマでも落ちない");
}

// --------------------------------------------------------------------
section("重なりの判定");
// --------------------------------------------------------------------
{
  ok(L.overlaps([0, 0, 10, 10], [5, 5, 10, 10]), "重なっていれば true");
  ok(!L.overlaps([0, 0, 10, 10], [10, 0, 10, 10]), "辺が接しているだけは重なりとみなさない");
  ok(!L.overlaps([0, 0, 10, 10], [11, 11, 10, 10]), "離れていれば false");
}

// --------------------------------------------------------------------
section("誤検知モード: 選択と巻き添え");
// --------------------------------------------------------------------
{
  // 離れた2つ。選んだほうだけが消える
  const boxes = L.autoBoxes([
    [100, 100, 50, 50, "d", 0.9],
    [300, 300, 50, 50, "i", 0.4],
  ]);

  let picked = [];
  let s = L.eraseSummary(boxes, picked, 0);
  ok(s.confirmDisabled && s.confirmLabel === "消す枠をタップしてください", "未選択では確定できない");
  ok(s.banner === L.MARK_MODES.erase.hint, "未選択のときは押す場所の案内が出る");

  const i0 = L.pickIndexAt(boxes, 125, 125);
  ok(i0 === 0, "枠の中をタップすればその枠が選ばれる");
  picked = L.togglePick(picked, i0);
  ok(picked.join() === "0", "選択に入る");
  ok(L.eraseVictims(boxes, picked).size === 1, "離れた枠は巻き添えにならない");

  s = L.eraseSummary(boxes, picked, 0);
  ok(!s.confirmDisabled && s.confirmLabel === "これを消す", "確定ボタンの文言");
  ok(!s.all, "全部消えるわけではない");
  ok(s.banner.includes("1 / 2 個が消えます"), "消える数を言葉でも出す: " + s.banner);

  picked = L.togglePick(picked, i0);
  ok(picked.length === 0, "もう一度タップで選択を外せる");

  ok(L.pickIndexAt(boxes, 600, 20) === null, "枠の外のタップでは何も選ばれない");

  picked = L.togglePick(L.togglePick([], 0), 1);
  s = L.eraseSummary(boxes, picked, 0);
  ok(picked.length === 2, "2つとも選べる");
  ok(s.all, "全部選ぶと all が立つ");
  ok(s.confirmLabel === "このコマは無処理になる", "全部消えるときの文言");
}

{
  // 重なった2つ。1つ選ぶと両方消える（corrections.apply がそう動く）
  const boxes = L.autoBoxes([
    [100, 100, 50, 50, "i", 0.4],
    [110, 105, 50, 50, "m", 0.3],
  ]);
  const i = L.pickIndexAt(boxes, 105, 102);
  const picked = L.togglePick([], i);
  ok(picked.length === 1, "選んだのは1つ");
  ok(L.eraseVictims(boxes, picked).size === 2, "重なっている枠も消える側に数える");
  const s = L.eraseSummary(boxes, picked, 0);
  ok(s.confirmLabel === "このコマは無処理になる", "巻き添えで全部消えることが出る");
  ok(s.banner.includes("重なっている枠"), "巻き添えを言葉でも出す: " + s.banner);
}

{
  // 入れ子。小さいほうが選ばれる。大きい枠は外側をタップすれば選べる
  const boxes = L.autoBoxes([
    [100, 100, 200, 200, "i", 0.4],
    [150, 150, 40, 40, "d", 0.9],
  ]);
  ok(L.pickIndexAt(boxes, 170, 170) === 1, "入れ子では小さい枠が選ばれる");
  ok(L.pickIndexAt(boxes, 110, 110) === 0, "外側をタップすれば大きい枠が選べる");
}

{
  // 前後へ広げるときは、消えるのが1コマではないことを書く
  const boxes = L.autoBoxes([[100, 100, 50, 50, "d", 0.9]]);
  const s = L.eraseSummary(boxes, [0], 5);
  ok(s.banner.includes("前後 5 コマ"), "適用範囲を広げていることが出る: " + s.banner);
  const s0 = L.eraseSummary(boxes, [0], 0);
  ok(!s0.banner.includes("前後"), "このコマだけなら前後の話は出さない");
}

// --------------------------------------------------------------------
section("座標変換: 正規化タップ -> フレーム座標の矩形");
// --------------------------------------------------------------------
{
  // review.py の tap_to_box と同じ規則であること
  const b = L.tapToBox(L.normPoint(0.5, 0.5), [64, 64], W, H);
  ok(b.join() === [288, 208, 64, 64].join(), "中央のタップは中心に置かれる: " + b.join());

  // 端に寄ったタップでは矩形を切り詰めずに内側へ押し戻す。
  // 切り詰めると画面端の対象を覆いきれず「押したのに漏れたまま」になる
  const l = L.tapToBox(L.normPoint(0, 0.5), [64, 64], W, H);
  ok(l[0] === 0 && l[2] === 64, "左端でも大きさは変わらない: " + l.join());
  const r = L.tapToBox(L.normPoint(1, 1), [64, 64], W, H);
  ok(r[0] === W - 64 && r[1] === H - 64, "右下でも枠は画面内に収まる: " + r.join());

  // 範囲外の値でも 0..1 に丸めてから使う
  const o = L.tapToBox(L.normPoint(-1, 2), [64, 64], W, H);
  ok(o[0] === 0 && o[1] === H - 64, "0..1 の外は丸める: " + o.join());

  // 動画より大きい矩形は動画に収める
  const big = L.tapToBox(L.normPoint(0.5, 0.5), [9999, 9999], W, H);
  ok(big[2] === W && big[3] === H, "動画より大きい矩形は動画の大きさに収まる");
}

{
  const rect = { left: 0, top: 0, width: 360, height: 270 };
  const p = L.normFromClient(rect, 180, 135);
  ok(Math.abs(p[0] - 0.5) < 1e-9 && Math.abs(p[1] - 0.5) < 1e-9, "画面中央は 0.5, 0.5");
  const f = L.frameFromClient(rect, 180, 135, W, H);
  ok(f[0] === 320 && f[1] === 240, "同じ点をフレーム座標にすると 320, 240");
  ok(p[0] !== f[0], "正規化座標とフレーム座標は別物（型でも区別している）");
}

// --------------------------------------------------------------------
section("矩形の伸縮");
// --------------------------------------------------------------------
{
  ok(L.scaledSize([64, 64], 100).join() === "64,64", "100% は等倍");
  ok(L.scaledSize([64, 64], 200).join() === "128,128", "200% で倍");
  ok(L.scaledSize([64, 64], 1).join() === "8,8", "小さすぎる指定でも 8px を下回らない");
}

// --------------------------------------------------------------------
section("キューの進み方");
// --------------------------------------------------------------------
{
  const items = [
    { verdict: "ok" },
    { verdict: null },
    { verdict: "fixed" },
    { verdict: null },
  ];
  ok(L.firstUnjudged(items, 0) === 1, "先頭から探すと最初の未判定");
  ok(L.firstUnjudged(items, 2) === 3, "途中から探すとその先の未判定");
  // 末尾まで行ったら先頭へ回る。飛ばした未判定を置き去りにしない
  ok(L.firstUnjudged(items, 4) === 1, "末尾まで来たら先頭へ回る");
  ok(L.firstUnjudged([{ verdict: "ok" }], 0) === 1, "全部判定済みなら末尾（見終わり）を指す");
  ok(L.firstUnjudged([], 0) === 0, "対象が無ければ 0");
}

// --------------------------------------------------------------------
section("適用範囲の選択肢");
// --------------------------------------------------------------------
{
  const o = L.spanOptions(5);
  ok(o.length === 3, "3択");
  ok(o[0].v === 0 && o[1].v === 5 && o[2].v === 15, "0 / 間引き幅 / その3倍");
  ok(o[1].label === "前後 5", "既定は間引き幅ぶん: " + o[1].label);
}

// --------------------------------------------------------------------
section("進捗と要求解像度");
// --------------------------------------------------------------------
{
  ok(L.progressPercent({ total: 0, done: 0 }) === 0, "対象が0件でも 0 で割らない");
  ok(L.progressPercent({ total: 4, done: 1 }) === 25, "1/4 は 25%");

  // 原寸を投げさせない。ただし小さい画面でも 480px は確保する
  ok(L.requestWidth(1920, 360, 2) === 720, "画面幅x2 を要求する");
  ok(L.requestWidth(1920, 200, 1) === 480, "小さい画面でも 480 は下回らない");
  ok(L.requestWidth(640, 1440, 2) === 640, "動画の原寸を超えては要求しない");
  ok(L.requestWidth(4096, 1440, 2) === 2560, "上限 1280 x dpr で頭打ち");
}

// --------------------------------------------------------------------
section("タイムラインの帯: 1px に何十フレームも入るとき");
// --------------------------------------------------------------------
{
  // 平均を取ると単発の素通しが消える。いちばん悪い状態を出す
  ok(L.worstCoverage("1111111111", 0, 10, 10) === 1, "全部被覆ありなら 1");
  ok(L.worstCoverage("1111211111", 0, 10, 10) === 2, "推定のみが1つでもあれば 2");
  ok(L.worstCoverage("1111201111", 0, 10, 10) === 0, "素通しが1つでもあれば 0");
  ok(L.worstCoverage("0111111111", 5, 10, 10) === 1, "範囲外は見ない");
  ok(L.worstCoverage("11", 0, 10, 2) === 1, "長さを超えて読まない");
}

// --------------------------------------------------------------------
section("重ね描き: 何を描くかの分岐");
// --------------------------------------------------------------------
{
  // canvas は無いので、呼ばれた命令を記録する薄い代役を立てる
  function fakeCtx() {
    const calls = [];
    const rec = (name) => (...a) => calls.push([name, ...a]);
    return {
      calls,
      clearRect: rec("clearRect"),
      fillRect: rec("fillRect"),
      strokeRect: rec("strokeRect"),
      setLineDash: rec("setLineDash"),
      beginPath: rec("beginPath"),
      moveTo: rec("moveTo"),
      lineTo: rec("lineTo"),
      stroke: rec("stroke"),
      fillText: rec("fillText"),
      lineWidth: 0,
      fillStyle: "",
      strokeStyle: "",
      font: "",
    };
  }
  const n = (ctx, name) => ctx.calls.filter((c) => c[0] === name).length;

  const boxes = [
    [100, 100, 50, 50, "d", 0.9],
    [300, 300, 50, 50, "i", 0.4],
    [10, 10, 20, 20, "x", 1.0],
  ];

  let ctx = fakeCtx();
  L.drawReviewOverlay(ctx, {
    width: W, height: H, boxes, showBoxes: false, markMode: null, picked: [], pending: null,
  });
  ok(n(ctx, "strokeRect") === 0, "枠の表示を切っていれば何も描かない");
  ok(n(ctx, "clearRect") === 1, "それでも消しはする");

  ctx = fakeCtx();
  L.drawReviewOverlay(ctx, {
    width: W, height: H, boxes, showBoxes: true, markMode: null, picked: [], pending: null,
  });
  ok(n(ctx, "strokeRect") === 3, "枠の表示が入っていれば手修正も含めて全部描く");

  // 「でかすぎる」「誤検知」では枠の表示設定に関係なく自動領域を出す。
  // 何を消そうとしているのか分からないまま確定させないため
  ctx = fakeCtx();
  L.drawReviewOverlay(ctx, {
    width: W, height: H, boxes, showBoxes: false, markMode: "shrink", picked: [], pending: null,
  });
  ok(n(ctx, "strokeRect") === 2, "狭めるモードでは自動領域だけを必ず出す");
  ok(n(ctx, "fillRect") === 2, "薄く塗って大きさを読み取れるようにする");

  ctx = fakeCtx();
  L.drawReviewOverlay(ctx, {
    width: W, height: H, boxes, showBoxes: false, markMode: "erase", picked: [0], pending: null,
  });
  ok(n(ctx, "strokeRect") === 2, "誤検知モードでも自動領域だけを必ず出す");
  ok(n(ctx, "fillRect") === 1, "消える枠だけ塗る");
  ok(n(ctx, "stroke") === 1, "選んだ枠には消える印（バツ）を入れる");

  // 重なっているときは、選んでいない枠も塗られる（巻き添えが見える）
  ctx = fakeCtx();
  L.drawReviewOverlay(ctx, {
    width: W, height: H,
    boxes: [[100, 100, 50, 50, "i", 0.4], [110, 105, 50, 50, "m", 0.3]],
    showBoxes: false, markMode: "erase", picked: [0], pending: null,
  });
  ok(n(ctx, "fillRect") === 2, "巻き添えで消える枠も塗って見せる");
  ok(n(ctx, "stroke") === 1, "バツが付くのは自分で選んだ枠だけ");

  // 置いた矩形は枠の表示を切っていても出す
  ctx = fakeCtx();
  L.drawReviewOverlay(ctx, {
    width: W, height: H, boxes, showBoxes: false, markMode: "add", picked: [],
    pending: [10, 20, 30, 40],
  });
  ok(n(ctx, "strokeRect") === 1, "置いた矩形は枠の表示に関係なく描く");

  // 区間の始点（issue #46）。枠の表示を切っていても、始点はそのコマにいる
  // あいだ見えていないと「区間を張ったつもりが見えない」になる
  ctx = fakeCtx();
  L.drawReviewOverlay(ctx, {
    width: W, height: H, boxes: [], showBoxes: false, markMode: "add", picked: [],
    pending: null, startBox: [5, 5, 40, 40],
  });
  ok(n(ctx, "strokeRect") === 1, "区間の始点は枠表示を切っていても描く");

  // startBox が無ければ何も足されない（他のケースの strokeRect 件数を変えない）
  ctx = fakeCtx();
  L.drawReviewOverlay(ctx, {
    width: W, height: H, boxes, showBoxes: true, markMode: null, picked: [], pending: null,
  });
  ok(n(ctx, "strokeRect") === 3, "startBox を渡さなければ増えない");
}

// --------------------------------------------------------------------
section("区間追従（issue #46）: 始点・終点の案内");
// --------------------------------------------------------------------
{
  let s = L.intervalStatus(null, 10, false);
  ok(!s.active, "始点が無ければ非アクティブ");
  ok(s.confirmDisabled, "始点が無ければ確定できない");

  s = L.intervalStatus({ frame: 10 }, 10, false);
  ok(s.active, "始点があればアクティブ");
  ok(s.onStartFrame, "始点そのもののコマにいることが分かる");
  ok(s.confirmDisabled, "終点のタップが無ければ確定できない（漏れを防ぐ）");
  ok(s.banner.includes("frame 10"), "始点のフレーム番号を案内に出す: " + s.banner);

  s = L.intervalStatus({ frame: 10 }, 40, true);
  ok(!s.onStartFrame, "終点のコマでは始点フレームと一致しない");
  ok(!s.confirmDisabled, "終点をタップしていれば確定できる");
  ok(s.banner.includes("frame 10") && s.banner.includes("frame 40"),
     "始点と終点の両方のフレーム番号が案内に出る: " + s.banner);
}

{
  // タイムライン画面の重ね描き。信頼度スライダで検出だけを隠す
  const calls = [];
  const rec = (name) => (...a) => calls.push([name, ...a]);
  const ctx = {
    clearRect: rec("clearRect"), strokeRect: rec("strokeRect"), fillRect: rec("fillRect"),
    fillText: rec("fillText"), setLineDash: rec("setLineDash"),
    lineWidth: 0, font: "", fillStyle: "", strokeStyle: "",
  };
  L.drawRegionOverlay(ctx, {
    width: W, height: H,
    regions: [
      [0, 0, 10, 10, "d", 0.10],
      [0, 0, 10, 10, "d", 0.90],
      [0, 0, 10, 10, "i", 0.10],
    ],
    pending: null,
    confMin: 0.5,
  });
  const drawn = calls.filter((c) => c[0] === "strokeRect").length;
  ok(drawn === 2, "しきい値未満の検出だけ隠れる（推定は隠さない）: " + drawn);
}

// --------------------------------------------------------------------
section("プロキシ動画の状態表示（issue #18 / job.tsx）");
// --------------------------------------------------------------------
{
  // 「まだ無い」（null）と「作れなかった」（failed）を画面が区別することが
  // 完了条件そのものなので、4状態それぞれで違う文字列が出ることを見る。
  // 単に truthy かどうかを見るテストだと、どれも同じ文字列を返す実装でも
  // 通ってしまうので、4値を互いに突き合わせる
  const labels = {
    null: L.proxyLabel(null),
    generating: L.proxyLabel("generating"),
    done: L.proxyLabel("done"),
    failed: L.proxyLabel("failed"),
  };
  ok(labels.null === "未生成", "未生成（null）: " + labels.null);
  ok(labels.generating === "生成中", "生成中: " + labels.generating);
  ok(labels.done === "完成", "完成: " + labels.done);
  ok(labels.failed === "失敗", "失敗: " + labels.failed);
  const set = new Set(Object.values(labels));
  ok(set.size === 4, "4状態がすべて異なる文字列になる（同じ表示にしない）");
}

// --------------------------------------------------------------------
section("キーマップ（issue #79）: 入力欄の除外");
// --------------------------------------------------------------------
{
  ok(L.isTypingTarget({ key: "a", targetTag: "INPUT" }), "INPUT は入力欄扱い");
  ok(L.isTypingTarget({ key: "a", targetTag: "SELECT" }), "SELECT は入力欄扱い");
  ok(L.isTypingTarget({ key: "a", targetTag: "TEXTAREA" }), "TEXTAREA は入力欄扱い（元は抜けていた）");
  ok(L.isTypingTarget({ key: "a", targetEditable: true }), "contenteditable は入力欄扱い（元は抜けていた）");
  ok(!L.isTypingTarget({ key: "a", targetTag: "DIV" }), "普通の要素は入力欄ではない");
  ok(!L.isTypingTarget({ key: "a" }), "target 情報が無ければ入力欄ではないとみなす");
}

// --------------------------------------------------------------------
section("キーマップ（issue #79）: キー -> アクションの解決");
// --------------------------------------------------------------------
{
  // ← / → / , / . はどれも「1フレーム移動」の同じアクションを指す。
  // ここが画面ごとに割れていたのが issue #79 の本体
  ok(L.resolveKey(L.TIMELINE_KEYS, { key: "ArrowLeft" }) === "stepBack", "← は stepBack");
  ok(L.resolveKey(L.TIMELINE_KEYS, { key: "," }) === "stepBack", ", も stepBack（同じアクション）");
  ok(L.resolveKey(L.TIMELINE_KEYS, { key: "ArrowRight" }) === "stepForward", "→ は stepForward");
  ok(L.resolveKey(L.TIMELINE_KEYS, { key: "." }) === "stepForward", ". も stepForward（同じアクション）");

  // Shift 併用は別アクション（大きく飛ぶ）。Shift の有無で別物になることを見る
  ok(L.resolveKey(L.TIMELINE_KEYS, { key: "ArrowLeft", shiftKey: true }) === "jumpBack",
     "Shift+← は jumpBack（stepBack ではない）");
  ok(L.resolveKey(L.TIMELINE_KEYS, { key: "ArrowRight", shiftKey: true }) === "jumpForward",
     "Shift+→ は jumpForward");
  ok(L.resolveKey(L.TIMELINE_KEYS, { key: "ArrowLeft" }) !== "jumpBack",
     "Shift 無しでは jumpBack にならない");

  // 入力欄にフォーカスがあれば、キーが一致していても何も返さない
  ok(L.resolveKey(L.TIMELINE_KEYS, { key: "ArrowLeft", targetTag: "INPUT" }) === null,
     "INPUT にフォーカス中はキーを拾わない");
  ok(L.resolveKey(L.TIMELINE_KEYS, { key: "ArrowLeft", targetEditable: true }) === null,
     "contenteditable にフォーカス中はキーを拾わない");

  // 割り当てにないキーは null
  ok(L.resolveKey(L.TIMELINE_KEYS, { key: "Q" }) === null, "割り当てのないキーは null");
}

// --------------------------------------------------------------------
section("キーマップ（issue #79）: dispatchKey はハンドラを1回だけ呼ぶ");
// --------------------------------------------------------------------
{
  let calls = 0;
  const handled = L.dispatchKey(L.TIMELINE_KEYS, { key: "ArrowLeft" }, {
    stepBack: () => { calls++; },
  });
  ok(handled === true, "対応するハンドラがあれば true を返す");
  ok(calls === 1, "ハンドラは1回だけ呼ばれる");

  const notHandled = L.dispatchKey(L.TIMELINE_KEYS, { key: "ArrowLeft" }, {
    stepForward: () => { calls++; },
  });
  ok(notHandled === false, "アクションは解決してもハンドラが無ければ false");
  ok(calls === 1, "ハンドラが無ければ何も呼ばれない（calls は増えない）");

  const filtered = L.dispatchKey(L.TIMELINE_KEYS, { key: "ArrowLeft", targetTag: "INPUT" }, {
    stepBack: () => { calls++; },
  });
  ok(filtered === false, "入力欄フォーカス中は割り当てがあっても発火しない");
  ok(calls === 1, "入力欄フォーカス中はハンドラを呼ばない");
}

// --------------------------------------------------------------------
section("キーマップ（issue #79）: 割り当てを1つ変えると全画面が変わる");
// --------------------------------------------------------------------
{
  // timeline / framestep / draw / review の4画面が、同じ束（CORE_TRANSPORT）
  // を「参照として」共有していることを見る。コピーではなく同じオブジェクトで
  // あることまで確かめるのは、画面側が別々にキーを定義し直す退行
  // （書いたつもりが実は独自実装、という issue #79 の再発）を検出するため。
  // このオブジェクトの中身（例えば STEP_BACK の keys）を直せば、
  // 4画面すべての ← / → の意味が同時に変わる。
  const screens = {
    timeline: L.TIMELINE_KEYS,
    framestep: L.FRAMESTEP_KEYS,
    draw: L.DRAW_KEYS,
    review: L.REVIEW_KEYS,
  };
  for (const [name, keys] of Object.entries(screens)) {
    ok(keys.includes(L.STEP_BACK), `${name} は共有の STEP_BACK をそのまま使っている（コピーでない）`);
    ok(keys.includes(L.STEP_FWD), `${name} は共有の STEP_FWD をそのまま使っている（コピーでない）`);
    ok(keys.includes(L.PLAY_TOGGLE), `${name} は共有の PLAY_TOGGLE をそのまま使っている（コピーでない）`);
    ok(keys.includes(L.GO_HOME), `${name} は共有の GO_HOME をそのまま使っている（コピーでない）`);
    ok(keys.includes(L.GO_END), `${name} は共有の GO_END をそのまま使っている（コピーでない）`);
    ok(keys.includes(L.SHUTTLE_REV), `${name} は共有の SHUTTLE_REV をそのまま使っている（コピーでない）`);
    ok(keys.includes(L.SHUTTLE_FWD), `${name} は共有の SHUTTLE_FWD をそのまま使っている（コピーでない）`);
    ok(keys.includes(L.HELP), `${name} は共有の HELP をそのまま使っている（コピーでない）`);
  }

  // 実際に「1つ直すと4画面とも変わる」ことを、直に動かして確かめる。
  // STEP_BACK の束をコピーして keys を書き換え、resolveKey の結果が
  // 4画面とも一致して変わることを見る（オブジェクトが共有されているので、
  // 実運用ではこの書き換えは keymap.ts を直すのと同じ効果になる）
  const savedKeys = L.STEP_BACK.keys;
  try {
    L.STEP_BACK.keys = ["Backspace"];
    for (const [name, keys] of Object.entries(screens)) {
      ok(L.resolveKey(keys, { key: "ArrowLeft" }) !== "stepBack",
         `${name}: STEP_BACK の割り当てを変えると ← はもう stepBack を指さない`);
      ok(L.resolveKey(keys, { key: "Backspace" }) === "stepBack",
         `${name}: 新しく割り当てたキーがそのまま効く`);
    }
  } finally {
    L.STEP_BACK.keys = savedKeys; // 他のテストへ影響しないよう必ず戻す
  }
  ok(L.resolveKey(L.TIMELINE_KEYS, { key: "ArrowLeft" }) === "stepBack", "後始末: 元の割り当てに戻っている");
}

// --------------------------------------------------------------------
section("キーマップ（issue #79）: review だけの判定キー・キュー送り");
// --------------------------------------------------------------------
{
  // 1〜5（判定）は RULES 0 により動かしていない。ここが動くとどのキーで
  // 何を判定したか作業者の手が覚えている操作を裏切ることになる
  ok(L.resolveKey(L.REVIEW_KEYS, { key: "1" }) === "judgeOk", "1 は問題なし（変更していない）");
  ok(L.resolveKey(L.REVIEW_KEYS, { key: "2" }) === "judgeAdd", "2 は漏れている（変更していない）");
  ok(L.resolveKey(L.REVIEW_KEYS, { key: "3" }) === "judgeUnsure", "3 は判断できない（変更していない）");
  ok(L.resolveKey(L.REVIEW_KEYS, { key: "4" }) === "judgeShrink", "4 はでかすぎる（変更していない）");
  ok(L.resolveKey(L.REVIEW_KEYS, { key: "5" }) === "judgeErase", "5 は誤検知（変更していない）");

  // キュー送りは ← / → から退避した。← / → は他画面と同じ1フレーム移動になる
  ok(L.resolveKey(L.REVIEW_KEYS, { key: "ArrowLeft" }) === "stepBack",
     "review の ← はもうキュー送りではなく1フレーム移動");
  ok(L.resolveKey(L.REVIEW_KEYS, { key: "PageUp" }) === "queuePrev", "キュー送り（前）は PageUp");
  ok(L.resolveKey(L.REVIEW_KEYS, { key: "PageDown" }) === "queueNext", "キュー送り（次）は PageDown");

  // timeline の 1 / 2（pending 適用）は review の判定キーと衝突していたので
  // timeline 側を動かした。timeline の 1 / 2 はもう何も指さない
  ok(L.resolveKey(L.TIMELINE_KEYS, { key: "1" }) === null, "timeline の 1 はもう何もしない（judgeOk と衝突していた）");
  ok(L.resolveKey(L.TIMELINE_KEYS, { key: "2" }) === null, "timeline の 2 はもう何もしない（judgeAdd と衝突していた）");
  ok(L.resolveKey(L.TIMELINE_KEYS, { key: "Enter" }) === "applyFrame", "timeline は Enter でこのフレームに適用");
  ok(L.resolveKey(L.TIMELINE_KEYS, { key: "Enter", shiftKey: true }) === "applySpan",
     "timeline は Shift+Enter で span 分に適用");

  // 区間追従（I/O）は webapp/review.tsx にしかない機能。review/app.tsx 用の
  // REVIEW_KEYS には含めない（両方に足すかは別途 issue の判断に委ねる）
  ok(!L.REVIEW_KEYS.includes(L.RV_INTERVAL_START), "REVIEW_KEYS 本体には区間の始点キーを含めない");
  ok(L.REVIEW_INTERVAL_KEYS.includes(L.RV_INTERVAL_START), "区間の始点は REVIEW_INTERVAL_KEYS 側にある");
}

// --------------------------------------------------------------------
section("キーマップ（issue #79）: 自動生成されるキー一覧");
// --------------------------------------------------------------------
{
  // 手で書き写すと腐るので、一覧は割り当てそのものから作る
  const rows = L.helpRows(L.TIMELINE_KEYS);
  ok(rows.some((r) => r.desc === "1フレーム戻る"), "一覧に1フレーム戻るの説明が出る");
  const backRow = rows.find((r) => r.desc === "1フレーム戻る");
  ok(backRow.label === "← / ,", "一覧のラベルは binding の label をそのまま使う: " + backRow.label);

  // stepBack は "ArrowLeft" と "," の2キーだが、一覧では1行にまとめる
  // （2行出ると「同じ意味の2キー」ではなく「別の意味」に見えてしまう）
  const backRows = rows.filter((r) => r.desc === "1フレーム戻る");
  ok(backRows.length === 1, "同じアクションは一覧で重複させない: " + backRows.length + "行");

  const line = L.helpLine(L.REVIEW_KEYS);
  ok(line.includes("1 問題なし"), "1行表示にも各キーの説明が出る: " + line);
  ok(line.includes("・"), "複数の割り当てを ・ で区切る");
}

// --------------------------------------------------------------------
section("シャトル速度（issue #79）: J/K/L の加減速");
// --------------------------------------------------------------------
{
  ok(L.nextShuttleSpeed(0, 1) === 1, "止まっているところから L を押すと1倍で動き出す");
  ok(L.nextShuttleSpeed(0, -1) === -1, "止まっているところから J を押すと逆1倍で動き出す");
  ok(L.nextShuttleSpeed(0, 0) === 0, "止まっているところで K を押しても0のまま");

  ok(L.nextShuttleSpeed(1, 1) === 2, "同じ向きの連打で倍になる: 1 -> 2");
  ok(L.nextShuttleSpeed(2, 1) === 4, "2 -> 4");
  ok(L.nextShuttleSpeed(4, 1) === 8, "4 -> 8");
  ok(L.nextShuttleSpeed(8, 1) === 8, "8倍で頭打ち（青天井にしない）: " + L.nextShuttleSpeed(8, 1));

  ok(L.nextShuttleSpeed(-2, -1) === -4, "逆向きも同様に加速する: -2 -> -4");

  // K は常に停止。どれだけ速く動いていても、どちら向きでも止まる
  ok(L.nextShuttleSpeed(8, 0) === 0, "全速力からでも K で即停止");
  ok(L.nextShuttleSpeed(-8, 0) === 0, "逆向きの全速力からでも K で即停止");

  // 動いている向きと逆を押すと、減速ではなく逆向きの1倍から入り直す
  ok(L.nextShuttleSpeed(4, -1) === -1, "順再生中に J を押すと、減速ではなく逆1倍から入り直す");
  ok(L.nextShuttleSpeed(-4, 1) === 1, "逆再生中に L を押すと、順1倍から入り直す");
}

console.log(fails ? `\n${count} 件中 ${fails} 件失敗` : `\n${count} 件すべて通過`);
process.exit(fails ? 1 : 0);
