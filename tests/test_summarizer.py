"""摘要器測試：map-reduce、章節時間接地、各種降級路徑。"""

from __future__ import annotations

import json
import re
import threading

import pytest

from summery.config import LLMConfig, SummarizeConfig
from summery.llm import Usage
from summery.summarizer import Summarizer, _chapter_title, split_blocks
from summery.transcript import Segment, Transcript

MAP_HINT = "影音內容分析助手"
REDUCE_HINT = "彙整助手"
FINAL_HINT = "資深影音內容編輯"


def make_transcript(paragraph_count: int = 6, chars: int = 60, per_seconds: float = 30.0) -> Transcript:
    segments = [
        Segment(start=i * per_seconds, end=(i + 1) * per_seconds - 1,
                text=f"第 {i + 1} 段說明內容，重點 {i + 1}。" + "補充細節。" * 4)
        for i in range(paragraph_count)
    ]
    return Transcript(duration=paragraph_count * per_seconds, language="zh", segments=segments)


class EmptyLLM:
    """有呼叫、有 token，但內容是空的（推理型模型把預算花在思考上的典型症狀）。"""

    def __init__(self):
        self.cfg = LLMConfig(max_concurrency=2)
        self.usage = Usage(calls=1, input_tokens=900, output_tokens=2000)

    def complete_json(self, system, user, *, stub_key=None, **kwargs):
        self.usage.calls += 1
        return {"title": "", "tldr": "", "overview": [], "key_points": [], "chapters": []}


class FakeLLM:
    """實作 LLMClient 對外介面，可依规则回應或製造失敗。"""

    def __init__(self, *, map_error: str | None = None, final_chapters=None, final_is_junk=False,
                 no_keywords=False, raise_on_reduce=False, final_open_questions=None):
        self.cfg = LLMConfig(max_concurrency=2, max_retries=1)
        self.usage = Usage()
        self.calls: list[dict] = []
        self.map_error = map_error
        self.final_chapters = final_chapters
        self.final_is_junk = final_is_junk
        self.no_keywords = no_keywords
        self.raise_on_reduce = raise_on_reduce
        self.final_open_questions = final_open_questions

    def _kind(self, system: str) -> str:
        if FINAL_HINT in system:
            return "final"
        if REDUCE_HINT in system:
            return "reduce"
        if MAP_HINT in system:
            return "map"
        return "other"

    def complete_json(self, system: str, user: str, *, stub_key: str | None = None, **kwargs):
        kind = self._kind(system)
        self.calls.append({"kind": kind, "system": system, "user": user, "stub_key": stub_key})

        def done(payload):
            self.usage.calls += 1
            self.usage.input_tokens += max(1, len(system + user) // 4)
            self.usage.output_tokens += 20
            return payload

        return done(self._respond(kind, system, user, stub_key))

    def _respond(self, kind: str, system: str, user: str, stub_key: str | None):
        if kind == "map":
            if self.map_error and stub_key and self.map_error in stub_key:
                raise RuntimeError("區塊模擬失敗")
            span = re.findall(r"\[(\d+)\]", user)
            return {"summary": f"區塊筆記 {stub_key}", "key_points": [f"重點 {span[0] if span else 1}"],
                    "terms": ["切塊", "時間戳"], "issues": []}
        if kind == "reduce":
            if self.raise_on_reduce:
                raise RuntimeError("reduce 模擬失敗")
            span = re.findall(r"段落 (\d+)-(\d+)", user)
            start = min(int(a) for a, _ in span) if span else 1
            end = max(int(b) for _, b in span) if span else 2
            return {"summary": f"彙整 {start}-{end}", "key_points": [f"彙整重點 {start}"], "terms": ["摘要"]}
        # final
        if self.final_is_junk:
            return "這是一段話，不是 JSON"
        indices = [int(m) for m in re.findall(r"^\[(\d+)\]", user, re.M)]
        total = max(indices) if indices else 4
        chapters = self.final_chapters
        if chapters is None:
            half = max(1, total // 2)
            chapters = [
                {"para_start": 1, "para_end": half, "title": "前半", "summary": "前半摘要", "points": ["a"]},
                {"para_start": half + 1, "para_end": total, "title": "後半", "summary": "後半摘要", "points": ["b"]},
            ]
        payload = {
            "title": "測試摘要", "tldr": "一句話", "overview": ["段落一", "段落二"],
            "key_points": ["重點一", "重點二"], "chapters": chapters,
            "keywords": [] if self.no_keywords else ["關鍵字"],
            "decisions": ["決定 A"], "action_items": [{"task": "任務", "owner": "小明", "due": "下週"}],
            "open_questions": self.final_open_questions or ["問題一"], "quotes": [{"text": "原話", "speaker": "主管", "para": 1}],
        }
        return payload


def make_summarizer(llm: FakeLLM, **overrides) -> Summarizer:
    cfg = SummarizeConfig()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return Summarizer(llm, cfg)


# ------------------------------------------------------------------ 直路
def test_short_transcript_uses_single_pass():
    llm = FakeLLM()
    transcript = make_transcript(6)
    summary = make_summarizer(llm).summarize(transcript)
    assert [c["kind"] for c in llm.calls] == ["final"]
    assert summary.meta["mode"] == "direct"
    assert summary.title == "測試摘要" and summary.chapters[1].title == "後半"
    assert summary.chapters[0].start == 0.0
    # 章節時間由段落對照表決定
    paras = transcript.paragraphs()
    assert summary.chapters[1].start == pytest.approx(paras[3].start, abs=0.01)


def test_prompts_reflect_language_and_style():
    llm = FakeLLM()
    make_summarizer(llm, language="en", style="concise").summarize(make_transcript(3))
    system = llm.calls[-1]["system"]
    assert "English" in system and "TL;DR ≤100" in system


def test_custom_instruction_is_included():
    llm = FakeLLM()
    make_summarizer(llm, custom_instruction="聚焦預算").summarize(make_transcript(3))
    assert "聚焦預算" in llm.calls[-1]["system"]


# ------------------------------------------------------------------ map-reduce
def test_long_transcript_triggers_map_reduce():
    llm = FakeLLM()
    transcript = make_transcript(24)
    summarizer = make_summarizer(llm, max_input_tokens=140, map_chunk_tokens=70, reduce_fan_in=2)
    summary = summarizer.summarize(transcript)
    kinds = [c["kind"] for c in llm.calls]
    assert kinds.count("map") >= 6 and "reduce" in kinds and kinds[-1] == "final"
    assert summary.meta["mode"] == "map-reduce"
    assert summary.meta["reduce_rounds"] >= 1
    # 筆記要連續涵蓋全部段落
    paras = transcript.paragraphs()
    total = len(paras)
    covered = sorted((n.para_start, n.para_end) for n in summary.notes)
    assert covered[0][0] == 1 and covered[-1][1] == total
    for (s1, e1), (s2, e2) in zip(covered, covered[1:]):
        assert s2 == e1 + 1, (covered,)


def test_map_blocks_run_in_parallel_and_results_keep_order():
    llm = FakeLLM()
    transcript = make_transcript(20)
    summarizer = make_summarizer(llm, max_input_tokens=120, map_chunk_tokens=60, reduce_fan_in=2)
    summary = summarizer.summarize(transcript)
    map_calls = [c["stub_key"] for c in llm.calls if c["kind"] == "map"]
    assert len(map_calls) >= 6                       # 區塊被拆開平行處理
    assert summary.notes[0].para_start == 1          # 併行完成順序不影響輸出順序
    starts = [n.para_start for n in summary.notes]
    assert starts == sorted(starts)


def test_failing_map_block_degrades_to_source_text():
    llm = FakeLLM(map_error="map:2")
    transcript = make_transcript(20)
    summary = make_summarizer(llm, max_input_tokens=120, map_chunk_tokens=60, reduce_fan_in=2).summarize(transcript)
    assert any("筆記失敗" in w or "原文截斷" in w for w in summary.meta["warnings"])
    # 失敗的區塊仍佔住原位：筆記必須連續涵蓋全場，不留洞
    paras = transcript.paragraphs()
    covered = sorted((n.para_start, n.para_end) for n in summary.notes)
    assert covered[0][0] == 1 and covered[-1][1] == len(paras)
    for (_s1, e1), (s2, _e2) in zip(covered, covered[1:]):
        assert s2 == e1 + 1


def test_reduce_failure_uses_local_merge():
    llm = FakeLLM(raise_on_reduce=True)
    transcript = make_transcript(20)
    summary = make_summarizer(llm, max_input_tokens=120, map_chunk_tokens=60, reduce_fan_in=2).summarize(transcript)
    assert summary.meta["mode"] == "map-reduce"
    assert summary.meta["reduce_rounds"] >= 1
    assert any("reduce 群" in w for w in summary.meta["warnings"])
    assert summary.notes  # 本地彙整仍產出筆記


# ------------------------------------------------------------------ 章節接地
def test_chapters_repaired_when_model_only_covers_beginning():
    llm = FakeLLM(final_chapters=[{"para_start": 1, "para_end": 2, "title": "只看到開頭", "summary": "x", "points": []}])
    transcript = make_transcript(20)
    total = len(transcript.paragraphs())
    summary = make_summarizer(llm, max_input_tokens=120, map_chunk_tokens=60, reduce_fan_in=2).summarize(transcript)
    assert summary.meta["chapters_source"] == "repaired_from_notes"
    assert summary.chapters[-1].para_end == total
    assert summary.chapters[0].start == 0.0
    assert summary.chapters[-1].end >= transcript.duration - 1
    assert any("未涵蓋全場" in w for w in summary.meta["warnings"])


def test_chapter_times_never_exceed_media():
    llm = FakeLLM(final_chapters=[{"para_start": 1, "para_end": 9999, "title": "超界", "summary": "x", "points": []}])
    transcript = make_transcript(4)
    summary = make_summarizer(llm).summarize(transcript)
    assert summary.chapters[0].end <= transcript.duration + 0.01
    assert summary.chapters[0].para_end == len(transcript.paragraphs())


def test_chapters_disabled_returns_none_source():
    llm = FakeLLM(final_chapters=[])
    summary = make_summarizer(llm, chapters=False).summarize(make_transcript(4))
    assert summary.chapters == []


def test_repaired_chapter_titles_drop_note_meta_prefix():
    assert _chapter_title("區塊 982ac4（段落 1-53）的重點：說明切塊做法與注意事項。", "x") == "說明切塊做法與注意事項"
    assert _chapter_title("段落 4-9：主管認為報價需要再確認。", "x") == "主管認為報價需要再確認"
    assert _chapter_title("正常的主題句。", "x") == "正常的主題句"
    assert _chapter_title("", "備用標題") == "備用標題"


def test_repaired_chapters_have_readable_titles():
    llm = FakeLLM(final_chapters=[{"para_start": 1, "para_end": 1, "title": "x", "summary": "y", "points": []}])
    summary = make_summarizer(llm, max_input_tokens=120, map_chunk_tokens=60, reduce_fan_in=2).summarize(
        make_transcript(20))
    assert all("區塊 " not in c.title and "的重點：" not in c.title for c in summary.chapters)
    assert all(c.title for c in summary.chapters)


# ------------------------------------------------------------------ 降級
def test_junk_final_payload_still_produces_summary():
    llm = FakeLLM(final_is_junk=True)
    transcript = make_transcript(16)
    summary = make_summarizer(llm, max_input_tokens=120, map_chunk_tokens=60, reduce_fan_in=2).summarize(transcript)
    assert summary.notes and summary.chapters
    assert summary.meta["mode"] == "map-reduce"
    assert "回傳不是 JSON" in " ".join(summary.meta["warnings"])


def test_keywords_fall_back_to_note_terms():
    llm = FakeLLM(no_keywords=True)
    transcript = make_transcript(16)
    summary = make_summarizer(llm, max_input_tokens=120, map_chunk_tokens=60, reduce_fan_in=2).summarize(transcript)
    assert "切塊" in summary.keywords or "摘要" in summary.keywords


def test_empty_llm_payload_never_yields_blank_summary():
    transcript = make_transcript(12)
    llm = EmptyLLM()
    summary = make_summarizer(llm).summarize(transcript)
    assert any("未回傳可用的摘要內容" in w for w in summary.meta["warnings"])
    assert summary.meta["chapters_source"] == "local_fallback"
    assert summary.tldr and summary.key_points and summary.chapters
    paras = transcript.paragraphs()
    assert summary.chapters[0].start == 0.0
    assert summary.chapters[-1].end >= transcript.duration - 1
    assert summary.chapters[-1].para_end == len(paras)
    md = summary.to_markdown(duration=transcript.duration)
    assert "TL;DR" in md and "[0:0" in md


def test_junk_string_payload_also_falls_back():
    class JunkLLM(EmptyLLM):
        def complete_json(self, system, user, *, stub_key=None, **kwargs):
            return "抱歉，我無法處理"
    summary = make_summarizer(JunkLLM()).summarize(make_transcript(8))
    assert summary.chapters and summary.tldr
    assert any("不是 JSON 物件" in w or "未回傳可用的摘要內容" in w for w in summary.meta["warnings"])


def test_empty_transcript_short_circuits():
    llm = FakeLLM()
    summary = make_summarizer(llm).summarize(Transcript(duration=0, segments=[]))
    assert summary.meta["mode"] == "empty" and llm.calls == []


def test_cancel_stops_before_llm_calls():
    llm = FakeLLM()
    cancel = threading.Event()
    cancel.set()
    cfg = SummarizeConfig()
    summarizer = Summarizer(llm, cfg, cancel=cancel)
    with pytest.raises(KeyboardInterrupt):
        summarizer.summarize(make_transcript(4))
    assert llm.calls == []


# ------------------------------------------------------------------ 區塊與輸出
def test_split_blocks_respects_budget_and_paragraph_bounds():
    transcript = make_transcript(30)
    paras = transcript.paragraphs()
    numbered = [f"[{i}] {p.text}" for i, p in enumerate(paras, start=1)]
    blocks = split_blocks(paras, budget_tokens=120, chars_per_token=2.8, numbered=numbered)
    assert len(blocks) > 1
    assert blocks[0].para_start == 1
    assert blocks[-1].para_end == len(paras)
    for block in blocks:
        assert block.end > block.start
    for first, second in zip(blocks, blocks[1:]):
        assert second.para_start == first.para_end + 1


def test_summary_markdown_and_json_shape():
    summary = make_summarizer(FakeLLM()).summarize(make_transcript(6))
    md = summary.to_markdown(duration=180)
    assert "# 測試摘要" in md and "[0:01:30]" in md and "待辦事項" in md and "小明" in md
    data = json.loads(summary.to_json())
    assert data["schema"] == "summery.summary/1" and data["chapters"][0]["clock"]
    assert data["action_items"][0]["owner"] == "小明"


def test_progress_callback_reaches_one():
    seen = []
    llm = FakeLLM()
    summarizer = make_summarizer(llm)
    summarizer.progress = lambda stage, frac, msg: seen.append((stage, frac))
    summarizer.summarize(make_transcript(6))
    assert seen and seen[-1][1] == pytest.approx(1.0)


def test_llm_usage_is_reported_in_meta():
    llm = FakeLLM()
    summary = make_summarizer(llm).summarize(make_transcript(6))
    assert summary.meta["llm_usage"]["calls"] == 1


# ------------------------------------------------------- 待釐清問題的時間點
def test_open_questions_are_timed_from_paragraph_index():
    llm = FakeLLM(final_open_questions=[
        {"text": "「合金的挑戰」是否為核心挑戰", "para": 4},
        {"text": "完全無法定位的問題", "para": None},
    ])
    transcript = make_transcript(6)
    summary = make_summarizer(llm).summarize(transcript)
    paras = transcript.paragraphs()
    first = summary.open_questions[0]
    assert first.para == 4 and first.start == pytest.approx(paras[3].start, abs=0.01)
    assert first.located == "para" and first.clock
    assert summary.open_questions[1].start is None
    assert summary.meta["open_questions_timed"] == "1/2"
    assert f"[{first.clock}]" in summary.to_markdown()


def test_open_questions_located_by_quoted_terms_when_model_omits_para():
    """模型懶得標段落時，用問題裡引用的原詞回查逐字稿，一樣能給出可跳轉的時間。"""
    llm = FakeLLM(final_open_questions=[{"text": "「第 3 段」這段語意不清", "para": None}])
    transcript = make_transcript(6)
    summary = make_summarizer(llm).summarize(transcript)
    question = summary.open_questions[0]
    assert question.located == "text" and question.para == 3
    assert question.start == pytest.approx(transcript.paragraphs()[2].start, abs=0.01)
    assert question.clock


def test_open_questions_keep_legacy_plain_strings():
    llm = FakeLLM()  # 預設回應仍是舊格式 ["問題一"]
    summary = make_summarizer(llm).summarize(make_transcript(6))
    assert summary.open_questions[0].text == "問題一"
    assert summary.open_questions[0].start is None
    assert "- 問題一" in summary.to_markdown()
    assert summary.to_dict()["open_questions"][0]["text"] == "問題一"


def test_quoted_term_wins_over_model_paragraph_number():
    """map-reduce 時模型只看到幾份筆記，段落編號常整排相同；引號裡的原詞比較可信。"""
    llm = FakeLLM(final_open_questions=[{"text": "「第 5 段」這個詞可能辨識錯了", "para": 1}])
    transcript = make_transcript(6)
    summary = make_summarizer(llm).summarize(transcript)
    question = summary.open_questions[0]
    assert question.located == "text" and question.para == 5


def test_repeated_term_picks_the_occurrence_nearest_the_reported_paragraph():
    llm = FakeLLM(final_open_questions=[{"text": "「補充細節」的斷句是否正確", "para": 5}])
    transcript = make_transcript(6)
    summary = make_summarizer(llm).summarize(transcript)
    question = summary.open_questions[0]
    assert question.located == "text" and question.para == 5, "同一詞出現多次時應挑最接近模型回報段落的那一次"
