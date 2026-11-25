/** 顯示用格式化：時間、檔案大小、狀態文案。 */

export function clock(seconds?: number | null): string {
  if (seconds === undefined || seconds === null || !Number.isFinite(seconds)) return "--:--";
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

export function fileSize(bytes?: number): string {
  if (!bytes || bytes <= 0) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value >= 10 || i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
}

export function relativeTime(epoch?: number): string {
  if (!epoch) return "";
  const diff = Date.now() / 1000 - epoch;
  if (diff < 60) return "剛剛";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分鐘前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小時前`;
  const date = new Date(epoch * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export const STATUS_LABEL: Record<string, string> = {
  queued: "排隊中",
  running: "進行中",
  cancelling: "取消中",
  done: "完成",
  partial: "完成（有片段失敗）",
  failed: "失敗",
  cancelled: "已取消",
  unknown: "離線紀錄",
};

export const STATUS_TONE: Record<string, "neutral" | "progress" | "done" | "warn" | "error"> = {
  queued: "neutral",
  running: "progress",
  cancelling: "warn",
  done: "done",
  partial: "warn",
  failed: "error",
  cancelled: "neutral",
  unknown: "neutral",
};

export const STYLE_LABEL: Record<string, string> = {
  concise: "精簡",
  standard: "標準",
  comprehensive: "完整",
  bullets: "條列",
};

export const FORMAT_LABEL: Record<string, string> = {
  md: "逐字稿 Markdown",
  json: "逐字稿 JSON",
  txt: "純文字",
  srt: "字幕 SRT",
  vtt: "字幕 VTT",
  "minutes.md": "會議紀錄 Markdown",
  "summary.md": "摘要 Markdown",
  "summary.json": "摘要 JSON",
  "job.json": "工作紀錄",
  input: "原始影音",
};

/** 秒數 → 「約 N 分鐘」的口语說法，用於切塊預覽。 */
export function humanSeconds(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  const minutes = seconds / 60;
  return Number.isInteger(minutes) ? `${minutes} 分鐘` : `${minutes.toFixed(1)} 分鐘`;
}

/** 文件工作的「逐字稿」其實是上傳文件的原文。 */
export function formatLabel(format: string, isDocument: boolean): string {
  const label = FORMAT_LABEL[format] ?? format;
  return isDocument ? label.replace("逐字稿", "原文") : label;
}
