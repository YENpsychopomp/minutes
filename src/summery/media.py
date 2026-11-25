"""ffmpeg / ffprobe 包裝：媒體探測、音訊正規化、靜音偵測、chunk 切割。"""

from __future__ import annotations

import dataclasses
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .config import AudioConfig, Config
from .util import LOG, fmt_bytes

ProgressFn = Callable[[str, float], None]  # (stage, 0.0~1.0)


class MediaError(RuntimeError):
    pass


@dataclass
class AudioStream:
    index: int
    codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    language: str | None = None
    title: str | None = None
    duration: float | None = None


@dataclass
class MediaInfo:
    path: str
    duration: float | None
    format_name: str | None = None
    size_bytes: int | None = None
    has_video: bool = False
    bit_rate: int | None = None
    audio_streams: list[AudioStream] = field(default_factory=list)

    @property
    def is_audio_only(self) -> bool:
        return not self.has_video


@dataclass
class Silence:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def mid(self) -> float:
        return (self.start + self.end) / 2.0


# --------------------------------------------------------------------------
# 程序執行
# --------------------------------------------------------------------------


def _require_bin(binary: str) -> str:
    found = shutil.which(binary)
    if not found:
        raise MediaError(
            f">>>找不到可執行檔 {binary!r}。請安裝 ffmpeg（macOS: brew install ffmpeg）。"
        )
    return found


def _run(cmd: Sequence[str], *, timeout: float | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            list(cmd), capture_output=True, text=True, errors="replace", timeout=timeout
        )
    except FileNotFoundError as exc:
        raise MediaError(f"無法執行 {cmd[0]!r}：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"執行逾時（{timeout}s）：{' '.join(cmd[:4])}…") from exc


def probe(path: str | Path, *, ffprobe_bin: str = "ffprobe") -> MediaInfo:
    """讀取媒體資訊（支援影片與音訊檔）。"""
    p = Path(path).expanduser()
    if not p.is_file():
        raise MediaError(f"找不到輸入檔：{p}")
    binary = _require_bin(ffprobe_bin)
    cmd = [
        binary, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(p),
    ]
    res = _run(cmd, timeout=120)
    if res.returncode != 0:
        raise MediaError(f"ffprobe 失敗（{p.name}）：{res.stderr.strip()[:500]}")
    import json as _json

    data = _json.loads(res.stdout or "{}")
    streams = data.get("streams", []) or []
    fmt = data.get("format", {}) or {}
    audio = [
        AudioStream(
            index=int(s.get("index", -1)),
            codec=s.get("codec_name"),
            sample_rate=int(s["sample_rate"]) if s.get("sample_rate") else None,
            channels=int(s["channels"]) if s.get("channels") else None,
            language=(s.get("tags") or {}).get("language"),
            title=(s.get("tags") or {}).get("title"),
            duration=float(s["duration"]) if s.get("duration") else None,
        )
        for s in streams
        if s.get("codec_type") == "audio"
    ]
    duration = None
    for candidate in (fmt.get("duration"), *(a.duration for a in audio)):
        if candidate:
            try:
                duration = float(candidate)
                break
            except (TypeError, ValueError):
                continue
    info = MediaInfo(
        path=str(p),
        duration=duration,
        format_name=fmt.get("format_name"),
        size_bytes=int(fmt["size"]) if fmt.get("size") else p.stat().st_size,
        has_video=any(s.get("codec_type") == "video" for s in streams),
        bit_rate=int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        audio_streams=audio,
    )
    if not audio:
        raise MediaError(f"來源沒有音軌，無法轉錄：{p.name}")
    return info


# --------------------------------------------------------------------------
# 音訊正規化
# --------------------------------------------------------------------------

_CODEC_ARGS: dict[str, tuple[list[str], str]] = {
    "wav": (["-c:a", "pcm_s16le"], "wav"),
    "m4a": (["-c:a", "aac", "-b:a", "{bitrate}", "-movflags", "+faststart"], "m4a"),
    "mp3": (["-c:a", "libmp3lame", "-b:a", "{bitrate}"], "mp3"),
    "opus": (["-c:a", "libopus", "-b:a", "{bitrate}"], "ogg"),
    "ogg": (["-c:a", "libopus", "-b:a", "{bitrate}"], "ogg"),
    "webm": (["-c:a", "libopus", "-b:a", "{bitrate}"], "webm"),
    "flac": (["-c:a", "flac"], "flac"),
}


def codec_spec(codec: str, bitrate: str) -> tuple[list[str], str]:
    if codec not in _CODEC_ARGS:
        raise MediaError(f"不支援的音訊編碼：{codec}")
    args, ext = _CODEC_ARGS[codec]
    return [a.replace("{bitrate}", bitrate) for a in args], ext


def _audio_map_args(stream_index: int | None) -> list[str]:
    if stream_index is None:
        return ["-map", "0:a:0"]
    return ["-map", f"0:{stream_index}"]


def normalize_audio(
    source: str | Path,
    out_path: str | Path,
    cfg: AudioConfig,
    *,
    ffmpeg_bin: str = "ffmpeg",
    audio_stream: int | None = None,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    total_duration: float | None = None,
) -> Path:
    """抽出第一軌（或指定軌）為單聲道、目標取樣率的 PCM wav。"""
    binary = _require_bin(ffmpeg_bin)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    args, ext = codec_spec(cfg.intermediate_codec, cfg.bitrate)
    cmd = [
        binary, "-hide_banner", "-nostdin", "-y", "-v", "error",
        "-progress", "pipe:1", "-nostats",
        "-i", str(source),
        "-vn", "-sn", "-dn",
        *_audio_map_args(audio_stream),
        "-ac", str(cfg.channels), "-ar", str(cfg.sample_rate),
        *args,
        str(out),
    ]
    _popen_with_progress(cmd, stage="audio", progress=progress, cancel=cancel, total=total_duration)
    if not out.is_file() or out.stat().st_size < 1024:
        raise MediaError(f"音訊抽取失敗或輸出過小：{out}")
    return out


def _popen_with_progress(
    cmd: Sequence[str],
    *,
    stage: str,
    progress: ProgressFn | None,
    cancel: threading.Event | None,
    total: float | None,
) -> str:
    """執行 ffmpeg 並解析 ``-progress pipe:1``；回傳 stderr 內容。"""
    proc = subprocess.Popen(
        list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace"
    )
    stderr_chunks: list[str] = []

    def drain_err() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_chunks.append(line)

    err_thread = threading.Thread(target=drain_err, daemon=True)
    err_thread.start()
    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            line = raw.strip()
            if cancel is not None and cancel.is_set():
                proc.terminate()
                raise KeyboardInterrupt("cancelled")
            if progress is None or total in (None, 0) or "=" not in line:
                continue
            key, _, value = line.partition("=")
            seconds = None
            if key in ("out_time_us", "out_time_ms"):
                try:
                    # out_time_ms 在多數版本其實也是 microsecond
                    seconds = int(value) / 1_000_000
                except ValueError:
                    seconds = None
            elif key == "out_time":
                seconds = _parse_hms(value)
            if seconds is not None:
                progress(stage, min(1.0, seconds / total))
    finally:
        proc.wait()
        err_thread.join(timeout=5)
    stderr = "".join(stderr_chunks)
    if proc.returncode != 0:
        tail = stderr.strip().splitlines()[-6:]
        raise MediaError(f"ffmpeg 失敗（exit {proc.returncode}）：" + " / ".join(tail)[:600])
    return stderr


def _parse_hms(value: str) -> float | None:
    m = re.match(r"(\d+):(\d{2}):(\d{2})(?:\.(\d+))?$", value.strip())
    if not m:
        return None
    h, mi, s, frac = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + (float("0." + frac) if frac else 0.0)


_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)")


def detect_silence(
    audio_path: str | Path,
    *,
    noise_db: float = -30.0,
    min_duration: float = 0.35,
    ffmpeg_bin: str = "ffmpeg",
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    total_duration: float | None = None,
) -> list[Silence]:
    """以 silencedetect 找出靜音區間（長音訊約 20~60 倍即時速度）。"""
    binary = _require_bin(ffmpeg_bin)
    cmd = [
        binary, "-hide_banner", "-nostdin", "-v", "info", "-i", str(audio_path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}",
        "-map", "0:a:0", "-f", "null", "-",
    ]
    stderr = _popen_with_progress(
        cmd, stage="silence", progress=progress, cancel=cancel, total=total_duration
    )
    silences: list[Silence] = []
    pending: float | None = None
    for line in stderr.splitlines():
        start = _SILENCE_START.search(line)
        end = _SILENCE_END.search(line)
        if start and not end:
            pending = float(start.group(1))
        elif end:
            e = float(end.group(1))
            d = float(end.group(2))
            silences.append(Silence(start=pending if pending is not None else max(0.0, e - d), end=e))
            pending = None
    silences.sort(key=lambda s: s.start)
    return silences


# --------------------------------------------------------------------------
# chunk 切割
# --------------------------------------------------------------------------


def extract_chunk(
    source: str | Path,
    start: float,
    duration: float,
    out_path: str | Path,
    cfg: AudioConfig,
    *,
    ffmpeg_bin: str = "ffmpeg",
    audio_stream: int | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    """從 ``source`` 切出 [start, start+duration) 並編碼成送 ASR 用的格式。

    使用輸入端 ``-ss``（快速且對 PCM 精確）+ 輸出端 ``-t``，避免全檔重播。
    """
    binary = _require_bin(ffmpeg_bin)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    args, ext = codec_spec(cfg.codec, cfg.bitrate)
    if out.suffix.lower().lstrip(".") != ext:
        out = out.with_suffix(f".{ext}")
    cmd = [
        binary, "-hide_banner", "-nostdin", "-y", "-v", "error",
        "-ss", f"{max(0.0, start):.3f}", "-i", str(source),
        "-t", f"{max(0.05, duration):.3f}",
    ]
    if audio_stream is not None:
        cmd += _audio_map_args(audio_stream)
    cmd += ["-vn", "-sn", "-dn", "-ac", str(cfg.channels), "-ar", str(cfg.sample_rate), *args, str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if cancel is not None and cancel.is_set():
        raise KeyboardInterrupt("cancelled")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-4:]
        raise MediaError(f"切割 chunk 失敗（{start:.1f}s）：" + " / ".join(tail)[:400])
    if not out.is_file() or out.stat().st_size < 256:
        raise MediaError(f"chunk 輸出異常（起點 {start:.1f}s）")
    return out


def estimate_wav_bytes(duration: float | None, cfg: AudioConfig) -> float | None:
    if duration is None:
        return None
    return duration * cfg.sample_rate * 2 * max(1, cfg.channels)


def describe(info: MediaInfo, cfg: Config) -> str:
    streams = ", ".join(
        f"#{a.index} {a.codec or '?'} {a.sample_rate or '?'}Hz {a.channels or '?'}ch"
        + (f" [{a.language}]" if a.language else "")
        for a in info.audio_streams
    )
    return (
        f"{Path(info.path).name}: {info.format_name or '?'} "
        f"{'video+audio' if info.has_video else 'audio-only'} "
        f"duration={info.duration or 0:.1f}s size={fmt_bytes(info.size_bytes)} audio[{streams}]"
    )
