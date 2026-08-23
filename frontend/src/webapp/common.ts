// 画面共通の下回り。
//
// 中身は shared/webapp-net.ts にある。各画面はそちらを直接取り込むので
// このファイル自体は使っていないが、HTML が
// <script src="/static/common.js"> で読み続けているので、
// 同じ名前を素の script と同じようにグローバルへ置いておく。
// HTML を触らずに済ませるための互換層。

import {
  STATUS_LABEL,
  TOKEN,
  api,
  fmtBytes,
  fmtSec,
  fmtTime,
  link,
  statusLabel,
  url,
} from "../shared/webapp-net.js";

const $ = (id: string): HTMLElement | null => document.getElementById(id);

Object.assign(globalThis, {
  $,
  TOKEN,
  url,
  link,
  api,
  fmtBytes,
  fmtSec,
  fmtTime,
  STATUS_LABEL,
  statusLabel,
});
