"""極簡 HTTP 客戶端（stdlib only）：JSON + multipart 上傳、重試、逾時。"""

from __future__ import annotations

import json
import mimetypes
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Mapping, Sequence

_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}
#: 部分伺服端會檢查 MIME，這裡用最通用的音訊 MIME（mimetypes 對 .m4a 的回值太冷門）
_AUDIO_MIME = {
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".webm": "audio/webm",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
    ".m4b": "audio/mp4",
    ".mpga": "audio/mpeg",
}


def guess_mime(filename: str) -> str:
    from pathlib import Path as _P

    ext = _P(filename).suffix.lower()
    if ext in _AUDIO_MIME:
        return _AUDIO_MIME[ext]
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"
_BOUNDARY_PREFIX = "----summeryBoundary"


class HttpError(RuntimeError):
    """HTTP 層錯誤。``retry_after`` 供重試退避使用。"""

    def __init__(
        self, message: str, *, status: int | None = None, body: str = "", retry_after: float | None = None
    ):
        super().__init__(message)
        self.status = status
        self.body = body
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        if self.status is None:
            return True
        return self.status in _RETRYABLE_STATUS


def endpoint(base_url: str, path: str) -> str:
    """把 base_url 與端點組成完整 URL（容忍結尾 /、含 /v1、或已含端點路徑）。"""
    base = (base_url or "").strip().rstrip("/")
    path = path.strip("/")
    if base.endswith(f"/{path}"):
        return base
    if base.endswith(f"/{path}/"):
        return base.rstrip("/")
    return f"{base}/{path}"


def _ssl_context(verify: bool = True) -> ssl.SSLContext:
    if verify:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _urlopen(req: urllib.request.Request, timeout: float, *, verify: bool) -> tuple[int, bytes, Mapping[str, str]]:
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context(verify)) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read()
        except Exception:  # noqa: BLE001
            pass
        raise HttpError(
            f"HTTP {exc.code}：{body.decode('utf-8', 'replace')[:400]}",
            status=exc.code,
            body=body.decode("utf-8", "replace"),
            retry_after=_parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None),
        ) from exc
    except urllib.error.URLError as exc:
        raise HttpError(f"連線失敗：{exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise HttpError(f"請求逾時（{timeout:.0f}s）") from exc
    except (ConnectionError, ssl.SSLError) as exc:
        raise HttpError(f"連線錯誤：{exc}") from exc


def post_json(
    url: str,
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 120.0,
    verify: bool = True,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json", **dict(headers or {})},
    )
    status, raw, _ = _urlopen(req, timeout, verify=verify)
    text = raw.decode("utf-8", "replace")
    if status >= 400:
        raise HttpError(f"HTTP {status}：{text[:400]}", status=status, body=text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise HttpError(f"回應不是合法 JSON：{text[:300]}", status=status, body=text) from exc


def post_multipart(
    url: str,
    fields: Mapping[str, Any] | Sequence[tuple[str, Any]],
    *,
    file_field: str,
    filename: str,
    file_bytes: bytes | None = None,
    file_path: str | None = None,
    content_type: str | None = None,
    extra_files: Sequence[tuple[str, str, bytes]] = (),
    headers: Mapping[str, str] | None = None,
    timeout: float = 600.0,
    verify: bool = True,
) -> Any:
    """multipart/form-data POST（ASR 上傳音訊用）。"""
    boundary = f"{_BOUNDARY_PREFIX}{uuid.uuid4().hex}"
    if file_bytes is None:
        if not file_path:
            raise ValueError("需要 file_bytes 或 file_path")
        with open(file_path, "rb") as fh:
            file_bytes = fh.read()
    if content_type is None:
        content_type = guess_mime(filename)
    parts: list[bytes] = []
    for key, value in dict(fields).items() if isinstance(fields, Mapping) else fields:
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = ",".join(str(v) for v in value)
        parts.append(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n"
            ).encode("utf-8")
        )
    parts.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'
        ).encode("utf-8")
    )
    parts.append(file_bytes)
    parts.append(b"\r\n")
    for name, fname, blob in extra_files:
        parts.append(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                f'filename="{fname}"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode("utf-8")
        )
        parts.append(blob)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", **dict(headers or {})},
    )
    status, raw, _ = _urlopen(req, timeout, verify=verify)
    text = raw.decode("utf-8", "replace")
    if status >= 400:
        raise HttpError(f"HTTP {status}：{text[:400]}", status=status, body=text)
    ctype = "application/json"
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, HttpError):
        return exc.retryable
    return isinstance(exc, (HttpError, TimeoutError, ConnectionError, OSError))


def with_retries(
    fn,
    *,
    attempts: int = 3,
    backoff: float = 2.0,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    on_retry=None,
    sleep=time.sleep,
):
    last: BaseException | None = None
    for i in range(1, max(1, attempts) + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if not is_retryable(exc) or i >= attempts:
                raise
            delay = getattr(exc, "retry_after", None) or base_delay * (backoff ** (i - 1))
            delay = min(max_delay, float(delay))
            if on_retry:
                on_retry(i, exc, delay)
            sleep(delay)
    assert last is not None  # pragma: no cover
    raise last  # pragma: no cover


def auth_headers(api_key: str | None, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    headers = dict(extra or {})
    if api_key:
        headers.setdefault("Authorization", f"Bearer {api_key}")
    return headers
