import { relativeTime, STATUS_LABEL, STATUS_TONE } from "../format";
import type { Job, ServiceConfig } from "../types";
import { IconInfo } from "./Icons";

interface Props {
  jobs: Job[];
  activeId: string | null;
  onSelect: (id: string) => void;
  config?: ServiceConfig;
}

export function Sidebar({ jobs, activeId, onSelect, config }: Props) {
  return (
    <aside className="aside">
      <section className="sheet">
        <div className="sheet-head">
          <h3>工作紀錄</h3>
          <span className="chip num" style={{ marginLeft: "auto" }}>
            {jobs.length}
          </span>
        </div>
        <div className="aside-body" style={{ paddingTop: 8, paddingBottom: 10 }}>
          {jobs.length === 0 ? <p className="empty">還沒有工作。上傳一個檔案就會出现在這裡。</p> : null}
          {jobs.slice(0, 30).map((job) => {
            const tone = STATUS_TONE[job.status] ?? "neutral";
            const running = job.status === "running" || job.status === "queued";
            return (
              <button
                type="button"
                key={job.job_id}
                className="job-card"
                aria-current={job.job_id === activeId}
                onClick={() => onSelect(job.job_id)}
              >
                <span className="job-card-name">
                  <span className={`chip chip-${tone === "done" ? "sage" : tone === "error" ? "rust" : tone === "progress" ? "amber" : ""}`} style={{ padding: "2px 7px" }}>
                    <span className="dot" />
                  </span>
                  <span style={{ minWidth: 0 }}>{job.name}</span>
                </span>
                <span className="job-card-meta">
                  {job.kind === "document" ? (
                    <>
                      <span>文件</span>
                      <span aria-hidden="true">·</span>
                    </>
                  ) : null}
                  {job.references?.length ? (
                    <>
                      <span>依範本</span>
                      <span aria-hidden="true">·</span>
                    </>
                  ) : null}
                  <span>{STATUS_LABEL[job.status] ?? job.status}</span>
                  <span aria-hidden="true">·</span>
                  <span>{relativeTime(job.created_at)}</span>
                </span>
                {running ? (
                  <span className="mini-track" style={{ display: "block" }}>
                    <span className="mini-fill" style={{ width: `${Math.max(3, Math.round((job.progress ?? 0) * 100))}%` }} />
                  </span>
                ) : null}
              </button>
            );
          })}
        </div>
      </section>

      <section className="sheet">
        <div className="sheet-head">
          <h3>這台服務</h3>
        </div>
        <div className="aside-body">
          <dl className="kv">
            <dt>轉錄模型</dt>
            <dd className="num">{config?.asr.model ?? "—"}</dd>
            <dt>摘要模型</dt>
            <dd className="num">{config?.llm.model ?? "—"}</dd>
            <dt>金鑰</dt>
            <dd>
              {config ? (
                config.asr.key_configured && config.llm.key_configured ? (
                  "已設定"
                ) : (
                  <span style={{ color: "var(--rust)" }}>缺少一組</span>
                )
              ) : (
                "—"
              )}
            </dd>
            <dt>預設切塊</dt>
            <dd className="num">{config ? `${config.chunking.target_seconds} 秒` : "—"}</dd>
            <dt>輸出位置</dt>
            <dd className="num">{config?.work_dir ?? "—"}</dd>
          </dl>
          <p className="notice notice-info" style={{ marginTop: 14 }}>
            <IconInfo size={17} />
            <span style={{ fontSize: 13 }}>
              檔案同時會寫回上傳時指定的輸出目錄；要停服務，回到終端機按
              <code style={{ fontFamily: "var(--font-num)" }}> Ctrl</code>+<code style={{ fontFamily: "var(--font-num)" }}>C</code>，或直接關閉終端機視窗。
            </span>
          </p>
        </div>
      </section>
    </aside>
  );
}
