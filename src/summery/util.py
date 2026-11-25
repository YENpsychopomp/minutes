"""共用工具：時間格式、原子寫檔、重試、日誌。"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")

LOG = logging.getLogger("summery")


def setup_logging(level: str | int = "INFO") -> None:
    lvl = getattr(logging, str(level).upper(), logging.INFO) if isinstance(level, str) else level
    logging.basicConfig(
        level=lvl, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s", datefmt="%H:%M:%S"
    )
    logging.getLogger("summery").setLevel(lvl)


def fmt_clock(seconds: float, *, force_hours: bool = False) -> str:
    """秒 → ``HH:MM:SS``（或 ``MM:SS``）。"""
    seconds = max(0.0, float(seconds))
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h or force_hours:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def fmt_srt_time(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_vtt_time(seconds: float) -> str:
    return fmt_srt_time(seconds).replace(",", ".")


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = float(seconds)
    if seconds < 90:
        return f"{seconds:.1f}s"
    if seconds < 5400:
        return f"{seconds / 60:.1f}min"
    return f"{seconds / 3600:.2f}hr"


def fmt_bytes(n: float | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}GB"


def atomic_write_text(path: os.PathLike[str] | str, text: str, *, encoding: str = "utf-8") -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, p)
    return p


def write_json(path: os.PathLike[str] | str, payload: Any) -> Path:
    return atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def read_json(path: os.PathLike[str] | str, default: Any = None) -> Any:
    p = Path(path)
    if not p.is_file():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


class RetryExhausted(RuntimeError):
    def __init__(self, attempts: int, last: BaseException, *, retry_after: float | None = None):
        super().__init__(f"重試 {attempts} 次後仍失敗：{last}")
        self.attempts = attempts
        self.last = last
        self.retry_after = retry_after


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    backoff: float = 2.0,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    is_retryable: Callable[[BaseException], bool] | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """指數退避重試；例外可帶 ``retry_after`` 屬性以尊重伺服器指示。"""
    attempts = max(1, int(attempts))
    last: BaseException | None = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:  # noqa: PERF203
            last = exc
            retryable = True if is_retryable is None else is_retryable(exc)
            if not retryable or i >= attempts:
                break
            suggested = getattr(exc, "retry_after", None)
            delay = float(suggested) if suggested else base_delay * (backoff ** (i - 1))
            delay = min(max_delay, delay) * random.uniform(0.75, 1.25)
            if on_retry:
                on_retry(i, exc, delay)
            sleep(delay)
    assert last is not None
    raise RetryExhausted(attempts, last)


def estimate_tokens(text: str, chars_per_token: float = 2.8) -> int:
    """粗估 tokens：中英混合逐字稿用字元數除以經驗係數。

    中文約 1 字 ≈ 0.6～1 token，英文約 4 字元 ≈ 1 token；
    2.8 是「字元數 ÷ 2.8」的折中值，寧可由呼叫端調高以確保安全。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff")
    other = len(text) - cjk
    per = max(0.5, float(chars_per_token))
    return int(math.ceil(cjk / 0.95 + other / per)) + 8


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def load_dotenv(path: str | os.PathLike[str] | None = None, *, override: bool = False) -> dict[str, str]:
    """載入 ``.env`` 風格的 KEY=VALUE 檔案（不覆蓋既有環境變數）。

    支援：``#`` 註解、空行、``export KEY=value``、單/雙引號、行尾註解（僅未加引號時）。
    回傳實際寫入 os.environ 的鍵值。
    """
    p = Path(path) if path else Path.cwd() / ".env"
    p = Path(p).expanduser()
    if not p.is_file():
        return {}
    applied: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        if override or key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


def slugify(text: str, max_len: int = 64, *, ascii_only: bool = False) -> str:
    """檔名/目錄名安全化。``ascii_only=True`` 用於會進 URL 路徑的識別碼。"""
    out = []
    for ch in text.strip():
        if ascii_only:
            if ch.isascii() and ch.isalnum():
                out.append(ch.lower())
        elif ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            out.append(ch)
        if ch in " -_":
            out.append("-")
    s = "".join(out).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return (s or "job")[:max_len]
