"""長逐字稿摘要：map → reduce → final 的階層式彙整。

重點設計：
* 逐字稿超出 LLM context 時，先分段產生「區塊筆記」（map），
  再多輪彙整（reduce）到可放進單次提示的大小，最後產生面向讀者的摘要。
* 章節時間戳由「編號段落 → 實際時間」對照表決定，模型只負責標題與內容，
  避免模型亂編時間。
"""

from __future__ import annotations

import re

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Sequence

from . import prompts
from .config import SummarizeConfig
from .llm import LLMClient, LLMError
from .transcript import Paragraph, Transcript
from .util import LOG, clamp, estimate_tokens, fmt_clock

ProgressFn = Callable[[str, float, str], None]


@dataclass
class Note:
    summary: str
    key_points: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    para_start: int = 1
    para_end: int = 1
    level: int = 0

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "key_points": self.key_points,
            "terms": self.terms,
            "issues": self.issues,
            "para_start": self.para_start,
            "para_end": self.para_end,
            "level": self.level,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Note":
        return cls(
            summary=str(d.get("summary", "")),
            key_points=[str(x) for x in (d.get("key_points") or [])],
            terms=[str(x) for x in (d.get("terms") or [])],
            issues=[str(x) for x in (d.get("issues") or [])],
            para_start=int(d.get("para_start", 1)),
            para_end=int(d.get("para_end", 1)),
            level=int(d.get("level", 0)),
        )

    def text(self) -> str:
        parts = [self.summary.strip()]
        if self.key_points:
            parts.append("重點：" + "；".join(k.strip() for k in self.key_points if k.strip()))
        if self.terms:
            parts.append("術語：" + "、".join(self.terms[:12]))
        if self.issues:
            parts.append("疑點：" + "；".join(self.issues[:4]))
        return "\n".join(p for p in parts if p)


@dataclass
class Chapter:
    start: float
    end: float
    title: str
    summary: str = ""
    points: list[str] = field(default_factory=list)
    para_start: int = 0
    para_end: int = 0

    def to_dict(self) -> dict:
        return {
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "clock": fmt_clock(self.start, force_hours=True),
            "title": self.title,
            "summary": self.summary,
            "points": self.points,
            "para_start": self.para_start,
            "para_end": self.para_end,
        }


@dataclass
class OpenQuestion:
    """一條待釐清問題，盡量附上「跳回去聽」的時間點。"""

    text: str
    para: int | None = None
    start: float | None = None
    clock: str | None = None
    located: str = "none"  # para=模型標的段落編號；text=由引號詞句比對；none=無法定位

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "para": self.para,
            "start": self.start,
            "clock": self.clock,
            "located": self.located,
        }


@dataclass
class Summary:
    title: str = ""
    tldr: str = ""
    overview: list[str] = field(default_factory=list)
    key_points: list[str] = field(default_factory=list)
    chapters: list[Chapter] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    action_items: list[dict] = field(default_factory=list)
    open_questions: list[OpenQuestion] = field(default_factory=list)
    quotes: list[dict] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    #: 依範本（或預設會議紀錄格式）撰寫的正式會議紀錄，沒有產生時為空字串
    minutes_markdown: str = ""

    @property
    def is_document(self) -> bool:
        """輸入是文件（沒有時間軸）：章節與問題以段落編號標示。"""
        return self.meta.get("input_kind") == "document"

    def _chapter_dicts(self) -> list[dict]:
        out = []
        for chapter in self.chapters:
            data = chapter.to_dict()
            if self.is_document:
                data["clock"] = _para_label(chapter.para_start, chapter.para_end)
            out.append(data)
        return out

    def to_dict(self) -> dict:
        return {
            "schema": "summery.summary/1",
            "title": self.title,
            "tldr": self.tldr,
            "overview": self.overview,
            "key_points": self.key_points,
            "chapters": self._chapter_dicts(),
            "keywords": self.keywords,
            "decisions": self.decisions,
            "action_items": self.action_items,
            "open_questions": [q.to_dict() for q in self.open_questions],
            "quotes": self.quotes,
            "minutes_markdown": self.minutes_markdown,
            "meta": self.meta,
        }

    def to_json(self, *, indent: int = 2) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def to_markdown(self, *, duration: float | None = None, with_notes: bool = False) -> str:
        lines: list[str] = []
        lines.append(f"# {self.title or ('會議紀錄' if self.is_document else '影音摘要')}")
        lines.append("")
        if self.is_document:
            names = [str(d.get("name")) for d in self.meta.get("documents") or [] if isinstance(d, dict)]
            if names:
                lines.append(f"> 來源文件：{'、'.join(names)}")
                lines.append("")
        elif duration:
            lines.append(f"> 長度 {fmt_clock(duration, force_hours=True)}")
            lines.append("")
        if self.tldr:
            lines += ["## TL;DR", "", self.tldr.strip(), ""]
        if self.overview:
            lines += ["## 內容概要", ""]
            lines += [p.strip() for p in self.overview if p.strip()]
            lines.append("")
        if self.chapters:
            lines += ["## 章節摘要", ""]
            for ch in self.chapters:
                head = _para_label(ch.para_start, ch.para_end) if self.is_document else fmt_clock(ch.start, force_hours=True)
                lines.append(f"- **[{head}] {ch.title}**")
                if ch.summary:
                    lines.append(f"  {ch.summary.strip()}")
                for pt in ch.points:
                    lines.append(f"  - {pt}")
            lines.append("")
        if self.key_points:
            lines += ["## 重點整理", ""] + [f"- {p}" for p in self.key_points] + [""]
        if self.decisions:
            lines += ["## 結論與決策", ""] + [f"- {d}" for d in self.decisions] + [""]
        if self.action_items:
            lines += ["## 待辦事項", ""]
            for item in self.action_items:
                if isinstance(item, dict):
                    task = item.get("task") or item.get("description") or ""
                    owner = item.get("owner") or "未標明"
                    due = item.get("due") or "未標明"
                    lines.append(f"- {task}（負責人：{owner}／期限：{due}）")
                else:
                    lines.append(f"- {item}")
            lines.append("")
        if self.open_questions:
            lines += ["## 待釐清問題", ""]
            for q in self.open_questions:
                head = f"`[{q.clock}]` " if q.clock else ""
                lines.append(f"- {head}{q.text}")
            lines.append("")
        if self.quotes:
            lines += ["## 重要原話", ""]
            for q in self.quotes:
                if isinstance(q, dict):
                    lines.append(f"> 「{q.get('text', '')}」 — {q.get('speaker', '未標明')}")
                else:
                    lines.append(f"> 「{q}」")
            lines.append("")
        if self.keywords:
            lines += ["## 關鍵字", "", " ".join(f"`{k}`" for k in self.keywords), ""]
        if with_notes and self.notes:
            lines += ["## 區塊筆記（除錯用）", ""]
            for n in self.notes:
                lines.append(f"- [p{n.para_start}-{n.para_end}] {n.summary}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _para_label(start: int, end: int) -> str:
    if not start:
        return "段落 ?"
    return f"段落 {start}" if not end or end == start else f"段落 {start}–{end}"


# --------------------------------------------------------------------------
# 區塊切分
# --------------------------------------------------------------------------


@dataclass
class Block:
    para_start: int
    para_end: int
    text: str
    start: float
    end: float


def split_blocks(paras: Sequence[Paragraph], *, budget_tokens: int, chars_per_token: float, numbered: Sequence[str]) -> list[Block]:
    """把編號段落打包成不超過 budget_tokens 的區塊（不切斷段落）。"""
    budget = max(120, int(budget_tokens))
    blocks: list[Block] = []
    buf: list[str] = []
    start_para = end_para = 1
    t0 = t1 = 0.0
    cur_tokens = 0
    for idx, (para, line) in enumerate(zip(paras, numbered), start=1):
        line_tokens = estimate_tokens(line, chars_per_token)
        if line_tokens > budget:  # 單段過長：獨立成塊（後續由 map 提示自行壓縮）
            if buf:
                blocks.append(Block(start_para, end_para, "\n".join(buf), t0, t1))
                buf, cur_tokens = [], 0
            blocks.append(Block(idx, idx, line, para.start, para.end))
            buf, start_para, t0 = [], 0, 0.0
            buf_start = None
            continue
        if buf and cur_tokens + line_tokens > budget:
            blocks.append(Block(start_para, end_para, "\n".join(buf), t0, t1))
            buf, cur_tokens = [], 0
            start_para, t0 = idx, para.start
        if not buf:
            start_para, t0 = idx, para.start
        buf.append(line)
        end_para, t1 = idx, para.end
        cur_tokens += line_tokens
    if buf:
        blocks.append(Block(start_para, end_para, "\n".join(buf), t0, t1))
    return blocks


# --------------------------------------------------------------------------
# Summarizer
# --------------------------------------------------------------------------


class Summarizer:
    def __init__(
        self, llm: LLMClient, cfg: SummarizeConfig, *, output_language: str | None = None,
        progress: ProgressFn | None = None, cancel: threading.Event | None = None,
        reference_hint: str | None = None,
    ):
        self.llm = llm
        self.cfg = cfg
        self.progress = progress or (lambda *_: None)
        self.cancel = cancel or threading.Event()
        self.language = output_language or cfg.language
        #: 過往會議紀錄抽出的人名／術語提示（見 minutes.ReferenceProfile.hint_text）
        self.reference_hint = reference_hint
        #: "document" 時改用段落編號、不放時間戳（由 summarize() 依逐字稿 meta 決定）
        self.source_kind: str | None = None
        self.documents = 1
        self.warnings: list[str] = []

    # -- 主流程 ----------------------------------------------------------
    def summarize(self, transcript: Transcript, *, title_hint: str | None = None) -> Summary:
        paras = transcript.paragraphs(
            gap=float(transcript.meta.get("paragraph_gap", 0.9)),
            max_chars=int(transcript.meta.get("paragraph_max_chars", 180)),
        )
        is_document = transcript.meta.get("source_kind") == "document"
        self.source_kind = "document" if is_document else None
        self.documents = len(transcript.meta.get("documents") or []) or 1
        if not paras:
            return Summary(title=title_hint or "無內容摘要", tldr="逐字稿沒有可摘要的內容。",
                           meta={"mode": "empty", "warnings": ["empty transcript"],
                                 "input_kind": "document" if is_document else "media"})
        numbered = [
            f"[{i}] [{fmt_clock(p.start, force_hours=True)}] {p.text}"
            if self.cfg.timestamp_lines and not is_document else f"[{i}] {p.text}"
            for i, p in enumerate(paras, start=1)
        ]
        full_text = "\n".join(numbered)
        total_tokens = estimate_tokens(full_text, self.cfg.chars_per_token)
        duration = transcript.duration or (paras[-1].end if paras else 0.0)
        meta: dict = {
            "mode": "direct",
            "paragraphs": len(paras),
            "chars": sum(len(p.text) for p in paras),
            "estimated_input_tokens": total_tokens,
            "context_budget_tokens": self.cfg.max_input_tokens,
            "language": self.language,
            "style": self.cfg.style,
            "input_kind": "document" if is_document else "media",
        }
        if is_document:
            meta["documents"] = list(transcript.meta.get("documents") or [])
        if self.reference_hint:
            meta["reference_hint"] = True

        if total_tokens <= self.cfg.max_input_tokens:
            self.progress("summarize", 0.15, f"逐字稿約 {total_tokens} tokens，單次摘要")
            system, user = prompts.final_prompt(
                full_text, duration=fmt_clock(duration, force_hours=True), chars=meta["chars"],
                language=self.language, style=self.cfg.style, custom=self.cfg.custom_instruction,
                **self._prompt_extras(documents=True),
            )
            payload = self._json_call(system, user, stub_key="final")
            summary = self._build_summary(payload, paras, notes=[], meta=meta, title_hint=title_hint)
            self.progress("summarize", 1.0, "摘要完成")
            return summary

        # ---- map -------------------------------------------------------
        self.progress("summarize", 0.05, f"逐字稿約 {total_tokens} tokens，啟用 map-reduce 摘要")
        blocks = split_blocks(
            paras, budget_tokens=self.cfg.map_chunk_tokens,
            chars_per_token=self.cfg.chars_per_token, numbered=numbered,
        )
        meta["map_blocks"] = len(blocks)
        meta["map_chunk_tokens"] = self.cfg.map_chunk_tokens
        LOG.info("摘要 map 階段：%d 個區塊（budget %d tokens）", len(blocks), self.cfg.map_chunk_tokens)
        notes = self._map_stage(blocks)
        if not notes:
            raise LLMError("摘要失敗：所有區塊筆記皆失敗")

        # ---- reduce ----------------------------------------------------
        rounds = 0
        while len(notes) > 1:
            combined = "\n\n".join(self._note_line(n, i) for i, n in enumerate(notes, start=1))
            if estimate_tokens(combined, self.cfg.chars_per_token) <= self.cfg.max_input_tokens * 0.72:
                break
            if rounds >= 6:
                self.warnings.append("彙整輪次達上限，以目前筆記直接產生摘要")
                break
            rounds += 1
            notes = self._reduce_stage(notes, round_no=rounds)
        meta["reduce_rounds"] = rounds
        meta["final_notes"] = len(notes)

        # ---- final -----------------------------------------------------
        notes_text = "\n\n".join(self._note_line(n, i) for i, n in enumerate(notes, start=1))
        head, tail = self._anchor_text(paras)
        self.progress("summarize", 0.9, f"彙整 {len(notes)} 份筆記 → 最終摘要")
        system, user = prompts.final_prompt_notes(
            notes_text, duration=fmt_clock(duration, force_hours=True), chars=meta["chars"],
            para_total=len(paras), head=head, tail=tail, language=self.language,
            style=self.cfg.style, custom=self.cfg.custom_instruction, **self._prompt_extras(documents=True),
        )
        meta["mode"] = "map-reduce"
        payload = self._json_call(system, user, stub_key="final")
        summary = self._build_summary(payload, paras, notes=notes, meta=meta, title_hint=title_hint)
        self.progress("summarize", 1.0, "摘要完成")
        return summary

    # -- 各階段 ----------------------------------------------------------
    def _map_stage(self, blocks: Sequence[Block]) -> list[Note]:
        total = len(blocks)
        workers = max(1, min(int(getattr(self.llm.cfg, "max_concurrency", 4) or 4), total))
        results: list[Note | None] = [None] * total
        done = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sum-map") as pool:
            futures = {
                pool.submit(self._map_one, b, i, total): i for i, b in enumerate(blocks)
            }
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("區塊 %d/%d 筆記失敗：%s", i + 1, total, exc)
                    self.warnings.append(f"區塊 {i + 1}/{total} 筆記失敗：{exc}")
                    results[i] = self._fallback_note(blocks[i])
                done += 1
                self.progress("summarize:map", done / max(1, total), f"區塊筆記 {done}/{total}")
        return [n for n in results if n is not None]

    def _map_one(self, block: Block, index: int, total: int) -> Note:
        self._check_cancel()
        if self.source_kind == "document":
            clock_start, clock_end = f"段落 {block.para_start}", f"段落 {block.para_end}"
        else:
            clock_start = fmt_clock(block.start, force_hours=True)
            clock_end = fmt_clock(block.end, force_hours=True)
        system, user = prompts.map_prompt(
            block.text, clock_start=clock_start, clock_end=clock_end, block_no=index + 1, block_total=total,
            language=self.language, style=self.cfg.style, custom=self.cfg.custom_instruction,
            **self._prompt_extras(),
        )
        payload = self._json_call(system, user, stub_key=f"map:{index}")
        note = self._note_from_payload(payload, fallback=block)
        note.para_start, note.para_end = block.para_start, block.para_end
        note.level = 0
        return note

    def _reduce_stage(self, notes: Sequence[Note], *, round_no: int) -> list[Note]:
        fan = max(2, int(self.cfg.reduce_fan_in))
        groups = [list(notes[i : i + fan]) for i in range(0, len(notes), fan)]
        if len(groups) == len(notes):  # 一組一筆沒有意義
            groups = [list(notes[i : i + max(2, fan)]) for i in range(0, len(notes), max(2, fan))]
        workers = max(1, min(int(getattr(self.llm.cfg, "max_concurrency", 4) or 4), len(groups)))
        out: list[Note | None] = [None] * len(groups)
        done = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sum-reduce") as pool:
            futures = {
                pool.submit(self._reduce_one, g, round_no, gi + 1, len(groups)): gi
                for gi, g in enumerate(groups)
            }
            for fut in as_completed(futures):
                gi = futures[fut]
                try:
                    out[gi] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("reduce 群 %d 失敗：%s", gi + 1, exc)
                    self.warnings.append(f"reduce 群 {gi + 1} 失敗：{exc}")
                    out[gi] = self._merge_notes_locally(groups[gi])
                done += 1
                self.progress("summarize:reduce", done / max(1, len(groups)), f"彙整 {done}/{len(groups)} 組（第 {round_no} 輪）")
        return [n for n in out if n is not None]

    def _reduce_one(self, group: Sequence[Note], round_no: int, index: int, count: int) -> Note:
        self._check_cancel()
        notes_text = "\n\n".join(self._note_line(n, i) for i, n in enumerate(group, start=1))
        start_para = min(n.para_start for n in group)
        end_para = max(n.para_end for n in group)
        system, user = prompts.reduce_prompt(
            notes_text, clock_start=f"para {start_para}", clock_end=f"para {end_para}",
            round_no=round_no, group_index=index, group_count=count,
            language=self.language, style=self.cfg.style, custom=self.cfg.custom_instruction,
            **self._prompt_extras(),
        )
        payload = self._json_call(system, user, stub_key=f"reduce:{round_no}:{index}")
        merged = self._note_from_payload(payload, fallback=None)
        merged.para_start, merged.para_end = start_para, end_para
        merged.level = max(n.level for n in group) + 1
        if not merged.terms:
            merged.terms = _top_terms([t for n in group for t in n.terms], limit=14)
        if not merged.issues:
            merged.issues = [i for n in group for i in n.issues][:4]
        return merged

    # -- helpers ---------------------------------------------------------
    def _prompt_extras(self, *, documents: bool = False) -> dict:
        extras: dict = {"source_kind": self.source_kind, "reference": self.reference_hint}
        if documents:
            extras["documents"] = self.documents
        return extras

    def _json_call(self, system: str, user: str, *, stub_key: str | None = None):
        self._check_cancel()
        return self.llm.complete_json(system, user, stub_key=stub_key)

    def _note_from_payload(self, payload, *, fallback: Block | None) -> Note:
        if isinstance(payload, list):
            payload = payload[0] if payload and isinstance(payload[0], dict) else {}
        if not isinstance(payload, dict):
            payload = {}
        summary = str(payload.get("summary") or payload.get("note") or "").strip()
        if not summary and fallback is not None:
            summary = fallback.text[:600]
            self.warnings.append("區塊筆記缺少 summary，改用原文前段")
        return Note(
            summary=summary,
            key_points=[str(x) for x in (payload.get("key_points") or []) if str(x).strip()],
            terms=[str(x) for x in (payload.get("terms") or payload.get("keywords") or []) if str(x).strip()],
            issues=[str(x) for x in (payload.get("issues") or []) if str(x).strip()],
        )

    def _fallback_note(self, block: Block) -> Note:
        self.warnings.append(f"區塊 para {block.para_start}-{block.para_end} 使用原文截斷作為筆記")
        return Note(
            summary=block.text[:500].strip(),
            key_points=[],
            para_start=block.para_start,
            para_end=block.para_end,
        )

    def _merge_notes_locally(self, group: Sequence[Note]) -> Note:
        """reduce 呼叫失敗時的本地保底彙整（不調 LLM）。"""
        return Note(
            summary="\n".join(n.summary for n in group if n.summary)[:1200],
            key_points=[k for n in group for k in n.key_points][:20],
            terms=_top_terms([t for n in group for t in n.terms], limit=14),
            issues=[i for n in group for i in n.issues][:4],
            para_start=min(n.para_start for n in group),
            para_end=max(n.para_end for n in group),
            level=max(n.level for n in group) + 1,
        )

    def _note_line(self, note: Note, index: int) -> str:
        body = note.text()
        return f"[筆記 {index}｜段落 {note.para_start}-{note.para_end}｜L{note.level}]\n{body}"

    def _anchor_text(self, paras: Sequence[Paragraph], *, limit_chars: int = 900) -> tuple[str, str]:
        head = "\n".join(p.text for p in paras if p.text)[:limit_chars]
        tail = "".join(p.text + "\n" for p in reversed(paras))[:limit_chars]
        tail = "\n".join(reversed(tail.split("\n")))
        return head, tail

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise KeyboardInterrupt("已被使用者取消")

    # -- 組裝 ------------------------------------------------------------
    def _build_summary(
        self, payload, paras: Sequence[Paragraph], *, notes: Sequence[Note], meta: dict, title_hint: str | None
    ) -> Summary:
        if isinstance(payload, list):
            payload = payload[0] if payload and isinstance(payload[0], dict) else {}
        if not isinstance(payload, dict):
            self.warnings.append("最終摘要回傳不是 JSON 物件，改用筆記組成")
            payload = {}
        local_only = False
        if not self._payload_has_content(payload):
            # 推理型模型把預算花在思考上、或回傳了非要求格式時，最常出現「有呼叫但沒內容」。
            self.warnings.append(
                "模型未回傳可用的摘要內容（常見原因：輸出被截斷 finish_reason=length、或回傳不是要求的 JSON），"
                "已改用本地彙整產出摘要；建議調高 llm.max_output_tokens 或改用非推理型模型"
            )
            LOG.warning("%s", self.warnings[-1])
            payload = self._local_payload(paras, notes)
            local_only = True
        meta = dict(meta)
        meta["warnings"] = list(self.warnings)
        meta["llm_usage"] = self.llm.usage.to_dict()
        chapters, chapter_source = self._chapters_from_payload(payload, paras, notes)
        if local_only:
            chapter_source = "local_fallback"
        meta["chapters_source"] = chapter_source
        if chapter_source == "repaired_from_notes":
            note = "模型回報的章節未涵蓋全場（只到結尾的開頭部分），已改用分區筆記重建章節"
            if note not in meta["warnings"]:
                meta["warnings"].append(note)
                LOG.warning("%s", note)
        overview = payload.get("overview") or payload.get("summary") or []
        if isinstance(overview, str):
            overview = [line.strip() for line in overview.split("\n") if line.strip()]
        keywords = [str(k) for k in (payload.get("keywords") or []) if str(k).strip()]
        if not keywords and notes:
            keywords = _top_terms([t for n in notes for t in n.terms], limit=12)
        tldr = str(payload.get("tldr") or payload.get("tldr_") or "").strip()
        if not tldr and notes:
            tldr = notes[0].summary[:160]
        open_questions, questions_timed = _normalize_open_questions(payload.get("open_questions"), paras)
        if self.source_kind == "document":
            for question in open_questions:  # 文件沒有時間軸：標成段落編號
                question.clock = _para_label(question.para, question.para) if question.para else None
        if open_questions:
            # 讓使用者看得出來這些問題有沒有時間點可跳
            meta["open_questions_timed"] = f"{questions_timed}/{len(open_questions)}"
        return Summary(
            title=str(payload.get("title") or title_hint or "").strip() or (title_hint or "影音摘要"),
            tldr=tldr,
            overview=[str(o).strip() for o in overview if str(o).strip()],
            key_points=[str(k).strip() for k in (payload.get("key_points") or []) if str(k).strip()],
            chapters=chapters,
            keywords=keywords,
            decisions=[str(d) for d in (payload.get("decisions") or []) if str(d).strip()],
            action_items=_normalize_action_items(payload.get("action_items")),
            open_questions=open_questions,
            quotes=_normalize_quotes(payload.get("quotes")),
            notes=list(notes),
            meta=meta,
        )

    @staticmethod
    def _payload_has_content(payload: dict) -> bool:
        """模型有沒有真的回傳可用的摘要欄位（用來避免產出空摘要）。"""
        for key in ("tldr", "tldr_", "overview", "summary", "key_points", "keyPoints", "chapters", "sections"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return True
            if isinstance(value, (list, dict)) and len(value) > 0:
                return True
        return False

    def _local_payload(self, paras: Sequence[Paragraph], notes: Sequence[Note]) -> dict:
        """完全沒有模型輸出時的保底摘要：內容取自逐字稿本身，章節大綱仍附正確時間軸。"""
        texts = [p.text.strip() for p in paras if p.text.strip()]
        if not texts:
            return {}
        picks = max(1, min(self.cfg.max_chapters, -(-len(paras) // 2)))  # 保底大綱不超過一半段落數
        per = max(1, -(-len(paras) // picks))  # ceil
        chapters: list[dict] = []
        for i in range(0, len(paras), per):
            group = list(paras[i : i + per])
            if not group:
                continue
            body = " ".join(g.text.strip() for g in group[:2]).strip()
            chapters.append(
                {
                    "para_start": i + 1,
                    "para_end": min(len(paras), i + per),
                    "title": _first_sentence(group[0].text, limit=28) or f"段落 {i + 1}",
                    "summary": (body[:220] + "…") if len(body) > 220 else body,
                    "points": [],
                }
            )
        stride = max(1, len(texts) // 5)
        sampled = [texts[i] for i in range(0, len(texts), stride)][:5]
        return {
            "tldr": (notes[0].summary[:200] if notes else _first_sentence(texts[0], limit=120)),
            "overview": [_first_sentence(t, limit=120) for t in sampled[:3]],
            "key_points": [p for n in notes for p in n.key_points][:8]
            or [_first_sentence(t, limit=100) for t in sampled],
            "chapters": chapters,
            "keywords": _top_terms(_split_terms(" ".join(texts)), limit=12),
        }

    def _chapters_from_payload(
        self, payload, paras: Sequence[Paragraph], notes: Sequence[Note]
    ) -> tuple[list[Chapter], str]:
        """章節時間一律由段落索引對照表決定。

        模型回報的段落編號若明顯偏離全場（例如只涵蓋前 60%），代表它只看到開頭就結案；
        此時改用「分區筆記」的段落範圍重建，時間仍然正確。
        """
        raw = payload.get("chapters") or payload.get("sections") or []
        chapters: list[Chapter] = []
        last_end = 0.0
        for item in raw:
            if not isinstance(item, dict):
                continue
            start_idx = _as_int(item.get("para_start") or item.get("start_para") or item.get("start"))
            end_idx = _as_int(item.get("para_end") or item.get("end_para") or item.get("end"))
            if start_idx is None and notes and _as_int(item.get("note_start")) is not None:
                note = notes[clamp(_as_int(item["note_start"], 1), 1, len(notes)) - 1]
                start_idx, end_idx = note.para_start, note.para_end
            if start_idx is None:
                continue
            end_idx = end_idx or start_idx
            start_idx = clamp(start_idx, 1, len(paras))
            end_idx = clamp(end_idx, start_idx, len(paras))
            start = paras[start_idx - 1].start
            end = paras[end_idx - 1].end
            if end <= start:
                end = min(start + 30.0, paras[-1].end if paras else start)
            points = item.get("points") or item.get("key_points") or []
            chapters.append(
                Chapter(
                    start=start,
                    end=end,
                    title=str(item.get("title") or f"章節 {len(chapters) + 1}").strip(),
                    summary=str(item.get("summary") or "").strip(),
                    points=[str(p) for p in points if str(p).strip()],
                    para_start=int(start_idx),
                    para_end=int(end_idx),
                )
            )
            last_end = end
        if chapters:
            chapters.sort(key=lambda c: c.start)
            covered = max((c.para_end for c in chapters), default=0) / max(1, len(paras))
            contiguous = _no_large_gaps(chapters, len(paras))
            if notes and len(notes) > 1 and (covered < 0.6 or not contiguous):
                LOG.info("模型章節僅涵蓋 %.0f%% 的內容（%s），改用筆記重建", covered * 100,
                         "覆蓋不足" if covered < 0.6 else "留有明顯空缺")
                rebuilt = self._chapters_from_notes(notes, paras)
                if rebuilt:
                    return rebuilt, "repaired_from_notes"
            return chapters, "model"
        # 模型未給章節 → 由筆記推導（時間仍正確）
        if not self.cfg.chapters:
            return [], "none"
        return self._chapters_from_notes(notes, paras), ("notes_fallback" if notes else "none")

    def _chapters_from_notes(self, notes: Sequence[Note], paras: Sequence[Paragraph]) -> list[Chapter]:
        if not notes or not paras:
            return []
        picked = list(notes)
        if len(picked) > self.cfg.max_chapters:  # 節點過多時等距抽樣，保持全場覆蓋
            step = len(picked) / float(self.cfg.max_chapters)
            picked = [picked[min(len(picked) - 1, int(i * step))] for i in range(self.cfg.max_chapters)]
        chapters: list[Chapter] = []
        for note in picked:
            s = clamp(note.para_start, 1, len(paras))
            e = clamp(note.para_end, int(s), len(paras))
            chapters.append(
                Chapter(
                    start=paras[s - 1].start,
                    end=paras[e - 1].end,
                    title=_chapter_title(note.summary, f"章節 {len(chapters) + 1}"),
                    summary=note.summary,
                    points=note.key_points[:4],
                    para_start=int(s),
                    para_end=int(e),
                )
            )
        return chapters


_NOTE_META_PREFIX = re.compile(r"^(?:區塊\s*[0-9a-fA-F]{4,}|段落\s*\d+\s*-\s*\d+)[^：:]*[：:]\s*")


def _chapter_title(note_summary: str, fallback: str) -> str:
    """筆記常以「區塊 abc123（段落 1-53）的重點：…」開頭，標題需去掉這層元描述。"""
    text = _NOTE_META_PREFIX.sub("", (note_summary or "").strip(), count=1)
    if not text:
        return fallback
    return _first_sentence(text, limit=28) or fallback


def _no_large_gaps(chapters: Sequence["Chapter"], total_paras: int, *, max_gap_ratio: float = 0.18) -> bool:
    """章節之間不留超過 18% 內容的空缺。"""
    if not chapters or total_paras <= 0:
        return False
    if chapters[0].para_start > max(2, int(total_paras * max_gap_ratio)):
        return False
    for prev, cur in zip(chapters, chapters[1:]):
        if cur.para_start - prev.para_end > max(2, int(total_paras * max_gap_ratio)):
            return False
    return total_paras - chapters[-1].para_end <= max(2, int(total_paras * max_gap_ratio))


def _as_float(value, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _first_sentence(text: str, *, limit: int = 28) -> str:
    text = (text or "").strip().replace("\n", " ")
    for sep in ("。", "；", "。 ", ".", "!"):
        if sep in text:
            head = text.split(sep, 1)[0].strip()
            if 4 <= len(head) <= limit:
                return head
    return text[:limit]


_TERM_SPLIT = re.compile(r"[\s,，。、；;：:！!？?（）()\u3000「」『』…—\-/]+")


def _split_terms(text: str) -> list[str]:
    """本地保底用的粗略詞切分（僅在模型完全沒輸出時使用）。

    中文沒有空格，因此長詞再拆 2-gram；純數字直接忽略。
    """
    out: list[str] = []
    for raw in _TERM_SPLIT.split(text or ""):
        if not raw or raw.isdigit():
            continue
        if len(raw) <= 8:
            out.append(raw)
            continue
        for run in re.findall(r"[\u4e00-\u9fff]{2,}", raw):
            out.extend(run[i : i + 2] for i in range(len(run) - 1))
    return [t for t in out if not t.isdigit()]


def _top_terms(terms: Sequence[str], *, limit: int = 12) -> list[str]:
    counts: dict[str, int] = {}
    for term in terms:
        key = str(term).strip()
        if 1 <= len(key) <= 24:
            counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [k for k, _ in ranked[:limit]]


def _normalize_action_items(raw) -> list[dict]:
    out: list[dict] = []
    for item in raw or []:
        if isinstance(item, dict):
            task = str(item.get("task") or item.get("action") or item.get("description") or "").strip()
            if not task:
                continue
            out.append({
                "task": task,
                "owner": str(item.get("owner") or "未標明").strip() or "未標明",
                "due": str(item.get("due") or item.get("deadline") or "未標明").strip() or "未標明",
            })
        elif isinstance(item, str) and item.strip():
            out.append({"task": item.strip(), "owner": "未標明", "due": "未標明"})
    return out


_QUOTED_TERM_RE = re.compile(r"[「『\"『]([^」』\"』]{2,24})[」』\"』]")
# 詞在多少段以內出現才算有辨識力；模型親手用「」括出來的詞比較可以放寬
_RARE_PARAGRAPH_HITS = 2
_QUOTED_PARAGRAPH_HITS = 8


def _candidate_terms(text: str) -> tuple[list[str], list[str]]:
    """從一句待釐清問題抽出可回查逐字稿的線索詞，分成兩組：

    * quoted：模型用「」括出的原詞，即使是兩個字（例：「解放」）也值得找；
    * inferred：從句子直接推得的拉丁字串與中文 n-gram，必須夠長夠少見才採信。

    會需要 inferred 是因為模型不一定加引號（例：「Beyond Meat 可能辨識為 Beyond Me」）。
    """
    split = re.compile(r"[／/、,，;；()（）\s]+")
    quoted: list[str] = []
    for chunk in _QUOTED_TERM_RE.findall(text):
        quoted.append(chunk.strip())  # 整段保留：比對時會忽略空白，「第 3 段」也找得到
        quoted += [t for t in split.split(chunk) if len(t.strip()) >= 2]
    body = _QUOTED_TERM_RE.sub(" ", text)
    latin = [t.replace(" ", "") for t in re.findall(r"[A-Za-z][A-Za-z .\-']{3,}", body)]
    grams = [
        run[i : i + n]
        for run in re.findall(r"[\u4e00-\u9fff]{3,}", body)
        for n in (5, 4, 3)
        for i in range(len(run) - n + 1)
    ]
    def uniq(items: list[str]) -> list[str]:
        out: list[str] = []
        for item in items:
            item = item.strip()
            if item and item not in out:
                out.append(item)
        return out

    return uniq(quoted), uniq([t for t in latin if len(t) >= 4] + grams)


def _find_terms_in_transcript(
    text: str, paras: Sequence[Paragraph], near: int | None = None
) -> tuple[int, float] | None:
    """把問題中的線索詞回查逐字稿，回傳 (段落編號, 時間)。

    推測出來的詞只採信「少見」者（≤2 段），否則「問題」「逐字稿」這種常用詞
    會把時間指到不相干的地方；同一詞出現多段時，挑離模型回報段落最近的一次。
    """
    quoted, inferred = _candidate_terms(text)
    bodies = [re.sub(r"\s+", "", para.text or "") for para in paras]
    best: tuple[tuple[int, int, int], tuple[int, float]] | None = None
    for tier, terms, min_len, cap in ((1, quoted, 2, _QUOTED_PARAGRAPH_HITS), (0, inferred, 3, _RARE_PARAGRAPH_HITS)):
        for term in terms:
            key = re.sub(r"\s+", "", term)
            if len(key) < min_len:
                continue
            hits = [number for number, body in enumerate(bodies, start=1) if key in body]
            if not hits or len(hits) > cap:
                continue
            number = min(hits, key=lambda n: (abs(n - near) if near else 0, n))
            score = (tier, -len(hits), len(key))  # 引號詞優先，接著比少見度與長度
            if best is None or score > best[0]:
                best = (score, (number, float(paras[number - 1].start)))
    if best:
        return best[1]
    # 中文逐字稿裡的英文詞不多，拼錯一個字母（Cosumer Journey Map）時，
    # 靠「同段出現兩個以上不同單字」仍能找到，但要等準確比對都失敗才用。
    words = list(dict.fromkeys(re.findall(r"[A-Za-z]{5,}", text)))
    if len(words) >= 2:
        scored = [
            (sum(1 for w in words if w.lower() in body.lower()), number)
            for number, body in enumerate(bodies, start=1)
        ]
        hits = [(score, number) for score, number in scored if score >= min(2, len(words))]
        if hits:
            top = max(score for score, _ in hits)
            number = min((n for score, n in hits if score == top), key=lambda n: (abs(n - near) if near else 0, n))
            return number, float(paras[number - 1].start)
    return None


def _normalize_open_questions(raw, paras: Sequence[Paragraph]) -> tuple[list[OpenQuestion], int]:
    """把 open_questions 統一成物件，並用段落編號／引號詞句定位到可播放的時間點。

    時間一律取自逐字稿段落，不接受模型自創的秒數，才不会出現跳過去卻是空白的時間戳。
    """
    out: list[OpenQuestion] = []
    timed = 0
    for item in raw or []:
        text: str
        para_no: int | None
        raw_seconds: float | None = None
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("question") or "").strip()
            para_no = _as_int(item.get("para") or item.get("para_start") or item.get("paragraph"))
            raw_seconds = _as_float(item.get("at") if item.get("at") is not None else item.get("start"))
        elif isinstance(item, str):
            text, para_no = item.strip(), None
        else:
            continue
        if not text:
            continue
        question = OpenQuestion(text=text, para=para_no)
        if not paras:
            out.append(question)
            continue
        # 引號裡的原詞是最直接的證據：它指到逐字稿中確實出現的那一句，
        # 比模型回報的段落編號準（map-reduce 模式模型只看到幾份筆記，常整排填同一個編號）。
        hit = _find_terms_in_transcript(text, paras, near=para_no)
        if hit:
            question.para, question.start, question.located = hit[0], hit[1], "text"
        elif para_no is not None:
            question.para = clamp(para_no, 1, len(paras))
            question.start, question.located = float(paras[question.para - 1].start), "para"
        elif raw_seconds is not None:
            question.para, question.start = _snap_to_paragraph(paras, raw_seconds)
            question.located = "para"
        if question.start is not None and paras:
            question.start = round(question.start, 2)
            question.clock = fmt_clock(question.start, force_hours=True)
            timed += 1
        out.append(question)
    return out, timed


def _normalize_quotes(raw) -> list[dict]:
    out: list[dict] = []
    for item in raw or []:
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("quote") or "").strip()
            if text:
                para = _as_int(item.get("para"))
                out.append({"text": text, "speaker": str(item.get("speaker") or "未標明").strip(), "para": para})
        elif isinstance(item, str) and item.strip():
            out.append({"text": item.strip(), "speaker": "未標明", "para": None})
    return out
