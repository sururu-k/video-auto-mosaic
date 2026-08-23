// TypeScript / TSX を束ねて、Python が既に配信している場所へ直に出す。
//
// 出力先を既存のファイル名と同じにしてあるのは、Python 側の配信コードに
// 一切手を入れずに済ませるため。ビルド結果はリポジトリにコミットするので、
// npm が入っていない環境でも Python サーバ単体で動く。
//
//   node build.mjs             1回だけ束ねる
//   node build.mjs --watch     src/ を見張って、変わるたびに束ね直す
//   node build.mjs --sourcemap 最小化した中身を現地で追うための .map も出す
//
// .map は既定で出さない。7本ぶんで 1MB を超え、本体（計 144KB）より大きい。
// 中身は Preact と TS のソースそのものなので、frontend/src を読めば済む。
// 現地で追いたいときだけ --sourcemap を付ける（.map は .gitignore 済み）。

import { build, context } from "esbuild";
import { copyFile, mkdir, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..");

const ENTRIES = [
  { src: "src/review/app.tsx", out: "automosaic/web/app.js" },
  { src: "src/timeline/timeline.tsx", out: "automosaic/web/timeline.js" },
  { src: "src/webapp/index.tsx", out: "automosaic/webapp/static/index.js" },
  { src: "src/webapp/job.tsx", out: "automosaic/webapp/static/job.js" },
  { src: "src/webapp/review.tsx", out: "automosaic/webapp/static/review.js" },
  { src: "src/webapp/draw.tsx", out: "automosaic/webapp/static/draw.js" },
];

// DOM に依存しない判断と描画だけを ESM で別に出す。
// tests/test_frontend.mjs がこれを読んで直接動かす（npm 無しで検査が回る）。
const LOGIC = { src: "src/shared/logic.ts", out: "frontend/build/logic.mjs" };

// 実行時に <script> で読ませる外部ライブラリ。CDN は参照しない。
// オフラインで動かないと、そもそも素材を外に出さないという前提が崩れる。
const VENDOR = [{ from: "vendor/konva.min.js", to: "automosaic/web/vendor/konva.min.js" }];

const COMMON = {
  bundle: true,
  target: ["es2020"],
  charset: "utf8",
  jsx: "automatic",
  jsxImportSource: "preact",
  legalComments: "none",
  logLevel: "warning",
};

function pageOptions(entry) {
  return {
    ...COMMON,
    entryPoints: [path.join(HERE, entry.src)],
    outfile: path.join(REPO, entry.out),
    format: "iife",
    minify: true,
    sourcemap: withMap ? "linked" : false,
    sourcesContent: withMap,
  };
}

function logicOptions() {
  return {
    ...COMMON,
    entryPoints: [path.join(HERE, LOGIC.src)],
    outfile: path.join(REPO, LOGIC.out),
    format: "esm",
    // 検査から読むものなので最小化しない。落ちたときに行が読める
    minify: false,
  };
}

async function copyVendor() {
  for (const v of VENDOR) {
    const to = path.join(REPO, v.to);
    await mkdir(path.dirname(to), { recursive: true });
    await copyFile(path.join(HERE, v.from), to);
    const s = await stat(to);
    console.log(`  vendor  ${v.to}  ${s.size} B`);
  }
}

const watch = process.argv.includes("--watch");
const withMap = process.argv.includes("--sourcemap");
const all = [...ENTRIES.map(pageOptions), logicOptions()];

await copyVendor();

if (watch) {
  const ctxs = await Promise.all(all.map((o) => context(o)));
  await Promise.all(ctxs.map((c) => c.watch()));
  console.log("見張っています。Ctrl-C で終了");
} else {
  await Promise.all(all.map((o) => build(o)));
  for (const e of [...ENTRIES, LOGIC]) {
    const s = await stat(path.join(REPO, e.out));
    console.log(`  ${e.out}  ${s.size} B`);
  }
}
