import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import { Masthead } from "./components/Masthead";
import { ProgressCard } from "./components/ProgressCard";
import { ResultCard } from "./components/ResultCard";
import { Sidebar } from "./components/Sidebar";
import { UploadCard } from "./components/UploadCard";
import { IconAlert, IconCheck, IconClose, IconInfo, IconQuill, IconUpload } from "./components/Icons";
import type { Job, JobDetail, JobOptionsInput, ServiceConfig } from "./types";

interface Toast {
  id: number;
  tone: "info" | "success" | "warn" | "error";
  text: string;
}

const RUNNING = new Set(["queued", "running", "cancelling"]);

export default function App() {
  const [config, setConfig] = useState<ServiceConfig | undefined>(undefined);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [detail, setDetail] = useState<JobDetail | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [options, setOptions] = useState<JobOptionsInput>({ style: "standard", summary_language: "zh-TW", concurrency: 4 });
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [isOver, setIsOver] = useState(false);
  const [activeTime, setActiveTime] = useState<number | null>(null);
  const seekRef = useRef<((seconds: number) => void) | null>(null);
  const fromHash = useRef<string | null>(jobIdFromHash());
  const toastId = useRef(0);

  const toast = useCallback((tone: Toast["tone"], text: string) => {
    toastId.current += 1;
    const id = toastId.current;
    setToasts((list) => [...list.slice(-2), { id, tone, text }]);
    window.setTimeout(() => setToasts((list) => list.filter((item) => item.id !== id)), 7000);
  }, []);

  const dismiss = (id: number) => setToasts((list) => list.filter((item) => item.id !== id));

  const refreshJobs = useCallback(async () => {
    try {
      const data = await api.jobs();
      setJobs(data.jobs ?? []);
      if (data.config) setConfig(data.config);
    } catch (exc) {
      setConfig(undefined);
      toast("error", exc instanceof ApiError ? exc.message : "讀取工作清單失敗");
    }
  }, [toast]);

  useEffect(() => {
    void refreshJobs().then(() => {
      if (fromHash.current) setActiveId(fromHash.current);
    });
  }, [refreshJobs]);

  // 網址裡的 #/job/<id> 讓結果可以复制或收藏
  useEffect(() => {
    const onHashChange = () => {
      const id = jobIdFromHash();
      if (id) setActiveId(id);
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  // 有工作在跑時，清單每 5 秒更新一次
  useEffect(() => {
    if (!jobs.some((job) => RUNNING.has(job.status))) return;
    const timer = window.setInterval(() => void refreshJobs(), 5000);
    return () => window.clearInterval(timer);
  }, [jobs, refreshJobs]);

  // 選取中的工作：每 1.2 秒更新進度
  useEffect(() => {
    if (!activeId) {
      setDetail(null);
      return;
    }
    let stopped = false;
    let timer = 0;
    const load = async () => {
      try {
        const job = await api.job(activeId);
        if (stopped) return;
        setDetail(job);
        if (RUNNING.has(job.status)) timer = window.setTimeout(() => void load(), 1200);
        else void refreshJobs();
      } catch (exc) {
        if (!stopped) toast("error", exc instanceof Error ? exc.message : "讀取工作失敗");
      }
    };
    void load();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [activeId, refreshJobs, toast]);

  const handleSubmit = async () => {
    if (!file || uploading) return;
    setUploading(true);
    setUploadProgress(0);
    try {
      const job = await api.upload(file, options, (fraction) => setUploadProgress(fraction));
      const detail = job.name ? job : { ...job, name: file.name };
      setActiveId(detail.job_id);
      setDetail(detail);
      toast("success", `「${detail.name}」已開始處理，可以換下一個檔案了。`);
      void refreshJobs();
    } catch (exc) {
      toast("error", exc instanceof Error ? exc.message : "上傳失敗");
    } finally {
      setUploading(false);
      setUploadProgress(null);
    }
  };

  useEffect(() => {
    if (!activeId) return;
    const target = `#/job/${activeId}`;
    if (window.location.hash !== target) window.history.replaceState(null, "", target);
  }, [activeId]);

  const handleCancel = async () => {
    if (!detail) return;
    setCancelling(true);
    try {
      await api.cancel(detail.job_id);
      toast("info", "已送出停止請求，處理中的片段跑完就會停下。");
    } catch (exc) {
      toast("error", exc instanceof Error ? exc.message : "停止失敗");
    } finally {
      setCancelling(false);
    }
  };

  const seek = useCallback((seconds: number) => {
    seekRef.current?.(seconds);
    setActiveTime(seconds);
  }, []);

  // 避免瀏覽器把拖进来的檔案直接開啓
  useEffect(() => {
    const stop = (event: DragEvent) => event.preventDefault();
    window.addEventListener("dragover", stop);
    window.addEventListener("drop", stop);
    return () => {
      window.removeEventListener("dragover", stop);
      window.removeEventListener("drop", stop);
    };
  }, []);

  const running = detail ? RUNNING.has(detail.status) : false;
  const finished = detail ? detail.status === "done" || detail.status === "partial" : false;
  const broken = detail ? detail.status === "failed" || detail.status === "cancelled" : false;

  return (
    <div
      className="shell"
      onDragEnter={() => setIsOver(true)}
      onDragOver={() => setIsOver(true)}
      onDragLeave={(event) => {
        if (event.currentTarget === event.target) setIsOver(false);
      }}
      onDrop={(event) => {
        setIsOver(false);
        const dropped = event.dataTransfer.files?.[0];
        if (dropped) setFile(dropped);
      }}
    >
      <Masthead config={config} />

      <main>
        <div className="columns">
          <div className="stack">
            <UploadCard
              file={file}
              onPick={setFile}
              options={options}
              onOptions={(patch) => setOptions((prev) => ({ ...prev, ...patch }))}
              onSubmit={() => void handleSubmit()}
              busy={uploading}
              uploadProgress={uploadProgress}
              config={config}
              isOver={isOver && !file}
            />

            {running && detail ? <ProgressCard job={detail} onCancel={() => void handleCancel()} cancelling={cancelling} /> : null}

            {finished && detail ? (
              <ResultCard job={detail} registerSeek={(fn) => (seekRef.current = fn)} activeTime={activeTime} onTime={seek} />
            ) : null}

            {broken && detail ? (
              <section className="sheet fade-up">
                <div className="sheet-head">
                  <span className="eyebrow">未完成</span>
                  <h2>{detail.name}</h2>
                </div>
                <div className="sheet-pad stack">
                  <p className="notice notice-error" role="alert">
                    <IconAlert size={18} />
                    <span>
                      <strong>{detail.status === "cancelled" ? "工作已停止" : "處理失敗"}</strong>
                      {detail.error || "詳情見終端機紀錄。"}
                    </span>
                  </p>
                  <div className="notice notice-info">
                    <IconInfo size={18} />
                    <span>
                      常見原因：金鑰或端點不正確、該 Gateway 不收這種音訊格式（把 <code style={{ fontFamily: "var(--font-num)" }}>audio.codec</code> 改成
                      mp3）、或模型輸出被截斷（調高 <code style={{ fontFamily: "var(--font-num)" }}>llm.max_output_tokens</code>）。
                    </span>
                  </div>
                  {file ? (
                    <button type="button" className="btn btn-quiet" style={{ alignSelf: "flex-start" }} onClick={() => void handleSubmit()}>
                      <IconUpload size={16} />
                      用同樣的設定再試一次
                    </button>
                  ) : null}
                </div>
              </section>
            ) : null}

            {!detail ? (
              <section className="sheet fade-up">
                <div className="sheet-head">
                  <span className="eyebrow">怎麼用</span>
                  <h3>三個步驟，得到一份能讀的摘要</h3>
                </div>
                <div className="sheet-pad stagger" style={{ display: "grid", gap: 18 }}>
                  <Step index={1} title="放檔案">
                    上面的虛線框可以把影片或錄音拖進來，長檔也没問題：系統會先照靜音點切塊，再平行送出去轉錄。
                  </Step>
                  <Step index={2} title="等它跑">
                    進度會顯示在左側，包括目前階層與已完成的區塊數；隨時可以停止，已完成的區塊不會白跑。
                  </Step>
                  <Step index={3} title="讀摘要、拿檔案">
                    完成後這裡會出現一份排好的文件：TL;DR、章節大綱（點時間可跳著聽）、重點、待辦與逐字稿，也可以下載 Markdown、SRT 等格式。
                  </Step>
                </div>
              </section>
            ) : null}
          </div>

          <Sidebar jobs={jobs} activeId={activeId} onSelect={setActiveId} config={config} />
        </div>
      </main>

      <footer className="footer">
        <div className="footer-inner">
          <span>summery — 以 Python 標準函式庫提供服務，無資料庫、無外部服務。</span>
          {config?.work_dir ? (
            <span style={{ marginLeft: "auto" }}>
              輸出與工作檔：<code>{config.work_dir}</code>
            </span>
          ) : null}
        </div>
      </footer>

      {isOver ? (
        <div className="notice notice-warn" role="status" style={{ position: "fixed", inset: "auto 22px 22px", zIndex: 70, justifyContent: "center" }}>
          <IconUpload size={18} />
          <span>放開即可選為要處理的檔案</span>
        </div>
      ) : null}

      <div className="toasts" aria-live="polite">
        {toasts.map((item) => (
          <div key={item.id} className={`toast notice notice-${item.tone}`}>
            {item.tone === "success" ? <IconCheck size={18} /> : item.tone === "error" ? <IconAlert size={18} /> : <IconQuill size={18} />}
            <span>{item.text}</span>
            <button type="button" className="btn btn-ghost" style={{ minHeight: 28, padding: "0 6px" }} onClick={() => dismiss(item.id)} aria-label="關閉提示">
              <IconClose size={14} />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

function jobIdFromHash(): string | null {
  const match = /^#\/job\/([^/?]+)/.exec(window.location.hash || "");
  return match ? decodeURIComponent(match[1]) : null;
}

function Step({ index, title, children }: { index: number; title: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "30px 1fr", gap: 14, alignItems: "start" }}>
      <span className="stage-mark" style={{ color: "var(--amber)", borderColor: "var(--amber-line)" }}>
        {index}
      </span>
      <div>
        <strong style={{ display: "block", fontSize: 15.5, marginBottom: 2 }}>{title}</strong>
        <span style={{ fontSize: 14.5, color: "var(--ink-soft)", lineHeight: 1.75 }}>{children}</span>
      </div>
    </div>
  );
}
