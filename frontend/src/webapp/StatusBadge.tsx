// ジョブの状態バッジ。一覧とジョブ画面で使い回す。

import type { JobStatus } from "../shared/api.js";
import { statusLabel } from "../shared/webapp-net.js";

export function StatusBadge({ status }: { status: JobStatus }) {
  return <span class={"badge " + status}>{statusLabel(status)}</span>;
}
