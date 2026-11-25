"""過往會議紀錄當範本 → 依範本產出會議紀錄。

流程（每一步都有不需要模型的保底做法）：

1. :func:`analyze_references` —— 讀過往紀錄，抽出「範本輪廓」：段落結構、開頭欄位、
   與會者、專有名詞與版面語氣（一次 LLM 呼叫，結果依內容雜湊快取在工作目錄）。
2. 摘要時把與會者／術語交給 :class:`~summery.summarizer.Summarizer`
   （``reference_hint``），讓 map／reduce／final 都以範本寫法校正聽錯的名字。
3. :func:`compose_minutes` —— 把結構化摘要依範本（沒有範本就用標準會議紀錄格式）
   寫成可以直接發出的 ``minutes.md``。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from . import prompts
from .config import Config
from .documents import Document, combine_documents_text
from .llm import LLMClient, LLMError
from .summarizer import Summary
from .util import LOG, fmt_clock, read_json, write_json


@dataclass
class ReferenceProfile:
    """從過往會議紀錄抽出的範本輪廓。"""

    files: list[str] = field(default_factory=list)
    chars: int = 0
    title_pattern: str = ""
    header_fields: list[str] = field(default_factory=list)
    outline: list[str] = field(default_factory=list)
    attendees: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    style_notes: list[str] = field(default_factory=list)
    #: 範本原文節錄（撰寫會議紀錄時給模型模仿版面；不寫進 summary.json）
    excerpt: str = ""
    #: model = 模型分析；local = 模型不可用時以標題規則推得
    source: str = "model"

    def to_dict(self, *, with_excerpt: bool = False) -> dict:
        data = {
            "files": self.files, "chars": self.chars, "title_pattern": self.title_pattern,
            "header_fields": self.header_fields, "outline": self.outline, "attendees": self.attendees,
            "terms": self.terms, "style_notes": self.style_notes, "source": self.source,
        }
        if with_excerpt:
            data["excerpt"] = self.excerpt
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ReferenceProfile":
        return cls(
            files=[str(x) for x in data.get("files") or []],
            chars=int(data.get("chars") or 0),
            title_pattern=str(data.get("title_pattern") or ""),
            header_fields=_str_list(data.get("header_fields")),
            outline=_str_list(data.get("outline")),
            attendees=_str_list(data.get("attendees")),
            terms=_str_list(data.get("terms")),
            style_notes=_str_list(data.get("style_notes")),
            excerpt=str(data.get("excerpt") or ""),
            source=str(data.get("source") or "model"),
        )

    def hint_text(self, *, max_terms: int = 40) -> str:
        """摘要各階段用的短提示：只放人名與術語（不放版面，避免每個區塊都重複大段文字）。"""
        lines = []
        if self.attendees:
            lines.append("・與會者：" + "、".join(self.attendees[:30]))
        if self.terms:
            lines.append("・專有名詞：" + "、".join(self.terms[:max_terms]))
        return "\n".join(lines)

    def prompt_json(self) -> str:
        return json.dumps(
            {k: v for k, v in self.to_dict().items() if k not in ("files", "chars", "source") and v},
            ensure_ascii=False, indent=1,
        )


def _str_list(value) -> list[str]:
    if isinstance(value, str):
        value = [v for v in re.split(r"[\n、,，;；]", value)]
    out: list[str] = []
    for item in value or []:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


# --------------------------------------------------------------------------
# 1. 範本分析
# --------------------------------------------------------------------------

_HEADER_FIELD = re.compile(r"^\s*[*_]*([^\s：:|#*]{2,8})[*_]*\s*[：:]")
_OUTLINE_LINE = re.compile(
    r"^\s*(?:#{1,4}\s+.+|[壹貳參肆伍陸柒捌玖拾]+[、.．].*|[一二三四五六七八九十]+[、.．].*|[（(][一二三四五六七八九十]+[)）].*)$"
)


def local_profile(docs: Sequence[Document], *, excerpt: str = "") -> ReferenceProfile:
    """不靠模型的範本輪廓：抓 Markdown 標題／「一、」類段落標題，以及「欄位：」開頭的欄位名稱。"""
    outline: list[str] = []
    fields: list[str] = []
    for doc in docs:
        for para in doc.paragraphs:
            line = para.strip()
            if _OUTLINE_LINE.match(line) and len(line) <= 40:
                title = line.lstrip("#").strip()
                if title not in outline:
                    outline.append(title)
            match = _HEADER_FIELD.match(line)
            if match and match.group(1) not in fields and len(fields) < 12:
                fields.append(match.group(1))
    return ReferenceProfile(
        files=[d.name for d in docs], chars=sum(d.chars for d in docs),
        header_fields=fields, outline=outline[:30], excerpt=excerpt, source="local",
    )


def analyze_references(
    docs: Sequence[Document], *, llm: LLMClient | None, cfg: Config, language: str | None = None,
    cache_dir: Path | None = None, warnings: list[str] | None = None,
) -> ReferenceProfile:
    """分析過往會議紀錄的格式與用語；模型不可用或失敗時退回 :func:`local_profile`。"""
    warnings = warnings if warnings is not None else []
    text = combine_documents_text(docs, max_chars=cfg.documents.reference_max_chars)
    excerpt = combine_documents_text(docs, max_chars=cfg.documents.template_excerpt_chars)
    language = language or cfg.summarize.language
    fallback = local_profile(docs, excerpt=excerpt)
    if llm is None:
        return fallback
    model = getattr(llm.cfg, "model", "")
    key = hashlib.sha256(f"{model}\n{language}\n{text}".encode("utf-8")).hexdigest()[:16]
    cache = (cache_dir / f"reference_{key}.json") if cache_dir else None
    if cache is not None:
        cached = read_json(cache)
        if isinstance(cached, dict) and cached.get("outline") is not None:
            profile = ReferenceProfile.from_dict(cached)
            profile.excerpt = excerpt
            LOG.info("沿用範本分析快取：%s", cache.name)
            return profile
    system, user = prompts.reference_prompt(text, count=len(docs), language=language)
    try:
        payload = llm.complete_json(system, user, stub_key="reference")
    except (LLMError, ValueError) as exc:
        warnings.append(f"範本分析失敗，改用標題規則推得段落結構：{exc}")
        LOG.warning("%s", warnings[-1])
        return fallback
    if isinstance(payload, list):
        payload = payload[0] if payload and isinstance(payload[0], dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    profile = ReferenceProfile.from_dict({**payload, "files": [d.name for d in docs], "chars": sum(d.chars for d in docs)})
    profile.source = "model"
    if not profile.outline:  # 模型沒給結構時，至少保留規則推得的標題順序
        profile.outline = fallback.outline
    if not profile.header_fields:
        profile.header_fields = fallback.header_fields
    profile.excerpt = excerpt
    if cache is not None:
        write_json(cache, profile.to_dict())
    return profile


# --------------------------------------------------------------------------
# 3. 撰寫會議紀錄
# --------------------------------------------------------------------------


def summary_digest(summary: Summary) -> dict:
    """給撰寫階段的精簡整理結果（不含除錯用筆記與用量資訊）。"""
    is_document = summary.is_document
    chapters = []
    for chapter in summary.chapters:
        where = (
            (f"段落 {chapter.para_start}–{chapter.para_end}" if chapter.para_end != chapter.para_start
             else f"段落 {chapter.para_start}")
            if is_document else fmt_clock(chapter.start, force_hours=True)
        )
        chapters.append({"where": where, "title": chapter.title, "summary": chapter.summary, "points": chapter.points})
    digest = {
        "title": summary.title,
        "tldr": summary.tldr,
        "overview": summary.overview,
        "topics": chapters,
        "key_points": summary.key_points,
        "decisions": summary.decisions,
        "action_items": summary.action_items,
        "open_questions": [q.text for q in summary.open_questions],
        "quotes": [
            {"text": q.get("text"), "speaker": q.get("speaker")} for q in summary.quotes if isinstance(q, dict)
        ],
    }
    return {k: v for k, v in digest.items() if v}


def _strip_fence(text: str) -> str:
    cleaned = (text or "").strip()
    match = re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", cleaned, re.S)
    return match.group(1).strip() if match else cleaned


def compose_minutes(
    summary: Summary, *, llm: LLMClient | None, cfg: Config, profile: ReferenceProfile | None = None,
    source_desc: str = "", language: str | None = None, warnings: list[str] | None = None,
) -> tuple[str, str]:
    """回傳 (會議紀錄 Markdown, 來源)；來源是 model／local。"""
    warnings = warnings if warnings is not None else []
    language = language or cfg.summarize.language
    if llm is None:
        return local_minutes(summary, profile=profile, source_desc=source_desc), "local"
    digest = summary_digest(summary)
    if source_desc:
        digest = {"source": source_desc, **digest}
    system, user = prompts.minutes_prompt(
        json.dumps(digest, ensure_ascii=False, indent=1),
        profile_json=profile.prompt_json() if profile else None,
        excerpt=profile.excerpt if profile else None,
        language=language,
        source_kind="document" if summary.is_document else None,
        custom=cfg.summarize.custom_instruction,
    )
    limit = int(cfg.documents.minutes_max_output_tokens or 0) or None
    try:
        res = llm.complete(system, user, max_tokens=limit, stub_key="minutes")
        text = _strip_fence(res.text)
        if not text:
            raise LLMError("模型沒有回傳會議紀錄內容", retryable=False)
    except LLMError as exc:
        warnings.append(f"會議紀錄撰寫失敗，改用預設格式組成：{exc}")
        LOG.warning("%s", warnings[-1])
        return local_minutes(summary, profile=profile, source_desc=source_desc), "local"
    return text.rstrip() + "\n", "model"


def local_minutes(summary: Summary, *, profile: ReferenceProfile | None = None, source_desc: str = "") -> str:
    """不靠模型的會議紀錄：標準格式，欄位沿用範本的欄位名稱。"""
    lines = [f"# {summary.title or '會議紀錄'}", ""]
    fields = (profile.header_fields if profile and profile.header_fields else
              ["時間", "地點", "主席", "出席人員", "記錄"])
    lines += ["## 會議資訊", ""]
    for name in fields:
        value = "、".join(profile.attendees) if profile and name in ("出席", "出席人員", "與會者", "出席者") and profile.attendees else "（未提及）"
        lines.append(f"- {name}：{value}")
    if source_desc:
        lines.append(f"- 來源：{source_desc}")
    lines.append("")
    if summary.tldr:
        lines += ["## 會議摘要", "", summary.tldr.strip(), ""]
    if summary.chapters:
        lines += ["## 討論事項", ""]
        for i, chapter in enumerate(summary.chapters, start=1):
            where = (f"段落 {chapter.para_start}–{chapter.para_end}" if summary.is_document
                     else fmt_clock(chapter.start, force_hours=True))
            lines.append(f"### {i}. {chapter.title}（{where}）")
            if chapter.summary:
                lines.append(chapter.summary.strip())
            lines += [f"- {p}" for p in chapter.points]
            lines.append("")
    lines += ["## 決議事項", ""]
    lines += [f"{i}. {d}" for i, d in enumerate(summary.decisions, start=1)] or ["（未提及）"]
    lines += ["", "## 待辦事項", "", "| 事項 | 負責人 | 期限 |", "| --- | --- | --- |"]
    for item in summary.action_items:
        lines.append(f"| {item.get('task', '')} | {item.get('owner') or '未標明'} | {item.get('due') or '未標明'} |")
    if not summary.action_items:
        lines.append("| （未提及） | | |")
    if summary.open_questions:
        lines += ["", "## 待釐清問題", ""] + [f"- {q.text}" for q in summary.open_questions]
    return "\n".join(lines).rstrip() + "\n"
