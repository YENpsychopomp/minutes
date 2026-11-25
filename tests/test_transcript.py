"""逐字稿模型、跨 chunk 合併、字幕輸出測試。"""

from __future__ import annotations

import json

import pytest

from summery.chunking import ChunkPlan
from summery.transcript import (
    Segment,
    Transcript,
    build_paragraphs,
    merge_chunk_results,
    parse_asr_payload,
    transcript_from_text,
)


def chunk(index: int, start: float, end: float, overlap: float = 0.0) -> ChunkPlan:
    return ChunkPlan(index=index, start=start, end=end, read_start=start - overlap, read_end=end)


# ---------------------------------------------------------------- ASR 解析
def test_parse_verbose_json_with_words():
    payload = {
        "language": "chinese",
        "segments": [
            {"start": 0.0, "end": 3.2, "text": " 第一句 ", "words": [{"word": "第一句", "start": 0.0, "end": 3.2}]},
            {"start": 3.5, "end": 6.0, "text": "第二句"},
        ],
    }
    segs, lang = parse_asr_payload(payload)
    assert [s.text for s in segs] == ["第一句", "第二句"]
    assert lang == "chinese"
    assert segs[0].words[0].text == "第一句"


def test_parse_plain_text_payload():
    segs, lang = parse_asr_payload({"text": "只有整段文字，沒有時間戳。"})
    assert len(segs) == 1 and segs[0].start == segs[0].end == 0.0


def test_parse_string_and_list_payloads():
    assert parse_asr_payload("純字串回應")[0][0].text == "純字串回應"
    segs, _ = parse_asr_payload([{"start": 0, "end": 2, "text": "A"}, {"start": 2, "end": 4, "text": "B"}])
    assert [s.text for s in segs] == ["A", "B"]


def test_parse_invalid_payload_raises():
    with pytest.raises(ValueError):
        parse_asr_payload({"unexpected": True})
    with pytest.raises(ValueError):
        parse_asr_payload(None)


# ---------------------------------------------------------------- 合併
def test_merge_offsets_timestamps_by_chunk_start():
    c0, c1 = chunk(0, 0, 60), chunk(1, 60, 120)
    tr = merge_chunk_results(
        [(c0, [Segment(0, 10, "前半"), Segment(50, 59, "後半")]),
         (c1, [Segment(1, 9, "第二塊開始")])],
        duration=120,
    )
    assert [(s.start, s.end) for s in tr.segments] == [(0, 10), (50, 59), (61, 69)]
    assert [s.chunk for s in tr.segments] == [0, 0, 1]


def test_merge_drops_segments_inside_backward_overlap():
    cfg_overlap = 4.0
    c0, c1 = chunk(0, 0, 100, overlap=cfg_overlap), chunk(1, 100, 200, overlap=cfg_overlap)
    tr = merge_chunk_results(
        [(c0, [Segment(0, 30, "第一塊內容")]),
         (c1, [Segment(0.2, 3.0, "重複的前一句"), Segment(3.1, 20, "新的內容")])],
        duration=200,
        overlap_seconds=cfg_overlap,
    )
    assert [s.text for s in tr.segments] == ["第一塊內容", "新的內容"]
    # 跨越邊界的 segment 起點會被裁剪到 chunk.start，避免與前一块重複涵蓋
    assert tr.segments[1].start == pytest.approx(100.0)
    assert tr.segments[1].chunk == 1


def test_boundary_duplicate_is_collapsed_to_longer_text():
    c0, c1 = chunk(0, 0, 100, overlap=2), chunk(1, 100, 200, overlap=2)
    tr = merge_chunk_results(
        [(c0, [Segment(90, 101, "這一句話說到了下一塊的開頭"), ]),
         (c1, [Segment(0.1, 1.9, "這一句話說到了下一塊"), Segment(2.2, 12, "繼續發展的句子")])],
        duration=200,
        overlap_seconds=2,
    )
    texts = [s.text for s in tr.segments]
    assert "這一句話說到了下一塊的開頭" in texts
    assert sum("這一句話" in t for t in texts) == 1


def test_merge_without_timestamps_distributes_time():
    c0 = chunk(0, 0, 100)
    tr = merge_chunk_results([(c0, [Segment(0, 0, "甲"), Segment(0, 0, "乙")])], duration=100)
    assert tr.segments[0].start == pytest.approx(0) and tr.segments[0].end > 0
    assert tr.segments[1].end == pytest.approx(100, abs=0.5)


def test_merge_sorts_and_clips_out_of_range():
    c0 = chunk(0, 0, 50)
    tr = merge_chunk_results([(c0, [Segment(48, 90, "越界"), Segment(1, 5, "開頭")])], duration=50)
    assert tr.segments[0].text == "開頭"
    assert tr.segments[-1].end <= 50.0


def test_empty_and_whitespace_segments_are_skipped():
    tr = merge_chunk_results([(chunk(0, 0, 30), [Segment(0, 1, "   "), Segment(2, 4, "有內容")])], duration=30)
    assert [s.text for s in tr.segments] == ["有內容"]


# ---------------------------------------------------------------- 段落與字幕
def test_paragraph_breaks_on_pause_and_length():
    segs = [Segment(0, 2, "一。"), Segment(2.1, 4, "二。"), Segment(12, 14, "三。")]
    paras = build_paragraphs(segs, gap=1.0, max_chars=180)
    assert [p.text for p in paras] == ["一。二。", "三。"]
    long_segs = [Segment(i, i + 1, "字" * 60) for i in range(5)]
    assert len(build_paragraphs(long_segs, gap=0.1, max_chars=100)) >= 3


def test_cjk_join_has_no_space_latin_keeps_space():
    segs = [Segment(0, 1, "中文"), Segment(1.05, 2, "繼續"), Segment(5, 6, "hello"), Segment(6.05, 7, "world")]
    text = Transcript(duration=7, segments=segs).to_text(paragraph_gap=0.2, paragraph_max_chars=200)
    assert "中文繼續" in text
    assert "hello world" in text


def test_srt_and_vtt_format():
    tr = Transcript(duration=65, segments=[Segment(0, 4, "第一句"), Segment(63.5, 66, "第二句")])
    srt = tr.to_srt().splitlines()
    assert srt[0] == "1" and "-->" in srt[1] and "00:00:04,000" in srt[1]
    assert "" in srt and srt[4] == "2"
    vtt = tr.to_vtt().splitlines()
    assert vtt[0] == "WEBVTT"
    cue = next(line for line in vtt if "-->" in line)
    assert "." in cue and "," not in cue and cue.startswith("00:00:00.000")


def test_markdown_contains_clock_and_metadata():
    tr = Transcript(duration=3700, language="zh", segments=[Segment(30, 40, "內容")], meta={"chunks": 5, "asr_model": "m"})
    md = tr.to_markdown(title="測試", source="a.mp4")
    assert md.startswith("# 測試")
    assert "`[0:00:30]` 內容" in md and "1:01:40" in md and "a.mp4" in md


def test_json_roundtrip():
    tr = Transcript(duration=10, language="zh", segments=[Segment(0, 5, "文", words=[__import__("summery.transcript", fromlist=["Word"]).Word(0, 1, "文")])])
    clone = Transcript.from_json(tr.to_json())
    assert clone.segments[0].text == "文" and clone.duration == 10
    assert clone.segments[0].words[0].text == "文"


def test_transcript_from_text_estimates_times():
    tr = transcript_from_text("第一句。\n第二句。\n第三句。", 30.0)
    assert len(tr.segments) == 3
    assert tr.segments[-1].end == pytest.approx(30, abs=0.5)
    assert tr.segments[0].start == 0


def test_window_slices_by_time():
    tr = Transcript(duration=100, segments=[Segment(0, 10, "a"), Segment(40, 50, "b"), Segment(90, 95, "c")])
    assert [s.text for s in tr.window(30, 60).segments] == ["b"]


def test_stats_reports_language_detection():
    tr = Transcript(duration=10, segments=[Segment(0, 5, "這是中文逐字稿")])
    assert tr.is_cjk() and tr.stats()["cjk"] is True
    assert tr.char_count() == 7
