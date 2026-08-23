// Web アプリ（automosaic/webapp）との通信と、画面共通の書式。
//
// もとは static/common.js として素の script で読ませていた下回り。
// 中身は同じで、各画面のバンドルに畳み込んである。

import type { JobStatus } from "./api.js";

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

export interface ApiOptions extends Omit<RequestInit, "body"> {
  /** これを渡すと POST + JSON で送る */
  json?: unknown;
  body?: BodyInit | null;
}

export async function api<T>(path: string, opts?: ApiOptions): Promise<T> {
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
