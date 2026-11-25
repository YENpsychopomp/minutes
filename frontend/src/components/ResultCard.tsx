import { useEffect, useRef, useState, type ReactNode } from "react";
import { fetchText, fileUrl } from "../api";
import { clock, FORMAT_LABEL } from "../format";
import type { JobDetail } from "../types";
import { IconBraces, IconDoc, IconDownload, IconPause, IconPlay, IconWave } from "./Icons";
import { SummaryDoc } from "./SummaryDoc";
import { TranscriptPanel } from "./TranscriptPanel";

type TabKey = "summary" | "transcript" | "raw";

interface Props {
  job: JobDetail;
  registerSeek?: (seek: ((seconds: number) => void) | null) => void;
  activeTime?: number | null;
  onTime?: (seconds: number) => void;
}

function Player({
  src,
  onTime,
  registerSeek,
}: {
  src: string;
  onTime: (seconds: number) => void;
  registerSeek?: (seek: ((seconds: number) => void) | null) => void;
}) {
  const ref = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [current, setCurrent] = useState(0);
  const [duration, setDuration] = useState(0);

  const seek = (seconds: number) => {
    const el = ref.current;
    if (!el) return;
    el.currentTime = seconds;
    setCurrent(seconds);
    if (el.paused) void el.play().catch(() => undefined);
  };

  useEffect(() => {
    registerSeek?.(seek);
    return () => registerSeek?.(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const pct = duration ? (current / duration) * 100 : 0;

  return (
    <div className="player">
      <audio
        ref={ref}
        src={src}
        preload="metadata"
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onTimeUpdate={(event) => {
          const value = event.currentTarget.currentTime;
          setCurrent(value);
          onTime(value);
        }}
        onLoadedMetadata={(event) => setDuration(event.currentTarget.duration || 0)}
      />
      <button
        type="button"
        className="play"
        onClick={() => {
          const el = ref.current;
          if (!el) return;
          if (el.paused) void el.play().catch(() => undefined);
          else el.pause();
        }}
        aria-label={playing ? "暫停" : "播放"}
      >
        {playing ? <IconPause size={18} /> : <IconPlay size={18} />}
      </button>
      <input
        type="range"
        min={0}
        max={Math.max(duration, 0.1)}
        step={0.1}
        value={current}
        style={{ ["--pct" as string]: `${pct}%` }}
        onChange={(event) => {
          const value = Number(event.target.value);
          const el = ref.current;
          if (el) el.currentTime = value;
          setCurrent(value);
          onTime(value);
        }}
        aria-label="播放位置"
      />
      <span className="player-time num">
        {clock(current)} / {clock(duration || undefined)}
      </span>
    </div>
  );
}

export function ResultCard({ job, registerSeek, activeTime, onTime }: Props) {
  const [tab, setTab] = useState<TabKey>("summary");
  const [raw, setRaw] = useState<string | null>(null);
  const summary = job.result?.summary;
  const hasTranscript = Boolean(job.files?.some((f) => f.format === "json") || job.result?.transcript?.text);

  useEffect(() => {
    if (tab !== "raw" || raw !== null) return;
    const target = job.files?.find((f) => f.name.endsWith(".summary.json") || f.format === "summary.json");
    if (!target) {
      setRaw("（這個工作還沒有摘要 JSON）");
      return;
    }
    fetchText(fileUrl(job.job_id, target.name))
      .then((text) => setRaw(JSON.stringify(JSON.parse(text), null, 2)))
      .catch((exc: Error) => setRaw(`讀取失敗：${exc.message}`));
  }, [tab, raw, job.files, job.job_id]);

  const tabs: { key: TabKey; label: string; icon: ReactNode; enabled: boolean }[] = [
    { key: "summary", label: "摘要", icon: <IconDoc size={16} />, enabled: Boolean(summary) },
    { key: "transcript", label: "逐字稿", icon: <IconWave size={16} />, enabled: hasTranscript },
    { key: "raw", label: "原始 JSON", icon: <IconBraces size={16} />, enabled: true },
  ];
  const currentTab = tabs.find((t) => t.key === tab && t.enabled) ? tab : summary ? "summary" : hasTranscript ? "transcript" : "raw";

  return (
    <section className="sheet fade-up">
      <div className="tabs" role="tablist" aria-label="結果檢視">
        {tabs.map((item) => (
          <button
            key={item.key}
            role="tab"
            type="button"
            className="tab"
            aria-selected={currentTab === item.key}
            disabled={!item.enabled}
            onClick={() => setTab(item.key)}
          >
            {item.icon}
            {item.label}
          </button>
        ))}
      </div>

      <div className="sheet-pad stack" style={{ gap: 20 }}>
        {job.media_url ? (
          <Player src={job.media_url} onTime={(value) => onTime?.(value)} registerSeek={registerSeek} />
        ) : null}

        {currentTab === "summary" && summary ? (
          <SummaryDoc
            summary={summary}
            duration={job.duration ?? job.counts?.duration}
            onSeek={job.media_url ? onTime : undefined}
          />
        ) : null}

        {currentTab === "transcript" && hasTranscript ? (
          <TranscriptPanel job={job} activeTime={activeTime} onSeek={job.media_url ? onTime : undefined} />
        ) : null}

        {currentTab === "raw" ? (
          raw === null ? (
            <div className="stack" aria-busy="true">
              <div className="skeleton" style={{ width: "70%" }} />
              <div className="skeleton" style={{ width: "45%" }} />
            </div>
          ) : (
            <pre className="raw-json">{raw}</pre>
          )
        ) : null}

        {job.files?.length ? (
          <div className="doc-section" style={{ marginTop: 4 }}>
            <h3>下載</h3>
            <div>
              {job.files.map((file) => (
                <div className="download-row" key={file.name}>
                  <IconDownload size={16} />
                  <span className="grow">
                    <a href={fileUrl(job.job_id, file.name)} download={file.name}>
                      {FORMAT_LABEL[file.format] ?? file.format}
                    </a>
                    {file.size ? <span className="chip num" style={{ marginLeft: 8 }}>{(file.size / 1024).toFixed(0)} KB</span> : null}
                  </span>
                  <span className="chip" style={{ textTransform: "none" }}>{file.format}</span>
                </div>
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </section>
  );
}
