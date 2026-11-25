"""ASR 客戶端（OpenAI 相容 ``/audio/transcriptions``）。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import _http
from .config import ASRConfig
from .transcript import Segment, parse_asr_payload
from .util import LOG, fmt_bytes


class ASRError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable

    @property
    def retry_after(self) -> float | None:  # 供 _http.with_retries 使用
        inner = self.__cause__
        return getattr(inner, "retry_after", None)

    @property
    def too_large(self) -> bool:
        """TRUE 代表是「請求體太大」（HTTP 413）：縮小同一塊重送有意義，重複送同樣的檔沒意義。"""
        inner = self.__cause__
        if getattr(inner, "status", None) == 413:
            return True
        text = str(self).lower()
        return "413" in text and "too large" in text


@dataclass
class ASROutcome:
    segments: list[Segment] = field(default_factory=list)
    language: str | None = None
    text: str = ""
    elapsed: float = 0.0
    bytes_sent: int = 0
    raw_meta: dict = field(default_factory=dict)


def _codec_hint(exc: _http.HttpError, path: Path) -> str:
    """400「音訊不受支援」是最常見的相容性問題，直接給出可行的修法。"""
    if exc.status != 400:
        return ""
    body = (exc.body or "").lower()
    if not any(word in body for word in ("audio file", "unsupported audio", "invalid audio", "invalid or unsupported")):
        return ""
    return (f"｜這通常代表該 ASR 服務不接受 .{path.suffix.lstrip('.')} 格式，"
            "請把 audio.codec 改成「mp3」或「wav」後重試")


def _size_hint(exc: _http.HttpError) -> str:
    """413 幾乎都是 Gateway／nginx 的請求體上限（常見預設 1MB），直接告訴使用者怎麼調。"""
    if exc.status != 413:
        return ""
    return ("｜上傳大小被 Gateway 擋下（常見預設上限 1MB）。"
            "請調小 chunking.target_seconds（例如 180）或降低 audio.bitrate，"
            "也可把 asr.max_upload_bytes 設為略低於該上限（例如 900000）讓規劃自動縮塊")


class ASRClient:
    """封裝單一 ASR 端點；執行緒安全（每次請求獨立）。"""

    def __init__(self, cfg: ASRConfig, *, api_key: str | None = None, verify_ssl: bool = True):
        self.cfg = cfg
        self.api_key = api_key
        self.verify_ssl = verify_ssl
        self.url = _http.endpoint(cfg.base_url, "audio/transcriptions")
        if cfg.stub_dir:
            self._stub = Path(cfg.stub_dir).expanduser()
            if not self._stub.is_dir():
                raise ASRError(f"asr.stub_dir 不存在：{self._stub}", retryable=False)
        elif not api_key and not cfg.stub_dir:
            raise ASRError("缺少 ASR API key", retryable=False)

    # ------------------------------------------------------------------
    def transcribe_file(
        self,
        path: str | Path,
        *,
        stub_key: str | None = None,
        language: str | None = None,
        prompt: str | None = None,
    ) -> ASROutcome:
        p = Path(path)
        if self.cfg.stub_dir:
            payload = self._load_stub(stub_key or p.stem, p)
            start = time.perf_counter()
        else:
            size = p.stat().st_size
            if self.cfg.max_upload_bytes and size > self.cfg.max_upload_bytes:
                raise ASRError(
                    f"chunk 檔案 {fmt_bytes(size)} 超過上傳上限 "
                    f"{fmt_bytes(self.cfg.max_upload_bytes)}，請調低 chunking.target_seconds",
                    retryable=False,
                )
            fields: list[tuple[str, Any]] = [
                ("model", self.cfg.model),
                ("response_format", self.cfg.response_format),
            ]
            if self.cfg.send_language and (language or self.cfg.language):
                fields.append(("language", language or self.cfg.language))
            if self.cfg.response_format == "verbose_json" and self.cfg.send_timestamp_granularity:
                # OpenAI 規格：重覆的 timestamp_granularity[] 表單欄位
                for gran in self.cfg.timestamp_granularity or ["segment"]:
                    fields.append(("timestamp_granularity[]", gran))
            if self.cfg.send_temperature:
                fields.append(("temperature", self.cfg.temperature))
            if prompt:
                fields.append(("prompt", prompt[:224]))  # whisper initial_prompt 上限約 224 字
            for key, value in (self.cfg.extra_params or {}).items():
                fields.append((key, value))
            start = time.perf_counter()
            try:
                payload = _http.with_retries(
                    lambda: _http.post_multipart(
                        self.url,
                        fields,
                        file_field="file",
                        filename=p.name,
                        file_path=str(p),
                        timeout=self.cfg.timeout,
                        headers=_http.auth_headers(self.api_key),
                        verify=self.verify_ssl,
                    ),
                    attempts=self.cfg.max_retries,
                    backoff=self.cfg.retry_backoff,
                    on_retry=lambda i, exc, delay: LOG.warning(
                        "ASR 重試 %d/%d：%s（%.1fs 後）", i, self.cfg.max_retries, exc, delay
                    ),
                )
            except _http.HttpError as exc:
                raise ASRError(str(exc) + _codec_hint(exc, p) + _size_hint(exc), retryable=exc.retryable) from exc
        elapsed = time.perf_counter() - start
        try:
            segments, detected = parse_asr_payload(payload)
        except ValueError as exc:
            raise ASRError(f"無法解析 ASR 回應：{exc}", retryable=False) from exc
        raw_meta = {k: v for k, v in (payload or {}).items() if k in {"language", "duration", "model"}}
        if isinstance(payload, dict) and payload.get("usage"):
            raw_meta["usage"] = payload["usage"]
        return ASROutcome(
            segments=segments,
            language=detected or self.cfg.language,
            text=" ".join(s.text for s in segments).strip(),
            elapsed=elapsed,
            bytes_sent=p.stat().st_size if p.is_file() else 0,
            raw_meta=raw_meta,
        )

    def _load_stub(self, key: str, path: Path) -> Any:
        """離線模式：讀 ``<stub_dir>/<key>.json`` 或 ``manifest.json``。"""
        for candidate in (self._stub / f"{key}.json", self._stub / f"{path.stem}.json"):
            if candidate.is_file():
                return json.loads(candidate.read_text(encoding="utf-8"))
        manifest = self._stub / "manifest.json"
        if manifest.is_file():
            data = json.loads(manifest.read_text(encoding="utf-8"))
            for probe_key in (key, path.stem, "default"):
                if probe_key in data:
                    return data[probe_key]
            if "segments" in data or "text" in data:
                return data
        raise ASRError(f"找不到 stub 資料：{key}（目錄 {self._stub}）", retryable=False)


def chunk_to_stub_key(index: int) -> str:
    return f"chunk_{index:05d}"
