"""文件輸入：純文字、.docx（zipfile + xml）、字幕、JSON 與 PDF（markitdown）。"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

from summery import documents
from summery.documents import (
    DocumentError,
    combine_documents_text,
    decode_text,
    docx_to_markdown,
    documents_to_transcript,
    load_document,
    parse_subtitles,
    split_paragraphs,
)


# ------------------------------------------------------------------ 純文字
def test_decode_text_handles_bom_and_big5():
    assert decode_text("會議".encode("utf-8-sig")) == "會議"
    assert decode_text("會議".encode("utf-16")) == "會議"
    assert decode_text("系務會議紀錄".encode("cp950")) == "系務會議紀錄"


def test_split_paragraphs_keeps_short_lines_apart():
    """「出席：…」「記錄：…」這種刻意換行不能被接成一段。"""
    text = "出席：王大明、李小華\n記錄：林雅婷\n\n# 討論事項\n一、預算調整\n二、課程規劃"
    assert split_paragraphs(text) == [
        "出席：王大明、李小華", "記錄：林雅婷", "# 討論事項", "一、預算調整", "二、課程規劃",
    ]


def test_split_paragraphs_rejoins_hard_wrapped_lines():
    """PDF 常把一句話硬換行：長行且不以句號結尾就接回同一段。"""
    line1 = "本次會議主要討論下學期的課程規劃與實驗室設備的採購時程，各組代表依序報告目前"
    line2 = "的進度與遇到的困難。"
    assert split_paragraphs(f"{line1}\n{line2}\n下一段。") == [line1 + line2, "下一段。"]


def test_split_paragraphs_splits_very_long_paragraphs_on_sentences():
    text = "。".join(f"第 {i} 句說明預算與時程的細節" for i in range(80)) + "。"
    parts = split_paragraphs(text, max_chars=200)
    assert len(parts) > 5
    assert all(len(p) <= 300 for p in parts)
    assert "".join(parts) == text


def test_split_paragraphs_drops_markdown_table_rule():
    text = "| 事項 | 負責人 |\n| --- | --- |\n| 採購 | 陳志強 |"
    assert split_paragraphs(text) == ["| 事項 | 負責人 |", "| 採購 | 陳志強 |"]


def test_load_text_document(tmp_path):
    path = tmp_path / "筆記.txt"
    path.write_bytes("開場說明本季目標。\n各組回報進度。\n".encode("cp950"))
    doc = load_document(path)
    assert doc.kind == "text" and doc.paragraphs == ["開場說明本季目標。", "各組回報進度。"]
    assert doc.info()["kind_label"] == "純文字" and not doc.timed


def test_empty_document_is_rejected(tmp_path):
    path = tmp_path / "空的.md"
    path.write_text("\n\n   \n", encoding="utf-8")
    with pytest.raises(DocumentError, match="空的"):
        load_document(path)


def test_unsupported_extensions_get_a_hint(tmp_path):
    path = tmp_path / "舊檔.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0")
    with pytest.raises(DocumentError, match="另存新檔"):
        load_document(path)
    other = tmp_path / "x.xyz"
    other.write_text("hi", encoding="utf-8")
    with pytest.raises(DocumentError, match="不支援"):
        load_document(other)


# ------------------------------------------------------------------ .docx
def test_docx_headings_numbering_and_tables(make_docx):
    md = docx_to_markdown(make_docx())
    lines = [line for line in md.split("\n") if line.strip()]
    assert lines[0] == "# 資訊工程系第 12 次系務會議紀錄"  # styleId「1」→ 樣式名稱 heading 1
    assert "時間：2026 年 10 月 7 日（三）中午 12 時" in lines
    # Word 自動編號依 numbering.xml 還原成「一、」「(1)」，下層在上層前進後重新起算
    assert "一、 報告事項" in lines
    assert "  (1) AI 中心機房擴充進度" in lines
    assert "二、 討論事項" in lines and "三、 決議事項" in lines and "四、 臨時動議" in lines
    assert "| 事項 | 負責人 | 期限 |" in lines and "| --- | --- | --- |" in lines
    assert "| 採購 GPU 伺服器 | 陳志強 | 11 月底 |" in lines


def test_docx_bullets_and_line_breaks(make_docx):
    path = make_docx("bullets.docx", [("bullet", "第一點"), ("bullet", "第二點"), ("p", "結語")])
    assert docx_to_markdown(path).split("\n\n") == ["- 第一點", "- 第二點", "結語"]


def test_docx_document_loads_as_paragraphs(make_docx):
    doc = load_document(make_docx())
    assert doc.kind == "docx"
    assert doc.paragraphs[0] == "# 資訊工程系第 12 次系務會議紀錄"
    assert "出席人員：王大明、李小華、陳志強" in doc.paragraphs


def test_broken_docx_is_reported(tmp_path):
    path = tmp_path / "壞掉.docx"
    path.write_bytes(b"not a zip")
    with pytest.raises(DocumentError, match="不是有效的 .docx"):
        load_document(path)
    bomb = tmp_path / "doctype.docx"
    with zipfile.ZipFile(bomb, "w") as zf:
        zf.writestr("word/document.xml", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>')
    with pytest.raises(DocumentError, match="不允許"):
        load_document(bomb)


# ------------------------------------------------------------------ 字幕／JSON
def test_parse_srt_and_vtt():
    srt = "1\n00:00:01,000 --> 00:00:03,500\n<i>大家好</i>\n\n2\n00:00:04,000 --> 00:00:06,000\n開始開會\n"
    segs = parse_subtitles(srt)
    assert [(s.start, s.end, s.text) for s in segs] == [(1.0, 3.5, "大家好"), (4.0, 6.0, "開始開會")]
    vtt = "WEBVTT\n\nNOTE 備註\n\n00:01.000 --> 00:02.000 align:start\n第一句\n\n01:00:00.000 --> 01:00:02.000\n第二句\n"
    segs = parse_subtitles(vtt)
    assert [(s.start, s.text) for s in segs] == [(1.0, "第一句"), (3600.0, "第二句")]


def test_subtitle_document_keeps_real_timeline(tmp_path):
    path = tmp_path / "會議.srt"
    path.write_text("1\n00:00:01,000 --> 00:00:03,000\n大家好。\n\n2\n00:00:10,000 --> 00:00:12,000\n開始開會。\n",
                    encoding="utf-8")
    doc = load_document(path)
    assert doc.kind == "subtitle" and doc.timed
    transcript = documents_to_transcript([doc])
    assert transcript.meta["source_kind"] == "subtitle"
    assert transcript.segments[1].start == 10.0


def test_json_transcript_and_summary(tmp_path):
    t = tmp_path / "talk.transcript.json"
    t.write_text(json.dumps({"duration": 20, "segments": [{"start": 0, "end": 5, "text": "第一段。"}]}), encoding="utf-8")
    assert load_document(t).kind == "transcript"
    s = tmp_path / "old.summary.json"
    s.write_text(json.dumps({"schema": "summery.summary/1", "title": "上次例會", "decisions": ["通過預算"],
                             "action_items": [{"task": "送簽", "owner": "王", "due": "週五"}]}, ensure_ascii=False),
                 encoding="utf-8")
    doc = load_document(s)
    assert doc.kind == "summary" and "通過預算" in doc.text and "送簽" in doc.text
    bad = tmp_path / "x.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(DocumentError, match="看不懂"):
        load_document(bad)


# ------------------------------------------------------------------ PDF
def _no_markitdown(monkeypatch):
    real = documents.importlib.util.find_spec
    monkeypatch.setattr(documents.importlib.util, "find_spec",
                        lambda name, *a, **k: None if name == "markitdown" else real(name, *a, **k))
    monkeypatch.setattr(documents.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(documents, "_conda_roots", lambda: [])


def test_pdf_without_markitdown_explains_options(tmp_path, monkeypatch):
    _no_markitdown(monkeypatch)
    pdf = tmp_path / "紀錄.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(DocumentError) as exc:
        load_document(pdf)
    assert "markitdown" in str(exc.value) and ".docx" in str(exc.value)
    assert documents.markitdown_status()["available"] is False


def _fake_markitdown(tmp_path: Path, body: str) -> str:
    """假的 markitdown：照真正的 CLI 介面 `markitdown 檔案 -o 輸出`。"""
    script = tmp_path / "fake_markitdown.py"
    script.write_text(
        "import sys\n"
        "out = sys.argv[sys.argv.index('-o') + 1]\n"
        f"open(out, 'w', encoding='utf-8').write({body!r})\n",
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def test_pdf_via_markitdown_command(tmp_path, monkeypatch):
    _no_markitdown(monkeypatch)
    pdf = tmp_path / "上次紀錄.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    cmd = _fake_markitdown(tmp_path, "# 系務會議紀錄\n\n一、報告事項\n\n二、討論事項\n")
    doc = load_document(pdf, markitdown_cmd=cmd)
    assert doc.kind == "pdf"
    assert doc.paragraphs == ["# 系務會議紀錄", "一、報告事項", "二、討論事項"]


def test_scanned_pdf_without_text_is_rejected(tmp_path, monkeypatch):
    _no_markitdown(monkeypatch)
    pdf = tmp_path / "掃描.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(DocumentError, match="掃描"):
        load_document(pdf, markitdown_cmd=_fake_markitdown(tmp_path, "\n\n"))


def test_missing_markitdown_cmd_is_reported(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(DocumentError, match="markitdown_cmd"):
        load_document(pdf, markitdown_cmd=str(tmp_path / "nope" / "markitdown.exe"))


def test_markitdown_is_found_in_a_conda_env(tmp_path, monkeypatch):
    """沒在 PATH 上時，會去 conda 的 envs/*/Scripts 找（使用者常另外建 markitdown 環境）。"""
    _no_markitdown(monkeypatch)
    exe = tmp_path / "anaconda3" / "envs" / "markitdown" / "Scripts" / "markitdown.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(documents, "_conda_roots", lambda: [tmp_path / "anaconda3"])
    assert documents.find_markitdown() == [str(exe)]


# ------------------------------------------------------------------ 組合
def test_documents_to_transcript_numbers_paragraphs(tmp_path, make_docx):
    a = tmp_path / "筆記.md"
    a.write_text("# 第一案\n\n同意採購。\n", encoding="utf-8")
    docs = [load_document(a), load_document(make_docx())]
    transcript = documents_to_transcript(docs)
    assert transcript.meta["source_kind"] == "document"
    assert [d["name"] for d in transcript.meta["documents"]] == ["筆記.md", "template.docx"]
    paras = transcript.paragraphs(gap=0.9, max_chars=180)  # 任何段落門檻都不會把兩段併起來
    assert len(paras) == len(transcript.segments)
    assert paras[0].text == "【文件：筆記.md】" and paras[1].text == "# 第一案"
    assert transcript.to_markdown(title="原文").count("`[") == len(paras)
    assert "長度" not in transcript.to_markdown(title="原文")


def test_combine_documents_text_clips_each_document(tmp_path):
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("A" * 5000, encoding="utf-8")
    b.write_text("B" * 5000, encoding="utf-8")
    text = combine_documents_text([load_document(a), load_document(b)], max_chars=2000)
    assert "範本：a.md" in text and "範本：b.md" in text
    assert text.count("A") < 1500 and text.count("B") < 1500 and "以下省略" in text


def test_kangxi_radicals_from_pdf_fonts_are_normalized(tmp_path):
    """中文 PDF 常把「工月大一」抽成康熙部首碼位（⼯⽉⼤⼀）；要換回一般漢字，全形標點保持不變。"""
    raw = "\u8cc7\u8a0a\u2f2f\u7a0b\u7cfb\uff1a\u738b\u2f24\u660e\uff08\u2f00\u3001\u5831\u544a\uff09"
    assert documents.normalize_cjk(raw) == "資訊工程系：王大明（一、報告）"
    path = tmp_path / "pdf_like.txt"
    path.write_text("\u2f00\u3001\u5831\u544a\u4e8b\u9805\n", encoding="utf-8")
    assert load_document(path).paragraphs == ["一、報告事項"]
