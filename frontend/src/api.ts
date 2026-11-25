import type { Job, JobDetail, JobOptionsInput, ServiceConfig } from "./types";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError("連不到服務，請確認終端機裡的 summery 還在跑", 0);
  }
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { error: text.slice(0, 400) };
    }
  }
  if (!res.ok) {
    const detail = (data as { error?: string; detail?: string } | null) ?? null;
    throw new ApiError(detail?.error ?? detail?.detail ?? `請求失敗（HTTP ${res.status}）`, res.status);
  }
  return data as T;
}

export function optionParams(options: JobOptionsInput): URLSearchParams {
  const params = new URLSearchParams();
  if (options.style) params.set("style", options.style);
  if (options.summary_language) params.set("summary_language", options.summary_language);
  if (options.asr_language) params.set("asr_language", options.asr_language);
  if (options.instruction?.trim()) params.set("instruction", options.instruction.trim());
  if (options.chunk_seconds) params.set("chunk_seconds", String(options.chunk_seconds));
  if (options.concurrency) params.set("concurrency", String(options.concurrency));
  if (options.no_summary) params.set("no_summary", "1");
  if (options.no_chapters) params.set("no_chapters", "1");
  if (options.strict) params.set("strict", "1");
  return params;
}

export const api = {
  config: () => request<ServiceConfig>("/api/config"),

  jobs: () => request<{ jobs: Job[]; config: ServiceConfig }>("/api/jobs"),

  job: (id: string) => request<JobDetail>(`/api/jobs/${encodeURIComponent(id)}`),

  cancel: (id: string) => request<JobDetail>(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" }),

  /** 上傳：檔案直接當 body（後端讀 Content-Length），檔名走 X-Filename 標頭。 */
  upload: (file: File, options: JobOptionsInput, onProgress?: (fraction: number) => void) =>
    new Promise<JobDetail>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/api/jobs?${optionParams(options).toString()}`);
      xhr.responseType = "text";
      xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total);
      };
      xhr.onerror = () => reject(new ApiError("上傳中斷，請檢查網路或檔案大小後再試一次", 0));
      xhr.onload = () => {
        let data: unknown = null;
        try {
          data = JSON.parse(xhr.responseText || "null");
        } catch {
          data = null;
        }
        const holder = (data ?? {}) as { error?: string; detail?: string };
        if (xhr.status < 200 || xhr.status >= 300) {
          reject(new ApiError(holder.error ?? holder.detail ?? `上傳失敗（HTTP ${xhr.status}）`, xhr.status));
          return;
        }
        resolve(data as JobDetail);
      };
      xhr.send(file);
    }),
};

export const fileUrl = (jobId: string, name: string) =>
  `/api/jobs/${encodeURIComponent(jobId)}/files/${encodeURIComponent(name)}`;

export async function fetchText(url: string): Promise<string> {
  const res = await fetch(url);
  if (!res.ok) throw new ApiError(`讀取失敗（HTTP ${res.status}）`, res.status);
  return res.text();
}
