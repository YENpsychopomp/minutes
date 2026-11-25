"""逐字稿資料模型、跨 chunk 合併、段落組裝與多格式輸出。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .chunking import ChunkPlan, Plan
from .util import fmt_clock, fmt_srt_time, fmt_vtt_time

_SENT_END = "。！？!?…；;."
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


@dataclass
class Word:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "text": self.text}

    @classmethod
    def from_dict(cls, d: dict) -> "Word":
        return cls(
            start=float(d.get("start", 0.0)),
            end=float(d.get("end", 0.0)),
            text=str(d.get("text") or d.get("word") or ""),
        )


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    #: 來源 chunk index（除錯與重疊去重用）
    chunk: int = -1
    confidence: float | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self, *, with_words: bool = True) -> dict:
        d = {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
            "clock": fmt_clock(self.start),
        }
        if self.chunk >= 0:
            d["chunk"] = self.chunk
        if self.confidence is not None:
            d["confidence"] = round(float(self.confidence), 4)
        if with_words and self.words:
            d["words"] = [w.to_dict() for w in self.words]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Segment":
        words = [Word.from_dict(w) for w in (d.get("words") or [])]
        return cls(
            start=float(d.get("start", 0.0)),
            end=float(d.get("end", 0.0)),
            text=str(d.get("text", "")).strip(),
            words=words,
            chunk=int(d.get("chunk", -1)),
            confidence=d.get("confidence"),
        )


@dataclass
class Transcript:
    duration: float
    language: str | None = None
    segments: list[Segment] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    # -- 基本屬性 --------------------------------------------------------
    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments if s.text).strip()

    def char_count(self) -> int:
        return sum(len(s.text) for s in self.segments)

    def is_cjk(self) -> bool:
        text = self.text[:4000]
        if not text:
            return False
        return sum(1 for ch in text if _CJK.match(ch)) / max(1, len(text)) > 0.25

    def stats(self) -> dict:
        return {
            "duration": round(self.duration, 2),
            "segments": len(self.segments),
            "chars": self.char_count(),
            "language": self.language,
            "cjk": self.is_cjk(),
            **{k: v for k, v in self.meta.items() if k in {"chunks", "chunks_ok", "asr_model", "llm_model"}},
        }

    # -- 序列化 ----------------------------------------------------------
    def to_dict(self, *, with_words: bool = True) -> dict:
        return {
            "schema": "summery.transcript/1",
            "duration": round(self.duration, 3),
            "language": self.language,
            "meta": self.meta,
            "stats": self.stats(),
            "segments": [s.to_dict(with_words=with_words) for s in self.segments],
        }

    def to_json(self, *, indent: int = 2, with_words: bool = True) -> str:
        return json.dumps(self.to_dict(with_words=with_words), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: dict) -> "Transcript":
        return cls(
            duration=float(data.get("duration", 0.0)),
            language=data.get("language"),
            segments=[Segment.from_dict(s) for s in data.get("segments", [])],
            meta=dict(data.get("meta", {})),
        )

    @classmethod
    def from_json(cls, text: str) -> "Transcript":
        return cls.from_dict(json.loads(text))

    # -- 輸出 ------------------------------------------------------------
    def to_text(self, *, paragraph_gap: float = 0.9, paragraph_max_chars: int = 180) -> str:
        return "\n\n".join(p.text for p in self.paragraphs(paragraph_gap, paragraph_max_chars))

    def to_markdown(
        self,
        *,
        title: str = "逐字稿",
        paragraph_gap: float = 0.9,
        paragraph_max_chars: int = 180,
        timestamps: bool = True,
        source: str | None = None,
    ) -> str:
        lines = [f"# {title}", ""]
        is_document = self.meta.get("source_kind") == "document"
        if is_document:
            # 文件輸入沒有時間軸：以段落編號標示（與摘要章節的「段落 a–b」對應）
            names = [str(d.get("name")) for d in self.meta.get("documents") or [] if isinstance(d, dict)]
            info = [f"- 來源文件：{'、'.join(names) or source or '?'}"]
        else:
            info = [f"- 長度：{fmt_clock(self.duration, force_hours=True)}"]
        if self.language:
            info.append(f"- 語言：{self.language}")
        if source and not is_document:
            info.append(f"- 來源：`{source}`")
        chunks = self.meta.get("chunks")
        if chunks:
            info.append(f"- ASR 分塊：{chunks} 塊（模型：{self.meta.get('asr_model', '?')}）")
        lines += info + ["", "## 全文", ""]
        for number, p in enumerate(self.paragraphs(paragraph_gap, paragraph_max_chars), start=1):
            if is_document:
                prefix = f"`[{number}]` "
            else:
                prefix = f"`[{fmt_clock(p.start, force_hours=True)}]` " if timestamps else ""
            lines.append(f"{prefix}{p.text}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def to_srt(self, *, max_chars: int | None = 42) -> str:
        out: list[str] = []
        for i, cue in enumerate(self._cues(max_chars), start=1):
            out.append(str(i))
            out.append(f"{fmt_srt_time(cue.start)} --> {fmt_srt_time(cue.end)}")
            out.append(cue.text)
            out.append("")
        return "\n".join(out)

    def to_vtt(self, *, max_chars: int | None = 42) -> str:
        out = ["WEBVTT", ""]
        for cue in self._cues(max_chars):
            out.append(f"{fmt_vtt_time(cue.start)} --> {fmt_vtt_time(cue.end)}")
            out.append(cue.text)
            out.append("")
        return "\n".join(out)

    def _cues(self, max_chars: int | None) -> list["Paragraph"]:
        cues: list[Paragraph] = []
        for seg in self.segments:
            if not seg.text:
                continue
            if seg.words:
                span = max(0.4, seg.duration)
                n = max(1, (len(seg.text) + (max_chars or 42) - 1) // (max_chars or 42))
                if n > 1:
                    step = span / n
                    size = (len(seg.text) + n - 1) // n
                    for i in range(n):
                        t0 = seg.start + i * step
                        t1 = seg.start + (i + 1) * step
                        cues.append(
                            Paragraph(t0, t1, seg.text[i * size : (i + 1) * size].strip())
                        )
                    continue
            cues.append(Paragraph(seg.start, max(seg.end, seg.start + 0.4), seg.text))
        return [c for c in cues if c.text]

    # -- 段落 ------------------------------------------------------------
    def paragraphs(self, gap: float = 0.9, max_chars: int = 180) -> list["Paragraph"]:
        return build_paragraphs(self.segments, gap=gap, max_chars=max_chars)

    def window(self, start: float, end: float) -> "Transcript":
        segs = [s for s in self.segments if s.end > start and s.start < end]
        return Transcript(duration=end - start, language=self.language, segments=segs, meta=dict(self.meta))


@dataclass
class Paragraph:
    start: float
    end: float
    text: str


def build_paragraphs(segments: Sequence[Segment], *, gap: float = 0.9, max_chars: int = 180) -> list[Paragraph]:
    """依停頓長度與標點組裝段落，讓逐字稿好讀且時間戳可用。"""
    out: list[Paragraph] = []
    buf: list[str] = []
    start: float | None = None
    end: float | None = None
    limit = max(20, int(max_chars))
    hard_limit = max(limit + 20, int(limit * 1.5))  # 沒有停頓也沒有標點時的保底斷點
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        if start is None:
            start, end = seg.start, seg.end
            buf = [text]
            continue
        pause = seg.start - (end or seg.start)
        too_long = sum(len(t) for t in buf) + len(text) > limit
        ends_sentence = bool(buf) and buf[-1].rstrip()[-1:] in _SENT_END if buf[-1].strip() else False
        over_hard = sum(len(t) for t in buf) + len(text) > hard_limit
        if pause >= gap or over_hard or (too_long and (ends_sentence or pause >= gap * 0.35)):
            out.append(Paragraph(start, end or start, _join(buf)))
            start, end, buf = seg.start, seg.end, [text]
        else:
            buf.append(text)
            end = max(end or 0.0, seg.end)
    if start is not None and buf:
        out.append(Paragraph(start, end or start, _join(buf)))
    return out


def _join(parts: Iterable[str]) -> str:
    items = [p.strip() for p in parts if p.strip()]
    if not items:
        return ""
    out = items[0]
    for nxt in items[1:]:
        if _CJK.match(out[-1] or "") or _CJK.match(nxt[0] or ""):
            out += nxt
        else:
            out += " " + nxt
    return out.strip()


# --------------------------------------------------------------------------
# ASR 回應解析與跨 chunk 合併
# --------------------------------------------------------------------------


class ASRPayloadError(ValueError):
    pass


def parse_asr_payload(payload: Any) -> tuple[list[Segment], str | None]:
    """把 ASR 回應轉成 segments（時間為 chunk 內相對時間）。

    支援：verbose_json（segments/words）、json（{"text"}）、純字串。
    """
    if payload is None:
        raise ASRPayloadError("ASR 回應為空")
    if isinstance(payload, str):
        return ([Segment(0.0, 0.0, payload.strip())] if payload.strip() else []), None
    if isinstance(payload, list):  # 某些服務直接回 segments list
        return _segments_from_list(payload), None
    if not isinstance(payload, dict):
        raise ASRPayloadError(f"無法解析的 ASR 回應型別：{type(payload).__name__}")

    language = payload.get("language")
    raw_segments = payload.get("segments") or payload.get("chunks") or payload.get("result")
    if isinstance(raw_segments, list) and raw_segments:
        return _segments_from_list(raw_segments), language
    text = payload.get("text")
    if isinstance(text, str):
        return ([Segment(0.0, 0.0, text.strip())] if text.strip() else []), language
    if isinstance(text, list):  # 少數服務 text 為 segment 列表
        return _segments_from_list(text), language
    raise ASRPayloadError(f"ASR 回應缺少 text/segments：{str(payload)[:200]}")


def _segments_from_list(items: Sequence[Any]) -> list[Segment]:
    segs: list[Segment] = []
    for item in items:
        if isinstance(item, str):
            if item.strip():
                segs.append(Segment(0.0, 0.0, item.strip()))
            continue
        if not isinstance(item, dict):
            continue
        text = item.get("text") or item.get("content") or ""
        text = str(text).strip()
        if not text:
            continue
        try:
            start = float(item.get("start", item.get("start_time", 0.0)) or 0.0)
            end = float(item.get("end", item.get("end_time", 0.0)) or 0.0)
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        words: list[Word] = []
        for w in item.get("words") or []:
            if isinstance(w, dict):
                try:
                    words.append(
                        Word(
                            start=float(w.get("start", 0.0) or 0.0),
                            end=float(w.get("end", 0.0) or 0.0),
                            text=str(w.get("word") or w.get("text") or ""),
                        )
                    )
                except (TypeError, ValueError):
                    continue
        conf = None
        for key in ("confidence", "probability", "avg_logprob"):
            if isinstance(item.get(key), (int, float)):
                conf = float(item[key])
                break
        segs.append(Segment(start=start, end=end if end >= start else start, text=text, words=words, confidence=conf))
    return segs


def merge_chunk_results(
    results: Sequence[tuple[ChunkPlan, Sequence[Segment]]],
    *,
    duration: float,
    overlap_seconds: float = 0.0,
    language: str | None = None,
) -> Transcript:
    """把各 chunk 的相對時間 segments 合併成全域逐字稿。

    * 依 chunk.start 位移時間戳。
    * 重疊區（前塊尾 = 後塊頭）以時間戳去重：完全落在重疊區內的 segment 丟棄，
      跨越邊界的 segment 保留並把起點裁剪到邊界。
    * 沒有時間戳的回應（只有整段 text）會被均匀分配到該 chunk 的時間範圍。
    """
    ordered = sorted(results, key=lambda rs: rs[0].start)
    merged: list[Segment] = []
    dropped = 0
    for chunk, segments in ordered:
        span = max(0.0, chunk.read_duration)
        segs = [s for s in segments if s.text.strip()]
        if not segs:
            continue
        untime = all(s.end <= s.start + 1e-6 for s in segs)
        if untime:
            step = span / len(segs)
            for i, s in enumerate(segs):
                s.start = i * step
                s.end = (i + 1) * step
        # 回溯重疊：本塊多讀了 [read_start, start)，這段內容由前一塊負責
        head = chunk.start - chunk.read_start
        shift = chunk.read_start
        for s in segs:
            abs_start, abs_end = s.start + shift, s.end + shift
            if head > 0.01 and chunk.start > 0.0 and abs_end <= chunk.start + 0.05:
                dropped += 1
                continue
            if head > 0.01 and chunk.start > 0.0 and abs_start < chunk.start:
                abs_start = chunk.start
            seg = Segment(
                start=round(abs_start, 3),
                end=round(max(abs_start, abs_end), 3),
                text=" ".join(s.text.split()),
                words=[
                    Word(w.start + shift, w.end + shift, w.text)
                    for w in s.words
                    if not (head > 0.01 and w.end + shift <= chunk.start + 0.05)
                ],
                chunk=chunk.index,
                confidence=s.confidence,
            )
            merged.append(seg)
    merged.sort(key=lambda s: (s.start, s.end))
    merged, boundary_deduped = _boundary_dedupe(merged, [c for c, _ in ordered], overlap_seconds)
    merged = _tidy(merged, duration)
    return Transcript(
        duration=duration,
        language=language,
        segments=merged,
        meta={
            "chunks": len(ordered),
            "overlap_dropped_segments": dropped,
            "boundary_deduped_segments": boundary_deduped,
            "overlap_seconds": overlap_seconds,
        },
    )


_PUNCT = re.compile(r"[\s\u3000\.,!?;:\u0022\u0027、。，！？；：「」『』（）()\[\]{}<>《》‘’“”~…—\-·*]+")


def _norm_text(text: str) -> str:
    return _PUNCT.sub("", text or "").lower()


def _boundary_dedupe(
    segments: list[Segment], chunks: Sequence[ChunkPlan], overlap_seconds: float
) -> tuple[list[Segment], int]:
    """處理跨 chunk 邊界的重複：相鄰、時間相接、文字互相包含 → 留較完整的。

    只在「跨越同一個 chunk 邊界」的相鄰兩段之間生效，避免誤刪重複口頭禪。
    """
    if not segments or len(chunks) < 2:
        return segments, 0
    ends = {c.index: c.end for c in chunks}
    window = max(1.0, overlap_seconds) + 4.0
    keep: list[Segment] = []
    removed = 0
    for seg in segments:
        if keep:
            prev = keep[-1]
            crosses = (
                prev.chunk >= 0
                and seg.chunk >= 0
                and seg.chunk == prev.chunk + 1
                and prev.chunk in ends
                and prev.end >= ends[prev.chunk] - window
            ) or (prev.chunk != seg.chunk and seg.start - prev.end <= 1.5)
            if crosses and seg.start - prev.end <= 1.5:
                a, b = _norm_text(prev.text), _norm_text(seg.text)
                if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
                    if len(b) >= len(a):
                        keep[-1] = Segment(
                            start=prev.start,
                            end=max(prev.end, seg.end),
                            text=seg.text,
                            words=seg.words or prev.words,
                            chunk=seg.chunk,
                            confidence=seg.confidence if seg.confidence is not None else prev.confidence,
                        )
                    else:
                        keep[-1] = Segment(
                            start=prev.start,
                            end=max(prev.end, seg.end),
                            text=prev.text,
                            words=prev.words or seg.words,
                            chunk=prev.chunk,
                            confidence=prev.confidence,
                        )
                    removed += 1
                    continue
        keep.append(seg)
    return keep, removed


def _tidy(segments: list[Segment], duration: float, *, max_overlap: float = 0.25) -> list[Segment]:
    """修剪越界與重疊，確保時間戳單調可用（SRT/VTT 需要）。"""
    out: list[Segment] = []
    prev_end = 0.0
    for seg in segments:
        start = max(0.0, seg.start)
        end = seg.end if seg.end > start else start + min(1.5, max(0.4, len(seg.text) * 0.12))
        if duration and end > duration:
            end = duration
        if start > end:
            start = max(0.0, end - 0.4)
        if out and start < prev_end - max_overlap:
            # 前一块已涵蓋多數時間：把起點往後推，避免字幕重疊
            start = prev_end
            if end <= start:
                if duration and start >= duration:
                    continue
                end = start + 0.4
        seg = Segment(start=round(start, 3), end=round(end, 3), text=seg.text, words=seg.words, chunk=seg.chunk, confidence=seg.confidence)
        out.append(seg)
        prev_end = max(prev_end, seg.end)
    return out


def transcript_from_text(text: str, duration: float, *, language: str | None = None) -> Transcript:
    """從純文字逐字稿建立 Transcript（依標點近似分配時間戳）。"""
    sentences = [s.strip() for s in re.split(r"(?<=[。！？!?；;\n])", text) if s.strip()]
    if not sentences:
        return Transcript(duration=duration, language=language, segments=[], meta={"source": "text"})
    weights = [max(1, len(s)) for s in sentences]
    total = sum(weights) or 1
    segs: list[Segment] = []
    cursor = 0.0
    for sent, w in zip(sentences, weights):
        span = duration * w / total
        segs.append(Segment(start=round(cursor, 3), end=round(min(duration, cursor + span), 3), text=sent))
        cursor += span
    return Transcript(duration=duration, language=language, segments=segs, meta={"source": "text"})
