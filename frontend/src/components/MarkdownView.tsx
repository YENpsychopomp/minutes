import { Fragment, type ReactNode } from "react";

/**
 * 輕量 Markdown 檢視（不引入套件）：標題、段落、清單（含縮排層級）、表格、引言、分隔線，
 * 以及行內的粗體／斜體／程式碼。所有文字都交給 React 轉義，不會插入原始 HTML。
 */

type Block =
  | { type: "h"; level: number; text: string }
  | { type: "p"; lines: string[] }
  | { type: "list"; ordered: boolean; items: { text: string; depth: number; marker?: string }[] }
  | { type: "table"; head: string[]; rows: string[][] }
  | { type: "quote"; lines: string[] }
  | { type: "hr" };

const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^(\s*)[-*+•●]\s+(.*)$/;
const ORDERED = /^(\s*)(\d+)[.)]\s+(.*)$/;
const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_RULE = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;
const HR = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;

function cells(line: string): string[] {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

export function parseMarkdown(source: string): Block[] {
  const blocks: Block[] = [];
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i += 1;
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      blocks.push({ type: "h", level: heading[1].length, text: heading[2].trim() });
      i += 1;
      continue;
    }
    if (HR.test(line)) {
      blocks.push({ type: "hr" });
      i += 1;
      continue;
    }
    if (TABLE_ROW.test(line)) {
      const rows: string[][] = [];
      while (i < lines.length && (TABLE_ROW.test(lines[i]) || TABLE_RULE.test(lines[i]))) {
        if (!TABLE_RULE.test(lines[i])) rows.push(cells(lines[i]));
        i += 1;
      }
      const [head, ...body] = rows;
      blocks.push({ type: "table", head: head ?? [], rows: body });
      continue;
    }
    if (BULLET.test(line) || ORDERED.test(line)) {
      const ordered = !BULLET.test(line);
      const items: { text: string; depth: number; marker?: string }[] = [];
      while (i < lines.length) {
        const bullet = BULLET.exec(lines[i]);
        const numbered = ORDERED.exec(lines[i]);
        if (bullet) items.push({ text: bullet[2], depth: Math.floor(bullet[1].length / 2) });
        else if (numbered) items.push({ text: numbered[3], depth: Math.floor(numbered[1].length / 2), marker: `${numbered[2]}.` });
        else if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && items.length) items[items.length - 1].text += ` ${lines[i].trim()}`;
        else break;
        i += 1;
      }
      blocks.push({ type: "list", ordered, items });
      continue;
    }
    if (line.trimStart().startsWith(">")) {
      const quote: string[] = [];
      while (i < lines.length && lines[i].trimStart().startsWith(">")) {
        quote.push(lines[i].trimStart().replace(/^>\s?/, ""));
        i += 1;
      }
      blocks.push({ type: "quote", lines: quote });
      continue;
    }
    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !HEADING.test(lines[i]) &&
      !TABLE_ROW.test(lines[i]) &&
      !BULLET.test(lines[i]) &&
      !ORDERED.test(lines[i]) &&
      !HR.test(lines[i])
    ) {
      para.push(lines[i].trim());
      i += 1;
    }
    blocks.push({ type: "p", lines: para });
  }
  return blocks;
}

const INLINE = /(\*\*[^*]+\*\*|__[^_]+__|`[^`]+`|\*[^*\s][^*]*\*)/g;

function inline(text: string): ReactNode {
  const parts = text.split(INLINE).filter((part) => part !== "");
  return parts.map((part, i) => {
    if ((part.startsWith("**") && part.endsWith("**")) || (part.startsWith("__") && part.endsWith("__"))) {
      return <strong key={i}>{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith("`") && part.endsWith("`")) return <code key={i}>{part.slice(1, -1)}</code>;
    if (part.startsWith("*") && part.endsWith("*") && part.length > 2) return <em key={i}>{part.slice(1, -1)}</em>;
    return <Fragment key={i}>{part}</Fragment>;
  });
}

function withBreaks(lines: string[]): ReactNode {
  return lines.map((line, i) => (
    <Fragment key={i}>
      {i > 0 ? <br /> : null}
      {inline(line)}
    </Fragment>
  ));
}

export function MarkdownView({ source }: { source: string }) {
  const blocks = parseMarkdown(source);
  return (
    <div className="md-view">
      {blocks.map((block, i) => {
        switch (block.type) {
          case "h": {
            const level = Math.min(4, block.level);
            const Tag = `h${level}` as "h1" | "h2" | "h3" | "h4";
            return <Tag key={i}>{inline(block.text)}</Tag>;
          }
          case "p":
            return <p key={i}>{withBreaks(block.lines)}</p>;
          case "quote":
            return <blockquote key={i}>{withBreaks(block.lines)}</blockquote>;
          case "hr":
            return <hr key={i} />;
          case "table":
            return (
              <div className="md-table" key={i}>
                <table>
                  <thead>
                    <tr>
                      {block.head.map((cell, j) => (
                        <th key={j} scope="col">
                          {inline(cell)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {block.rows.map((row, r) => (
                      <tr key={r}>
                        {block.head.map((_h, c) => (
                          <td key={c}>{inline(row[c] ?? "")}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          case "list": {
            const Tag = block.ordered ? "ol" : "ul";
            return (
              <Tag key={i}>
                {block.items.map((item, j) => (
                  <li key={j} style={item.depth ? { marginLeft: item.depth * 20 } : undefined} value={item.marker ? Number.parseInt(item.marker, 10) : undefined}>
                    {inline(item.text)}
                  </li>
                ))}
              </Tag>
            );
          }
          default:
            return null;
        }
      })}
    </div>
  );
}
