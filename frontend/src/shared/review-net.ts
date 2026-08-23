// 単体レビューサーバ（automosaic/review.py）との通信。
//
// トークンは URL から拾う。サーバが Cookie にも移すので画像は素の URL でも
// 通るが、Cookie が消えている端末でも動くように毎回付け直す。

export const TOKEN = new URLSearchParams(location.search).get("t") ?? "";

export type QueryValue = string | number | boolean;

export function url(path: string, params?: Record<string, QueryValue>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params ?? {})) p.set(k, String(v));
  if (TOKEN) p.set("t", TOKEN);
  const q = p.toString();
  return q ? path + "?" + q : path;
}

export async function get<T>(path: string, params?: Record<string, QueryValue>): Promise<T> {
  const res = await fetch(url(path, params));
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return (await res.json()) as T;
}

export async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(url(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return (await res.json()) as T;
}

/** 例外から画面に出す文字列を取る。Error 以外が飛んでくることもある */
export function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
