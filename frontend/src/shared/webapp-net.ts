// Web アプリ（automosaic/webapp）との通信と、画面共通の書式。
//
// もとは static/common.js として素の script で読ませていた下回り。
// 中身は同じで、各画面のバンドルに畳み込んである。

import type { JobStatus } from "./api.js";
import { webBuildProblem, withMatchingWebBuild } from "./web-build.js";
import { WEB_BUILD_ID } from "automosaic:web-build-id";

/** build.mjs が source hash をここへ埋め込む。手書きの版番号は持たない。 */
export { WEB_BUILD_ID };

// トークンは URL から拾う。サーバが Cookie にも移すので素の URL でも通るが、
// Cookie を消している端末でも動くように毎回付け直す。
export const TOKEN = new URLSearchParams(location.search).get("t") ?? "";

export type QueryValue = string | number | boolean;

export function url(path: string, params?: Record<string, QueryValue>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params ?? {})) p.set(k, String(v));
  if (TOKEN) p.set("t", TOKEN);
  const q = p.toString();
  return q ? path + (path.includes("?") ? "&" : "?") + q : path;
}

// 画面から画面へ移るときにトークンを落とさない。落とすと 403 になって
// 「さっきまで見えていたのに開けない」という分かりにくい詰まり方をする
export function link(path: string): string {
  return url(path);
}

function showWebBuildProblem(message: string): void {
  document.body.textContent = "";
  const main = document.createElement("main");
  const card = document.createElement("div");
  card.className = "card";
  const heading = document.createElement("h2");
  heading.textContent = "サーバの再起動が必要です";
  const detail = document.createElement("p");
  detail.className = "warn";
  detail.textContent = message;
  card.append(heading, detail);
  main.append(card);
  document.body.append(main);
}

async function checkWebBuild(): Promise<string | null> {
  try {
    const headers: Record<string, string> = {};
    if (TOKEN) headers["X-Review-Token"] = TOKEN;
    const res = await fetch(url("/api/version"), {
      headers,
      cache: "no-store",
      credentials: "same-origin",
    });
    if (!res.ok) {
      return webBuildProblem(WEB_BUILD_ID, null) + `（版確認 HTTP ${res.status}）`;
    }
    const payload = (await res.json()) as { web_build_id?: unknown };
    return webBuildProblem(WEB_BUILD_ID, payload.web_build_id);
  } catch (e) {
    const why = e instanceof Error ? e.message : String(e);
    return webBuildProblem(WEB_BUILD_ID, null) + `（版確認に失敗: ${why}）`;
  }
}

// issue #80: App が描画される前から入力を止める。index の直接 XHR も、
// review/draw の window キー操作も、この検査が終わるまで操作対象にしない。
// api() 側も同じ Promise を待つので、inert を迂回してイベントが発火しても
// 不一致のサーバへ変更要求は送られない。
document.documentElement.inert = true;
const WEB_BUILD_GATE = checkWebBuild().then((problem) => {
  if (problem) {
    showWebBuildProblem(problem);
  } else {
    document.documentElement.inert = false;
  }
  return problem;
});

/** fetch 以外（進捗表示つき XHR など）も同じ fail-closed gate を通す。 */
export function withCurrentWebBuild<T>(operation: () => T | Promise<T>): Promise<T> {
  return withMatchingWebBuild(WEB_BUILD_GATE, operation);
}

export interface ApiOptions extends Omit<RequestInit, "body"> {
  /** これを渡すと POST + JSON で送る */
  json?: unknown;
  body?: BodyInit | null;
}

export async function api<T>(path: string, opts?: ApiOptions): Promise<T> {
  return withCurrentWebBuild(async () => {
    const o: RequestInit = { ...opts };
    const headers: Record<string, string> = { ...((opts?.headers as Record<string, string>) ?? {}) };
    if (TOKEN) headers["X-Review-Token"] = TOKEN;
    if (opts && opts.json !== undefined) {
      o.method = opts.method ?? "POST";
      headers["Content-Type"] = "application/json";
      o.body = JSON.stringify(opts.json);
      delete (o as ApiOptions).json;
    }
    o.headers = headers;
    const res = await fetch(url(path), o);
    if (!res.ok) {
      let msg = String(res.status);
      try {
        const d = (await res.json()) as { detail?: string; error?: string };
        msg = d.detail ?? d.error ?? msg;
      } catch {
        /* 本文が JSON でないことはある */
      }
      throw new Error(msg);
    }
    if (res.status === 204) return null as T;
    return (await res.json()) as T;
  });
}

/** 例外から画面に出す文字列を取る */
export function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function fmtBytes(n: number | null | undefined): string {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return v.toFixed(i ? 1 : 0) + " " + u[i];
}

export function fmtSec(s: number | null | undefined): string {
  if (s == null) return "-";
  const t = Math.round(s);
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const x = t % 60;
  return h
    ? `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}`
    : `${m}:${String(x).padStart(2, "0")}`;
}

export function fmtTime(t: number | null | undefined): string {
  if (!t) return "-";
  const d = new Date(t * 1000);
  const p = (v: number) => String(v).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export const STATUS_LABEL: Record<JobStatus, string> = {
  new: "未処理",
  queued: "起動待ち",
  running: "処理中",
  done: "完了",
  failed: "失敗",
  canceled: "中断",
  interrupted: "中断（サーバ停止）",
};

export function statusLabel(status: JobStatus): string {
  return STATUS_LABEL[status] ?? status;
}
