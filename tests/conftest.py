"""pytest 共用 fixtures：把 src 加入 path、啟動 mock 伺服器、產生小型測試媒體。"""

from __future__ import annotations

import os

# 測試期間絕不自動載入開發者本機的 .env（避免真实金鑰/網路進入測試）
os.environ["SUMMERY_DOTENV"] = "0"
import os

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from summery.config import Config  # noqa: E402
from summery.mock_provider import start_mock_server  # noqa: E402


@pytest.fixture(scope="session")
def ffmpeg_bin() -> str:
    from shutil import which

    path = which("ffmpeg")
    if not path:
        pytest.skip("此環境沒有 ffmpeg")
    return path


@pytest.fixture(scope="session")
def mock_server():
    server, _thread, url = start_mock_server(port=0)
    yield url
    server.shutdown()


@pytest.fixture(scope="session")
def media_factory(tmp_path_factory, ffmpeg_bin):
    """產生「語音段 + 靜音」測試媒體；相同參數會快取重覆使用。"""
    cache = tmp_path_factory.mktemp("media")
    made: dict[tuple, Path] = {}

    def make(minutes: float = 1.0, segments: int = 8, video: bool = False) -> Path:
        key = (minutes, segments, video)
        if key in made:
            return made[key]
        out = cache / f"clip_{minutes:g}m_{segments}s_{'v' if video else 'a'}.mkv"
        if not out.is_file():
            seconds = minutes * 60.0
            cycle = seconds / segments
            speech = cycle * 0.85
            expr = f"0.3*sin(2*PI*(210+130*sin(2*PI*t/23))*t)*lt(mod(t\\,{cycle:.4f})\\,{speech:.4f})"
            cmd = [ffmpeg_bin, "-hide_banner", "-v", "error", "-y",
                   "-f", "lavfi", "-i", f"aevalsrc='{expr}':d={seconds:.3f}:s=16000:c=1"]
            if video:
                cmd += ["-f", "lavfi", "-i", f"testsrc=size=160x90:rate=2:duration={seconds:.3f}",
                        "-map", "0:a", "-map", "1:v", "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", "-g", "30"]
            else:
                cmd += ["-map", "0:a"]
            cmd += ["-c:a", "pcm_s16le", str(out)]
            res = subprocess.run(cmd, capture_output=True, text=True)
            assert res.returncode == 0, f"無法產生測試媒體：{res.stderr[-500:]}"
        made[key] = out
        return out

    return make


@pytest.fixture
def base_config(tmp_path, mock_server) -> Config:
    """指向 mock 伺服器的設定；工作目錄落在 tmp_path。"""
    cfg = Config()
    cfg.work_dir = str(tmp_path / ".summery_work")
    for section in (cfg.asr, cfg.llm):
        section.base_url = mock_server
        section.api_key = "test-key"
        section.timeout = 30.0
        section.max_retries = 2
        section.retry_backoff = 1.0
        section.max_concurrency = 3
    cfg.chunking.target_seconds = 20.0
    cfg.chunking.min_seconds = 5.0
    cfg.chunking.silence_snap_window = 12.0
    cfg.audio.codec = "wav"
    cfg.llm.max_concurrency = 3
    return cfg


@pytest.fixture
def mock_requests() -> list:
    """給需要檢視請求序的測試使用。"""
    return []


@pytest.fixture
def mock_server_inject():
    """需要自訂 mock 行為（延遲、失敗率）的測試自己啟動。"""
    servers = []

    def start(**kwargs):
        server, _thread, url = start_mock_server(port=0, **kwargs)
        servers.append(server)
        return url

    yield start
    for server in servers:
        server.shutdown()


def env_without_keys(monkeypatch) -> None:
    for name in ("ASR_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                 "SUMMERY_CONFIG", "SUMMERY_ASR_BASE_URL", "SUMMERY_LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def clean_env(monkeypatch):
    env_without_keys(monkeypatch)
    for name in list(os.environ):
        if name.startswith("SUMMERY_"):
            monkeypatch.delenv(name, raising=False)
    # 清完一定要重新關掉 .env 自動載入，否則 repo 根目錄的真實 .env 會混進測試
    monkeypatch.setenv("SUMMERY_DOTENV", "0")
