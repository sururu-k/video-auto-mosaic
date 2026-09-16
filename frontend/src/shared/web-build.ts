/** Web 画面と、起動済み API のビルド版を突き合わせる純粋な判断。 */

const BUILD_ID = /^[0-9a-f]{64}$/;

function shownBuildId(value: unknown): string {
  return typeof value === "string" && BUILD_ID.test(value)
    ? value.slice(0, 12)
    : "取得不能";
}

/** 一致していれば null。不一致・欠落・壊れた値なら操作停止用の文言を返す。 */
export function webBuildProblem(frontendId: unknown, serverId: unknown): string | null {
  if (
    typeof frontendId === "string" &&
    typeof serverId === "string" &&
    BUILD_ID.test(frontendId) &&
    frontendId === serverId
  ) {
    return null;
  }
  return (
    "画面とサーバのバージョンが一致しないか、確認できません" +
    `（画面 ${shownBuildId(frontendId)} / サーバ ${shownBuildId(serverId)}）。` +
    "誤った API へ操作を送らないため、この画面の操作を停止しました。" +
    "処理中のジョブが無いことを確認してからサーバを再起動し、画面を再読み込みしてください。"
  );
}

/** 一致確認が済むまで操作を呼ばず、不一致ならそのまま拒否する。 */
export async function withMatchingWebBuild<T>(
  problem: Promise<string | null>,
  operation: () => T | Promise<T>,
): Promise<T> {
  const message = await problem;
  if (message) throw new Error(message);
  return await operation();
}
