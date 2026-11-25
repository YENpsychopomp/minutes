"""過往會議紀錄當範本、文件當輸入：管線端到端（mock 伺服器）與各階段的保底行為。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from summery.config import Config
from summery.documents import documents_to_transcript, load_document
from summery.llm import ChatResult, LLMError, Usage
from summery.minutes import ReferenceProfile, analyze_references, compose_minutes, local_profile
from summery.pipeline import JobOptions, Pipeline
from summery.summarizer import Summarizer


class RecordingLLM:
    """記下每次提示詞的假 LLM（回傳最小可用的 JSON）。"""

    def __init__(self, *, fail_minutes: bool = False):
        self.cfg = type("Cfg", (), {"max_concurrency": 2, "model": "fake"})()
        self.usage = Usage()
        self.calls: list[tuple[str, str, str | None]] = []
        self.fail_minutes = fail_minutes
        self.lock = threading.Lock()

    def complete_json(self, system: str, user: str, *, stub_key: str | None = None, **_kw):
        with self.lock:
            self.calls.append((system, user, stub_key))
        if stub_key == "reference":
            return {"outline": ["一、報告事項", "二、討論事項"], "header_fields": ["時間", "主席"],
                    "attendees": ["王大明", "李小華"], "terms": ["AI 中心"], "style_notes": ["正式語氣"]}
        if stub_key and stub_key.startswith("map"):
            return {"summary": "筆記", "key_points": ["重點"]}
        return {"title": "系務會議", "tldr": "討論預算", "overview": ["概要"], "key_points": ["重點"],
                "chapters": [{"para_start": 1, "para_end": 2, "title": "預算", "summary": "說明"}],
                "decisions": ["通過預算"], "action_items": [{"task": "送簽", "owner": "王大明", "due": "週五"}],
                "open_questions": [{"text": "經費來源？", "para": 2}]}

    def complete(self, system: str, user: str, *, stub_key: str | None = None, **_kw) -> ChatResult:
        with self.lock:
            self.calls.append((system, user, stub_key))
        if self.fail_minutes:
            raise LLMError("模型掛了", retryable=False)
        return ChatResult(text="```markdown\n# 系務會議紀錄\n\n一、報告事項\n```")


def _notes(tmp_path: Path, name: str = "筆記.md") -> Path:
    path = tmp_path / name
    path.write_text("# 預算討論\n\n王大明說明明年度預算。\n\n決定先送簽採購案。\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------ Summarizer
def test_document_mode_prompts_use_paragraph_numbers_and_reference_hint(tmp_path):
    llm = RecordingLLM()
    transcript = documents_to_transcript([load_document(_notes(tmp_path))])
    cfg = Config().summarize
    summary = Summarizer(llm, cfg, reference_hint="・與會者：王大明").summarize(transcript, title_hint="筆記")
    system, user, _key = llm.calls[-1]
    assert "文件段落編號" in system and "與會者：王大明" in system
    assert "【輸入文件】1 份" in user and "[1] # 預算討論" in user
    assert "[00:00" not in user  # 文件沒有時間軸，不放時間戳
    data = summary.to_dict()
    assert data["meta"]["input_kind"] == "document"
    assert data["chapters"][0]["clock"] == "段落 1–2"
    assert data["open_questions"][0]["clock"] == "段落 2"
    md = summary.to_markdown()
    assert "[段落 1–2] 預算" in md and "來源文件：筆記.md" in md and "長度" not in md


def test_media_mode_prompts_keep_clock_and_reference(tmp_path):
    from summery.transcript import Segment, Transcript

    llm = RecordingLLM()
    transcript = Transcript(duration=60, segments=[Segment(0, 5, "大家好。"), Segment(20, 25, "開始。")])
    Summarizer(llm, Config().summarize, reference_hint="・專有名詞：AI 中心").summarize(transcript)
    system, user, _key = llm.calls[-1]
    assert "專有名詞：AI 中心" in system and "文件段落編號" not in system
    assert "【媒體長度】" in user and "[1] [0:00:00] 大家好。" in user


# ------------------------------------------------------------------ 範本分析
def test_reference_profile_is_cached(tmp_path, make_docx):
    docs = [load_document(make_docx())]
    cfg = Config()
    llm = RecordingLLM()
    first = analyze_references(docs, llm=llm, cfg=cfg, cache_dir=tmp_path)
    assert first.source == "model" and first.attendees == ["王大明", "李小華"]
    assert "系務會議紀錄" in first.excerpt
    assert "與會者：王大明" in first.hint_text()
    again = analyze_references(docs, llm=llm, cfg=cfg, cache_dir=tmp_path)
    assert [c[2] for c in llm.calls].count("reference") == 1  # 第二次讀快取
    assert again.outline == first.outline and again.excerpt


def test_local_profile_reads_outline_and_fields(make_docx):
    profile = local_profile([load_document(make_docx())])
    assert profile.source == "local"
    assert profile.header_fields[:4] == ["時間", "地點", "主席", "出席人員"]
    assert "一、 報告事項" in profile.outline and "三、 決議事項" in profile.outline


def test_reference_failure_falls_back_to_local_profile(make_docx):
    class Broken(RecordingLLM):
        def complete_json(self, *a, **k):
            raise LLMError("掛了")

    warnings: list[str] = []
    profile = analyze_references([load_document(make_docx())], llm=Broken(), cfg=Config(), warnings=warnings)
    assert profile.source == "local" and profile.outline and "範本分析失敗" in warnings[0]


# ------------------------------------------------------------------ 撰寫紀錄
def _summary(tmp_path):
    transcript = documents_to_transcript([load_document(_notes(tmp_path))])
    return Summarizer(RecordingLLM(), Config().summarize).summarize(transcript)


def test_compose_minutes_sends_template_and_strips_code_fence(tmp_path):
    llm = RecordingLLM()
    profile = ReferenceProfile(files=["t.docx"], outline=["一、報告事項"], header_fields=["時間"],
                               excerpt="# 舊紀錄\n一、報告事項")
    text, origin = compose_minutes(_summary(tmp_path), llm=llm, cfg=Config(), profile=profile, source_desc="文件：筆記.md")
    assert origin == "model" and text.startswith("# 系務會議紀錄") and "```" not in text
    system, user, key = llm.calls[-1]
    assert key == "minutes" and "沿用範本" in system and "段落編號" in system
    assert "【範本格式分析】" in user and "【範本原文節錄" in user and "通過預算" in user


def test_compose_minutes_without_template_uses_default_format(tmp_path):
    llm = RecordingLLM()
    compose_minutes(_summary(tmp_path), llm=llm, cfg=Config())
    system, user, _key = llm.calls[-1]
    assert "標準會議紀錄格式" in system and "【範本格式分析】" not in user


def test_compose_minutes_failure_falls_back_to_local(tmp_path):
    warnings: list[str] = []
    profile = ReferenceProfile(header_fields=["時間", "出席人員"], attendees=["王大明"])
    text, origin = compose_minutes(_summary(tmp_path), llm=RecordingLLM(fail_minutes=True), cfg=Config(),
                                   profile=profile, warnings=warnings)
    assert origin == "local" and "會議紀錄撰寫失敗" in warnings[0]
    assert "- 出席人員：王大明" in text and "## 決議事項" in text and "通過預算" in text
    assert "| 送簽 | 王大明 | 週五 |" in text


# ------------------------------------------------------------------ 管線端到端（mock）
def test_pipeline_documents_with_template(base_config, tmp_path, make_docx):
    notes = _notes(tmp_path)
    extra = tmp_path / "補充.txt"
    extra.write_text("李小華補充：AI 中心機房下月完工。\n", encoding="utf-8")
    template = make_docx()
    out = tmp_path / "out"
    result = Pipeline(base_config, options=JobOptions(
        out_dir=str(out), references=[str(template)], extra_inputs=[str(extra)],
    )).run(notes)
    assert result.ok and result.stats["input_kind"] == "document" and result.stats["documents"] == 2
    assert set(result.outputs) >= {"summary.md", "summary.json", "minutes.md", "md", "json", "txt"}
    assert "srt" not in result.outputs and "vtt" not in result.outputs  # 文件沒有時間軸
    minutes = Path(result.outputs["minutes.md"]).read_text(encoding="utf-8")
    # mock 依範本分析出的段落順序撰寫：範本的段落標題要依序出現
    order = [minutes.index(h) for h in ("報告事項", "討論事項", "決議事項", "臨時動議")]
    assert order == sorted(order)
    assert "時間：" in minutes and "主席：" in minutes
    summary = json.loads(Path(result.outputs["summary.json"]).read_text(encoding="utf-8"))
    assert summary["meta"]["input_kind"] == "document"
    assert summary["meta"]["minutes"] == {"source": "model", "format": "template", "template": ["template.docx"]}
    assert summary["meta"]["reference"]["outline"] and summary["minutes_markdown"] == minutes
    assert all(ch["clock"].startswith("段落") for ch in summary["chapters"])
    transcript_md = Path(result.outputs["md"]).read_text(encoding="utf-8")
    assert "來源文件：筆記.md、補充.txt" in transcript_md and "【文件：補充.txt】" in transcript_md
    state = json.loads((Path(result.work_dir) / "state.json").read_text(encoding="utf-8"))
    assert state["kind"] == "document" and state["references"] == ["template.docx"]


def test_pipeline_document_without_template_still_writes_minutes(base_config, tmp_path):
    result = Pipeline(base_config, options=JobOptions(out_dir=str(tmp_path / "o"))).run(_notes(tmp_path))
    assert "minutes.md" in result.outputs
    assert result.summary.meta["minutes"]["format"] == "default"


def test_pipeline_document_never_overwrites_its_input(base_config, tmp_path):
    """輸入 notes.txt、輸出目錄同一處時，輸出的純文字不可蓋掉原檔。"""
    src = tmp_path / "notes.txt"
    src.write_text("原始筆記。\n", encoding="utf-8")
    result = Pipeline(base_config, options=JobOptions(formats=["txt"])).run(src)
    assert src.read_text(encoding="utf-8") == "原始筆記。\n"
    assert Path(result.outputs["txt"]).name == "notes.out.txt"


def test_pipeline_media_with_template(base_config, media_factory, tmp_path, make_docx):
    media = media_factory(minutes=0.5, segments=4)
    result = Pipeline(base_config, options=JobOptions(
        out_dir=str(tmp_path / "out"), references=[str(make_docx())],
    )).run(media)
    assert result.ok and result.stats["input_kind"] == "media"
    assert {"minutes.md", "srt", "vtt"} <= set(result.outputs)
    assert result.summary.meta["minutes"]["format"] == "template"
    assert result.summary.meta["input_kind"] == "media"


def test_pipeline_media_without_template_has_no_minutes_by_default(base_config, media_factory, tmp_path):
    media = media_factory(minutes=0.5, segments=4)
    result = Pipeline(base_config, options=JobOptions(out_dir=str(tmp_path / "out"))).run(media)
    assert "minutes.md" not in result.outputs and not result.summary.minutes_markdown
    forced = Pipeline(base_config, options=JobOptions(out_dir=str(tmp_path / "out2"), minutes=True)).run(media)
    assert "minutes.md" in forced.outputs


def test_bad_template_fails_before_asr(base_config, media_factory, tmp_path, mock_server_inject):
    """範本讀不到（例如舊版 .doc）要在送 ASR 之前就失敗，不浪費轉錄費用。"""
    from summery.pipeline import JobError

    log: list = []
    url = mock_server_inject(requests_log=log)
    base_config.asr.base_url = base_config.llm.base_url = url
    bad = tmp_path / "舊紀錄.doc"
    bad.write_bytes(b"\xd0\xcf\x11\xe0")
    with pytest.raises(JobError, match="另存新檔"):
        Pipeline(base_config, options=JobOptions(references=[str(bad)])).run(media_factory(minutes=0.5, segments=4))
    assert log == []


def test_pipeline_without_llm_key_uses_local_minutes(base_config, tmp_path, make_docx, monkeypatch):
    for name in ("LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    base_config.llm.api_key = None
    result = Pipeline(base_config, options=JobOptions(
        out_dir=str(tmp_path / "o"), references=[str(make_docx())],
    )).run(_notes(tmp_path))
    assert result.summary.meta["mode"] == "fallback"
    assert result.summary.meta["minutes"]["source"] == "local"
    assert result.summary.meta["reference"]["source"] == "local"
    minutes = Path(result.outputs["minutes.md"]).read_text(encoding="utf-8")
    assert "- 時間：（未提及）" in minutes and "## 討論事項" in minutes
