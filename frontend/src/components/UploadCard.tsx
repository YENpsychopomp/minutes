import { useId, useRef, useState } from "react";
import { fileSize } from "../format";
import type { JobOptionsInput, ServiceConfig } from "../types";
import { IconClose, IconDoc, IconSpinner, IconUpload } from "./Icons";

const ACCEPT = ".mp4,.mov,.mkv,.webm,.avi,.m4v,.flv,.ts,.mpg,.mpeg,.mp3,.wav,.m4a,.aac,.ogg,.opus,.flac,.amr,.aiff,.caf";

const STYLES: { value: NonNullable<JobOptionsInput["style"]>; label: string; hint: string }[] = [
  { value: "concise", label: "精簡", hint: "只要重點與少量章節" },
  { value: "standard", label: "標準", hint: "TL;DR、章節、重點、待辦" },
  { value: "comprehensive", label: "完整", hint: "含金句、未決問題，篇幅較長" },
  { value: "bullets", label: "條列", hint: "全以條列呈現，適合會議" },
];

const LANGS = [
  { value: "zh-TW", label: "繁體中文" },
  { value: "zh-CN", label: "简体中文" },
  { value: "en", label: "English" },
  { value: "ja", label: "日本語" },
];

const CHUNKS = [
  { value: 30, label: "30 秒（測試用）" },
  { value: 60, label: "1 分鐘" },
  { value: 180, label: "3 分鐘" },
  { value: 300, label: "5 分鐘（建議）" },
  { value: 600, label: "10 分鐘（長講座）" },
];

interface Props {
  file: File | null;
  onPick: (file: File | null) => void;
  options: JobOptionsInput;
  onOptions: (patch: Partial<JobOptionsInput>) => void;
  onSubmit: () => void;
  busy: boolean;
  uploadProgress: number | null;
  config?: ServiceConfig;
  isOver?: boolean;
}

export function UploadCard({
  file,
  onPick,
  options,
  onOptions,
  onSubmit,
  busy,
  uploadProgress,
  config,
  isOver,
}: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const styleHintId = useId();
  const [showMore, setShowMore] = useState(false);
  const activeStyle = STYLES.find((s) => s.value === (options.style ?? "standard")) ?? STYLES[1];
  const chunkDefault = config?.chunking.target_seconds;
  const chosenChunk = Number(options.chunk_seconds ?? chunkDefault ?? 300);
  const chunkCap = config?.limits?.chunk_seconds_cap ?? null;
  const capped = chunkCap !== null && chosenChunk > chunkCap ? chunkCap : null;

  return (
    <section className="sheet fade-up">
      <div className="sheet-head">
        <span className="eyebrow">第 1 步</span>
        <h2>放進一個檔案</h2>
      </div>
      <div className="sheet-pad stack">
        {file ? (
          <div className="picked">
            <span className="drop-icon" style={{ width: 44, height: 44, margin: 0 }}>
              <IconDoc size={20} />
            </span>
            <div style={{ minWidth: 0 }}>
              <div className="picked-name">{file.name}</div>
              <div className="picked-meta">{fileSize(file.size)}・準備上傳</div>
            </div>
            <button
              type="button"
              className="btn btn-ghost"
              style={{ marginLeft: "auto" }}
              onClick={() => {
                onPick(null);
                if (inputRef.current) inputRef.current.value = "";
              }}
              disabled={busy}
            >
              <IconClose size={16} />
              換一個
            </button>
          </div>
        ) : (
          <label className={`dropzone${isOver ? " is-over" : ""}`}>
            <input
              ref={inputRef}
              type="file"
              accept={ACCEPT}
              onChange={(event) => onPick(event.target.files?.[0] ?? null)}
              aria-describedby="drop-hint"
            />
            <span className="drop-icon">
              <IconUpload size={24} />
            </span>
            <span className="drop-title" style={{ display: "block" }}>
              把影音檔拖進來，或點此選擇
            </span>
            <span className="drop-hint" id="drop-hint" style={{ display: "block" }}>
              支援 mp4、mov、mkv、mp3、wav、m4a、ogg 等；長影片、長錄音都可以
            </span>
          </label>
        )}

        {uploadProgress !== null ? (
          <div>
            <div className="progress-meta">
              <span>上傳中</span>
              <span className="num">{Math.round(uploadProgress * 100)}%</span>
            </div>
            <div className="progress-track" role="progressbar" aria-valuenow={Math.round(uploadProgress * 100)} aria-valuemin={0} aria-valuemax={100}>
              <div className="progress-fill" style={{ width: `${Math.max(2, uploadProgress * 100)}%` }} />
            </div>
          </div>
        ) : null}

        <hr className="rule" />

        <div className="field">
          <span className="legend" id={styleHintId}>
            摘要風格
          </span>
          <div className="segmented" role="group" aria-describedby={styleHintId}>
            {STYLES.map((style) => (
              <button
                key={style.value}
                type="button"
                aria-pressed={(options.style ?? "standard") === style.value}
                onClick={() => onOptions({ style: style.value })}
              >
                {style.label}
              </button>
            ))}
          </div>
          <span className="field-hint">{activeStyle.hint}</span>
        </div>

        <div className="field-grid">
          <div className="field">
            <label htmlFor="sum-lang">摘要語言</label>
            <select
              id="sum-lang"
              value={options.summary_language ?? "zh-TW"}
              onChange={(event) => onOptions({ summary_language: event.target.value })}
            >
              {LANGS.map((lang) => (
                <option key={lang.value} value={lang.value}>
                  {lang.label}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="chunk-sec">每塊長度</label>
            <select
              id="chunk-sec"
              value={String(options.chunk_seconds ?? chunkDefault ?? 300)}
              onChange={(event) => onOptions({ chunk_seconds: Number(event.target.value) })}
            >
              {CHUNKS.map((chunk) => (
                <option key={chunk.value} value={chunk.value}>
                  {chunk.label}
                </option>
              ))}
            </select>
            <span className="field-hint">
              {capped
                ? `這台服務每塊最長約 ${Math.floor(capped / 60)} 分鐘（上傳上限 ${fileSize(config?.limits?.max_upload_bytes)}），系統會自動縮短`
                : "越長越省呼叫次數，但單次要處理較多內容"}
            </span>
          </div>
          <div className="field">
            <label htmlFor="conc">平行上傳</label>
            <select
              id="conc"
              value={String(options.concurrency ?? 4)}
              onChange={(event) => onOptions({ concurrency: Number(event.target.value) })}
            >
              {[1, 2, 4, 6, 8].map((n) => (
                <option key={n} value={n}>
                  {n} 路
                </option>
              ))}
            </select>
            <span className="field-hint">服務限流時調低</span>
          </div>
        </div>

        <button type="button" className="btn btn-ghost" style={{ alignSelf: "flex-start", paddingLeft: 0 }} onClick={() => setShowMore((v) => !v)}>
          {showMore ? "收進進階選項" : "進階選項（自訂指示、不做摘要…）"}
        </button>

        {showMore ? (
          <div className="stack fade-up" style={{ gap: 10 }}>
            <div className="field">
              <label htmlFor="instruction">額外指示（選填）</label>
              <textarea
                id="instruction"
                placeholder="例：請特別標出師生的提問與回答；專有名詞「某某」請保留原文。"
                value={options.instruction ?? ""}
                onChange={(event) => onOptions({ instruction: event.target.value })}
              />
            </div>
            <div className="switch-row">
              <input
                id="no-summary"
                type="checkbox"
                checked={Boolean(options.no_summary)}
                onChange={(event) => onOptions({ no_summary: event.target.checked })}
              />
              <span className="switch-text">
                <strong>只做逐字稿，不做摘要</strong>
                <span>省下游模型的費用與時間</span>
              </span>
            </div>
            <div className="switch-row">
              <input
                id="no-chapters"
                type="checkbox"
                checked={Boolean(options.no_chapters)}
                onChange={(event) => onOptions({ no_chapters: event.target.checked })}
              />
              <span className="switch-text">
                <strong>不要章節大綱</strong>
                <span>輸出較短的摘要</span>
              </span>
            </div>
            <div className="switch-row">
              <input
                id="strict"
                type="checkbox"
                checked={Boolean(options.strict)}
                onChange={(event) => onOptions({ strict: event.target.checked })}
              />
              <span className="switch-text">
                <strong>嚴格模式</strong>
                <span>任何一個片段失敗就中斷，不保留部分結果</span>
              </span>
            </div>
          </div>
        ) : null}

        <button
          type="button"
          className="btn btn-primary btn-block"
          onClick={onSubmit}
          disabled={!file || busy}
          aria-describedby={file ? undefined : "submit-hint"}
        >
          {busy ? <IconSpinner /> : <IconUpload />}
          {busy ? "上傳中…" : "開始轉錄並摘要"}
        </button>
        {!file ? <span className="field-hint" id="submit-hint">先選擇一個檔案，按鈕就會亮起。</span> : null}
      </div>
    </section>
  );
}
