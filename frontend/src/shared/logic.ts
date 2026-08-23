// 画面の判断と描画のうち、DOM にもフレームワークにも依存しない部分だけを
// まとめた入口。
//
// build.mjs はこれを frontend/build/logic.mjs へ ESM で束ねて出す。
// tests/test_frontend.mjs がそれを読んで直接動かす。ブラウザを使えないので、
// 「押した結果として何が選ばれるか」をここで確かめられる状態にしておく。

export * from "./review-logic.js";
export * from "./geom.js";
export * from "./canvas-draw.js";
