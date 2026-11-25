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
}

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
  };
  summary_markdown?: string;
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
}

export const STAGES: { key: string; label: string; hint: string }[] = [
  { key: "probe", label: "讀取媒體", hint: "長度、音軌、是否含視訊" },
  { key: "audio", label: "正規化音訊", hint: "16kHz 單聲道" },
  { key: "silence", label: "掃描靜音點", hint: "長音訊需要數十秒" },
  { key: "plan", label: "安排切塊", hint: "邊界對齊到句子的停頓" },
  { key: "asr", label: "語音轉文字", hint: "多塊平行送出" },
  { key: "summarize", label: "產生摘要", hint: "重點、章節與待辦" },
  { key: "write", label: "輸出檔案", hint: "逐字稿與摘要檔" },
];
