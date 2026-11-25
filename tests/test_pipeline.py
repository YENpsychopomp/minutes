"""端到端管線測試：以 mock ASR/LLM 驗證長影音流程、resume 與失敗處理。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from summery import pipeline as pipeline_mod
from summery.asr import ASRError, ASROutcome
from summery.config import Config
from summery.pipeline import JobError, JobOptions, JobStateStore, Pipeline, run_job
from summery.transcript import Transcript


@pytest.fixture
def cfg1m(base_config) -> Config:
    base_config.chunking.target_seconds = 20.0
    base_config.chunking.min_seconds = 6.0
    return base_config


@pytest.fixture
def short_media(media_factory):
    return media_factory(minutes=1.0, segments=8)


def run(cfg, source, **option_kwargs) -> tuple:
    options = JobOptions(out_dir=str(cfg_dir(cfg) / "out"), **option_kwargs)
    return Pipeline(cfg, options=options).run(source), options


def cfg_dir(cfg: Config):
    from pathlib import Path

    return Path(cfg.work_dir).parent


# ------------------------------------------------------------------ happy path
def test_full_run_produces_all_outputs(cfg1m, short_media):
    result, _options = run(cfg1m, str(short_media))
    assert result.ok and not result.partial
    assert result.duration == pytest.approx(60.0, abs=1.0)
    assert result.plan["count"] > 1, "1 分鐘媒體應被切成多塊"
    assert result.transcript.char_count() > 400
    # 逐字稿時間軸完整覆蓋且單調
    segs = result.transcript.segments
    assert segs[0].start == pytest.approx(0.0, abs=0.05)
    assert segs[-1].end == pytest.approx(result.duration, abs=1.0)
    assert all(b.start >= a.start for a, b in zip(segs, segs[1:]))
    # 摘要與檔案
    assert result.summary and result.summary.chapters
    out = cfg_dir(cfg1m) / "out"
    for suffix in (".transcript.md", ".transcript.json", ".txt", ".srt", ".vtt", ".summary.md", ".summary.json"):
        matches = list(out.glob(f"*{suffix}"))
        assert matches, f"缺少輸出檔案 {suffix}"
    # 工作目錄狀態與中間檔清理
    state = json.loads((cfg_path(cfg1m) / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "done" and state["progress"] == 1.0
    assert list((cfg_path(cfg1m) / "results").glob("chunk_*.json")), "chunk 結果應被快取"
    assert not list((cfg_path(cfg1m) / "chunks").glob("*")), "預設應清理 chunk 中間檔"


def cfg_path(cfg: Config):
    from pathlib import Path

    job_dirs = list(Path(cfg.work_dir).expanduser().iterdir())
    assert job_dirs, "工作目錄未建立"
    return job_dirs[0]


def test_video_source_is_accepted(media_factory, base_config):
    video = media_factory(minutes=0.6, segments=5, video=True)
    base_config.chunking.target_seconds = 15.0
    base_config.chunking.min_seconds = 5.0
    result, _ = run(base_config, str(video))
    assert result.ok and result.transcript.segments
    assert result.plan["count"] >= 2


# ------------------------------------------------------------------ resume
def test_resume_reuses_cached_chunks(media_factory, base_config, mock_server_inject):
    requests: list = []
    url = mock_server_inject(requests_log=requests)
    base_config.asr.base_url = url
    base_config.llm.base_url = url
    media = media_factory(minutes=1.0, segments=8)

    first, _ = run(base_config, str(media))
    asr_calls = [r for r in requests if r["path"].endswith("/audio/transcriptions")]
    assert len(asr_calls) == first.plan["count"]
    requests.clear()

    second, _ = run(base_config, str(media))
    asr_again = [r for r in requests if r["path"].endswith("/audio/transcriptions")]
    assert asr_again == [], "第二輪應完全沿用 chunk 快取"
    assert second.transcript.char_count() == first.transcript.char_count()
    assert [r for r in requests if r["path"].endswith("/chat/completions")], "摘要仍會重新執行"


def test_chunk_signature_change_invalidates_cache(media_factory, base_config, mock_server_inject):
    requests: list = []
    url = mock_server_inject(requests_log=requests)
    base_config.asr.base_url = url
    base_config.llm.base_url = url
    media = media_factory(minutes=1.0, segments=8)
    run(base_config, str(media))
    first_count = len([r for r in requests if r["path"].endswith("/audio/transcriptions")])
    requests.clear()

    base_config2 = base_config  # 同一份設定物件，只改切塊長度
    base_config2.chunking.target_seconds = 35.0
    base_config2.chunking.max_seconds = 60.0
    second, _ = run(base_config2, str(media))
    assert len([r for r in requests if r["path"].endswith("/audio/transcriptions")]) == second.plan["count"]
    assert first_count > 0


def test_job_id_is_deterministic(cfg1m, short_media):
    a, _ = run(cfg1m, str(short_media), job_id=None)
    b, _ = run(cfg1m, str(short_media), job_id=None)
    assert a.job_id == b.job_id


# ------------------------------------------------------------------ dry-run
def test_dry_run_makes_no_api_calls(base_config, media_factory, mock_server_inject):
    requests: list = []
    url = mock_server_inject(requests_log=requests)
    base_config.asr.base_url = url
    base_config.llm.base_url = url
    media = media_factory(minutes=1.0, segments=8)
    result, _ = run(base_config, str(media), dry_run=True)
    assert result.stats["dry_run"] is True
    assert result.plan["count"] > 1
    assert result.transcript.segments == []
    assert requests == []
    state = json.loads((cfg_path(base_config) / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "dry-run"


# ------------------------------------------------------------------ 失敗處理
class ExplodingASR:
    """對檔名符合 ``fail_index`` 的 chunk 拋錯的 ASR client。"""

    fail_index = "chunk_1_"

    def __init__(self, cfg, *, api_key=None, verify_ssl=True):
        self.cfg = cfg
        self.real = None

    def transcribe_file(self, path, *, stub_key=None, **kwargs):
        from summery.asr import ASRClient

        if self.real is None:
            self.real = ASRClient(self.cfg, api_key="k")
        if self.fail_index in str(path):
            raise ASRError("模擬單塊失敗")
        return self.real.transcribe_file(path, stub_key=stub_key, **kwargs)


class FailingAllASR(ExplodingASR):
    """所有 chunk 都失敗。"""

    fail_index = "chunk_"


def test_partial_result_when_one_chunk_fails(cfg1m, short_media, monkeypatch):
    monkeypatch.setattr(pipeline_mod, "ASRClient", ExplodingASR)
    result, _ = run(cfg1m, str(short_media))
    assert result.partial and len(result.errors) == 1
    assert result.errors[0]["chunk"] == 1
    assert result.transcript.segments, "部分結果仍應產出逐字稿"
    assert any("chunk 失敗" in w for w in result.warnings)
    out = cfg_dir(cfg1m) / "out"
    assert list(out.glob("*.transcript.md"))


def test_strict_mode_fails_job(cfg1m, short_media, monkeypatch):
    monkeypatch.setattr(pipeline_mod, "ASRClient", ExplodingASR)
    with pytest.raises(JobError, match="chunk"):
        run(cfg1m, str(short_media), allow_partial=False)


def test_all_chunks_failing_raises_clear_error(cfg1m, short_media, monkeypatch):
    monkeypatch.setattr(pipeline_mod, "ASRClient", FailingAllASR)
    with pytest.raises(JobError, match="全數失敗"):
        run(cfg1m, str(short_media))


def test_oversized_chunk_is_split_automatically(cfg1m, short_media):
    """chunk 超過 ASR 上傳上限時應自適應二分，而不是直接失敗。"""
    cfg1m.audio.codec = "wav"          # 未壓縮，容易超過上限
    cfg1m.chunking.target_seconds = 30.0
    # 用 min_seconds 釘住塊長：規劃階段就不會先把塊縮小到上限內，
    # 才能驗證「真的超限了」之後的執行期安全網仍會拆半。
    cfg1m.chunking.min_seconds = 30.0
    # 一個 30s/16kHz/mono wav ≈ 960KB → 上限設 600KB 強迫拆半
    cfg1m.asr.max_upload_bytes = 600_000
    result, _ = run(cfg1m, str(short_media))
    assert result.ok
    results = {p.name for p in (cfg_path(cfg1m) / "results").glob("chunk_*.json")}
    assert any(letter in name for name in results for letter in "ab"), results
    assert result.transcript.segments[0].start == pytest.approx(0.0, abs=0.1)


def test_cancelled_run_marks_state(cfg1m, short_media):
    cancel = threading.Event()
    cancel.set()
    options = JobOptions(out_dir=str(cfg_dir(cfg1m) / "out"))
    with pytest.raises(KeyboardInterrupt):
        Pipeline(cfg1m, options=options, cancel=cancel).run(str(short_media))
    state = json.loads((cfg_path(cfg1m) / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "cancelled"


# ------------------------------------------------------------------ 選項
def test_skip_summary_writes_only_transcript(cfg1m, short_media):
    result, _ = run(cfg1m, str(short_media), skip_summary=True)
    assert result.summary is None
    out = cfg_dir(cfg1m) / "out"
    assert list(out.glob("*.transcript.md")) and not list(out.glob("*.summary.md"))


def test_formats_selection_and_keep_chunks(cfg1m, short_media):
    cfg1m.output.keep_chunks = True
    result, _ = run(cfg1m, str(short_media), formats=["srt"])
    out = cfg_dir(cfg1m) / "out"
    assert list(out.glob("*.srt"))
    assert not list(out.glob("*.vtt"))
    assert list((cfg_path(cfg1m) / "chunks").glob("chunk_*")), "--keep-chunks 應保留中間檔"


def test_transcript_input_can_recompute_summary(base_config, media_factory, tmp_path):
    """只重跑摘要：從既有逐字稿 JSON 出發，不再打 ASR。"""
    media = media_factory(minutes=0.7, segments=5)
    base_config.chunking.target_seconds = 20.0
    base_config.chunking.min_seconds = 5.0
    first, _ = run(base_config, str(media))
    transcript_path = tmp_path / "existing.transcript.json"
    transcript_path.write_text(first.transcript.to_json(), encoding="utf-8")

    options = JobOptions(out_dir=str(tmp_path / "out2"), skip_asr=True,
                         transcript_input=str(transcript_path), job_id="summary-only")
    result = Pipeline(base_config, options=options).run(str(media))
    assert result.summary and result.transcript.char_count() == first.transcript.char_count()
    assert list((tmp_path / "out2").glob("*.summary.md"))


def test_run_job_helper_writes_job_json(cfg1m, short_media):
    result = run_job(str(short_media), cfg=cfg1m, options=JobOptions(out_dir=str(cfg_dir(cfg1m) / "out")))
    job_file = cfg_path(cfg1m) / "job.json"
    assert job_file.is_file()
    data = json.loads(job_file.read_text(encoding="utf-8"))
    assert data["job_id"] == result.job_id
    assert "outputs" in data and data["plan"]["count"] >= 1


class TooLargeASR:
    """模擬 Gateway／nginx 的請求體上限：超過 ``limit`` 就回 413。"""

    limit = 400_000

    def __init__(self, cfg, *, api_key=None, verify_ssl=True):
        self.cfg = cfg
        self.real = None
        self.sizes: list[int] = []

    def transcribe_file(self, path, *, stub_key=None, **kwargs):
        from summery.asr import ASRClient

        if self.real is None:
            self.real = ASRClient(self.cfg, api_key="k")
        size = Path(path).stat().st_size
        self.sizes.append(size)
        if size > self.limit:
            raise ASRError(
                "<html><body><h1>413 Request Entity Too Large</h1></body></html>",
                retryable=False,
            )
        return self.real.transcribe_file(path, stub_key=stub_key, **kwargs)


def test_http_413_makes_the_pipeline_halve_the_chunk(cfg1m, short_media, monkeypatch):
    """端點回 413 時應把同一塊二分重送，而不是讓整個工作失敗。"""
    import summery.pipeline as mod

    cfg1m.asr.max_upload_bytes = 24 * 1024 * 1024   # 規劃階段不知道端點其實更嚴
    captured: dict = {}

    class Capture(TooLargeASR):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured["client"] = self

    monkeypatch.setattr(mod, "ASRClient", Capture)
    result, _ = run(cfg1m, str(short_media))
    assert result.ok
    sizes = captured["client"].sizes
    assert max(sizes) > TooLargeASR.limit, "應至少送過一次超過上限的檔"
    assert any(s <= TooLargeASR.limit for s in sizes[1:]), "拆半後應降到上限以下"
    results = {p.name for p in (cfg_path(cfg1m) / "results").glob("chunk_*.json")}
    assert any(letter in name for name in results for letter in "ab"), results


def test_upload_budget_caps_both_target_and_max_seconds(base_config):
    """只壓 target 不夠：規劃器用 max_seconds 合併塊，兩個都要壓進上限內。"""
    base_config.audio.codec = "mp3"
    base_config.audio.bitrate = "32k"
    base_config.chunking.target_seconds = 300.0
    base_config.chunking.max_seconds = 900.0
    base_config.chunking.min_seconds = 60.0
    base_config.asr.max_upload_bytes = 900_000      # 實測這台 Gateway 只收 1MB

    capped = JobOptions().apply_to(base_config)
    assert capped.chunking.target_seconds < 300.0
    assert capped.chunking.max_seconds <= capped.chunking.target_seconds + 0.001
    per_chunk = capped.chunking.max_seconds * 32_000 / 8 * 1.1
    assert per_chunk < 900_000


def test_apply_to_does_not_mutate_the_shared_config(base_config):
    """Web 服務只有一份 Config：某個工作的「每塊長度」不可以留給下一個工作。"""
    before = (base_config.chunking.target_seconds, base_config.chunking.min_seconds)
    JobOptions(chunk_seconds=99.0, keep_chunks=True, summary_style="concise").apply_to(base_config)
    assert (base_config.chunking.target_seconds, base_config.chunking.min_seconds) == before
