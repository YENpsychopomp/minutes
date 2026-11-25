import { useEffect, useMemo, useState } from "react";
import { fetchText, fileUrl } from "../api";
import { clock } from "../format";
import type { JobDetail } from "../types";

interface Segment {
  start: number;
  end: number;
  text: string;
  chunk?: number;
}

interface Props {
  job: JobDetail;
  activeTime?: number | null;
  onSeek?: (seconds: number) => void;
}

export function TranscriptPanel({ job, activeTime, onSeek }: Props) {
  const [segments, setSegments] = useState<Segment[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  const file = useMemo(() => {
    const json = job.files?.find((f) => f.format === "json") ?? job.files?.find((f) => f.name.endsWith(".json"));
    return json ? fileUrl(job.job_id, json.name) : null;
  }, [job.files, job.job_id]);

  useEffect(() => {
    let cancelled = false;
    if (!file) {
      const inline = job.result?.transcript?.text ?? "";
      const parsed = inline
        .split("\n")
        .map((line) => line.match(/^\[(\d+):(\d+)(?::(\d+))?\]\s*(.*)$/))
        .filter((m): m is RegExpMatchArray => Boolean(m))
        .map((m) => {
          const parts = m.slice(1, 4).map((v) => (v === undefined ? 0 : Number(v)));
          const seconds = m[3] === undefined ? parts[0] * 60 + parts[1] : parts[0] * 3600 + parts[1] * 60 + parts[2];
          return { start: seconds, end: seconds, text: m[4] ?? "" } as Segment;
        });
      setSegments(parsed);
      return;
    }
    setSegments(null);
    fetchText(file)
      .then((text) => {
        if (cancelled) return;
        const data = JSON.parse(text) as { segments?: Segment[] };
        setSegments(Array.isArray(data.segments) ? data.segments : []);
      })
      .catch((exc: Error) => !cancelled && setError(exc.message));
    return () => {
      cancelled = true;
    };
  }, [file, job.result?.transcript?.text]);

  const filtered = useMemo(() => {
    const list = segments ?? [];
    const needle = query.trim().toLowerCase();
    if (!needle) return list;
    return list.filter((segment) => segment.text.toLowerCase().includes(needle));
  }, [segments, query]);

  const activeIndex = useMemo(() => {
    if (activeTime === undefined || activeTime === null || !segments?.length) return -1;
    let best = -1;
    segments.forEach((segment, i) => {
      if (segment.start <= activeTime + 0.5) best = i;
    });
    return best;
  }, [segments, activeTime]);

  if (error) {
    return (
      <p className="notice notice-error" role="alert">
        <span>讀取逐字稿失敗：{error}</span>
      </p>
    );
  }

  if (!segments) {
    return (
      <div className="stack" aria-busy="true">
        {[92, 76, 88, 64, 84].map((w, i) => (
          <div className="skeleton" key={i} style={{ width: `${w}%` }} />
        ))}
        <span className="sr-only">載入逐字稿中</span>
      </div>
    );
  }

  if (!segments.length) {
    return <p className="empty">這個工作還沒有逐字稿。</p>;
  }

  return (
    <div>
      <div className="transcript-toolbar">
        <div className="field" style={{ flex: 1, minWidth: 200 }}>
          <label className="sr-only" htmlFor="find">
            在逐字稿中搜尋
          </label>
          <input
            id="find"
            type="search"
            placeholder="搜尋逐字稿內容…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
        <span className="chip num">
          {filtered.length} / {segments.length} 段
        </span>
        {onSeek ? <span className="chip">點時間即可跳到播放位置</span> : null}
      </div>

      {filtered.length === 0 ? <p className="empty">找不到「{query}」</p> : null}

      <ol className="transcript">
        {filtered.map((segment, i) => {
          const originalIndex = segments.indexOf(segment);
          return (
            <li className={`line${originalIndex === activeIndex ? " is-active" : ""}`} key={`${segment.start}-${i}`}>
              <div className="line-time num">
                {onSeek ? (
                  <button type="button" className="btn btn-ghost" style={{ padding: 0, minHeight: 0 }} onClick={() => onSeek(segment.start)}>
                    {clock(segment.start)}
                  </button>
                ) : (
                  clock(segment.start)
                )}
              </div>
              <div className="line-text">{segment.text}</div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
