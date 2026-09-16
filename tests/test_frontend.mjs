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
import { readFileSync } from "node:fs";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const LOGIC = path.resolve(HERE, "..", "frontend", "build", "logic.mjs");
const TIMELINE = path.resolve(HERE, "..", "automosaic", "web", "timeline.js");
const TIMELINE_SOURCE = path.resolve(HERE, "..", "frontend", "src", "timeline", "timeline.tsx");

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

// 実素材（マイビデオ-3.mp4, 1920x1080/30fps/55,303フレーム）を焼いたときの
// report.json の uncovered_ranges をそのまま埋め込む（2026-08-25 測定、
// data/library/20260823-234604-9be9/report.json）。222区間、最長644
// フレーム（43862-44505）、最短1フレーム（frame 7130 のみ）。トラックの
// 全画素判定・トラックの拡大縮小（issue #84）の両方でこの実測値を使う
// （合成データで「消えない」を確かめても、実際の分布で踏む境界とは限らない
// ため。RULES.md 2.1「既知の基準を借りてこない」）。
const N_FRAMES = 55303;
const RANGES = [[0,42],[75,117],[135,138],[156,163],[170,268],[540,553],[684,699],[902,1047],[1116,1163],[1187,1197],[1221,1432],[1469,1542],[1684,1718],[1731,1734],[1755,1759],[1766,1997],[2133,2146],[2193,2221],[2228,2233],[2607,2698],[2859,3110],[3129,3285],[3297,3316],[3394,3396],[3530,3654],[3663,3667],[3678,3793],[3813,3924],[4119,4323],[4633,4700],[4867,5106],[5179,5192],[5200,5208],[5215,5317],[5349,5400],[5407,5551],[5564,5595],[5618,5654],[5667,5802],[6133,6201],[6309,6364],[7071,7110],[7130,7130],[7137,7144],[7160,7170],[7176,7279],[7286,7378],[7470,7517],[7572,7607],[8297,8316],[8497,8649],[8658,8831],[9049,9059],[9247,9358],[9530,9591],[9613,9615],[9699,9700],[9708,9728],[9830,9871],[9878,9916],[10041,10044],[10078,10084],[10096,10130],[10214,10234],[10262,10308],[10720,10744],[11205,11237],[11317,11318],[11410,11438],[11500,11504],[11528,11589],[11761,11778],[11921,11928],[12061,12196],[12294,12302],[12729,12737],[12745,12759],[13536,13578],[13650,13656],[13858,13886],[13893,14170],[14364,14500],[14717,14729],[15298,15311],[15771,15938],[16569,16736],[16749,16773],[17459,17472],[17505,17560],[18023,18045],[18352,18353],[18643,18680],[19065,19112],[19124,19256],[19346,19375],[19481,19515],[19608,19649],[20233,20253],[20327,20480],[20628,20805],[20814,20858],[20866,20877],[20884,21158],[21165,21413],[21660,21765],[21788,21879],[22676,22688],[22843,22863],[22880,22885],[22892,22932],[22940,22962],[23059,23339],[23405,23416],[23423,23464],[23505,23558],[23575,23577],[23588,23624],[25064,25217],[25325,25363],[25389,25512],[25592,25752],[25940,25965],[25985,26075],[26207,26221],[26583,26589],[26612,26698],[26711,26938],[26952,27152],[27820,27898],[28278,28282],[28295,28429],[29087,29339],[29422,29429],[29632,29743],[30168,30199],[30208,30236],[30246,30305],[30313,30339],[30393,30413],[30431,30446],[30468,30469],[30997,31001],[31271,31403],[31744,31793],[31946,32053],[32176,32273],[32316,32474],[33132,33144],[33236,33307],[33581,33643],[33652,33674],[33792,33815],[34345,34348],[34446,34447],[35466,35529],[36529,36644],[36651,36685],[36695,36785],[37028,37576],[38859,38867],[39122,39149],[39644,39691],[39700,39701],[39743,39747],[39805,39832],[40096,40177],[40565,40627],[40640,40715],[40950,40969],[41753,41836],[42674,42810],[42952,43049],[43092,43094],[43305,43310],[43398,43521],[43781,43812],[43862,44505],[44646,44804],[44916,45017],[45034,45044],[45570,45734],[45748,45785],[45919,45938],[46017,46248],[46296,46544],[46558,46603],[46683,46719],[46824,46873],[47139,47162],[47454,47547],[47923,47969],[47978,48072],[48082,48093],[48163,48226],[48252,48316],[49855,49975],[50629,50636],[50898,50911],[50918,51135],[51149,51258],[51267,51421],[51446,51480],[51714,51783],[52157,52461],[52777,52917],[52935,53064],[53086,53347],[53358,53420],[53487,53802],[53853,53863],[53873,54215],[54223,54227],[54315,54336],[54348,54373],[54665,54666],[54673,54697],[54758,54771],[54781,54789],[54941,54952],[54959,54965],[55057,55225],[55260,55302]];

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

// --------------------------------------------------------------------
section("検査キュー画面のトラック（issue #84）: 画素の判定");
// --------------------------------------------------------------------
{
  // 100フレームを10画素に潰す（1画素=10フレーム）。frame 55 は画素5に入る
  {
    const mask = L.uncoveredPixelMask([55], 10, 100);
    ok(mask.length === 10, "画素数は width と同じ");
    ok(mask[5] === true, "frame 55 は画素5を未塗装にする");
    ok(mask.filter(Boolean).length === 1, "他の画素は未塗装にならない: " + mask.filter(Boolean).length);
  }

  // 1画素の中に未塗装が1フレームでもあれば未塗装あり（RULES.md 0）。
  // 画素の端ちょうど（a, b-1）でも拾えることを境界で確かめる
  {
    const mask = L.uncoveredPixelMask([9], 10, 100); // 画素0は frame 0..9
    ok(mask[0] === true, "画素の右端ちょうどのフレームも画素0に入る");
    const mask2 = L.uncoveredPixelMask([10], 10, 100); // frame 10 は画素1の左端
    ok(mask2[0] === false && mask2[1] === true, "境界の次のフレームは隣の画素に入る（漏れない・こぼれない）");
  }

  ok(L.uncoveredPixelMask([], 10, 100).every((v) => v === false), "未塗装フレームが無ければ全画素とも塗装扱い");
  ok(L.uncoveredPixelMask([15, 25, 85], 10, 100).filter(Boolean).length === 3,
     "順不同・複数画素にまたがるフレーム番号でも正しく拾う（呼び出し側でソートしなくてよい）");
  ok(L.uncoveredPixelMask([85, 15, 25], 10, 100).filter(Boolean).length === 3,
     "同じ集合を逆順で渡しても結果は変わらない");
  ok(L.uncoveredPixelMask([5], 0, 100).length === 0, "width が0でも落ちない");
  ok(L.uncoveredPixelMask([5], 10, 0).every((v) => v === false), "n_frames が0でも落ちない（全部false）");

  // 動画編集ソフトのトラックと同じ発想: 押した画素の位置からフレームへ戻す
  ok(L.frameFromTrackX(0, 1000, 55303) === 0, "トラック左端をクリックすると frame 0");
  const lastFramePx = L.frameFromTrackX(999, 1000, 55303);
  ok(lastFramePx > 55303 * 0.9, "トラック右端付近のクリックは末尾に近いフレームを指す: " + lastFramePx);
  ok(L.frameFromTrackX(999, 1000, 55303) === L.frameFromTrackX(999, 1000, 55303),
     "同じ入力なら常に同じフレームを返す（決定的）");
  ok(L.frameFromTrackX(-50, 1000, 55303) === 0, "範囲外（左）は0へ丸める");
  ok(L.frameFromTrackX(5000, 1000, 55303) === 55302, "範囲外（右）は最終フレームへ丸める");

  ok(L.playheadPixel(0, 1000, 55303) === 0, "再生ヘッド: frame 0 は画素0");
  ok(L.playheadPixel(55302, 1000, 55303) === 999, "再生ヘッド: 最終フレームは最終画素");
  ok(L.playheadPixel(-10, 1000, 55303) === 0, "再生ヘッド: 範囲外（負）は端に寄せる（消さない）");
  ok(L.playheadPixel(999999, 1000, 55303) === 999, "再生ヘッド: 範囲外（超過）も端に寄せる（消さない）");
}

// --------------------------------------------------------------------
section("検査キュー画面のトラック（issue #84）: 実素材222区間で最短が消えない");
// --------------------------------------------------------------------
{
  // 実素材（マイビデオ-3.mp4, 1920x1080/30fps/55,303フレーム）を焼いたときの
  // report.json の uncovered_ranges。RANGES / N_FRAMES はファイル冒頭で
  // 定義した実測値（トラックの拡大縮小の検証でもこのあと再利用する）。
  // review.tsx はキュー標本（QueueItem.boxes が空のフレーム）から画素を
  // 塗るので、ここでは「各区間の代表フレームが1つでも画素に届けば消えない」
  // という契約を、区間の start をキュー標本の代わりに使って検証する
  // （build_queue._sample_range は必ず区間内の1点を拾うことを review.py 側
  // で確認済み。ここは「拾えた点がどの画素にも取りこぼされない」側の検証）。
  ok(RANGES.length === 222, "埋め込んだ区間数は実測どおり222件");
  const lens = RANGES.map(([s, e]) => e - s + 1);
  ok(Math.max(...lens) === 644, "最長区間は実測どおり644フレーム");
  ok(Math.min(...lens) === 1, "最短区間は実測どおり1フレーム");
  const shortest = RANGES.filter((_, i) => lens[i] === 1);
  ok(shortest.length === 1 && shortest[0][0] === 7130, "最短（1フレーム）区間は実測どおり frame 7130 のみ");

  // 実際の画面幅に近い値（1000px）と、それより狭い値（360px、携帯の画面幅
  // 相当）の両方で、222区間すべての代表フレームが画素として残ることを見る
  for (const width of [1000, 360]) {
    const samples = RANGES.map(([s]) => s); // 各区間の start を代表点にする
    const mask = L.uncoveredPixelMask(samples, width, N_FRAMES);
    const nHitPixels = mask.filter(Boolean).length;
    ok(nHitPixels >= 1, `width=${width}: 222区間の代表点が画素として1つ以上残る: ${nHitPixels}`);

    // 最短区間（frame 7130 だけ）が焼き込まれる画素を個別に確かめる。
    // 「どの画素か」は uncoveredPixelMask 自身の割り当てで確かめる（呼ぶ側で
    // 別式 floor(f*w/n) を独自に計算すると、画素の左右端の丸めが
    // uncoveredPixelMask 内部の a=floor(px*n/w) と1画素分ずれることがあり、
    // 実装ではなく検証側の式が間違っているだけの「失敗」を作ってしまう
    // ため、frame 7130 だけを渡した結果を直接見る）
    const soloMask = L.uncoveredPixelMask([7130], width, N_FRAMES);
    ok(soloMask.some(Boolean), `width=${width}: 最短区間（frame 7130 のみ）を渡すと必ずどこかの画素が未塗装になる`);
    const px7130 = soloMask.indexOf(true);
    ok(mask[px7130] === true,
       `width=${width}: 222区間まとめて渡したときも、最短区間の画素(${px7130})が未塗装のまま残る`);
  }

  // 222区間それぞれについて、その区間の start 1枚だけを渡したときに
  // どこにも画素が立たない（=完全に消える）ものが無いか、全数で確かめる。
  // 1件でも false ならその区間は「何も無い」に見える壊れ方をしている
  for (const width of [1000, 360]) {
    let missed = 0;
    for (const [s] of RANGES) {
      const soloMask = L.uncoveredPixelMask([s], width, N_FRAMES);
      if (!soloMask.some(Boolean)) missed++;
    }
    ok(missed === 0, `width=${width}: 222区間のうち代表点がどの画素にも立たなかったもの: ${missed}件（0でなければならない）`);
  }
}

// --------------------------------------------------------------------
section("トラックの拡大縮小（issue #84）: ビューポートの計算");
// --------------------------------------------------------------------
{
  ok(JSON.stringify(L.fullViewport(55303)) === JSON.stringify({ start: 0, end: 55303 }),
     "全体表示は [0, nFrames)");
  ok(JSON.stringify(L.fullViewport(0)) === JSON.stringify({ start: 0, end: 0 }), "nFrames が0でも落ちない");

  ok(L.clampViewportStart(-100, 1000, 55303) === 0, "start が負なら0へ");
  ok(L.clampViewportStart(60000, 1000, 55303) === 55303 - 1000, "start が末尾を超えるなら末尾ちょうどに収まる位置へ");
  ok(L.clampViewportStart(1000, 1000, 55303) === 1000, "範囲内ならそのまま");

  ok(L.zoomLen(1000, 0.5, 55303) === 500, "factor 0.5 で長さが半分（拡大）");
  ok(L.zoomLen(1000, 2, 55303) === 2000, "factor 2 で長さが倍（縮小）");
  ok(L.zoomLen(1, 0.5, 55303) === 1, "1フレーム未満にはならない（0にすると画面から全部消える）");
  ok(L.zoomLen(55303, 2, 55303) === 55303, "nFrames を超えて広げない");

  const vp1 = L.zoomViewport({ start: 0, end: 55303 }, 0.5, 27000, 55303);
  ok(vp1.end - vp1.start === Math.round(55303 * 0.5), "ズームすると長さが factor どおりに変わる");
  ok(vp1.start <= 27000 && 27000 < vp1.end, "ズームの中心にした再生ヘッドは新しいビューポートの中に入っている");

  // 端に近い位置でズームしても再生ヘッドが画面外へ押し出されない
  // （中心固定＋クランプなので、必ず中に残る）ことを、端に寄せて確かめる
  const vpEdge = L.zoomViewport({ start: 0, end: 55303 }, 0.01, 100, 55303);
  ok(vpEdge.start <= 100 && 100 < vpEdge.end, "先頭近くでの強いズームでも再生ヘッドが画面内に残る");
  const vpEdge2 = L.zoomViewport({ start: 0, end: 55303 }, 0.01, 55200, 55303);
  ok(vpEdge2.start <= 55200 && 55200 < vpEdge2.end, "末尾近くでの強いズームでも再生ヘッドが画面内に残る");

  // 再生ヘッドの追従（完了条件「拡大したまま移動したら追従する」）
  const vpNarrow = { start: 1000, end: 1200 };
  ok(L.followPlayhead(vpNarrow, 1100, 55303) === vpNarrow,
     "ビューポートの中に居るなら動かさない（同じ参照を返す＝無駄な再描画をしない）");
  const followed = L.followPlayhead(vpNarrow, 5000, 55303);
  ok(followed.start <= 5000 && 5000 < followed.end, "ビューポートの外に出たら追従して再生ヘッドを画面内に戻す");
  ok(followed.end - followed.start === vpNarrow.end - vpNarrow.start,
     "追従してもズームの長さ（拡大率）は変わらない");
}

// --------------------------------------------------------------------
section("トラックの拡大縮小（issue #84）: 範囲つきの画素判定・座標変換");
// --------------------------------------------------------------------
{
  // [1000, 2000) を10画素に潰す（1画素=100フレーム）。frame 1550 は画素5
  const mask = L.pixelSampleMaskInRange([1550], 10, 1000, 2000);
  ok(mask[5] === true, "rangeStart を0以外にしても、同じ考え方で画素が決まる");
  ok(mask.filter(Boolean).length === 1, "範囲外の画素は立たない");

  // rangeStart=0 のときは既存の uncoveredPixelMask と一致する（後方互換の確認）
  const a = L.pixelSampleMaskInRange([15, 25, 85], 10, 0, 100);
  const b = L.uncoveredPixelMask([15, 25, 85], 10, 100);
  ok(JSON.stringify(a) === JSON.stringify(b), "rangeStart=0 なら uncoveredPixelMask と同じ結果になる");

  ok(L.frameFromTrackXInRange(0, 1000, 1000, 2000) === 1000, "ズーム中のトラック左端クリックは viewStart のフレーム");
  ok(L.frameFromTrackXInRange(999, 1000, 1000, 2000) === 1999, "ズーム中のトラック右端付近クリックは viewEnd 直前のフレーム");
  ok(L.frameFromTrackXInRange(-50, 1000, 1000, 2000) === 1000, "範囲外（左）は viewStart へ丸める");
  ok(L.frameFromTrackXInRange(5000, 1000, 1000, 2000) === 1999, "範囲外（右）は viewEnd 直前へ丸める");

  ok(L.playheadPixelInRange(1000, 1000, 1000, 2000) === 0, "再生ヘッド: viewStart のフレームは画素0");
  ok(L.playheadPixelInRange(1999, 1000, 1000, 2000) === 999, "再生ヘッド: viewEnd 直前のフレームは最終画素");
  ok(L.playheadPixelInRange(500, 1000, 1000, 2000) === 0, "再生ヘッド: ビューポートより手前は左端に寄せる（消さない）");
  ok(L.playheadPixelInRange(5000, 1000, 1000, 2000) === 999, "再生ヘッド: ビューポートより後ろは右端に寄せる（消さない）");
}

// --------------------------------------------------------------------
section("トラックの拡大縮小（issue #84）: 区間の帯（I/O・手修正・ドラッグ中）の画素変換");
// --------------------------------------------------------------------
{
  // 1フレームの区間でも必ず1px以上（RULES 0: 消えない）
  const rng1 = L.frameRangeToPixels(7130, 7130, 1000, 0, 55303);
  ok(rng1 !== null, "1フレームの区間は null（画面外）にならない");
  ok(rng1[1] - rng1[0] >= 1, `1フレームの区間でも幅1px以上になる: ${JSON.stringify(rng1)}`);

  ok(L.frameRangeToPixels(100, 200, 1000, 500, 1000) === null, "区間がビューポートより完全に手前なら null");
  ok(L.frameRangeToPixels(2000, 3000, 1000, 500, 1000) === null, "区間がビューポートより完全に後ろなら null");

  const clipped = L.frameRangeToPixels(400, 600, 1000, 500, 1000);
  ok(clipped !== null && clipped[0] === 0, "始点が画面の左に食い込む区間は左端0から描く");

  // 順不同（O が I より前でも）でも同じ結果になる
  const rA = L.frameRangeToPixels(100, 300, 1000, 0, 1000);
  const rB = L.frameRangeToPixels(300, 100, 1000, 0, 1000);
  ok(JSON.stringify(rA) === JSON.stringify(rB), "始点と終点が逆順でも同じピクセル範囲になる");
}

// --------------------------------------------------------------------
section("トラックの拡大縮小（issue #84）: 実素材222区間がどの拡大率でも消えない");
// --------------------------------------------------------------------
{
  // RANGES / N_FRAMES はファイル冒頭の実測値（222区間、最長644フレーム、
  // 最短1フレーム＝frame 7130 のみ）。PR #88 は全体表示（[0, nFrames)）
  // でこれを確かめた。ここでは同じ222区間を、全体表示から強い拡大まで
  // 複数のビューポート長で、かつ各長さについて全体をタイル張りする窓の
  // 位置をずらしながら走査し、「窓の中に入っている区間が、その窓では
  // 見えなくなっていないか」を全数で確かめる。
  const WIDTH = 1000;
  const LENS = [N_FRAMES, 20000, 5000, 1000, 200, 50, 10, 2, 1];
  for (const len of LENS) {
    const stepPx = Math.max(1, Math.floor(len / 2));
    let checked = 0;
    let missed = 0;
    for (let s = 0; s < N_FRAMES; s += stepPx) {
      const viewStart = s;
      const viewEnd = Math.min(N_FRAMES, s + len);
      if (viewEnd <= viewStart) continue;
      for (const [a] of RANGES) {
        if (a < viewStart || a >= viewEnd) continue;
        checked++;
        const mask = L.pixelSampleMaskInRange([a], WIDTH, viewStart, viewEnd);
        if (!mask.some(Boolean)) missed++;
      }
    }
    ok(checked > 0, `len=${len}: 窓に入った区間の代表点を ${checked} 件チェックした`);
    ok(missed === 0,
       `len=${len}（width=${WIDTH}）: 窓の中にあるのにどの画素にも立たなかった区間: ${missed}/${checked}件（0でなければならない）`);
  }

  // 最短区間（frame 7130 のみ）を名指しで、複数の拡大率・複数の窓位置で
  // 個別に確かめる（生ログとして読みやすいよう1件ずつ出す）
  const SHORTEST = 7130;
  const ZOOMS = [
    { len: N_FRAMES, start: 0, label: "全体表示" },
    { len: 20000, start: 0, label: "縮小（frame 7130 を含む前半）" },
    { len: 5000, start: 5303, label: "中間ズーム" },
    { len: 1000, start: 6630, label: "拡大（前後500フレーム）" },
    { len: 200, start: 7030, label: "強拡大（前後100フレーム）" },
    { len: 20, start: 7120, label: "最大拡大付近（前後10フレーム）" },
    { len: 2, start: 7129, label: "1画面2フレーム" },
  ];
  for (const z of ZOOMS) {
    const viewEnd = Math.min(N_FRAMES, z.start + z.len);
    const mask = L.pixelSampleMaskInRange([SHORTEST], WIDTH, z.start, viewEnd);
    ok(mask.some(Boolean),
       `${z.label}（len=${z.len}, view=[${z.start},${viewEnd})）: frame 7130（最短区間）が画素に残る`);
  }

  // 窓の外に出れば映らないのは正常（バグではない）。誤って「常に映る」実装に
  // すり替わっていないことも確かめる（映るべきでない場面まで映していないか）
  const outside = L.pixelSampleMaskInRange([SHORTEST], WIDTH, 0, 5000);
  ok(!outside.some(Boolean),
     "窓の外（frame 7130 を含まない [0,5000)）では映らない（見えなくなるのは正常。バグではない）");
}

// --------------------------------------------------------------------
section("ドラッグでの範囲選択（issue #84）: I/O と同じ区間になる");
// --------------------------------------------------------------------
{
  // I を押したときに作られる形（IntervalStart = { frame }）と比較する。
  // ドラッグの起点フレームが同じ値になっていれば「同じ区間」と言える
  // （2つの別々の状態を持たない、という完了条件をここで確かめる）
  const d1 = L.dragToInterval(1000, 1300, "add", true);
  ok(d1.intervalStart !== null, "「漏れている」でタップ済みのままドラッグすると区間になる");
  ok(d1.intervalStart.frame === 1000, "区間の始点はドラッグの起点フレーム（I を frame 1000 で押したのと同じ形）");
  ok(d1.previewFrame === 1300, "ドラッグの終点へプレビューが動く（PageUp/PageDown での終点移動と同じ役目）");

  const d2 = L.dragToInterval(1300, 1000, "add", true);
  ok(d2.intervalStart.frame === 1300, "逆方向にドラッグしても始点はドラッグの起点（マウスを離した位置ではない）");
  ok(d2.previewFrame === 1000, "終点はドラッグを離した位置");

  ok(L.dragToInterval(1000, 1000, "add", true).intervalStart === null,
     "始点と終点が同じ＝ドラッグしていない（ただのクリック）は区間にしない");
  ok(L.dragToInterval(1000, 1300, "add", false).intervalStart === null,
     "タップが置かれていなければ区間にしない（I も tap が無ければ始点を置けない）");
  ok(L.dragToInterval(1000, 1300, null, true).intervalStart === null,
     "「漏れている」モードでなければ区間にしない");
  ok(L.dragToInterval(1000, 1300, "shrink", true).intervalStart === null,
     "「でかすぎる」モード中のドラッグも区間にしない（区間追従は add 専用。review.py の mark_interval と同じ制約）");
}

// --------------------------------------------------------------------
section("キーマップ（issue #84）: トラックの拡大縮小");
// --------------------------------------------------------------------
{
  ok(L.resolveKey(L.TRACK_ZOOM_KEYS, { key: "=" }) === "trackZoomIn", "= はトラックを拡大");
  ok(L.resolveKey(L.TRACK_ZOOM_KEYS, { key: "+" }) === "trackZoomIn", "+ も同じアクション");
  ok(L.resolveKey(L.TRACK_ZOOM_KEYS, { key: "-" }) === "trackZoomOut", "- はトラックを縮小");
  ok(L.resolveKey(L.TRACK_ZOOM_KEYS, { key: "_" }) === "trackZoomOut", "_ も同じアクション");
  ok(L.resolveKey(L.TRACK_ZOOM_KEYS, { key: "0" }) === "trackZoomFit", "0 は全体表示に戻す");
  ok(L.resolveKey(L.TRACK_ZOOM_KEYS, { key: "=", targetTag: "INPUT" }) === null, "入力欄では拾わない");

  // review 画面が実際に束ねるキー一覧（review.tsx の ALL_KEYS と同じ組み立て）で
  // 既存の判定キー（1〜5）や区間キー（I/O）と衝突していないことも確かめる
  const combined = [...L.REVIEW_KEYS, ...L.REVIEW_INTERVAL_KEYS, ...L.TRACK_ZOOM_KEYS];
  ok(L.resolveKey(combined, { key: "1" }) === "judgeOk", "拡大縮小を足しても 1 は判定キーのまま");
  ok(L.resolveKey(combined, { key: "i" }) === "intervalStart", "拡大縮小を足しても I は区間の始点のまま");
  ok(L.resolveKey(combined, { key: "=" }) === "trackZoomIn", "束ねた一覧でも + でズームできる");
  ok(L.resolveKey(combined, { key: "0" }) === "trackZoomFit", "束ねた一覧でも 0 で全体表示に戻る");
}

// --------------------------------------------------------------------
section("remove+add の不変条件（issue #6）");
// --------------------------------------------------------------------
{
  const items = [
    { frame: 10, kind: "remove" },
    { frame: 10, kind: "add" },
    { frame: 20, kind: "remove" },
  ];
  let got = L.correctionsAfterDrop(items, [1]);
  ok(got.length === 1 && got[0] === items[2],
     "組の add を落とすと相方の remove も落ち、別フレームは巻き込まない");

  got = L.correctionsAfterDrop(items, [0]);
  ok(got.length === 1 && got[0] === items[2],
     "組の remove を落とすと相方の add も落ちる");

  got = L.correctionsAfterDrop(items, [99, -1]);
  ok(got.length === items.length, "範囲外の番号では修正を消さない");

  const separated = [
    { frame: 30, kind: "remove" },
    { frame: 40, kind: "add" },
    { frame: 30, kind: "add" },
  ];
  got = L.correctionsAfterDrop(separated, [2]);
  ok(!got.some((c) => c.frame === 30),
     "隣接していない add を落としても、同じフレームに remove だけ残さない");
  ok(got.length === 1 && got[0] === separated[1],
     "安全側の全削除でも別フレームは巻き込まない");
}

{
  // 2フレーム x add/remove の4種類を長さ0〜6で全列挙し、各列の全 drop
  // 組み合わせを当てる。選択したフレームで、元に add があったのに
  // 結果が remove のみ、という完全素通しの形が1件も残らないことを見る。
  const symbols = [
    { frame: 0, kind: "remove" },
    { frame: 0, kind: "add" },
    { frame: 1, kind: "remove" },
    { frame: 1, kind: "add" },
  ];
  let checked = 0;
  let bad = null;
  outer:
  for (let n = 0; n <= 6; n++) {
    const sequences = 4 ** n;
    for (let encoded = 0; encoded < sequences; encoded++) {
      let value = encoded;
      const items = [];
      for (let i = 0; i < n; i++) {
        items.push(symbols[value & 3]);
        value = Math.floor(value / 4);
      }
      for (let mask = 0; mask < 2 ** n; mask++) {
        const drop = [];
        for (let i = 0; i < n; i++) if (mask & (1 << i)) drop.push(i);
        const got = L.correctionsAfterDrop(items, drop);
        const touched = new Set(drop.map((i) => items[i]?.frame).filter((f) => f !== undefined));
        for (const frame of touched) {
          const hadAdd = items.some((c) => c.frame === frame && c.kind === "add");
          const hasAdd = got.some((c) => c.frame === frame && c.kind === "add");
          const hasRemove = got.some((c) => c.frame === frame && c.kind === "remove");
          if (hadAdd && hasRemove && !hasAdd) {
            bad = { items, drop, got, frame };
            break outer;
          }
        }
        checked++;
      }
    }
  }
  ok(bad === null,
     `全 ${checked} 通りで、drop 後に add が消えて remove だけ残るフレームが無い` +
     (bad ? `: ${JSON.stringify(bad)}` : ""));
}

// --------------------------------------------------------------------
section("数値入力の空欄と 0（issue #6）");
// --------------------------------------------------------------------
{
  ok(L.numOr("", 7) === 7, "空欄は既定値へ倒す");
  ok(L.numOr("   ", 7) === 7, "空白だけも既定値へ倒す");
  ok(L.numOr("abc", 7) === 7, "数でない文字は既定値へ倒す");
  ok(L.numOr("Infinity", 7) === 7, "有限でない値は既定値へ倒す");
  ok(L.numOr("0", 7) === 0, "明示した 0 は既定値に化けない");
  ok(L.numOr("12", 7) === 12, "整数をそのまま読む");
  ok(L.numOr("1.5", 7) === 1.5, "小数をそのまま読む");
}

// --------------------------------------------------------------------
section("修正一覧の版を送受信する生成済み画面（issue #6）");
// --------------------------------------------------------------------
{
  const timeline = readFileSync(TIMELINE, "utf8");
  const source = readFileSync(TIMELINE_SOURCE, "utf8");
  ok(timeline.includes("base_sha256"), "保存時に取得済みの版をサーバへ送る");
  ok(timeline.includes("corrections_sha256"), "読込・保存結果から最新の版を受け取る");
  ok(timeline.includes("サーバから再同期用の状態が返りませんでした"),
     "競合回復の full state 処理が生成済み画面にも入っている");
  ok(timeline.includes("修正一覧の版が分かりません"), "版が無い古い状態では保存を止める");
  ok(source.includes("saveQueue.current = saveQueue.current.then"),
     "連続した保存要求を前の応答後まで待たせる");
  ok(source.includes("const current = correctionsRef.current"),
     "再描画前の連続操作も直前の楽観更新へ積む");
  ok(source.includes("saveGeneration.current++"),
     "競合後は同じ古い一覧から作った待機中の保存を破棄する");
  const reloadStart = source.indexOf("async function reloadCorrectionsAfterConflict()");
  const reloadEnd = source.indexOf("\n  function pushCorrections", reloadStart);
  const reload = source.slice(reloadStart, reloadEnd);
  ok(reloadStart >= 0 && reloadEnd > reloadStart,
     "競合回復の処理本体を検査できる");
  ok(reload.includes('u("/api/corrections", { state: 1 })'),
     "競合後は correction と同じ時点の full state を要求する");
  ok(reload.includes("setRegionsMap(refreshed.regions)"),
     "競合後は楽観更新した矩形をサーバの勝者へ戻す");
  ok(reload.includes("length: refreshed.n_frames"),
     "競合後は全フレームの検証済み状態も full state に合わせる");
  ok(reload.includes("if (!playingRef.current) loadStill(curRef.current)"),
     "競合後は表示中のモザイク画像も取り直す");
}

console.log(fails ? `\n${count} 件中 ${fails} 件失敗` : `\n${count} 件すべて通過`);
process.exit(fails ? 1 : 0);
