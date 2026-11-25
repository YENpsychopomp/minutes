"""切塊計畫測試：長影音的邊界品質是整條管線的關鍵。"""

from __future__ import annotations

import random

import pytest

from summery.chunking import (
    ChunkPlan,
    estimate_chunk_bytes,
    max_seconds_for_bytes,
    plan_chunks,
    split_chunk,
)
from summery.config import ChunkConfig
from summery.media import Silence


def make_cfg(**kwargs) -> ChunkConfig:
    cfg = ChunkConfig()
    cfg.target_seconds = kwargs.pop("target", 600.0)
    cfg.max_seconds = kwargs.pop("max", 900.0)
    cfg.min_seconds = kwargs.pop("min", 60.0)
    cfg.silence_snap_window = kwargs.pop("window", 240.0)
    cfg.overlap_seconds = kwargs.pop("overlap", 2.0)
    for key, value in kwargs.items():
        setattr(cfg, key, value)
    return cfg


def sil_at(*centres, width: float = 2.0):
    return [Silence(c - width / 2, c + width / 2) for c in centres]


def test_short_media_single_chunk():
    plan = plan_chunks(45.0, sil_at(20), make_cfg())
    assert plan.count == 1
    assert plan.chunks[0].start == 0.0 and plan.chunks[0].end == 45.0


def test_boundaries_snap_to_nearest_silence():
    cfg = make_cfg(target=100, max=150, min=20, window=40)
    silences = sil_at(96.0, 402.0)  # 理想邊界 100 / 200 / 300
    plan = plan_chunks(450.0, silences, cfg)
    starts = [c.start for c in plan.chunks][1:]
    assert any(abs(s - 96.0) < 0.5 for s in starts), starts
    assert plan.snapped >= 1
    assert not plan.coverage_check()


def test_silence_window_respects_max_seconds():
    """靜音點會讓區塊超過 max_seconds 時必須改用硬性邊界。"""
    cfg = make_cfg(target=100, max=110, min=20, window=300)
    plan = plan_chunks(600.0, sil_at(160.0, 320.0), cfg)
    for chunk in plan.chunks:
        assert chunk.duration <= cfg.max_seconds + 0.01, chunk
    assert not plan.coverage_check()


def test_no_silence_falls_back_to_fixed_lengths():
    plan = plan_chunks(3600.0, [], make_cfg(target=600))
    assert plan.count == 6
    assert all(abs(c.duration - 600) < 0.01 for c in plan.chunks)
    assert any("固定長度" in w for w in plan.warnings)


def test_tiny_tail_merged_into_previous_chunk():
    cfg = make_cfg(target=100, max=200, min=30)
    plan = plan_chunks(620.0, [], cfg)
    assert plan.chunks[-1].end == 620.0
    assert all(c.duration >= cfg.min_seconds - 0.01 for c in plan.chunks), [c.duration for c in plan.chunks]


def test_backward_overlap_semantics():
    cfg = make_cfg(target=100, max=200, min=20, overlap=5.0)
    plan = plan_chunks(400.0, [], cfg)
    assert plan.chunks[0].read_start == 0.0  # 第一塊不往回多讀
    for chunk in plan.chunks[1:-1]:
        assert abs(chunk.read_start - (chunk.start - 5.0)) < 0.01
        assert abs(chunk.read_end - chunk.end) < 0.01
        assert abs(chunk.head_overlap - 5.0) < 0.01


def test_plan_is_gapless_for_many_durations():
    rng = random.Random(7)
    cfg = make_cfg(target=120, max=180, min=15, window=60)
    for duration in (30, 119, 121, 240, 241, 555, 1000, 3725, 7263, 3600 * 6):
        silences = [Silence(t, t + rng.uniform(0.3, 3.0)) for t in
                    sorted(rng.uniform(0, duration) for _ in range(int(duration / 90) + 1))]
        plan = plan_chunks(duration, silences, cfg)
        problems = plan.coverage_check()
        assert not problems, (duration, problems)
        assert plan.chunks[0].start == 0.0
        assert abs(plan.chunks[-1].end - duration) < 0.01
        assert max(c.duration for c in plan.chunks) <= cfg.max_seconds + 0.01
        assert min(c.duration for c in plan.chunks) >= min(15, duration) - 0.01


def test_chunk_key_unique_after_split():
    cfg = make_cfg(target=100, max=200, min=10)
    parent = ChunkPlan(4, 400.0, 500.0, 398.0, 500.0)
    a, b = split_chunk(parent, sil_at(450.5), cfg)
    assert (a.key, b.key) == ("4a", "4b")
    assert a.end == b.start
    assert a.read_start == a.start  # 拆出的前半不往回多讀
    assert b.read_start < b.start  # 後半保留回溯重疊
    assert a.duration > 0 and b.duration > 0


def test_estimate_chunk_bytes_respects_codec():
    wav = estimate_chunk_bytes(600, 16000, 1, "wav", "24k")
    m4a = estimate_chunk_bytes(600, 16000, 1, "m4a", "24k")
    assert wav == pytest.approx(600 * 16000 * 2)
    assert m4a == pytest.approx(600 * 24_000 / 8 * 1.1)  # 壓縮後小一個數量級
    assert m4a < wav / 8


def test_plan_serialisation_roundtrip():
    plan = plan_chunks(1500.0, sil_at(600.0, 1200.0), make_cfg(target=500, min=50))
    clone = plan.__class__.from_dict(plan.to_dict())
    assert clone.count == plan.count
    assert clone.chunks[1].start == plan.chunks[1].start
    assert clone.chunks[1].read_start == plan.chunks[1].read_start


def test_zero_duration_rejected():
    with pytest.raises(ValueError):
        plan_chunks(0.0, [], make_cfg())


def test_max_seconds_for_bytes_matches_the_gateway_body_limit():
    """32kbps 單聲道、上限 900KB → 每塊約 220 秒（實測這台 Gateway 只能收 1MB）。"""
    seconds = max_seconds_for_bytes(900_000, 16_000, 1, "mp3", "32k")
    assert 200 < seconds < 235
    # 估算與實際档位於同一式子：縮到該秒數後一定要低於上限
    assert estimate_chunk_bytes(seconds, 16_000, 1, "mp3", "32k") < 900_000


def test_max_seconds_for_bytes_edge_cases():
    assert max_seconds_for_bytes(0, 16_000, 1, "mp3", "32k") == float("inf")
    # 上限小到裝不下時，至少留一個可用的下限，讓執行期 413 應變接手
    assert max_seconds_for_bytes(1_000, 16_000, 1, "wav", "32k", floor=45.0) == 45.0
    # wav 未壓縮，同樣上限下應比压缩格式短得多
    assert max_seconds_for_bytes(900_000, 16_000, 1, "wav", "32k") < max_seconds_for_bytes(
        900_000, 16_000, 1, "mp3", "32k"
    )
