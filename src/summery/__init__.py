"""summery：影片/音訊 → 逐字稿 + 摘要 的處理管線。

設計重點（長影音）：
1. 以 ffmpeg 將來源正規化為 16kHz 單聲道音訊。
2. 依「靜音點對齊」把長音訊切成多個 chunk，避免把句子切成兩半。
3. 多 chunk 並行送 ASR，逐 chunk 快取 → 中斷可續跑。
4. 依時間戳合併逐字稿（含重疊區去重），輸出 md/txt/json/srt/vtt。
5. 摘要採 map-reduce 階層式彙整，不受限於單次 LLM context 上限。

Entrypoint：
    from summery import Config, run_job
"""

from __future__ import annotations

__all__ = [
    "Config",
    "ConfigError",
    "load_config",
    "run_job",
    "JobOptions",
    "JobResult",
    "JobStateStore",
    "Transcript",
    "Segment",
    "__version__",
]

__version__ = "0.1.0"

_LAZY = {
    "Config": ("summery.config", "Config"),
    "ConfigError": ("summery.config", "ConfigError"),
    "load_config": ("summery.config", "load_config"),
    "run_job": ("summery.pipeline", "run_job"),
    "JobOptions": ("summery.pipeline", "JobOptions"),
    "JobResult": ("summery.pipeline", "JobResult"),
    "JobStateStore": ("summery.pipeline", "JobStateStore"),
    "Transcript": ("summery.transcript", "Transcript"),
    "Segment": ("summery.transcript", "Segment"),
}


def __getattr__(name: str):  # PEP 562 惰性匯入
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
