"""長音訊切塊計畫：以靜音點對齊 chunk 邊界。

核心想法：
* 先以 ``target_seconds`` 排出理想邊界。
* 每個邊界在 ``silence_snap_window`` 內找「最近的可用靜音點」調整，
  避免把一句話切成兩半（這是長影音逐字稿品質的關鍵）。
* 邊界受 ``min_seconds`` / ``max_seconds`` 約束，避免出現過短或超限的塊。
* 重疊 ``overlap_seconds`` 採「回溯重疊」：每塊往前多讀幾秒當作語意上下文，
  合併時把完全落在重疊區內的 segment 交由前一塊負責（見 ``merge_chunk_results``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .config import ChunkConfig
from .media import Silence
from .util import LOG, clamp


@dataclass
class ChunkPlan:
    index: int
    start: float
    end: float
    #: 實際送給 ASR 的讀取視窗：[read_start, read_end)
    #: 採用「回溯重疊」：read_start = start - overlap（第一塊除外），read_end = end
    read_start: float = 0.0
    read_end: float = 0.0
    #: 邊界由何決定：silence / exact / head / tail
    boundary: str = "exact"
    #: 使用的靜音長度（若有）
    silence_duration: float = 0.0
    #: 自適應二分產生的子塊後綴（"a"/"b"/"aa"…）；原始塊為空字串
    variant: str = ""

    @property
    def key(self) -> str:
        """快取與檔案命名用的唯一鍵。"""
        return f"{self.index}{self.variant or ''}"

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def read_duration(self) -> float:
        return max(0.0, self.read_end - self.read_start)

    @property
    def head_overlap(self) -> float:
        """本塊往前多讀的秒數（這些內容由前一塊負責，合併時去重）。"""
        return max(0.0, self.start - self.read_start)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "read_start": round(self.read_start, 3),
            "read_end": round(self.read_end, 3),
            "duration": round(self.duration, 3),
            "boundary": self.boundary,
            "variant": self.variant,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ChunkPlan":
        return cls(
            index=int(d["index"]),
            start=float(d["start"]),
            end=float(d["end"]),
            read_start=float(d.get("read_start", d["start"])),
            read_end=float(d.get("read_end", d["end"])),
            boundary=d.get("boundary", "exact"),
            silence_duration=float(d.get("silence_duration", 0.0)),
            variant=str(d.get("variant", "")),
        )


@dataclass
class Plan:
    duration: float
    chunks: list[ChunkPlan] = field(default_factory=list)
    silence_count: int = 0
    snapped: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.chunks)

    def to_dict(self) -> dict:
        return {
            "duration": round(self.duration, 3),
            "count": self.count,
            "silence_count": self.silence_count,
            "snapped": self.snapped,
            "warnings": self.warnings,
            "chunks": [c.to_dict() for c in self.chunks],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        return cls(
            duration=float(d.get("duration", 0.0)),
            chunks=[ChunkPlan.from_dict(c) for c in d.get("chunks", [])],
            silence_count=int(d.get("silence_count", 0)),
            snapped=int(d.get("snapped", 0)),
            warnings=list(d.get("warnings", [])),
        )

    def coverage_check(self) -> list[str]:
        """驗證切塊無縫且無重疊（重疊只允許在 read_end 上）。"""
        problems: list[str] = []
        if not self.chunks:
            return ["切塊結果為空"]
        if self.chunks[0].start > 0.01:
            problems.append(f"起點未從 0 開始：{self.chunks[0].start}")
        for prev, cur in zip(self.chunks, self.chunks[1:]):
            if abs(prev.end - cur.start) > 0.01:
                problems.append(f"邊界不連續：{prev.end:.3f} → {cur.start:.3f}")
            if cur.start < prev.start:
                problems.append(f"順序異常：chunk {cur.index} 起點倒退")
            if cur.read_start > cur.start + 0.01:
                problems.append(f"chunk {cur.index} read_start 晚於 start")
        if self.duration and self.chunks[-1].end < self.duration - 0.05:
            problems.append(f"尾段未涵蓋：{self.chunks[-1].end:.3f} < {self.duration:.3f}")
        return problems


def _candidates(
    silences: Sequence[Silence], low: float, high: float, min_duration: float
) -> list[Silence]:
    return [
        s
        for s in silences
        if s.duration >= min_duration and s.mid >= low and s.mid <= high and s.mid > 0.05
    ]


def best_cut_point(
    low: float,
    high: float,
    target: float,
    silences: Sequence[Silence],
    cfg: ChunkConfig,
    *,
    min_chunk_seconds: float | None = None,
) -> tuple[float, float, str]:
    """在 [low, high] 內找最接近 ``target`` 的切點。

    Returns: (cut_time, silence_duration, how)  how ∈ {"silence", "exact"}
    """
    lo, hi = max(0.0, low), high
    if hi <= lo:
        return clamp(target, lo, hi), 0.0, "exact"
    window = max(0.0, cfg.silence_snap_window)
    w_lo, w_hi = max(lo, target - window), min(hi, target + window)
    for min_dur in (cfg.silence_min_duration, max(0.1, cfg.silence_min_duration / 3)):
        cands = _candidates(silences, w_lo, w_hi, min_dur)
        if min_chunk_seconds and cands:
            filtered = [c for c in cands if (c.mid - lo) >= min_chunk_seconds or hi - c.mid < min_chunk_seconds]
            cands = filtered or cands
        if cands:
            # 離目標最近優先；同距離時優先較早的切點（塊較不會超長）
            best = min(cands, key=lambda s: (abs(s.mid - target), s.mid))
            return float(best.mid), float(best.duration), "silence"
        window *= 1.6
        w_lo, w_hi = max(lo, target - window), min(hi, target + window)
    return clamp(target, lo, hi), 0.0, "exact"


def plan_chunks(
    duration: float,
    silences: Sequence[Silence],
    cfg: ChunkConfig,
    *,
    overlap_seconds: float | None = None,
) -> Plan:
    """產生切塊計畫。``duration`` 必須 > 0。"""
    duration = float(duration)
    if duration <= 0:
        raise ValueError("切塊需要有效的媒體長度（duration <= 0）")

    overlap = cfg.overlap_seconds if overlap_seconds is None else float(overlap_seconds)
    warnings: list[str] = []
    target = max(1.0, cfg.target_seconds)
    max_len = max(target, cfg.max_seconds)
    min_len = max(1.0, min(cfg.min_seconds, target * 0.5))

    if duration <= max(target, cfg.single_chunk_under) or duration <= target * 1.15:
        chunks = [ChunkPlan(index=0, start=0.0, end=duration, read_end=duration, boundary="head")]
        return Plan(duration, chunks, len(silences), 0, warnings)

    if not silences and not cfg.skip_silence_scan:
        warnings.append("未偵測到靜音點，改用固定長度切分（可能切到句子）")

    # --- 1. 逐個決定內部邊界（單調推進，不重複掃描）------------------
    edges: list[tuple[float, str]] = [(0.0, "head")]
    cursor = 0.0
    k = 1
    guard = 0
    max_chunks = 100_000
    while guard < max_chunks:
        guard += 1
        if duration - cursor <= min_len:
            break  # 剩下的太短，由最後一塊涵蓋
        ideal = target * k
        if duration - ideal < min_len:
            if duration - cursor <= max_len:
                break  # 現块可直接吃到尾
            # 否則尾段會超過 max_seconds（可能爆掉 ASR 上傳上限）→ 仍必須再切
            ideal = cursor + max_len
            warnings.append(f"尾段長度 {duration - cursor:.0f}s 超過單塊上限 {max_len:.0f}s，已繼續切分")
        ideal = min(ideal, cursor + max_len)
        low = cursor + min_len
        high = min(duration - min_len, cursor + max_len, ideal + max(0.0, cfg.silence_snap_window))
        if high - low < 1e-6:
            # 約束內沒有可切空間：在 max_len 上限硬切
            hard = min(cursor + max_len, duration - min_len)
            if hard - cursor < min_len:
                break
            edges.append((hard, "exact"))
            LOG.debug("硬切於 %.2fs（無可用靜音視窗）", hard)
        else:
            cut, _sdur, how = best_cut_point(
                low, high, min(max(ideal, low), high), silences, cfg, min_chunk_seconds=min_len
            )
            if cut - cursor < min_len:  # 靜音點太靠近前邊界
                cut = min(cursor + max_len, duration - min_len)
                how = "exact"
                if cut - cursor < min_len:
                    break
            edges.append((cut, how))
        cursor = edges[-1][0]
        k += 1
        if cursor >= duration - min_len:
            break
    if guard >= max_chunks:  # pragma: no cover - 防禦
        warnings.append("切塊數量超過安全上限，提前結束")

    # --- 2. 尾段處理：太短就併入前一块（在不超過 max_len 的前提下）---
    tail = duration - edges[-1][0]
    if tail <= 0.01:
        edges = edges[:-1] + [(duration, "tail")]
    elif tail < min_len:
        if duration - edges[-2][0] <= max_len * 1.15:
            warnings.append(f"尾段過短（{tail:.0f}s）已併入前一塊")
            edges = edges[:-1] + [(duration, "tail")]
        else:
            edges.append((duration, "tail"))
    else:
        edges.append((duration, "tail"))

    # --- 3. 組裝 chunk ------------------------------------------------
    chunks: list[ChunkPlan] = []
    snapped = 0
    last = len(edges) - 2
    for i in range(last + 1):
        start, end_t = edges[i][0], edges[i + 1][0]
        how = edges[i + 1][1]
        if how == "silence":
            snapped += 1
        read_start = start if i == 0 else max(0.0, start - max(0.0, overlap))
        chunks.append(
            ChunkPlan(
                index=i,
                start=round(start, 3),
                end=round(end_t, 3),
                read_start=round(read_start, 3),
                read_end=round(end_t, 3),
                boundary=how,
            )
        )
    plan = Plan(duration, chunks, len(silences), snapped, warnings)
    problems = plan.coverage_check()
    for prob in problems:
        warnings.append(f"[coverage] {prob}")
        LOG.warning("切塊檢查：%s", prob)
    return plan


def estimate_chunk_bytes(duration: float, sample_rate: int, channels: int, codec: str, bitrate: str) -> float:
    """估計 chunk 檔大小，用於避免超過 ASR 上傳上限。"""
    if codec == "wav":
        return duration * sample_rate * 2 * max(1, channels)
    if codec == "flac":
        return duration * sample_rate * 1.1 * max(1, channels)
    bps = _bitrate_to_bps(bitrate)
    return duration * bps / 8.0 * max(1, channels) * 1.1


def max_seconds_for_bytes(
    limit_bytes: float, sample_rate: int, channels: int, codec: str, bitrate: str, *, floor: float = 15.0
) -> float:
    """在給定的上傳上限內，單塊最多可以幾秒（用與實際估算相同的式子反推）。

    多数 whisper 端點寫在 25MB，但前方的 nginx／Gateway 常把請求體限制在 1MB，
    这时與其讓每一塊都吃一次 413，不如在規劃階段就把塊縮小。
    """
    if not limit_bytes or limit_bytes <= 0:
        return float("inf")
    per_second = estimate_chunk_bytes(1.0, sample_rate, channels, codec, bitrate)
    if per_second <= 0:
        return float("inf")
    return max(floor, (float(limit_bytes) - 8192.0) / per_second)  # 預留 multipart 標頭


def _bitrate_to_bps(bitrate: str) -> float:
    """'24k' / '32 kbps' / '128000' → bits per second。"""
    text = str(bitrate).strip().lower().replace("bps", "").replace("kbps", "k").strip()
    factor = 1.0
    if text.endswith("m"):
        factor, text = 1_000_000.0, text[:-1]
    elif text.endswith("k"):
        factor, text = 1_000.0, text[:-1]
    try:
        value = float(text)
    except ValueError:
        return 24_000.0
    if factor == 1.0 and value < 1000:  # 寫 "24" 當作 24kbps
        factor = 1_000.0
    return max(1_000.0, value * factor)


def split_chunk(
    chunk: ChunkPlan,
    silences: Sequence[Silence],
    cfg: ChunkConfig,
    *,
    min_len: float = 5.0,
) -> list[ChunkPlan]:
    """把一個 chunk 二分成兩塊（用於 chunk 檔案超過上傳上限時的自適應處理）。"""
    target = (chunk.start + chunk.end) / 2.0
    cut, sdur, how = best_cut_point(
        chunk.start + min_len, chunk.end - min_len, target, silences, cfg, min_chunk_seconds=min_len
    )
    a = ChunkPlan(
        chunk.index, chunk.start, round(cut, 3), chunk.start, round(cut, 3), how, sdur,
        variant=(chunk.variant or "") + "a",
    )
    b = ChunkPlan(
        chunk.index,
        round(cut, 3),
        chunk.end,
        max(chunk.start, round(cut, 3) - max(0.0, cfg.overlap_seconds)),
        chunk.end,
        chunk.boundary,
        0.0,
        (chunk.variant or "") + "b",
    )
    return [a, b]
