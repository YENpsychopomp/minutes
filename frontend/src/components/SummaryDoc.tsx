import { clock } from "../format";
import type { SummaryPayload } from "../types";
import { IconClock, IconQuill } from "./Icons";

interface Props {
  summary: SummaryPayload;
  duration?: number | null;
  onSeek?: (seconds: number) => void;
}

function TimeChip({ seconds, label, onSeek }: { seconds: number; label?: string; onSeek?: (s: number) => void }) {
  const text = label ?? clock(seconds);
  if (!onSeek) return <span className="chip num">{text}</span>;
  return (
    <button type="button" className="time-chip num" onClick={() => onSeek(seconds)} title={`跳到 ${text}`}>
      {text}
    </button>
  );
}

export function SummaryDoc({ summary, duration, onSeek }: Props) {
  const meta = summary.meta ?? {};
  const chips: string[] = [];
  if (duration) chips.push(`長度 ${clock(duration)}`);
  if (meta.paragraphs) chips.push(`${meta.paragraphs} 段逐字稿`);
  if (meta.chars) chips.push(`${meta.chars.toLocaleString("zh-TW")} 字`);
  if (meta.mode === "map-reduce") chips.push(`分層摘要 ${meta.map_blocks ?? "?"} 區塊`);
  if (meta.llm_usage?.total_tokens) chips.push(`${meta.llm_usage.total_tokens.toLocaleString("zh-TW")} tokens`);
  if (meta.language) chips.push(String(meta.language));

  const warnings = (meta.warnings ?? []) as string[];

  return (
    <article className="doc">
      <header className="doc-head">
        <h2 className="doc-title">{summary.title || "（未命名）"}</h2>
        {chips.length ? (
          <div className="doc-meta">
            {chips.map((chip) => (
              <span className="chip" key={chip}>
                {chip}
              </span>
            ))}
          </div>
        ) : null}
      </header>

      {summary.tldr ? <p className="pull">{summary.tldr}</p> : null}

      {warnings.length ? (
        <p className="notice notice-warn" style={{ marginTop: 20 }}>
          <IconQuill size={18} />
          <span>
            <strong>摘要過程的提醒</strong>
            {warnings.join("；")}
          </span>
        </p>
      ) : null}

      {summary.overview?.length ? (
        <section className="doc-section">
          <h3>內容概要</h3>
          <div className="prose">
            {summary.overview.map((text, i) => (
              <p key={i}>{text}</p>
            ))}
          </div>
        </section>
      ) : null}

      {summary.chapters?.length ? (
        <section className="doc-section">
          <h3>章節大綱</h3>
          {summary.chapters.map((chapter, i) => (
            <div className="chapter" key={`${chapter.start}-${i}`}>
              <div className="chapter-head">
                <TimeChip seconds={chapter.start} label={chapter.clock} onSeek={onSeek} />
                <h4>{chapter.title}</h4>
                {chapter.end > chapter.start ? (
                  <span className="chip num" style={{ marginLeft: "auto" }}>
                    {clock(chapter.end - chapter.start)}
                  </span>
                ) : null}
              </div>
              {chapter.summary ? <p className="chapter-summary">{chapter.summary}</p> : null}
              {chapter.points?.length ? (
                <ul className="points">
                  {chapter.points.map((point, j) => (
                    <li key={j}>{point}</li>
                  ))}
                </ul>
              ) : null}
            </div>
          ))}
        </section>
      ) : null}

      {summary.key_points?.length ? (
        <section className="doc-section">
          <h3>重點整理</h3>
          <ul className="points">
            {summary.key_points.map((point, i) => (
              <li key={i}>{point}</li>
            ))}
          </ul>
        </section>
      ) : null}

      {summary.decisions?.length ? (
        <section className="doc-section">
          <h3>結論與決定</h3>
          <ul className="points">
            {summary.decisions.map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </section>
      ) : null}

      {summary.action_items?.length ? (
        <section className="doc-section">
          <h3>待辦事項</h3>
          <table className="checklist">
            <thead>
              <tr>
                <th scope="col">事項</th>
                <th scope="col">負責人</th>
                <th scope="col">期限</th>
              </tr>
            </thead>
            <tbody>
              {summary.action_items.map((item, i) => (
                <tr key={i}>
                  <td>{item.task}</td>
                  <td>{item.owner || "未指派"}</td>
                  <td>{item.due || "未定"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}

      {summary.open_questions?.length ? (
        <section className="doc-section">
          <h3>尚未釐清</h3>
          <ul className="points">
            {summary.open_questions.map((item, i) => {
              const question = typeof item === "string" ? { text: item } : item;
              const seconds = typeof question.start === "number" ? question.start : null;
              return (
                <li key={i}>
                  {seconds !== null ? (
                    <>
                      <TimeChip seconds={seconds} label={question.clock ?? undefined} onSeek={onSeek} />{" "}
                    </>
                  ) : null}
                  {question.text}
                </li>
              );
            })}
          </ul>
        </section>
      ) : null}

      {summary.quotes?.length ? (
        <section className="doc-section">
          <h3>值得留下來的一句話</h3>
          {summary.quotes.map((quote, i) => (
            <blockquote className="quote" key={i}>
              {quote.text}
              {quote.speaker || quote.clock ? (
                <cite className="quote-cite">
                  {quote.speaker ? `${quote.speaker}　` : ""}
                  {quote.clock ? <span className="num">{quote.clock}</span> : null}
                </cite>
              ) : null}
            </blockquote>
          ))}
        </section>
      ) : null}

      {summary.keywords?.length ? (
        <section className="doc-section">
          <h3>關鍵字</h3>
          <div>
            {summary.keywords.map((word, i) => (
              <span className="tag" key={`${word}-${i}`}>
                {word}
              </span>
            ))}
          </div>
        </section>
      ) : null}

      {!summary.tldr && !summary.key_points?.length && !summary.chapters?.length ? (
        <p className="notice notice-warn" style={{ marginTop: 22 }}>
          <IconClock size={18} />
          <span>
            這個工作只有逐字稿，沒有摘要內容（可能是選擇了「只做逐字稿」，或下游模型當時沒有回傳可用內容）。
            切到「逐字稿」分頁仍可閱讀與下載全文。
          </span>
        </p>
      ) : null}
    </article>
  );
}
