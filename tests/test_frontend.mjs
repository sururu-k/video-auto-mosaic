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

console.log(fails ? `\n${count} 件中 ${fails} 件失敗` : `\n${count} 件すべて通過`);
process.exit(fails ? 1 : 0);
