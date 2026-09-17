// TypeScript / TSX を束ねて、Python が既に配信している場所へ直に出す。
//
// 出力先を既存のファイル名と同じにしてあるのは、Python 側の配信コードに
// 一切手を入れずに済ませるため。ビルド結果はリポジトリにコミットするので、
// npm が入っていない環境でも Python サーバ単体で動く。
//
//   node build.mjs             1回だけ束ねる
//   node build.mjs --check     コミット済み成果物とのずれを検査する（書き込まない）
//   node build.mjs --watch     src/ を見張って、変わるたびに束ね直す
//   node build.mjs --sourcemap 最小化した中身を現地で追うための .map も出す
//
// .map は既定で出さない。7本ぶんで 1MB を超え、本体（計 144KB）より大きい。
// 中身は Preact と TS のソースそのものなので、frontend/src を読めば済む。
// 現地で追いたいときだけ --sourcemap を付ける（.map は .gitignore 済み）。

import { spawnSync } from "node:child_process";
import { readFile, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { generatedTextMatches, isCanonicalRemoteUrl } from "./build-check.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..");

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
const TARGETS = [...ENTRIES, LOGIC];

const COMMON = {
  absWorkingDir: HERE,
  bundle: true,
  target: ["es2020"],
  charset: "utf8",
  jsx: "automatic",
  jsxImportSource: "preact",
  legalComments: "eof",
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

const args = process.argv.slice(2);
const knownArgs = new Set(["--check", "--watch", "--sourcemap"]);
const unknownArgs = args.filter((arg) => !knownArgs.has(arg));
if (unknownArgs.length) {
  console.error(`不明な引数です: ${unknownArgs.join(", ")}`);
  process.exit(2);
}

const check = args.includes("--check");
const watch = args.includes("--watch");
const withMap = args.includes("--sourcemap");
if (check && (watch || withMap)) {
  console.error("--check は --watch / --sourcemap と同時には使えません");
  process.exit(2);
}

function runGit(args) {
  return spawnSync("git", ["-c", `safe.directory=${REPO}`, ...args], {
    cwd: REPO,
    encoding: "utf8",
  });
}

function findBaseRef() {
  const configured = process.env.AUTOMOSAIC_FRONTEND_BASE_REF;
  let candidate = configured;

  if (!candidate) {
    const listed = runGit(["remote"]);
    if (listed.error || listed.status !== 0) {
      const detail = listed.error?.message || (listed.stderr || listed.stdout || `exit ${listed.status}`).trim();
      console.error(`git remote の確認に失敗しました: ${detail}`);
      return null;
    }

    const canonicalRemotes = [];
    for (const remote of listed.stdout.split(/\r?\n/).filter(Boolean)) {
      const urls = runGit(["remote", "get-url", "--all", remote]);
      if (urls.error || urls.status !== 0) {
        const detail = urls.error?.message || (urls.stderr || urls.stdout || `exit ${urls.status}`).trim();
        console.error(`remote ${remote} の URL 確認に失敗しました: ${detail}`);
        return null;
      }
      if (urls.stdout.split(/\r?\n/).some(isCanonicalRemoteUrl)) canonicalRemotes.push(remote);
    }

    if (canonicalRemotes.length !== 1) {
      const found = canonicalRemotes.length ? canonicalRemotes.join(", ") : "無し";
      console.error(`canonical repository を指す remote を一意に決められません（候補: ${found}）。`);
      console.error("`AUTOMOSAIC_FRONTEND_BASE_REF=<canonical-remote>/master` を明示してください。");
      return null;
    }
    candidate = `${canonicalRemotes[0]}/master`;
  }

  const verified = runGit(["rev-parse", "--verify", "--quiet", `${candidate}^{commit}`]);
  if (verified.error) {
    console.error(`git を実行できません: ${verified.error.message}`);
    return null;
  }
  if (verified.status === 0) return candidate;
  if (verified.status !== 1) {
    const detail = (verified.stderr || verified.stdout || `exit ${verified.status}`).trim();
    console.error(`${candidate} の確認に失敗しました: ${detail}`);
    return null;
  }
  console.error(`比較基準 ${candidate} がありません。canonical repository の master を fetch してから再実行してください。`);
  return null;
}

function branchContainsBase() {
  const baseRef = findBaseRef();
  if (baseRef === null) return false;

  const result = runGit(["merge-base", "--is-ancestor", baseRef, "HEAD"]);
  if (result.error) {
    console.error(`git を実行できません: ${result.error.message}`);
    return false;
  }
  if (result.status === 0) return true;
  if (result.status === 1) {
    console.error(`現在の HEAD は ${baseRef} を含んでいません。最新 master を取り込んでからビルドしてください。`);
    console.error("先に canonical repository を fetch し、その remote の master へ rebase してください。");
    return false;
  }

  const detail = (result.stderr || result.stdout || `exit ${result.status}`).trim();
  console.error(`git merge-base に失敗しました: ${detail}`);
  return false;
}

if (check && !branchContainsBase()) process.exit(1);

let build;
let context;
try {
  ({ build, context } = await import("esbuild"));
} catch (error) {
  if (error?.code === "ERR_MODULE_NOT_FOUND" && String(error.message).includes("esbuild")) {
    console.error("esbuild がありません。`cd frontend && npm ci` の後に再実行してください。成果物の同期は未確認です。");
    process.exit(2);
  }
  throw error;
}

const all = [...ENTRIES.map(pageOptions), logicOptions()];

if (check) {
  const mismatches = (
    await Promise.all(all.map(async (options, index) => {
      const result = await build({ ...options, write: false });
      if (result.outputFiles.length !== 1) {
        throw new Error(`${options.outfile}: 出力が ${result.outputFiles.length} 件生成されました（1件でなければならない）`);
      }

      let committed;
      try {
        committed = await readFile(options.outfile, "utf8");
      } catch (error) {
        if (error?.code === "ENOENT") {
          return { out: TARGETS[index].out, reason: "ファイルが無い" };
        }
        throw error;
      }

      if (!generatedTextMatches(result.outputFiles[0].text, committed)) {
        return { out: TARGETS[index].out, reason: "内容が古い" };
      }
      return null;
    }))
  ).filter(Boolean);

  if (mismatches.length) {
    console.error("コミット済みのフロント成果物がソースと一致しません:");
    for (const mismatch of mismatches) {
      console.error(`  ${mismatch.out}（${mismatch.reason}）`);
    }
    console.error("master を取り込んでから `node frontend/build.mjs` を実行し、変わった成果物をすべてコミットしてください。");
    console.error("共有モジュールを変えると、それを束ねている複数の成果物が同時に変わります。");
    process.exitCode = 1;
  } else {
    console.log(`フロント成果物 ${all.length} 件はソースと一致しています（CRLF/LF の差は無視）`);
  }
} else if (watch) {
  const ctxs = await Promise.all(all.map((o) => context(o)));
  await Promise.all(ctxs.map((c) => c.watch()));
  console.log("見張っています。Ctrl-C で終了");
} else {
  await Promise.all(all.map((o) => build(o)));
  for (const e of TARGETS) {
    const s = await stat(path.join(REPO, e.out));
    console.log(`  ${e.out}  ${s.size} B`);
  }
}
