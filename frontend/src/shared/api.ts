// サーバとやり取りする JSON の形。
//
// ここに書いてあるのは推測ではなく、Python 側が実際に返している形。
// 出どころは以下のとおりで、あちらを直したらここも直す。
//
//   automosaic/review.py     ReviewSession.state_payload() / queue_payload()
//                            / progress_payload() / update_payload()
//                            / frame_regions_payload() と、do_GET / do_POST の各分岐
//   automosaic/corrections.py Correction.as_dict()
//   automosaic/webapp/app.py  /api/jobs 以下すべて
//   automosaic/webapp/jobs.py Job.summary() / Job.detail()
//   automosaic/webapp/runner.py JobRunner.snapshot() / _set_progress()
//   automosaic/webapp/handdraw.py load() / put() / expand()

// --------------------------------------------------------------------
// レビューセッション（review.py）
// --------------------------------------------------------------------

/** 判定の種類。review.py の VERDICTS と一対一。文字列の打ち間違いはここで止まる */
export type Verdict = "ok" | "fixed" | "unsure" | "toobig" | "false_positive";

/** 矩形の由来。review.py の SOURCE_CODE の値。d=検出 i=補間 m=memory b=橋渡し x=手修正 */
export type SourceCode = "d" | "i" | "m" | "b" | "x";

/** 検査キューに載せた理由。review.py の QUEUE_REASONS のキー */
export type QueueReason =
  | "despiked"
  | "estimated"
  | "uncovered"
  | "area_jump"
  | "low_conf"
  | "sampled";

/** 手修正の種類。add=漏れを埋める remove=過剰なモザイクを打ち消す */
export type CorrectionKind = "add" | "remove";

/** 矩形。[x, y, w, h] いずれもフレーム座標のピクセル */
export type Box = [number, number, number, number];

/**
 * 画面に重ねる領域。ReviewSession.frame_regions() が返す並びそのまま。
 * [x, y, w, h, 由来, スコア] で、座標は膨張後（実際にモザイクが乗る範囲）。
 */
export type Region = [number, number, number, number, SourceCode, number];

/** 手修正1件。corrections.Correction.as_dict() と同じ */
export interface Correction {
  frame: number;
  box: Box;
  class: string;
  kind: CorrectionKind;
}

/** 区間。ranges_payload() が返す */
export interface FrameRange {
  start: number;
  end: number;
  frames: number;
}

/** despike が捨てた帯。区間 + どのクラスをどのスコアで捨てたか */
export interface DespikedRange extends FrameRange {
  class: string;
  max_score: number;
}

export interface RangesPayload {
  estimated_only_ranges: FrameRange[];
  uncovered_ranges: FrameRange[];
  despiked_ranges: DespikedRange[];
}

/**
 * issue #16: report.json の実効設定フィンガープリントとの突き合わせ結果。
 * webapp/app.py の _check_effective_settings() が組む。突き合わせで
 * 食い違えば /state 自体が 409 になるので、ここに乗るのは通った場合のみ。
 * restored=true は「report.json に記録が無く meta.argv から復元した」印で、
 * 復元は「今のコードの絞り込みが焼いたときと同じ」という仮定に基づく
 * （確実な一致の保証ではない）。黙って通さず、画面に出し続けること。
 */
export interface EffectiveCheck {
  restored: boolean;
  note: string | null;
}

/** state_payload(light=True) の中身。重い配列を落としたもの */
export interface StateLight {
  video: string;
  rendered: string | null;
  has_video: boolean;
  corrections_path: string;
  width: number;
  height: number;
  fps: number;
  n_frames: number;
  block: number;
  classes: string[];
  default_size: [number, number];
  default_class: string;
  stats: Record<string, unknown>;
  version: number;
  queue_step: number;
  n_corrections: number;
  effective_check: EffectiveCheck;
}

/**
 * state_payload(light=False) の中身。
 * coverage は1フレーム1文字（0=素通し 1=実観測 2=推定のみ）。
 * regions のキーはフレーム番号の文字列で、矩形が無いフレームは載らない。
 */
export interface StateFull extends StateLight, RangesPayload {
  coverage: string;
  regions: Record<string, Region[]>;
}

/** 検査キューの1枚 */
export interface QueueItem {
  frame: number;
  reason: QueueReason;
  priority: number;
  label: string;
  /** まだ判定していなければ null */
  verdict: Verdict | null;
  boxes: Region[];
}

/** progress_payload()。counts は VERDICTS 全部のキーを必ず持つ */
export interface Progress {
  total: number;
  done: number;
  remaining: number;
  counts: Record<Verdict, number>;
  can_undo: boolean;
}

/** queue_payload() */
export interface QueuePayload {
  step: number;
  all_frames: boolean;
  version: number;
  n_corrections: number;
  items: QueueItem[];
  progress: Progress;
}

/**
 * /api/mark に送る中身。
 * x, y は正規化タップ座標（0..1）。画面ピクセルを送ると端末ごとの
 * devicePixelRatio の扱いで静かにずれるので、比率だけを送る。
 * pick は「誤検知」で消す自動領域を座標で指す。番号で送ると、
 * 送っている間にキューが組み直されたときに指すものが変わる。
 *
 * start_frame / start_x / start_y / start_w / start_h は区間の始点
 * （issue #46）。frame/x/y/w/h は区間の終点（＝いま見ているコマ）を指す。
 * 始点を送ると verdict は "fixed" 専用（サーバ側 review.mark_interval）。
 */
export interface MarkRequest {
  frame: number;
  verdict: Verdict;
  x?: number;
  y?: number;
  w?: number;
  h?: number;
  span?: number;
  class?: string;
  pick?: Box[];
  start_frame?: number;
  start_x?: number;
  start_y?: number;
  start_w?: number;
  start_h?: number;
}

export interface MarkResponse {
  ok: true;
  frame: number;
  verdict: Verdict;
  added: number;
  version: number;
  n_corrections: number;
  regions: Region[];
  progress: Progress;
}

/** /api/undo。戻せる操作が無いときは 200 で ok:false が返る */
export type UndoResponse =
  | { ok: false; error: string }
  | {
      ok: true;
      frame: number;
      removed: number;
      version: number;
      n_corrections: number;
      regions: Region[];
      progress: Progress;
    };

/** GET /api/corrections */
export interface CorrectionsPayload {
  video: string;
  width: number;
  height: number;
  corrections: Correction[];
  corrections_sha256: string;
  /** state=1 の競合回復用。同じサーバロック時点の派生状態 */
  state?: StateFull;
}

/**
 * POST /api/corrections の戻り。update_payload()。
 *
 * 本文に frame を渡すと、regions はそのコマ1つぶんだけになる（#24）。
 * 渡さなければ従来どおり全フレームぶん（後方互換）。timeline.tsx は
 * 常に frame を渡すので、キーは1個だけだと思ってよい。
 */
export interface UpdatePayload extends RangesPayload {
  ok: true;
  coverage: string;
  regions: Record<string, Region[]>;
  version: number;
  n_corrections: number;
  corrections_sha256: string;
}

/** GET /api/regions。表示中の1コマぶんだけ矩形を返す軽量版（#24） */
export interface RegionsResponse {
  frame: number;
  version: number;
  regions: Region[];
}

// --------------------------------------------------------------------
// Web アプリ（webapp/）
// --------------------------------------------------------------------

/** jobs.py の STATUS_* */
export type JobStatus =
  | "new"
  | "queued"
  | "running"
  | "done"
  | "failed"
  | "canceled"
  | "interrupted";

/** 起動設定のうち値を取るもの。job.html の入力欄の id とそろえてある */
export const SETTING_FIELDS = [
  "infer_size",
  "conf",
  "classes",
  "mode",
  "block",
  "crf",
  "limit_frames",
  "provider",
] as const;

/** 起動設定のうちチェックボックスのもの */
export const SETTING_FLAGS = ["tta", "estimate_gaps", "detect_only"] as const;

export type SettingField = (typeof SETTING_FIELDS)[number];
export type SettingFlag = (typeof SETTING_FLAGS)[number];

/**
 * 起動設定。画面からは文字列で入るが、meta.json から読み直したものは
 * 数値のこともある（CLI の既定値をそのまま書いてあるため）。
 */
/** null は「この項目は指定しない」。キーを落とすと、保存済み設定に残ったままになる */
export type JobSettings = Partial<Record<SettingField, string | number | null>> &
  Partial<Record<SettingFlag, boolean>>;

/** runner.py の _set_progress() が積む1パスぶんの進捗 */
export interface PassProgress {
  label: string;
  n: number;
  total: number;
  /** total が 0 のときは null */
  percent: number | null;
  fps: number;
  eta: string;
  seq: number;
  t: number;
}

/** パスは走り始めるまでキーごと無い */
export interface JobProgress {
  pass1?: PassProgress;
  pass2?: PassProgress;
}

/** Job.summary() */
export interface JobSummary {
  id: string;
  name: string;
  created_at: number | null;
  status: JobStatus;
  size_bytes: number;
  width: number | null;
  height: number | null;
  fps: number | null;
  n_frames: number | null;
  duration_sec: number | null;
  has_source: boolean;
  has_detections: boolean;
  has_output: boolean;
  has_dataset: boolean;
  n_corrections: number;
  elapsed_sec: number | null;
  progress: JobProgress;
  error: string | null;
}

/** proxy.py の STATUS_*。生成を一度も始めていなければ null（キー自体は常にある） */
export type ProxyStatus = "generating" | "done" | "failed" | null;

/** Job.detail() */
export interface JobDetail extends JobSummary {
  settings: JobSettings;
  stats: Record<string, unknown>;
  argv: string[];
  output_size_bytes: number;
  /** 素通し（モザイクが1つも乗っていない）区間の数。焼く前は null */
  n_uncovered_ranges: number | null;
  /** 検出が1つも無く、補間と memory だけで塗っている区間の数。焼く前は null */
  n_estimated_only_ranges: number | null;
  /** 確認用プロキシ動画（issue #18）の生成状態。「まだ無い」（null）と
   *  「作れなかった」（failed）を画面が区別するためのもの */
  proxy_status: ProxyStatus;
  proxy_error: string | null;
  has_proxy: boolean;
  proxy_size_bytes: number;
}

/** GET /api/jobs/{id}。job_state() は detail に生存確認を足す */
export interface JobState extends JobDetail {
  alive: boolean;
  seq: number;
  detached: boolean;
}

/** GET /api/jobs */
export interface JobsPayload {
  jobs: JobSummary[];
  library: string;
  /** 走っているジョブの id */
  active: string[];
}

/** ログ1行。連番付きで、取りこぼしたぶんだけ後から取り直せる */
export interface LogLine {
  seq: number;
  text: string;
  t: number;
}

/** SSE の progress / end イベント、および GET .../progress の戻り */
export interface ProgressSnapshot {
  job: string;
  status: JobStatus;
  progress: JobProgress;
  log: LogLine[];
  seq: number;
  alive: boolean;
  error?: string | null;
  has_output?: boolean;
  returncode?: number | null;
  elapsed_sec?: number | null;
  detail?: JobDetail;
}

/** GET /api/jobs/{id}/log */
export interface LogPayload {
  lines: string[];
}

/** POST /api/jobs/{id}/start */
export interface StartResponse {
  ok: true;
  id: string;
  argv: string[];
  status: JobStatus;
}

/** POST /api/jobs/{id}/dataset */
export interface DatasetResponse {
  ok: true;
  dir: string;
  frames: number;
  classes: string[];
}

/**
 * 手描きの打点。box が null なら「ここには無い」で、そこで補間が止まる。
 * class は打つときに付けていなければ無い。
 */
export interface Annotation {
  frame: number;
  box: Box | null;
  class?: string;
}

export interface AnnotationsPayload {
  annotations: Annotation[];
}

/** POST /api/jobs/{id}/annotations/expand */
export interface ExpandResponse {
  ok: true;
  annotations: number;
  points: number;
  corrections: number;
  expanded: number;
  /** 打点が1つも無ければ null */
  frame_range: [number, number] | null;
}
