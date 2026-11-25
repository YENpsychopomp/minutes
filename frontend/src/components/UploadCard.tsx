import { useId, useRef, useState } from "react";
import { fileSize } from "../format";
import { DOC_EXT, MEDIA_EXT, type DocInfo, type JobOptionsInput, type ServiceConfig } from "../types";
import { IconAlert, IconCheck, IconClose, IconDoc, IconFeather, IconSpinner, IconUpload } from "./Icons";

const ACCEPT_ALL = [...MEDIA_EXT, ...DOC_EXT].join(",");
const ACCEPT_DOCS = DOC_EXT.join(",");

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

/** 一份已選的文件：選了就先上傳、讓後端解析，讀不到（例如掃描 PDF）當場就知道。 */
export interface DocItem {
  key: string;
  file: File;
  status: "uploading" | "ready" | "error";
  progress: number;
  info?: DocInfo;
  error?: string;
}

interface Props {
  media: File | null;
  docs: DocItem[];
  refs: DocItem[];
  onPickMain: (files: File[]) => void;
  onClearMedia: () => void;
  onRemoveDoc: (key: string) => void;
  onPickRefs: (files: File[]) => void;
  onRemoveRef: (key: string) => void;
  options: JobOptionsInput;
  onOptions: (patch: Partial<JobOptionsInput>) => void;
  onSubmit: () => void;
  canSubmit: boolean;
  busy: boolean;
  uploadProgress: number | null;
  config?: ServiceConfig;
  isOver?: boolean;
}

function DocList({ items, onRemove, busy, label }: { items: DocItem[]; onRemove: (key: string) => void; busy: boolean; label: string }) {
  if (!items.length) return null;
  return (
    <ul className="doc-list" aria-label={label}>
      {items.map((item) => (
        <li className="doc-row" key={item.key} data-state={item.status}>
          <span className="doc-row-icon" aria-hidden="true">
            {item.status === "uploading" ? <IconSpinner size={16} /> : item.status === "error" ? <IconAlert size={16} /> : <IconCheck size={16} />}
          </span>
          <span className="grow">
            <span className="doc-row-name">{item.file.name}</span>
            <span className="doc-row-meta">
              {item.status === "uploading"
                ? `上傳並解析中… ${Math.round(item.progress * 100)}%`
                : item.status === "error"
                  ? item.error
                  : `${item.info?.kind_label ?? "文件"}・${item.info?.paragraphs ?? 0} 段・約 ${(item.info?.chars ?? 0).toLocaleString("zh-TW")} 字`}
            </span>
            {item.info?.warnings?.length ? <span className="doc-row-meta doc-row-warn">{item.info.warnings.join("；")}</span> : null}
          </span>
          <button type="button" className="btn btn-ghost" onClick={() => onRemove(item.key)} disabled={busy} aria-label={`移除 ${item.file.name}`}>
            <IconClose size={15} />
          </button>
        </li>
      ))}
    </ul>
  );
}

export function UploadCard({
  media,
  docs,
  refs,
  onPickMain,
  onClearMedia,
  onRemoveDoc,
  onPickRefs,
  onRemoveRef,
  options,
  onOptions,
  onSubmit,
  canSubmit,
  busy,
  uploadProgress,
  config,
  isOver,
}: Props) {
  const mainRef = useRef<HTMLInputElement>(null);
  const moreRef = useRef<HTMLInputElement>(null);
  const refsRef = useRef<HTMLInputElement>(null);
  const styleHintId = useId();
  const [showMore, setShowMore] = useState(false);
  const activeStyle = STYLES.find((s) => s.value === (options.style ?? "standard")) ?? STYLES[1];
  const chunkDefault = config?.chunking.target_seconds;
  const chosenChunk = Number(options.chunk_seconds ?? chunkDefault ?? 300);
  const chunkCap = config?.limits?.chunk_seconds_cap ?? null;
  const capped = chunkCap !== null && chosenChunk > chunkCap ? chunkCap : null;
  const docMode = !media && docs.length > 0;
  const hasRefs = refs.some((r) => r.status !== "error");
  const pdfMissing = config?.documents?.pdf ? !config.documents.pdf.available : false;
  const pick = (input: HTMLInputElement | null, handler: (files: File[]) => void) => {
    const files = Array.from(input?.files ?? []);
    if (files.length) handler(files);
    if (input) input.value = "";
  };

  const submitLabel = busy
    ? "上傳中…"
    : docMode
      ? hasRefs
        ? "依範本產出會議紀錄"
        : "根據文件產出會議紀錄"
      : hasRefs
        ? "轉錄並依範本產出會議紀錄"
        : "開始轉錄並摘要";

  return (
    <section className="sheet fade-up">
      <div className="sheet-head">
        <span className="eyebrow">第 1 步</span>
        <h2>放進影音或文件</h2>
      </div>
      <div className="sheet-pad stack">
        {media ? (
          <div className="picked">
            <span className="drop-icon" style={{ width: 44, height: 44, margin: 0 }}>
              <IconDoc size={20} />
            </span>
            <div style={{ minWidth: 0 }}>
              <div className="picked-name">{media.name}</div>
              <div className="picked-meta">{fileSize(media.size)}・準備上傳</div>
            </div>
            <button type="button" className="btn btn-ghost" style={{ marginLeft: "auto" }} onClick={onClearMedia} disabled={busy}>
              <IconClose size={16} />
              換一個
            </button>
          </div>
        ) : docs.length ? (
          <div className="stack" style={{ gap: 10 }}>
            <DocList items={docs} onRemove={onRemoveDoc} busy={busy} label="要整理成會議紀錄的文件" />
            <input ref={moreRef} type="file" accept={ACCEPT_DOCS} multiple hidden onChange={(event) => pick(event.currentTarget, onPickMain)} />
            <button type="button" className="btn btn-quiet" style={{ alignSelf: "flex-start" }} onClick={() => moreRef.current?.click()} disabled={busy}>
              <IconUpload size={16} />
              再加一份文件
            </button>
            <span className="field-hint">多份文件（例如幾個人的筆記）會合併成同一份會議紀錄。</span>
          </div>
        ) : (
          <label className={`dropzone${isOver ? " is-over" : ""}`}>
            <input
              ref={mainRef}
              type="file"
              accept={ACCEPT_ALL}
              multiple
              onChange={(event) => pick(event.currentTarget, onPickMain)}
              aria-describedby="drop-hint"
            />
            <span className="drop-icon">
              <IconUpload size={24} />
            </span>
            <span className="drop-title" style={{ display: "block" }}>
              把影音或文件拖進來，或點此選擇
            </span>
            <span className="drop-hint" id="drop-hint" style={{ display: "block" }}>
              影音：mp4、mov、mkv、mp3、wav、m4a…；文件：Word（.docx）、PDF、Markdown、純文字、字幕（.srt／.vtt）
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
          <span className="legend">過往會議紀錄（範本，選填）</span>
          <span className="field-hint">
            放一兩份以前的紀錄，產出的會議紀錄會沿用它的格式、段落結構、用語、與會者與專有名詞；範本裡的舊內容不會被抄進來。
          </span>
          <DocList items={refs} onRemove={onRemoveRef} busy={busy} label="過往會議紀錄範本" />
          <input ref={refsRef} type="file" accept={ACCEPT_DOCS} multiple hidden onChange={(event) => pick(event.currentTarget, onPickRefs)} />
          <button type="button" className="btn btn-quiet" style={{ alignSelf: "flex-start" }} onClick={() => refsRef.current?.click()} disabled={busy}>
            <IconFeather size={16} />
            {refs.length ? "再加一份過往紀錄" : "加入過往紀錄"}
          </button>
          {pdfMissing ? (
            <span className="field-hint">這台服務找不到 markitdown，PDF 無法讀取；請改用 .docx 或 .txt（或在服務端安裝 markitdown）。</span>
          ) : null}
        </div>

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
          {!docMode ? (
            <>
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
            </>
          ) : null}
        </div>

        <button type="button" className="btn btn-ghost" style={{ alignSelf: "flex-start", paddingLeft: 0 }} onClick={() => setShowMore((v) => !v)}>
          {showMore ? "收進進階選項" : docMode ? "進階選項（自訂指示…）" : "進階選項（自訂指示、不做摘要…）"}
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
            {!docMode && !hasRefs ? (
              <div className="switch-row">
                <input
                  id="minutes"
                  type="checkbox"
                  checked={Boolean(options.minutes)}
                  disabled={Boolean(options.no_summary)}
                  onChange={(event) => onOptions({ minutes: event.target.checked })}
                />
                <span className="switch-text">
                  <strong>另外產出會議紀錄</strong>
                  <span>沒有範本時用標準格式（會議資訊、討論、決議、待辦）</span>
                </span>
              </div>
            ) : null}
            {!docMode ? (
              <div className="switch-row">
                <input
                  id="no-summary"
                  type="checkbox"
                  checked={Boolean(options.no_summary)}
                  disabled={hasRefs}
                  onChange={(event) => onOptions({ no_summary: event.target.checked })}
                />
                <span className="switch-text">
                  <strong>只做逐字稿，不做摘要</strong>
                  <span>{hasRefs ? "有範本時需要摘要才能撰寫會議紀錄" : "省下游模型的費用與時間"}</span>
                </span>
              </div>
            ) : null}
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
            {!docMode ? (
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
            ) : null}
          </div>
        ) : null}

        <button
          type="button"
          className="btn btn-primary btn-block"
          onClick={onSubmit}
          disabled={!canSubmit || busy}
          aria-describedby={canSubmit ? undefined : "submit-hint"}
        >
          {busy ? <IconSpinner /> : <IconUpload />}
          {submitLabel}
        </button>
        {!canSubmit ? (
          <span className="field-hint" id="submit-hint">
            {[...docs, ...refs].some((d) => d.status === "uploading")
              ? "文件還在上傳解析，完成後按鈕就會亮起。"
              : "先選擇一個影音檔或至少一份文件，按鈕就會亮起。"}
          </span>
        ) : null}
      </div>
    </section>
  );
}
