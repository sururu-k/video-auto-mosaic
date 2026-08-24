// ジョブ画面の判断のうち、Preact にも DOM にも依存しない部分。
//
// issue #18: プロキシ動画の生成状態（未生成・生成中・完成・失敗）を
// 画面が区別して出す。「まだ無い」（null）と「作れなかった」（failed）を
// 同じ表示にしないことが要件そのものなので、4状態それぞれに違う文字列が
// 出ることを node から直接確かめられるようにしてある。

import type { ProxyStatus } from "./api.js";

/** プロキシの生成状態を画面表示用の文字列にする。4状態を区別すること自体が要件 */
export function proxyLabel(status: ProxyStatus): string {
  switch (status) {
    case "generating":
      return "生成中";
    case "done":
      return "完成";
    case "failed":
      return "失敗";
    default:
      return "未生成";
  }
}
