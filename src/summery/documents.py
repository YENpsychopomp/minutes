"""文件輸入：把 .txt／.md／.docx／.pdf／.srt／.vtt／.json 轉成可摘要的文字或逐字稿。

設計重點：
* 執行期零第三方相依：.docx 以 ``zipfile`` + ``xml.etree`` 自行解析（段落、標題層級、
  Word 自動編號、表格），保留範本最在意的「段落結構」。
* PDF 交給 Microsoft 的開源工具 markitdown：同一個 Python 環境有裝就直接 import；
  否則依序找 ``documents.markitdown_cmd``、PATH 上的 ``markitdown``、conda 環境裡的
  ``markitdown``（例如另外建的 ``conda create -n markitdown``）。都沒有就清楚告知替代做法。
* 文件沒有時間軸：每個段落以「段落編號」為錨點。轉成 :class:`Transcript` 時用等距的
  虛擬時間排序，並在 meta 標記 ``source_kind = "document"``，輸出時顯示段落編號而非時間。
"""

from __future__ import annotations

import codecs
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence
from xml.etree import ElementTree as ET

from .transcript import Segment, Transcript
from .util import LOG

#: 文件類輸入（相對於影音）
TEXT_EXT = {".txt", ".md", ".markdown"}
DOCX_EXT = {".docx"}
PDF_EXT = {".pdf"}
SUBTITLE_EXT = {".srt", ".vtt"}
JSON_EXT = {".json"}
DOCUMENT_EXT = TEXT_EXT | DOCX_EXT | PDF_EXT | SUBTITLE_EXT | JSON_EXT

#: 常見但無法直接讀的格式 → 給出具體的轉檔建議
_UNSUPPORTED_HINTS = {
    ".doc": "舊版 Word（.doc）無法直接讀取，請用 Word「另存新檔」成 .docx",
    ".pages": "Pages 文件請先匯出成 .docx 或 .pdf",
    ".odt": "請先另存成 .docx",
    ".rtf": "請先另存成 .docx 或 .txt",
}

KIND_LABEL = {
    "text": "純文字",
    "markdown": "Markdown",
    "docx": "Word 文件",
    "pdf": "PDF",
    "subtitle": "字幕（含時間軸）",
    "transcript": "summery 逐字稿",
    "summary": "summery 摘要",
}

#: 文件段落的虛擬時間間隔：大於任何合理的 paragraph_gap，確保一段就是一個編號段落
DOC_STEP_SECONDS = 10.0
_MAX_XML_BYTES = 64 * 1024 * 1024  # 解壓後的 document.xml 上限（防壓縮炸彈）


class DocumentError(ValueError):
    """文件無法讀取或格式不支援（訊息直接給使用者看）。"""


@dataclass
class Document:
    name: str
    path: str
    kind: str
    text: str
    paragraphs: list[str] = field(default_factory=list)
    #: 有真實時間軸時（srt／vtt／summery 逐字稿 JSON）
    transcript: Transcript | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(p) for p in self.paragraphs)

    @property
    def timed(self) -> bool:
        return self.transcript is not None

    def info(self) -> dict:
        preview = "\n".join(self.paragraphs[:6])
        return {
            "name": self.name,
            "kind": self.kind,
            "kind_label": KIND_LABEL.get(self.kind, self.kind),
            "chars": self.chars,
            "paragraphs": len(self.paragraphs),
            "timed": self.timed,
            "preview": preview[:400],
            "warnings": list(self.warnings),
        }


def is_document(path: str | Path) -> bool:
    return Path(path).suffix.lower() in DOCUMENT_EXT


def unsupported_hint(path: str | Path) -> str | None:
    return _UNSUPPORTED_HINTS.get(Path(path).suffix.lower())


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def load_document(
    path: str | Path, *, name: str | None = None, markitdown_cmd: str | None = None,
    markitdown_timeout: float = 300.0, paragraph_max_chars: int = 500,
) -> Document:
    """讀取一份文件；失敗時拋出訊息可直接顯示的 :class:`DocumentError`。"""
    p = Path(path).expanduser()
    display = name or p.name
    ext = p.suffix.lower()
    if not p.is_file():
        raise DocumentError(f"找不到文件：{p}")
    if ext in _UNSUPPORTED_HINTS:
        raise DocumentError(f"{display}：{_UNSUPPORTED_HINTS[ext]}")
    if ext not in DOCUMENT_EXT:
        raise DocumentError(f"{display}：不支援的文件格式 {ext or '(無副檔名)'}")
    warnings: list[str] = []
    transcript: Transcript | None = None
    if ext in TEXT_EXT:
        kind = "markdown" if ext in (".md", ".markdown") else "text"
        text = decode_text(p.read_bytes())
    elif ext in DOCX_EXT:
        kind, text = "docx", docx_to_markdown(p)
    elif ext in PDF_EXT:
        kind, text = "pdf", pdf_to_markdown(p, markitdown_cmd=markitdown_cmd, timeout=markitdown_timeout)
    elif ext in SUBTITLE_EXT:
        kind = "subtitle"
        segments = parse_subtitles(decode_text(p.read_bytes()))
        if not segments:
            raise DocumentError(f"{display}：字幕檔裡沒有可用的字幕段落")
        transcript = Transcript(
            duration=max(s.end for s in segments), segments=segments,
            meta={"source": display, "source_kind": "subtitle"},
        )
        text = "\n".join(s.text for s in segments)
    else:  # .json
        kind, text, transcript = _load_json(p, display)

    text = normalize_cjk(text)
    if transcript is not None:
        for seg in transcript.segments:
            seg.text = normalize_cjk(seg.text)
        paragraphs = [para.text for para in transcript.paragraphs() if para.text.strip()]
    else:
        paragraphs = split_paragraphs(text, max_chars=paragraph_max_chars)
    if not paragraphs:
        if kind == "pdf":
            raise DocumentError(
                f"{display}：PDF 裡沒有可擷取的文字（多半是掃描影像）。請先做 OCR，或另存成 .docx／.txt 再上傳"
            )
        raise DocumentError(f"{display}：文件是空的，沒有可用的文字")
    if kind == "pdf" and sum(len(x) for x in paragraphs) < 40:
        warnings.append("PDF 擷取到的文字很少，可能是掃描影像；結果可能不完整")
    return Document(name=display, path=str(p), kind=kind, text=text, paragraphs=paragraphs,
                    transcript=transcript, warnings=warnings)


def load_documents(paths: Iterable[str | Path], **kwargs) -> list[Document]:
    return [load_document(path, **kwargs) for path in paths]


# --------------------------------------------------------------------------
# 純文字
# --------------------------------------------------------------------------


def decode_text(data: bytes) -> str:
    """UTF-8（含 BOM）→ UTF-16（有 BOM）→ cp950／Big5 → GB18030 → 以 UTF-8 取代錯字。"""
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8):].decode("utf-8", "replace")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for encoding in ("cp950", "big5hkscs", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


_COMPAT_TABLE: dict[int, str] | None = None


def normalize_cjk(text: str) -> str:
    """\u628a PDF \u5e38\u898b\u7684\u300c\u5eb7\u7199\u90e8\u9996\uff0f\u76f8\u5bb9\u6f22\u5b57\u300d\u63db\u56de\u4e00\u822c\u6f22\u5b57\uff08\u2f2f\u2192\u5de5\u3001\u2f49\u2192\u6708\u3001\u2f24\u2192\u5927\u3001\u2f00\u2192\u4e00\uff09\u3002

    \u5f88\u591a\u4e2d\u6587 PDF \u7684\u5b57\u578b\u628a\u5b57\u5f62\u5c0d\u61c9\u5230\u90e8\u9996\u5340\u78bc\u4f4d\uff0c\u770b\u8d77\u4f86\u4e00\u6a23\u3001\u6bd4\u5c0d\u537b\u5931\u6557\uff08\u4eba\u540d\u3001\u300c\u4e00\u3001\u300d\u7de8\u865f
    \u90fd\u8a8d\u4e0d\u51fa\u4f86\uff09\u3002\u53ea\u8f49\u9019\u5169\u500b\u5340\u6bb5\uff0c\u4e0d\u505a\u6574\u9ad4 NFKC\uff0c\u4ee5\u514d\u5168\u5f62\u6a19\u9ede\u300c\uff1a\uff08\uff0c\u300d\u88ab\u6539\u6210\u534a\u5f62\u3002
    """
    global _COMPAT_TABLE
    if _COMPAT_TABLE is None:
        import unicodedata

        table: dict[int, str] = {}
        for code in list(range(0x2F00, 0x2FE0)) + list(range(0x2E80, 0x2F00)) + list(range(0xF900, 0xFB00)):
            mapped = unicodedata.normalize("NFKC", chr(code))
            if mapped != chr(code) and len(mapped) == 1:
                table[code] = mapped
        _COMPAT_TABLE = table
    return text.translate(_COMPAT_TABLE) if text else text


_CJK = re.compile(r"[\u3000-\u303f\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]")
_HEADING = re.compile(r"^\s*#{1,6}\s+\S")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_LIST_START = re.compile(
    r"^\s*(?:[-*+•●○■□◆◇▪]\s+|\d+[.)、．]\s*|[（(]?[一二三四五六七八九十百]+[)）、．.]\s*|[（(]\d+[)）]\s*|[a-zA-Z][.)]\s+)"
)
_SENT_END = "。！？!?；;:：」』）)】.…"
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])")


def _width(text: str) -> int:
    return sum(2 if _CJK.match(ch) else 1 for ch in text)


def _join_wrapped(left: str, right: str) -> str:
    if not left:
        return right
    if _CJK.match(left[-1]) or _CJK.match(right[:1] or " "):
        return left + right
    if left.endswith("-") and right[:1].islower():
        return left[:-1] + right
    return left + " " + right


def split_paragraphs(text: str, *, max_chars: int = 500) -> list[str]:
    """把文件文字切成編號段落。

    * 空行、標題、表格列、清單項目一定斷開；
    * PDF 這類「硬換行」的長行（且不以句號結尾）會接回同一段；
      較短的行（例如「出席：王小明」）視為刻意換行，不會被合併；
    * 單段過長時依句子切成約 ``max_chars`` 字的小段，章節定位才夠細。
    """
    out: list[str] = []
    buf = ""
    last_width = 0

    def flush() -> None:
        nonlocal buf, last_width
        if buf.strip():
            out.extend(_split_long(buf.strip(), max_chars))
        buf, last_width = "", 0

    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line or _TABLE_RULE.match(line):
            flush()
            continue
        if _HEADING.match(line) or _TABLE_ROW.match(line):
            flush()
            out.append(line)
            continue
        if _LIST_START.match(line) or not buf:
            flush()
            buf, last_width = line, _width(line)
            continue
        wrapped = last_width >= 56 and buf[-1] not in _SENT_END
        if wrapped:
            buf = _join_wrapped(buf, line)
            last_width = _width(line)
        else:
            flush()
            buf, last_width = line, _width(line)
    flush()
    return [p for p in out if p.strip()]


def _split_long(text: str, max_chars: int) -> list[str]:
    limit = max(80, int(max_chars or 500))
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    buf = ""
    for sentence in (s for s in _SENT_SPLIT.split(text) if s):
        if buf and len(buf) + len(sentence) > limit:
            pieces.append(buf.strip())
            buf = ""
        while len(sentence) > limit * 1.5:  # 整句都沒有標點：硬切
            pieces.append(sentence[:limit].strip())
            sentence = sentence[limit:]
        buf += sentence
    if buf.strip():
        pieces.append(buf.strip())
    return [p for p in pieces if p]


# --------------------------------------------------------------------------
# .docx（zipfile + xml）
# --------------------------------------------------------------------------

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _w(tag: str) -> str:
    return f"{{{_W}}}{tag}"


def _wval(el: ET.Element | None, attr: str = "val") -> str | None:
    if el is None:
        return None
    return el.get(_w(attr))


def _read_xml(zf: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > _MAX_XML_BYTES:
        raise DocumentError(f"Word 文件內容過大（{name} 解壓後 {info.file_size // 1_000_000}MB）")
    data = zf.read(info)
    if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data[:4096]:
        raise DocumentError("Word 文件含不允許的 XML 宣告")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise DocumentError(f"Word 文件的 {name} 損毀：{exc}") from exc


def _heading_styles(styles: ET.Element | None) -> dict[str, int]:
    """styleId → 標題層級。中文版 Word 的標題樣式 ID 常是「1」「2」，要看樣式名稱。"""
    levels: dict[str, int] = {}
    if styles is None:
        return levels
    for style in styles.iter(_w("style")):
        sid = style.get(_w("styleId")) or ""
        name = (_wval(style.find(_w("name"))) or "").strip().lower()
        level = None
        match = re.match(r"heading\s*(\d)", name) or re.match(r"heading(\d)", sid.lower())
        if match:
            level = int(match.group(1))
        elif name == "title" or sid.lower() == "title":
            level = 1
        else:
            outline = style.find(f"{_w('pPr')}/{_w('outlineLvl')}")
            if outline is not None and (_wval(outline) or "").isdigit() and int(_wval(outline) or 9) < 6:
                level = int(_wval(outline) or 0) + 1
        if level:
            levels[sid] = max(1, min(6, level))
    return levels


@dataclass
class _Level:
    fmt: str = "decimal"
    text: str = "%1."
    start: int = 1


def _numbering(numbering: ET.Element | None) -> dict[str, dict[int, _Level]]:
    """numId → {ilvl: 格式}（Word 的自動編號，例如「一、」「(一)」「1.」）。"""
    if numbering is None:
        return {}
    abstract: dict[str, dict[int, _Level]] = {}
    for node in numbering.iter(_w("abstractNum")):
        levels: dict[int, _Level] = {}
        for lvl in node.iter(_w("lvl")):
            try:
                ilvl = int(lvl.get(_w("ilvl")) or 0)
            except ValueError:
                continue
            start = _wval(lvl.find(_w("start")))
            levels[ilvl] = _Level(
                fmt=_wval(lvl.find(_w("numFmt"))) or "decimal",
                text=_wval(lvl.find(_w("lvlText"))) if lvl.find(_w("lvlText")) is not None else "%1.",
                start=int(start) if start and start.isdigit() else 1,
            )
        abstract[node.get(_w("abstractNumId")) or ""] = levels
    out: dict[str, dict[int, _Level]] = {}
    for num in numbering.iter(_w("num")):
        aid = _wval(num.find(_w("abstractNumId"))) or ""
        out[num.get(_w("numId")) or ""] = abstract.get(aid, {})
    return out


_CN_DIGITS = "零一二三四五六七八九"
_TIANGAN = "甲乙丙丁戊己庚辛壬癸"


def _cn_number(n: int) -> str:
    if n <= 0:
        return str(n)
    if n < 10:
        return _CN_DIGITS[n]
    if n < 20:
        return "十" + (_CN_DIGITS[n - 10] if n > 10 else "")
    if n < 100:
        return _CN_DIGITS[n // 10] + "十" + (_CN_DIGITS[n % 10] if n % 10 else "")
    return str(n)


def _roman(n: int) -> str:
    vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
            (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = ""
    for value, sym in vals:
        while n >= value:
            out += sym
            n -= value
    return out


def _format_number(n: int, fmt: str) -> str:
    fmt = fmt or "decimal"
    if fmt in ("decimal", "decimalZero", "decimalFullWidth", "decimalHalfWidth", "decimalEnclosedParen"):
        return f"{n:02d}" if fmt == "decimalZero" else str(n)
    if fmt.startswith(("taiwaneseCounting", "chineseCounting", "ideographTraditional", "chineseLegalSimplified")):
        if fmt == "ideographTraditional":
            return _TIANGAN[(n - 1) % 10]
        return _cn_number(n)
    if fmt == "upperLetter":
        return chr(ord("A") + (n - 1) % 26)
    if fmt == "lowerLetter":
        return chr(ord("a") + (n - 1) % 26)
    if fmt == "upperRoman":
        return _roman(n)
    if fmt == "lowerRoman":
        return _roman(n).lower()
    return str(n)


class _Numberer:
    def __init__(self, numbering: dict[str, dict[int, _Level]]):
        self.numbering = numbering
        self.counters: dict[str, dict[int, int]] = {}

    def label(self, num_id: str, ilvl: int) -> str:
        levels = self.numbering.get(num_id) or {}
        level = levels.get(ilvl, _Level())
        if level.fmt == "bullet" or level.fmt == "none":
            return "-" if level.fmt == "bullet" else ""
        counters = self.counters.setdefault(num_id, {})
        counters[ilvl] = counters.get(ilvl, level.start - 1) + 1
        for deeper in [k for k in counters if k > ilvl]:
            del counters[deeper]  # 上層前進時，下層重新起算
        text = level.text or "%1."

        def repl(match: re.Match) -> str:
            k = int(match.group(1)) - 1
            lv = levels.get(k, _Level())
            return _format_number(counters.get(k, lv.start), lv.fmt)

        return re.sub(r"%(\d)", repl, text).strip()


def _paragraph_text(p: ET.Element) -> str:
    parts: list[str] = []
    for el in p.iter():
        tag = el.tag
        if tag == _w("t"):
            parts.append(el.text or "")
        elif tag == _w("tab"):
            parts.append("\t")
        elif tag in (_w("br"), _w("cr")):
            parts.append("\n")
        elif tag == _w("noBreakHyphen"):
            parts.append("-")
    return "".join(parts).replace("\u00a0", " ")


def _cell_text(tc: ET.Element) -> str:
    texts = [_paragraph_text(p).strip() for p in tc.iter(_w("p"))]
    return " ".join(t.replace("\n", " ") for t in texts if t).replace("|", "／")


def docx_to_markdown(path: str | Path) -> str:
    """把 .docx 轉成 Markdown：標題 → #、自動編號 → 原樣的「一、」「1.」、表格 → | 表格 |。"""
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocumentError(f"{Path(path).name}：不是有效的 .docx（{exc}）") from exc
    with zf:
        document = _read_xml(zf, "word/document.xml")
        if document is None:
            raise DocumentError(f"{Path(path).name}：.docx 裡找不到 word/document.xml")
        headings = _heading_styles(_read_xml(zf, "word/styles.xml"))
        numberer = _Numberer(_numbering(_read_xml(zf, "word/numbering.xml")))
    body = document.find(_w("body"))
    if body is None:
        return ""
    blocks: list[str] = []

    def walk(container: ET.Element) -> None:
        for el in container:
            if el.tag == _w("p"):
                blocks.append(_render_paragraph(el, headings, numberer))
            elif el.tag == _w("tbl"):
                blocks.append(_render_table(el))
            elif el.tag in (_w("sdt"), _w("customXml")):
                content = el.find(_w("sdtContent"))
                walk(content if content is not None else el)

    walk(body)
    return "\n\n".join(b for b in blocks if b.strip())


def _render_paragraph(p: ET.Element, headings: dict[str, int], numberer: _Numberer) -> str:
    text = _paragraph_text(p).strip()
    if not text:
        return ""
    ppr = p.find(_w("pPr"))
    style = _wval(ppr.find(_w("pStyle"))) if ppr is not None else None
    level = headings.get(style or "")
    if level is None and ppr is not None:
        outline = _wval(ppr.find(_w("outlineLvl")))
        if outline and outline.isdigit() and int(outline) < 6:
            level = int(outline) + 1
    prefix = ""
    num_pr = ppr.find(_w("numPr")) if ppr is not None else None
    if num_pr is not None:
        num_id = _wval(num_pr.find(_w("numId"))) or ""
        ilvl_raw = _wval(num_pr.find(_w("ilvl"))) or "0"
        ilvl = int(ilvl_raw) if ilvl_raw.isdigit() else 0
        if num_id and num_id != "0":
            label = numberer.label(num_id, ilvl)
            prefix = ("  " * ilvl + label + " ") if label else ""
    if level:
        return "#" * level + " " + (prefix.strip() + " " if prefix else "") + text.replace("\n", " ")
    return prefix + text


def _render_table(tbl: ET.Element) -> str:
    rows: list[list[str]] = []
    for tr in tbl.findall(_w("tr")):
        cells = [_cell_text(tc) for tc in tr.findall(_w("tc"))]
        if any(c.strip() for c in cells):
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    lines = []
    for i, row in enumerate(rows):
        row = row + [""] * (width - len(row))
        lines.append("| " + " | ".join(row) + " |")
        if i == 0:
            lines.append("| " + " | ".join(["---"] * width) + " |")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# PDF（markitdown）
# --------------------------------------------------------------------------


def _conda_roots() -> list[Path]:
    roots: list[Path] = []
    for var in ("CONDA_EXE", "CONDA_PYTHON_EXE"):
        value = os.environ.get(var)
        if value:
            exe = Path(value)
            roots.append(exe.parent.parent if exe.parent.name.lower() in ("scripts", "bin", "condabin") else exe.parent)
    prefix = os.environ.get("CONDA_PREFIX")
    if prefix:
        pp = Path(prefix)
        roots.append(pp.parent.parent if pp.parent.name == "envs" else pp)
    home = Path.home()
    for name in ("anaconda3", "miniconda3", "miniforge3", "mambaforge", "Anaconda3", "Miniconda3"):
        roots.append(home / name)
        roots.append(Path("C:/ProgramData") / name)
    seen: list[Path] = []
    for root in roots:
        if root not in seen and root.is_dir():
            seen.append(root)
    return seen


def _bin_candidates(env: Path) -> list[Path]:
    return [env / "Scripts" / "markitdown.exe", env / "Scripts" / "markitdown", env / "bin" / "markitdown"]


def _split_command(command: str) -> list[str]:
    """``documents.markitdown_cmd`` 可以是路徑，也可以是整串指令（例：conda run -n markitdown markitdown）。"""
    raw = command.strip()
    if Path(raw).expanduser().is_file():
        return [str(Path(raw).expanduser())]
    if os.name == "nt":  # Windows 路徑裡的反斜線不能被當成跳脫字元
        tokens = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] == '"' else t for t in shlex.split(raw, posix=False)]
    else:
        tokens = shlex.split(raw)
    return [t for t in tokens if t]


def find_markitdown(explicit: str | None = None) -> list[str] | None:
    """找 markitdown 指令；回傳 argv 前綴（不含輸入檔）或 None。"""
    if explicit:
        argv = _split_command(explicit)
        head = argv[0] if argv else ""
        found = shutil.which(head) or (head if head and Path(head).expanduser().is_file() else None)
        if not found:
            raise DocumentError(f"documents.markitdown_cmd 指向的指令不存在：{explicit}")
        return [str(Path(found).expanduser()), *argv[1:]]
    found = shutil.which("markitdown")
    if found:
        return [found]
    for root in _conda_roots():
        envs = [root] + sorted((root / "envs").glob("*")) if (root / "envs").is_dir() else [root]
        # 名稱裡有 markitdown 的環境優先
        envs.sort(key=lambda e: 0 if "markitdown" in e.name.lower() else 1)
        for env in envs:
            for candidate in _bin_candidates(env):
                if candidate.is_file():
                    return [str(candidate)]
    return None


def markitdown_status(explicit: str | None = None) -> dict:
    """給 Web UI／CLI 顯示 PDF 是否可用。"""
    if importlib.util.find_spec("markitdown") is not None and not explicit:
        return {"available": True, "via": "python"}
    try:
        cmd = find_markitdown(explicit)
    except DocumentError as exc:
        return {"available": False, "via": None, "error": str(exc)}
    return {"available": bool(cmd), "via": cmd[0] if cmd else None}


_MARKITDOWN_MISSING = (
    "PDF 需要 Microsoft 的 markitdown 才能轉成文字：可在同一個環境執行 "
    "pip install \"markitdown[pdf]\"，或把設定 documents.markitdown_cmd（環境變數 "
    "SUMMERY_DOCUMENTS_MARKITDOWN_CMD）指向 markitdown 指令（例如 conda 環境裡的 "
    "Scripts\\markitdown.exe）；也可以把 PDF 另存成 .docx／.txt 再上傳"
)


def pdf_to_markdown(path: str | Path, *, markitdown_cmd: str | None = None, timeout: float = 300.0) -> str:
    src = Path(path)
    if not markitdown_cmd and importlib.util.find_spec("markitdown") is not None:
        try:
            from markitdown import MarkItDown  # type: ignore[import-not-found]

            result = MarkItDown().convert(str(src))
            return str(getattr(result, "markdown", None) or getattr(result, "text_content", "") or "")
        except Exception as exc:  # noqa: BLE001 - 第三方套件的錯誤型別不可控
            raise DocumentError(f"{src.name}：markitdown 轉換 PDF 失敗：{exc}") from exc
    cmd = find_markitdown(markitdown_cmd)
    if not cmd:
        raise DocumentError(f"{src.name}：{_MARKITDOWN_MISSING}")
    with tempfile.TemporaryDirectory(prefix="summery-md-") as tmp:
        out = Path(tmp) / "out.md"
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        LOG.info("以 markitdown 轉換 PDF：%s", src.name)
        try:
            res = subprocess.run(
                [*cmd, str(src), "-o", str(out)], capture_output=True, timeout=timeout, env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise DocumentError(f"{src.name}：markitdown 超過 {timeout:.0f} 秒未完成") from exc
        except OSError as exc:
            raise DocumentError(f"{src.name}：無法執行 markitdown（{cmd[0]}）：{exc}") from exc
        if res.returncode != 0 or not out.is_file():
            err = decode_text(res.stderr or b"").strip().splitlines()
            tail = err[-1] if err else f"exit {res.returncode}"
            raise DocumentError(f"{src.name}：markitdown 轉換 PDF 失敗：{tail[:300]}")
        return out.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# 字幕／JSON
# --------------------------------------------------------------------------

_TS = r"(?:\d+:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?"
_CUE = re.compile(rf"^\s*({_TS})\s*-->\s*({_TS})")
_TAG = re.compile(r"<[^>]+>|\{\\[^}]*\}")


def _ts_seconds(value: str) -> float:
    value = value.replace(",", ".")
    parts = value.split(":")
    seconds = float(parts[-1])
    minutes = int(parts[-2]) if len(parts) >= 2 else 0
    hours = int(parts[-3]) if len(parts) >= 3 else 0
    return hours * 3600 + minutes * 60 + seconds


def parse_subtitles(text: str) -> list[Segment]:
    """解析 SRT／WebVTT（忽略序號、樣式標籤與 NOTE／STYLE 區塊）。"""
    segments: list[Segment] = []
    blocks = re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        for i, line in enumerate(lines):
            match = _CUE.match(line)
            if not match:
                continue
            body = " ".join(_TAG.sub("", t).strip() for t in lines[i + 1:] if t.strip())
            body = " ".join(body.split())
            if body:
                start, end = _ts_seconds(match.group(1)), _ts_seconds(match.group(2))
                segments.append(Segment(start=start, end=max(start, end), text=body))
            break
    segments.sort(key=lambda s: (s.start, s.end))
    return segments


def _load_json(path: Path, display: str) -> tuple[str, str, Transcript | None]:
    try:
        data = json.loads(decode_text(path.read_bytes()))
    except json.JSONDecodeError as exc:
        raise DocumentError(f"{display}：JSON 格式錯誤：{exc}") from exc
    if isinstance(data, dict) and isinstance(data.get("segments"), list):
        transcript = Transcript.from_dict(data)
        transcript.meta.setdefault("source", display)
        transcript.meta["source_kind"] = "transcript"
        return "transcript", "\n".join(s.text for s in transcript.segments), transcript
    if isinstance(data, dict) and str(data.get("schema", "")).startswith("summery.summary"):
        return "summary", summary_json_to_markdown(data), None
    raise DocumentError(f"{display}：看不懂這個 JSON（支援 summery 的 transcript.json 或 summary.json）")


def summary_json_to_markdown(data: dict) -> str:
    """把先前產出的 summary.json 攤平成可讀文字（拿來當範本或輸入都可以）。"""
    lines = [f"# {data.get('title') or '會議紀錄'}"]
    if data.get("minutes_markdown"):
        return str(data["minutes_markdown"])
    if data.get("tldr"):
        lines += ["", str(data["tldr"])]
    if data.get("overview"):
        lines += ["", "## 內容概要", *[str(x) for x in data["overview"]]]
    for chapter in data.get("chapters") or []:
        if isinstance(chapter, dict):
            lines += ["", f"### {chapter.get('title', '')}", str(chapter.get("summary") or "")]
            lines += [f"- {p}" for p in chapter.get("points") or []]
    if data.get("decisions"):
        lines += ["", "## 決議", *[f"- {d}" for d in data["decisions"]]]
    if data.get("action_items"):
        lines += ["", "## 待辦事項"]
        for item in data["action_items"]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('task')}（負責人：{item.get('owner', '未標明')}／期限：{item.get('due', '未標明')}）")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 文件 → Transcript（給摘要器）
# --------------------------------------------------------------------------


def documents_to_transcript(docs: Sequence[Document], *, language: str | None = None) -> Transcript:
    """把一或多份文件組成摘要器吃的 Transcript。

    單一份有時間軸的輸入（srt／vtt／逐字稿 JSON）直接沿用真實時間；
    其餘情況以「段落編號」為錨點，用等距虛擬時間排列，並標記 ``source_kind = document``。
    """
    if not docs:
        raise DocumentError("沒有任何文件可以處理")
    if len(docs) == 1 and docs[0].transcript is not None:
        transcript = docs[0].transcript
        transcript.meta.setdefault("source", docs[0].name)
        transcript.meta.setdefault("documents", [docs[0].info()])
        return transcript
    segments: list[Segment] = []
    multi = len(docs) > 1

    def add(text: str) -> None:
        start = len(segments) * DOC_STEP_SECONDS
        segments.append(Segment(start=start, end=start + 1.0, text=text))

    for doc in docs:
        if multi:
            add(f"【文件：{doc.name}】")
        for para in doc.paragraphs:
            add(para)
    return Transcript(
        duration=len(segments) * DOC_STEP_SECONDS,
        language=language,
        segments=segments,
        meta={
            "source": "、".join(d.name for d in docs),
            "source_kind": "document",
            "documents": [d.info() for d in docs],
            # 段落之間的虛擬停頓是 9 秒：任何段落門檻都不會把兩段併在一起
            "paragraph_gap": 0.5,
            "paragraph_max_chars": 100_000,
        },
    )


def combine_documents_text(docs: Sequence[Document], *, max_chars: int) -> str:
    """把多份範本文件合成一段文字，超過上限時各份依比例截斷（保留每份的開頭結構）。"""
    if not docs:
        return ""
    budget = max(500, int(max_chars))
    texts = [d.text.strip() or "\n".join(d.paragraphs) for d in docs]
    total = sum(len(t) for t in texts) or 1
    parts: list[str] = []
    for doc, text in zip(docs, texts):
        share = max(300, int(budget * len(text) / total)) if total > budget else len(text)
        clipped = text[:share]
        if len(text) > share:
            clipped = clipped.rstrip() + "\n…（以下省略）"
        parts.append(f"===== 範本：{doc.name} =====\n{clipped}" if len(docs) > 1 else clipped)
    return "\n\n".join(parts)
