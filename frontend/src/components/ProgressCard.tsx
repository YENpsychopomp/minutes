import { stagesFor, type JobDetail } from "../types";
import { IconCheck, IconClose, IconSpinner, IconStop } from "./Icons";

interface Props {
  job: JobDetail;
  onCancel: () => void;
  cancelling: boolean;
}

export function ProgressCard({ job, onCancel, cancelling }: Props) {
  const STAGES = stagesFor(job);
  const stageIndex = STAGES.findIndex((stage) => stage.key === job.stage);
  const failed = job.status === "failed";
  const percent = Math.max(3, Math.round((job.progress ?? 0) * 100));
  const counts = job.counts ?? {};

  return (
    <section className="sheet fade-up" aria-live="polite">
      <div className="sheet-head">
        <span className="eyebrow">進行中</span>
        <h2>{job.name}</h2>
        <button
          type="button"
          className="btn btn-ghost"
          style={{ marginLeft: "auto" }}
          onClick={onCancel}
          disabled={cancelling || job.status === "cancelling"}
        >
          {cancelling || job.status === "cancelling" ? <IconSpinner size={16} /> : <IconStop size={16} />}
          停止
        </button>
      </div>
      <div className="sheet-pad stack" style={{ gap: 18 }}>
        <div>
          <div className="progress-meta">
            <span>{job.message || "準備中…"}</span>
            <span className="num">{percent}%</span>
          </div>
          <div
            className="progress-track"
            role="progressbar"
            aria-valuenow={percent}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label="處理進度"
          >
            <div className="progress-fill" style={{ width: `${percent}%` }} />
          </div>
        </div>

        <ol className="stages">
          {STAGES.map((stage, index) => {
            const state = failed && index === stageIndex ? "error" : stageIndex < 0 ? "done" : index < stageIndex ? "done" : index === stageIndex ? "active" : "todo";
            const isAsr = stage.key === "asr" && counts.total;
            const isSummary = stage.key === "summarize" && counts.summary_total;
            const isDocs = stage.key === "document" && counts.documents;
            return (
              <li key={stage.key} className="stage" data-state={state}>
                <span className="stage-mark" aria-hidden="true">
                  {state === "done" ? <IconCheck size={14} /> : state === "active" ? <IconSpinner size={14} /> : state === "error" ? <IconClose size={14} /> : index + 1}
                </span>
                <span>
                  <span className="stage-title">{stage.label}</span>
                  <span className="stage-hint">{stage.hint}</span>
                </span>
                <span className="stage-note num">
                  {state === "active" && isAsr ? `${counts.done ?? 0} / ${counts.total} 塊` : null}
                  {state === "active" && isSummary ? `${counts.summary_done ?? 0} / ${counts.summary_total} 份筆記` : null}
                  {isDocs ? `${counts.documents} 份・${counts.paragraphs ?? 0} 段` : null}
                </span>
              </li>
            );
          })}
        </ol>

        {job.status === "cancelling" ? (
          <p className="notice notice-warn">
            <IconStop size={18} />
            <span>正在停止：已送出的片段會先跑完，之後不再送新的。</span>
          </p>
        ) : null}

        {counts.errors ? (
          <p className="notice notice-warn">
            <IconClose size={18} />
            <span>
              有 {counts.errors} 個片段失敗{job.errors?.[0]?.error ? `（例如：${job.errors[0].error}）` : ""}。
              完成後仍會輸出已成功的部分，失敗片段可以在終端機的紀錄裡看到細節。
            </span>
          </p>
        ) : null}
      </div>
    </section>
  );
}
