// Konva は vendor/konva.min.js を <script> で読ませて使う。
//
// バンドルには含めない。188KB を全ページのバンドルに畳み込む理由が無いのと、
// 実行時に差し替えられるほうが版を上げやすいため。CDN は参照しない
// （オフラインで動かないと、素材を外に出さないという前提が崩れる）。
// 型だけ npm の konva から借りて、実体はグローバルとして宣言する。

export {};

declare global {
  const Konva: typeof import("konva").default;
}
