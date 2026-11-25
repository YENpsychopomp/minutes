"""設定載入：預設值 → TOML 設定檔 → 環境變數 → CLI 參數（後者優先）。

零外部相依（使用 stdlib ``tomllib``）。

環境變數命名：``SUMMERY_<SECTION>_<FIELD>``，例如：
    SUMMERY_ASR_BASE_URL / SUMMERY_ASR_MODEL / SUMMERY_LLM_MAX_INPUT_TOKENS
另支援常見別名：OPENAI_BASE_URL、OPENAI_API_KEY、ASR_API_KEY、LLM_API_KEY。
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping

# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------


@dataclass
class ASRConfig:
    """OpenAI 相容 /audio/transcriptions 端點設定。"""

    base_url: str = "https://api.openai.com/v1"
    model: str = "whisper-1"
    api_key: str | None = None
    api_key_env: str = "ASR_API_KEY"
    #: ISO-639-1 或 'zh'; None = 自動偵測
    language: str | None = None
    #: verbose_json（含 segments）| json | text
    response_format: str = "verbose_json"
    #: verbose_json 的時間粒度：segment / word
    timestamp_granularity: list[str] = field(default_factory=lambda: ["segment"])
    #: 部分相容服務不支援某些表單欄位，可關閉
    send_timestamp_granularity: bool = True
    send_language: bool = True
    send_temperature: bool = False
    temperature: float = 0.0
    #: 自訂附加表單欄位
    extra_params: dict = field(default_factory=dict)
    timeout: float = 600.0
    max_concurrency: int = 4
    max_retries: int = 3
    retry_backoff: float = 2.0
    #: 單一 chunk 上傳上限（多數 whisper API 為 25MB）
    max_upload_bytes: int = 24 * 1024 * 1024
    #: 除錯用：直接讀本地目錄的 JSON 而不打 API（{"0": {...}, ...} 或 {"0": "text"}）
    stub_dir: str | None = None


@dataclass
class LLMConfig:
    """OpenAI 相容 /chat/completions 端點設定。"""

    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key: str | None = None
    api_key_env: str = "LLM_API_KEY"
    temperature: float = 0.2
    max_output_tokens: int = 2000
    timeout: float = 300.0
    max_concurrency: int = 4
    max_retries: int = 3
    retry_backoff: float = 2.0
    #: 若服務不支援 response_format=json_object，設 False
    supports_response_format: bool = True
    #: 部分服務需要 max_tokens 而非 max_completion_tokens
    max_tokens_field: str = "max_tokens"
    extra_body: dict = field(default_factory=dict)
    stub_dir: str | None = None


@dataclass
class AudioConfig:
    sample_rate: int = 16000
    channels: int = 1
    #: 送給 ASR 的 chunk 編碼：m4a(aac) / opus / mp3 / wav
    codec: str = "m4a"
    bitrate: str = "24k"
    #: 抽取全軌音訊時使用的中間檔格式（wav 精度最高）
    intermediate_codec: str = "wav"
    #: 來源已是目標音訊格式且採樣率相符時，仍重新編碼以統一規格
    force_reencode: bool = True
    #: chunk 來源："auto" = 中間 wav 太大時直接從原始檔切；"wav"；"source"
    chunk_source: str = "auto"
    #: "auto" 判斷中間 wav 是否過大的門檻（bytes）
    chunk_source_auto_threshold_bytes: int = 768 * 1024 * 1024


@dataclass
class ChunkConfig:
    #: 期望的單塊長度（秒）
    target_seconds: float = 600.0
    #: 上限：超過會往內縮到靜音點
    max_seconds: float = 900.0
    #: 下限：最後一塊太短且可併入前塊時就併入
    min_seconds: float = 60.0
    #: 往兩側找靜音點的視窗（秒）
    silence_snap_window: float = 240.0
    #: 視為可用切點的靜音最短長度
    silence_min_duration: float = 0.35
    #: silencedetect 的雜訊門檻（dB）； Podcast/演講 -30 左右，環境噪音高可調 -25
    silence_noise_db: float = -30.0
    #: 相鄰 chunk 重疊秒數（用於減少跨句斷裂；合併時依時間戳去重）
    overlap_seconds: float = 2.0
    #: 關閉靜音掃描（純固定長度切分，較快但可能切到句子）
    skip_silence_scan: bool = False
    #: 總時長低於此值只切一塊
    single_chunk_under: float = 0.0


@dataclass
class SummarizeConfig:
    #: 摘要輸出語言；'auto' = 追隨逐字稿語言
    language: str = "zh-TW"
    #: comprehensive / standard / concise / bullets
    style: str = "standard"
    #: 送給 LLM 的單次最大輸入 tokens（安全上限）
    max_input_tokens: int = 12000
    #: map 階段單塊目標 tokens
    map_chunk_tokens: int = 7000
    #: reduce 階段每次彙整的節點數
    reduce_fan_in: int = 6
    #: 字元數 → tokens 的換算除數（中英混合約 2.5～3.5）
    chars_per_token: float = 2.8
    #: 是否輸出章節摘要（含時間戳）
    chapters: bool = True
    #: 章節摘要的最大章節數
    max_chapters: int = 12
    #: 每個章節摘要塊的目標 tokens
    chapter_block_tokens: int = 4000
    #: 傳給 LLM 的逐字稿每段前綴時間戳（有利章節對齊）
    timestamp_lines: bool = True
    #: 是否同時抽取關鍵字/實體
    keywords: bool = True
    #: 自訂摘要指示（附加在系統提示後）
    custom_instruction: str | None = None


@dataclass
class OutputConfig:
    #: 要輸出的逐字稿格式
    formats: list[str] = field(default_factory=lambda: ["md", "json", "txt", "srt", "vtt"])
    #: 段落斷句的最大靜音（秒）
    paragraph_gap: float = 0.9
    #: 單段最大字元數
    paragraph_max_chars: int = 180
    #: 保留 chunk 中間檔
    keep_chunks: bool = False
    #: 輸出檔名基底（None = 用來源檔名）
    stem: str | None = None


@dataclass
class Config:
    asr: ASRConfig = field(default_factory=ASRConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    chunking: ChunkConfig = field(default_factory=ChunkConfig)
    summarize: SummarizeConfig = field(default_factory=SummarizeConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    #: 工作目錄（放中間檔與快取）
    work_dir: str = ".summery_work"
    #: ffmpeg / ffprobe
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    log_level: str = "INFO"

    # -- API key 解析 ----------------------------------------------------
    def resolve_api_key(self, section: str) -> str | None:
        """依序：直接設定 → 指定環境變數 → 常見別名。"""
        sec: Any = getattr(self, section)
        if sec.api_key:
            return sec.api_key
        for name in (sec.api_key_env, "OPENAI_API_KEY"):
            if name and os.environ.get(name):
                return os.environ[name]
        return None

    def require_api_key(self, section: str) -> str | None:
        key = self.resolve_api_key(section)
        sec = getattr(self, section)
        if key is None and not sec.stub_dir:
            raise ConfigError(
                f"缺少 {section} 的 API key：請設定環境變數 {sec.api_key_env}"
                "（或 OPENAI_API_KEY），或在設定檔中設定 "
                f"[{section}] api_key / stub_dir。"
            )
        return key

    def redacted(self) -> dict:
        """供 Web UI 顯示的安全設定快照。"""
        d = dataclasses.asdict(self)
        for sec in ("asr", "llm"):
            if d[sec].get("api_key"):
                d[sec]["api_key"] = "***"
        return d


class ConfigError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# loader
# --------------------------------------------------------------------------

LOG = logging.getLogger("summery")

_SECTIONS = ("asr", "llm", "audio", "chunking", "summarize", "output")

_ALIASES: dict[tuple[str, str], str] = {
    ("asr", "base_url"): "OPENAI_BASE_URL",
    ("asr", "model"): "ASR_MODEL",
    ("llm", "base_url"): "OPENAI_BASE_URL",
    ("llm", "model"): "LLM_MODEL",
}


def _coerce(value: Any, typ: str | None) -> Any:
    """字串/JSON 值 → 欄位宣告型別。"""
    if typ == "str":
        return str(value)
    if typ == "int":
        return int(float(value))
    if typ == "float":
        return float(value)
    if typ == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "y")
        return bool(value)
    if typ == "list[str]":
        if isinstance(value, str):
            return [v.strip() for v in value.replace(";", ",").split(",") if v.strip()]
        return [str(v) for v in (value or [])]
    if typ == "dict":
        if isinstance(value, str):
            out: dict[str, str] = {}
            for pair in value.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    out[k.strip()] = v.strip()
            return out
        return dict(value or {})
    return value


_TYPE_NAMES = {str: "str", int: "int", float: "float", bool: "bool"}


def _field_type_name(f: dataclasses.Field) -> str | None:
    declared = f.type
    if isinstance(declared, type):
        return _TYPE_NAMES.get(declared)
    text = str(declared).strip()
    # 處理 'str | None' 等 Optional 標註
    if "|" in text:
        parts = [x.strip() for x in text.split("|") if x.strip() and x.strip() != "None"]
        if len(parts) == 1:
            text = parts[0]
    for kind, name in (("list[str]", "list[str]"), ("dict", "dict")):
        if text.startswith(kind):
            return name
    for kind, name in (("str", "str"), ("int", "int"), ("float", "float"), ("bool", "bool")):
        if text == kind:
            return name
    return None


def _fill(section: Any, values: Mapping[str, Any], *, source: str) -> None:
    fields = {f.name: f for f in dataclasses.fields(section)}
    for key, value in values.items():
        norm = key.replace("-", "_")
        if norm not in fields:
            raise ConfigError(f"未知設定 [{source}].{norm}（來源：{source}）")
        annotation = str(getattr(fields[norm], "type", ""))
        optional = "None" in annotation or "Optional" in annotation
        if optional and isinstance(value, str) and not value.strip():
            setattr(section, norm, None)  # 設定寫 `language = ""` 等同「未設定 / 自動」
            continue
        try:
            setattr(section, norm, _coerce(value, _field_type_name(fields[norm])))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"設定值不合法 [{source}].{norm}={value!r}：{exc}") from exc


_TOP_KEYS = {"WORK_DIR": "work_dir", "FFMPEG_BIN": "ffmpeg_bin", "FFPROBE_BIN": "ffprobe_bin", "LOG_LEVEL": "log_level"}


def _env_overrides(env: Mapping[str, str]) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """解析環境變數。

    支援兩種寫法（大小寫不敏感）：
        SUMMERY_ASR_BASE_URL / SUMMERY_CHUNKING_TARGET_SECONDS
        SUMMERY__ASR__BASE_URL
    另有頂層設定：SUMMERY_WORK_DIR、SUMMERY_FFMPEG_BIN、SUMMERY_LOG_LEVEL。
    """
    out: dict[str, dict[str, str]] = {}
    top: dict[str, str] = {}
    for name, value in env.items():
        if value is None or not name.startswith("SUMMERY_") or name == "SUMMERY_CONFIG":
            continue
        rest = re.sub(r"_+", "_", name[len("SUMMERY_") :]).upper().strip("_")
        if rest in _TOP_KEYS:
            top[_TOP_KEYS[rest]] = value
            continue
        for sec in _SECTIONS:
            prefix = sec.upper() + "_"
            if rest.startswith(prefix):
                key = rest[len(prefix) :].lower()
                if key:
                    out.setdefault(sec, {}).setdefault(key, value)
                break
    for (sec, key), alias in _ALIASES.items():
        if env.get(alias):
            out.setdefault(sec, {}).setdefault(key, env[alias])
    return out, top


def load_config(
    config_file: str | os.PathLike[str] | None = None,
    *,
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    """建立設定。

    Args:
        config_file: TOML 檔；未給則嘗試 ``SUMMERY_CONFIG`` 環境變數。
        overrides: ``{"asr": {"model": "..."}}`` 形式的 CLI 覆蓋。
        env: 環境變數（預設 ``os.environ``）。
    """
    env = os.environ if env is None else env
    cfg = Config()

    path = config_file or env.get("SUMMERY_CONFIG")
    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            if config_file:
                raise ConfigError(f"找不到設定檔：{p}")
            # 來自環境變數（常寫在 .env 裡當範例）→ 警告即可，不要讓所有指令都掛掉
            LOG.warning("SUMMERY_CONFIG 指向的檔案不存在，已忽略：%s", p)
        else:
            with p.open("rb") as fh:
                data = tomllib.load(fh)
            for sec in _SECTIONS:
                if sec in data:
                    _fill(getattr(cfg, sec), data[sec], source=sec)
            for key in ("work_dir", "ffmpeg_bin", "ffprobe_bin", "log_level"):
                if key in data:
                    setattr(cfg, key, _coerce(data[key], str))

    env_sections, env_top = _env_overrides(env)
    for key, value in env_top.items():
        setattr(cfg, key, str(value))
    for sec, values in env_sections.items():
        _fill(getattr(cfg, sec), values, source=sec)

    if overrides:
        for sec, values in overrides.items():
            if sec in _SECTIONS and values:
                _fill(getattr(cfg, sec), values, source=sec)
            elif values:
                raise ConfigError(f"未知設定區塊：{sec}")

    # 將環境中的 API key 解析進設定物件，讓 Config 本身自給自足
    for section in ("asr", "llm"):
        sec = getattr(cfg, section)
        if sec.api_key:
            continue
        for name in (sec.api_key_env, "OPENAI_API_KEY"):
            if name and env.get(name):
                sec.api_key = env[name]
                break

    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    ch = cfg.chunking
    if ch.target_seconds <= 0:
        raise ConfigError("chunking.target_seconds 必須 > 0")
    if ch.max_seconds < ch.target_seconds:
        ch.max_seconds = ch.target_seconds
    if ch.overlap_seconds < 0:
        raise ConfigError("chunking.overlap_seconds 不可為負")
    if ch.overlap_seconds >= ch.target_seconds * 0.25:
        raise ConfigError("chunking.overlap_seconds 過大（不可超過 target_seconds 的 25%）")
    if cfg.audio.codec not in ("m4a", "opus", "mp3", "wav", "ogg", "flac", "webm"):
        raise ConfigError(f"不支援的 audio.codec：{cfg.audio.codec}")
    if cfg.output.formats:
        bad = set(cfg.output.formats) - {"md", "json", "txt", "srt", "vtt"}
        if bad:
            raise ConfigError(f"未知的 output.formats：{sorted(bad)}")
    if cfg.audio.chunk_source not in ("auto", "wav", "source"):
        raise ConfigError('audio.chunk_source 只能是 "auto" / "wav" / "source"')
    if cfg.summarize.chars_per_token <= 0:
        raise ConfigError("summarize.chars_per_token 必須 > 0")
    for sec in ("asr", "llm"):
        s = getattr(cfg, sec)
        if not s.stub_dir and not str(s.base_url).startswith(("http://", "https://")):
            raise ConfigError(f"{sec}.base_url 必須是 http(s) URL：{s.base_url!r}")


def apply_cli_overrides(
    cfg: Config, section: str, pairs: Iterable[str] | None
) -> None:
    """``--set asr.model=xxx --set chunking.target_seconds=300`` 風格的覆蓋。"""
    if section not in _SECTIONS:
        raise ConfigError(f"未知設定區塊：{section}（可用：{', '.join(_SECTIONS)}）")
    if not pairs:
        return
    bucket: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ConfigError(f"--set 需要 key=value 格式：{pair!r}")
        key, value = pair.split("=", 1)
        parts = key.strip().replace("-", "_").split(".")
        if len(parts) == 2 and parts[0] == section:
            bucket[parts[1]] = value
        elif len(parts) == 2 and parts[0] in _SECTIONS:
            _fill(getattr(cfg, parts[0]), {parts[1]: value}, source=parts[0])
        elif len(parts) == 1:
            bucket[key.strip()] = value
        else:
            raise ConfigError(f"無法解析設定路徑：{pair!r}")
    if bucket:
        _fill(getattr(cfg, section), bucket, source=section)
    _validate(cfg)
