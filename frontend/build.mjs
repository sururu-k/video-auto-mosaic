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
import { createHash } from "node:crypto";
import { readFile, readdir, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..");
const BUILD_MANIFEST = path.join(REPO, "automosaic/webapp/static/build.json");
const WEB_BUILD_MODULE = "automosaic:web-build-id";
const WEB_BUILD_NAMESPACE = "automosaic-web-build";

async function filesUnder(dir) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(await filesUnder(full)));
    else if (entry.isFile()) out.push(full);
  }
  return out;
}

/**
 * 手書きの版番号を置かず、ビルド入力そのものを版にする（issue #80）。
 * パスも混ぜるので、同じ内容のファイルを移動しても別ビルドになる。
 */
async function computeWebBuild() {
  const inputs = [
    fileURLToPath(import.meta.url),
    path.join(HERE, "package.json"),
    path.join(HERE, "package-lock.json"),
    ...(await filesUnder(path.join(HERE, "src"))),
  ];
  // localeCompare は実行環境の ICU に左右されるので、正規化した相対パスを
  // コードポイント順に並べる。
  inputs.sort((a, b) => {
    const left = path.relative(HERE, a).replaceAll("\\", "/");
    const right = path.relative(HERE, b).replaceAll("\\", "/");
    return left < right ? -1 : left > right ? 1 : 0;
  });
  const hash = createHash("sha256");
  for (const file of inputs) {
    const rel = path.relative(HERE, file).replaceAll("\\", "/");
    hash.update(rel, "utf8");
    hash.update("\0", "utf8");
    // Git の autocrlf による checkout 差を版の差にしない。対象は build.mjs、
    // package*.json、src/*.ts(x) だけなので、すべて UTF-8 テキストである。
    const content = (await readFile(file, "utf8"))
      .replaceAll("\r\n", "\n")
      .replaceAll("\r", "\n");
    hash.update(content, "utf8");
    hash.update("\0", "utf8");
  }
  return { id: hash.digest("hex"), inputs };
}

async function writeBuildManifest(id) {
  await writeFile(BUILD_MANIFEST, JSON.stringify({ web_build_id: id }) + "\n", "utf8");
}

const INITIAL_WEB_BUILD = await computeWebBuild();
const watch = process.argv.includes("--watch");
const withMap = process.argv.includes("--sourcemap");

const ENTRIES = [
  { src: "src/review/app.tsx", out: "automosaic/web/app.js" },
  { src: "src/timeline/timeline.tsx", out: "automosaic/web/timeline.js" },
  { src: "src/webapp/index.tsx", out: "automosaic/webapp/static/index.js" },
  { src: "src/webapp/job.tsx", out: "automosaic/webapp/static/job.js" },
  { src: "src/webapp/review.tsx", out: "automosaic/webapp/static/review.js" },
  { src: "src/webapp/draw.tsx", out: "automosaic/webapp/static/draw.js" },
  { src: "src/framestep/framestep.tsx", out: "automosaic/webapp/static/framestep.js" },
];

// DOM に依存しない判断と描画だけを ESM で別に出す。
// tests/test_frontend.mjs がこれを読んで直接動かす（npm 無しで検査が回る）。
const LOGIC = { src: "src/shared/logic.ts", out: "frontend/build/logic.mjs" };

const COMMON = {
  bundle: true,
  target: ["es2020"],
  charset: "utf8",
  jsx: "automatic",
  jsxImportSource: "preact",
  legalComments: "eof",
  logLevel: "warning",
};

/**
 * Web API を使う4画面へ、同じ入力 hash を仮想 module として渡す。
 * watch 時は全 src を watchFiles にするため、どの画面の変更でも4本全部が
 * 新しい ID で再ビルドされる。index の完了時だけ manifest を進める。
 */
function webBuildPlugin(writeManifestOnEnd) {
  let current = INITIAL_WEB_BUILD;
  return {
    name: "automosaic-web-build-id",
    setup(buildApi) {
      if (watch) {
        buildApi.onStart(async () => {
          current = await computeWebBuild();
        });
      }
      buildApi.onResolve({ filter: /^automosaic:web-build-id$/ }, () => ({
        path: WEB_BUILD_MODULE,
        namespace: WEB_BUILD_NAMESPACE,
      }));
      buildApi.onLoad(
        { filter: /.*/, namespace: WEB_BUILD_NAMESPACE },
        () => ({
          contents: `export const WEB_BUILD_ID = ${JSON.stringify(current.id)};`,
          loader: "js",
          watchFiles: current.inputs,
          watchDirs: [path.join(HERE, "src")],
        }),
      );
      if (writeManifestOnEnd) {
        buildApi.onEnd(async (result) => {
          if (result.errors.length) return;
          await writeBuildManifest(current.id);
          console.log(`  Web build ${current.id}`);
        });
      }
    },
  };
}

function pageOptions(entry) {
  const usesWebApi = entry.src.startsWith("src/webapp/");
  return {
    ...COMMON,
    entryPoints: [path.join(HERE, entry.src)],
    outfile: path.join(REPO, entry.out),
    format: "iife",
    minify: true,
    sourcemap: withMap ? "linked" : false,
    sourcesContent: withMap,
    plugins: usesWebApi
      ? [webBuildPlugin(watch && entry.src === "src/webapp/index.tsx")]
      : [],
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

const all = [...ENTRIES.map(pageOptions), logicOptions()];

if (watch) {
  const ctxs = await Promise.all(all.map((o) => context(o)));
  await Promise.all(ctxs.map((c) => c.watch()));
  console.log("見張っています。Ctrl-C で終了");
} else {
  await Promise.all(all.map((o) => build(o)));
  // bundle が全部成功してから manifest を進める。途中失敗で manifest だけ
  // 新しくなると、古い画面を新しい版としてサーバが記憶してしまう。
  await writeBuildManifest(INITIAL_WEB_BUILD.id);
  for (const e of [...ENTRIES, LOGIC]) {
    const s = await stat(path.join(REPO, e.out));
    console.log(`  ${e.out}  ${s.size} B`);
  }
  console.log(`  automosaic/webapp/static/build.json  ${INITIAL_WEB_BUILD.id}`);
}
