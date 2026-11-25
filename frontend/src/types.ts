/** summery 後端 API 的型別定義（對應 src/summery/web.py 的輸出）。 */

export type JobStatus =
  | "queued"
  | "running"
  | "cancelling"
  | "done"
  | "partial"
  | "failed"
  | "cancelled"
  | "unknown";

export interface ServiceConfig {
  asr: { base_url: string; model: string; stub: boolean; key_configured: boolean };
  llm: { base_url: string; model: string; stub: boolean; key_configured: boolean };
  chunking: {
    target_seconds: number;
    max_seconds: number;
    min_seconds: number;
    overlap_seconds: number;
  };
  summarize: { language: string; style: string; chapters: boolean; max_chapters: number };
  limits?: {
    max_upload_bytes: number;
    chunk_seconds_cap: number | null;
    effective_chunk_seconds: number;
    audio_codec: string;
    audio_bitrate: string;
  };
  work_dir: string;
  documents?: {
    extensions: string[];
    kinds?: Record<string, string>;
    pdf?: { available: boolean; via?: string | null; error?: string };
  };
}

/** POST /api/documents 的回應：暫存並解析好的一份文件。 */
export interface DocInfo {
  doc_id: string;
  name: string;
  size: number;
  kind: string;
  kind_label: string;
  chars: number;
  paragraphs: number;
  timed: boolean;
  preview: string;
  warnings: string[];
}

export type JobKind = "media" | "document";

export interface OpenQuestion {
  text: string;
  para?: number | null;
  start?: number | null;
  clock?: string | null;
  located?: string;
}

export interface JobFile {
  format: string;
  name: string;
  size: number;
  url: string;
}

export interface JobError {
  chunk?: number;
  start?: number;
  end?: number;
  error?: string;
  [key: string]: unknown;
}

export interface JobSummaryMeta {
  mode?: string;
  paragraphs?: number;
  chars?: number;
  estimated_input_tokens?: number;
  map_blocks?: number;
  reduce_rounds?: number;
  chapters_source?: string;
  warnings?: string[];
  input_kind?: JobKind;
  documents?: Array<{ name: string; kind?: string; chars?: number }>;
  minutes?: { source?: string; format?: "template" | "default"; template?: string[] };
  reference?: { files?: string[]; outline?: string[]; attendees?: string[]; terms?: string[]; source?: string };
  llm_usage?: { calls?: number; total_tokens?: number; seconds?: number };
  [key: string]: unknown;
}

export interface Chapter {
  start: number;
  end: number;
  clock?: string;
  title: string;
  summary?: string;
  points?: string[];
  para_start?: number;
  para_end?: number;
}

export interface ActionItem {
  task: string;
  owner?: string;
  due?: string;
  clock?: string;
}

export interface Quote {
  text: string;
  speaker?: string;
  clock?: string;
}

export interface SummaryPayload {
  schema?: string;
  title: string;
  tldr: string;
  overview: string[];
  key_points: string[];
  chapters: Chapter[];
  keywords: string[];
  decisions: string[];
  action_items: ActionItem[];
  open_questions: Array<string | OpenQuestion>;
  quotes: Quote[];
  minutes_markdown?: string;
  meta?: JobSummaryMeta;
}

export interface ResultPayload {
  summary?: SummaryPayload;
  transcript?: {
    duration?: number;
    stats?: { segments?: number; chars?: number; cjk?: boolean };
    segments?: number;
    text?: string;
    truncated?: boolean;
    source_kind?: string | null;
  };
  summary_markdown?: string;
  minutes_markdown?: string;
}

export interface Job {
  job_id: string;
  name: string;
  status: JobStatus;
  created_at: number;
  error?: string | null;
  input?: string;
  out_dir?: string;
  options?: Record<string, unknown>;
  stage?: string | null;
  progress?: number;
  message?: string;
  counts?: Record<string, number>;
  outputs?: Record<string, string>;
  errors?: JobError[];
  updated_at?: number;
  finished_at?: number;
  historical?: boolean;
  kind?: JobKind;
  references?: string[];
  documents?: string[];
}

export interface JobDetail extends Job {
  files: JobFile[];
  result?: ResultPayload;
  media_url?: string | null;
  duration?: number | null;
}

export interface JobOptionsInput {
  style?: "concise" | "standard" | "comprehensive" | "bullets";
  summary_language?: string;
  asr_language?: string;
  instruction?: string;
  chunk_seconds?: number;
  concurrency?: number;
  no_summary?: boolean;
  no_chapters?: boolean;
  strict?: boolean;
  /** 影音工作另外產出 minutes.md（有範本或文件工作會自動產出） */
  minutes?: boolean;
}

export interface Stage {
  key: string;
  label: string;
  hint: string;
}

const MEDIA_STAGES: Stage[] = [
  { key: "probe", label: "讀取媒體", hint: "長度、音軌、是否含視訊" },
  { key: "audio", label: "正規化音訊", hint: "16kHz 單聲道" },
  { key: "silence", label: "掃描靜音點", hint: "長音訊需要數十秒" },
  { key: "plan", label: "安排切塊", hint: "邊界對齊到句子的停頓" },
  { key: "asr", label: "語音轉文字", hint: "多塊平行送出" },
];
const REFERENCE_STAGE: Stage = { key: "reference", label: "分析過往紀錄", hint: "段落結構、用語、與會者與專有名詞" };
const SUMMARY_STAGE: Stage = { key: "summarize", label: "產生摘要", hint: "重點、章節與待辦" };
const MINUTES_STAGE: Stage = { key: "minutes", label: "撰寫會議紀錄", hint: "依範本的格式與段落順序" };

/** 依工作型態（影音／文件、有沒有範本）組出進度階段。 */
export function stagesFor(job: Pick<Job, "kind" | "references" | "options">): Stage[] {
  const hasRefs = Boolean(job.references?.length);
  const isDoc = job.kind === "document";
  const wantsMinutes = isDoc || hasRefs || job.options?.minutes === true;
  const head: Stage[] = isDoc ? [{ key: "document", label: "讀取文件", hint: "段落、標題、表格與編號" }] : MEDIA_STAGES;
  const minutes = wantsMinutes ? [{ ...MINUTES_STAGE, hint: hasRefs ? MINUTES_STAGE.hint : "標準會議紀錄格式" }] : [];
  return [
    ...head,
    ...(hasRefs ? [REFERENCE_STAGE] : []),
    SUMMARY_STAGE,
    ...minutes,
    { key: "write", label: "輸出檔案", hint: isDoc ? "會議紀錄、摘要與原文" : "逐字稿與摘要檔" },
  ];
}

/** 上傳卡可直接接受的影音副檔名 */
export const MEDIA_EXT = [
  ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".ts", ".mpg", ".mpeg",
  ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma", ".amr", ".aiff", ".caf",
];
export const DOC_EXT = [".docx", ".md", ".markdown", ".txt", ".pdf", ".srt", ".vtt", ".json"];

export function extOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i).toLowerCase() : "";
}
