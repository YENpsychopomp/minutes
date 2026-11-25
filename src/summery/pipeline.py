"""端到端管線：媒體 → 切塊 → ASR（並行＋快取）→ 合併 → 摘要 → 輸出檔。

可resume 設計：
* 每個 chunk 的 ASR 結果單獨存檔（含 chunk 簽名），重跑時自動跳過已完成塊。
* ``state.json`` 持續更新進度，Web UI 或另一個行程可輪詢。
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .asr import ASRClient, ASRError, ASROutcome
from .chunking import ChunkPlan, Plan, max_seconds_for_bytes, plan_chunks, split_chunk
from .config import Config
from .documents import Document, DocumentError, documents_to_transcript, is_document, load_document
from .llm import LLMClient, LLMError
from .media import Silence, describe, detect_silence, estimate_wav_bytes, extract_chunk, normalize_audio, probe
from .minutes import ReferenceProfile, analyze_references, compose_minutes, local_profile
from .summarizer import Chapter, Summarizer, Summary
from .transcript import Segment, Transcript, merge_chunk_results, transcript_from_text
from .util import LOG, atomic_write_text, fmt_bytes, fmt_clock, read_json, slugify, write_json

ProgressFn = Callable[[str, float, str], None]


# --------------------------------------------------------------------------
# 資料型別
# --------------------------------------------------------------------------


@dataclass
class JobOptions:
    """單次工作的選項（與 Config 分離，方便 Web UI 傳入）。"""

    out_dir: str | None = None
    job_id: str | None = None
    formats: list[str] | None = None
    keep_chunks: bool | None = None
    resume: bool = True
    #: 有 chunk 失敗時是否仍輸出部分結果
    allow_partial: bool = True
    skip_summary: bool = False
    skip_asr: bool = False  # 僅重跑摘要
    transcript_input: str | None = None  # 搭配 skip_asr 使用既有逐字稿
    dry_run: bool = False
    asr_language: str | None = None
    summary_language: str | None = None
    summary_style: str | None = None
    custom_instruction: str | None = None
    chapters: bool | None = None
    audio_stream: int | None = None
    chunk_seconds: float | None = None
    chunk_max_seconds: float | None = None
    max_chunks: int | None = None  # 除錯：只跑前 N 塊
    concurrency: int | None = None
    title_hint: str | None = None
    verify_ssl: bool = True
    #: 過往會議紀錄（範本）：沿用其格式、段落結構、用語、與會者與專有名詞
    references: list[str] | None = None
    #: 文件輸入時的其他文件（第一份是 run() 的 input_path）
    extra_inputs: list[str] | None = None
    #: 是否另外產出 minutes.md（None = 有範本或輸入是文件時自動產出）
    minutes: bool | None = None

    def apply_to(self, cfg: Config) -> Config:
        """把 CLI/Web 選項蓋到設定複本上。

        巢狀小節（chunking/asr/…）也要一起複製：Web 服務整個程序只持有一份 Config，
        若只 shallow-copy，上一個工作的「每塊長度」就會留給下一個工作。
        """
        out = dataclasses.replace(
            cfg,
            audio=dataclasses.replace(cfg.audio),
            asr=dataclasses.replace(cfg.asr),
            llm=dataclasses.replace(cfg.llm),
            chunking=dataclasses.replace(cfg.chunking),
            summarize=dataclasses.replace(cfg.summarize),
            output=dataclasses.replace(cfg.output),
            documents=dataclasses.replace(cfg.documents),
        )
        if self.asr_language:
            out.asr.language = self.asr_language
        if self.concurrency:
            out.asr.max_concurrency = max(1, self.concurrency)
            out.llm.max_concurrency = max(1, self.concurrency)
        if self.keep_chunks is not None:
            out.output.keep_chunks = self.keep_chunks
        if self.formats:
            out.output.formats = list(self.formats)
        if self.summary_language:
            out.summarize.language = self.summary_language
        if self.summary_style:
            out.summarize.style = self.summary_style
        if self.custom_instruction:
            out.summarize.custom_instruction = self.custom_instruction
        if self.chapters is not None:
            out.summarize.chapters = self.chapters
        if self.chunk_seconds:
            out.chunking.target_seconds = float(self.chunk_seconds)
        if self.chunk_max_seconds:
            out.chunking.max_seconds = float(self.chunk_max_seconds)
        out.chunking.max_seconds = max(out.chunking.max_seconds, out.chunking.target_seconds)

        # 依「上傳上限 ÷ 每秒位元組數」反推每塊最長秒數：多數 whisper 端點寫 25MB，
        # 但前方的 nginx／Gateway 常把請求體限制在 1MB，先縮塊比吃一排 413 好。
        budget = max_seconds_for_bytes(
            out.asr.max_upload_bytes, out.audio.sample_rate, out.audio.channels,
            out.audio.codec, out.audio.bitrate, floor=out.chunking.min_seconds,
        )
        if budget < out.chunking.max_seconds:
            if budget < out.chunking.target_seconds:
                LOG.info("每塊長度上限 %.0fs（由 asr.max_upload_bytes %s 與音訊位元率反推，原設定 %.0fs）",
                         budget, fmt_bytes(out.asr.max_upload_bytes), out.chunking.target_seconds)
            out.chunking.target_seconds = min(out.chunking.target_seconds, budget)
            # max_seconds 是規劃器合併塊時的真正上限，只壓 target 還是會切出超標的塊
            out.chunking.max_seconds = max(
                min(out.chunking.max_seconds, budget), out.chunking.target_seconds
            )
        elif budget < out.chunking.min_seconds * 2:
            LOG.warning("asr.max_upload_bytes（%s）小到只能切 %.0fs 的塊，建議改以降低 audio.bitrate 取代",
                        fmt_bytes(out.asr.max_upload_bytes), budget)
        return out


@dataclass
class JobResult:
    job_id: str
    work_dir: str
    out_dir: str
    duration: float
    plan: dict
    transcript: Transcript
    summary: Summary | None
    outputs: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    partial: bool = False
    elapsed: float = 0.0
    stats: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "work_dir": self.work_dir,
            "out_dir": self.out_dir,
            "duration": round(self.duration, 2),
            "plan": {k: v for k, v in self.plan.items() if k != "chunks"},
            "partial": self.partial,
            "elapsed": round(self.elapsed, 2),
            "warnings": self.warnings,
            "errors": self.errors,
            "outputs": self.outputs,
            "stats": self.stats,
            "transcript": self.transcript.stats(),
            "summary": self.summary.to_dict() if self.summary else None,
        }


class JobError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 狀態檔
# --------------------------------------------------------------------------


class JobStateStore:
    """``state.json`` 讀寫（給 Web UI 輪詢、CLI 進度與 resume 用）。"""

    def __init__(self, work_dir: Path, job_id: str):
        self.dir = Path(work_dir)
        self.job_id = job_id
        self.path = self.dir / "state.json"
        self._lock = threading.Lock()
        self._last_write = 0.0
        self.state: dict[str, Any] = {
            "job_id": job_id,
            "status": "pending",
            "stage": "init",
            "progress": 0.0,
            "message": "",
            "created_at": time.time(),
            "updated_at": time.time(),
            "counts": {},
        }

    def load(self) -> dict | None:
        data = read_json(self.path)
        if isinstance(data, dict):
            self.state = data
            return data
        return None

    def start(self, **fields: Any) -> None:
        self.update(status="running", progress=0.0, message="", stage="init", **fields)

    def update(
        self, *, stage: str | None = None, progress: float | None = None,
        message: str | None = None, force: bool = False, **fields: Any,
    ) -> None:
        with self._lock:
            self.state.update({k: v for k, v in fields.items() if v is not None or k in ("progress",)})
            if stage is not None:
                self.state["stage"] = stage
            if progress is not None:
                self.state["progress"] = round(min(1.0, max(0.0, float(progress))), 4)
            if message is not None:
                self.state["message"] = message
            self.state["updated_at"] = time.time()
            now = time.perf_counter()
            if force or (now - self._last_write) > 0.4 or progress in (0.0, 1.0):
                self._last_write = now
                self.state["updated_at"] = time.time()
                write_json(self.path, self.state)

    def finish(self, status: str, *, message: str = "", outputs: dict | None = None, errors: Sequence[dict] = ()) -> None:
        """結束狀態：成功/部分完成一律把進度推到 100%，避免 UI 卡在 95%。"""
        progress = 1.0 if status in ("done", "partial") else None
        self.update(status=status, progress=progress, message=message or status, stage="done", force=True,
                    outputs=outputs or {}, errors=list(errors), finished_at=time.time())


# --------------------------------------------------------------------------
# 工作身分
# --------------------------------------------------------------------------


def make_job_id(source: Path, cfg: Config, options: JobOptions) -> str:
    stat = source.stat()
    h = hashlib.sha256()
    h.update(str(source.resolve()).encode("utf-8"))
    h.update(str(stat.st_size).encode())
    h.update(f"{cfg.chunking.target_seconds}:{cfg.chunking.overlap_seconds}:{cfg.audio.sample_rate}".encode())
    h.update(f"{cfg.asr.model}:{cfg.asr.language}".encode())
    for extra in options.extra_inputs or []:  # 多份文件輸入：組合不同就是不同工作
        path = Path(extra).expanduser()
        h.update(str(path.resolve()).encode("utf-8"))
        h.update(str(path.stat().st_size if path.is_file() else 0).encode())
    digest = h.hexdigest()[:10]
    return f"{slugify(source.stem, 40)}-{digest}"


def chunk_signature(chunk: ChunkPlan) -> str:
    return f"{chunk.start:.3f}-{chunk.end:.3f}"


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


class Pipeline:
    def __init__(
        self, cfg: Config, *, options: JobOptions | None = None,
        progress: ProgressFn | None = None, cancel: threading.Event | None = None,
    ):
        self.options = options or JobOptions()
        self.cfg = self.options.apply_to(cfg)
        self.progress = progress or (lambda *_: None)
        self.cancel = cancel or threading.Event()
        self.warnings: list[str] = []
        self.errors: list[dict] = []
        self.references: list[Document] = []
        self.profile: ReferenceProfile | None = None
        self._work: Path | None = None

    # -- 目錄 ------------------------------------------------------------
    def _prepare_dirs(self, source: Path) -> tuple[Path, str]:
        job_id = self.options.job_id or make_job_id(source, self.cfg, self.options)
        work = Path(self.cfg.work_dir).expanduser() / job_id
        out = Path(self.options.out_dir or source.parent).expanduser()
        for sub in ("", "audio", "chunks", "results"):
            (work / sub).mkdir(parents=True, exist_ok=True)
        out.mkdir(parents=True, exist_ok=True)
        return work, job_id

    # -- 主入口 ----------------------------------------------------------
    def run(self, input_path: str | Path) -> JobResult:
        started = time.perf_counter()
        source = Path(input_path).expanduser()
        if not source.exists():
            raise JobError(f"找不到輸入檔：{source}")
        work, job_id = self._prepare_dirs(source)
        store = JobStateStore(work, job_id)
        store.start(input=str(source), options=dataclasses.asdict(self.options), config=self.cfg.redacted())
        try:
            return self._run_with_store(source, work, job_id, store, started)
        except KeyboardInterrupt:
            store.update(status="cancelled", message="工作已取消", force=True)
            raise
        except Exception as exc:  # noqa: BLE001
            store.update(status="failed", message=f"{type(exc).__name__}: {exc}", force=True)
            raise

    def _run_with_store(self, source: Path, work: Path, job_id: str, store: JobStateStore, started: float) -> JobResult:
        out_dir = Path(self.options.out_dir or source.parent).expanduser()
        stem = self.cfg.output.stem or slugify(source.stem, 60) or "transcript"
        self._work = work
        doc_input = is_document(source)
        store.update(kind="document" if doc_input else "media", force=True)
        # 範本先讀：PDF 缺 markitdown 之類的問題要在花 ASR 費用之前就報出來
        if self.options.references:
            self.references = self._load_documents(self.options.references, store, role="範本")
            store.update(references=[d.name for d in self.references], force=True)
        if doc_input:
            transcript = self._document_transcript(source, store)
            duration = transcript.duration
            plan = Plan(duration=duration, chunks=[])
            if transcript.meta.get("source_kind") == "document":
                # 文件沒有時間軸：字幕檔只會是一串虛擬時間，不輸出
                self.cfg.output.formats = [f for f in self.cfg.output.formats if f not in ("srt", "vtt")]
        elif self.options.skip_asr and self.options.transcript_input:
            info = probe(source, ffprobe_bin=self.cfg.ffprobe_bin)
            transcript = _load_transcript(Path(self.options.transcript_input), fallback_duration=info.duration or 0.0)
            duration = transcript.duration or info.duration or 0.0
            plan = Plan(duration=duration, chunks=[])
            store.update(stage="transcript", progress=0.5, message=f"使用既有逐字稿：{Path(self.options.transcript_input).name}", force=True)
        else:
            store.update(stage="probe", progress=0.01, message="讀取媒體資訊", force=True)
            info = probe(source, ffprobe_bin=self.cfg.ffprobe_bin)
            if info.duration is None or info.duration <= 0:
                store.update(message="來源未取得長度，先抽取音訊後再取長度")
            store.update(counts={"audio_streams": len(info.audio_streams), "has_video": info.has_video}, force=True)
            LOG.info("來源：%s", describe(info, self.cfg))
            transcript, plan, duration = self._transcribe(source, work, info, store)

        if self.options.dry_run:
            store.finish("dry-run", message=f"切塊計畫 {plan.count} 塊（未執行 ASR）", outputs={})
            return JobResult(job_id, str(work), str(out_dir), duration, plan.to_dict(), transcript, None,
                             warnings=self.warnings + list(plan.warnings), errors=self.errors,
                             elapsed=time.perf_counter() - started, stats={"dry_run": True})

        # ---- 摘要 ------------------------------------------------------
        summary: Summary | None = None
        if not self.options.skip_summary:
            store.update(stage="reference" if self.references else "summarize",
                         progress=0.15 if doc_input else 0.8, message="產生摘要", force=True)
            summary = self._summarize(transcript, store, title_hint=self.options.title_hint or source.stem,
                                      doc_input=doc_input, source=source)
            if summary is None:
                summary = self._fallback_summary(transcript, self.options.title_hint or source.stem, reason="未取得摘要")
        else:
            store.update(stage="summarize", progress=0.9, message="略過摘要", force=True)

        # ---- 輸出 ------------------------------------------------------
        store.update(stage="write", progress=0.95, message="輸出檔案", force=True)
        outputs = write_outputs(
            out_dir=out_dir, stem=stem, transcript=transcript, summary=summary, cfg=self.cfg,
            source_name=source.name, job_id=job_id,
            protect=[source, *(self.options.extra_inputs or []), *(self.options.references or [])],
        )
        elapsed = time.perf_counter() - started
        partial = bool(self.errors)
        status = "done" if not self.errors else ("partial" if self.options.allow_partial else "failed")
        stats = {
            "input_kind": "document" if doc_input else "media",
            "chunks_total": plan.count,
            "chunks_ok": transcript.meta.get("chunks_ok"),
            "asr_seconds": _asr_seconds(work / "results"),
            "elapsed": round(elapsed, 2),
            "realtime_factor": round(duration / elapsed, 2) if elapsed > 0 and duration and not doc_input else None,
            "llm_usage": summary.meta.get("llm_usage") if summary else None,
        }
        if doc_input:
            stats["documents"] = len(transcript.meta.get("documents") or []) or 1
        if self.references:
            stats["references"] = [d.name for d in self.references]
        job_result = JobResult(
            job_id=job_id, work_dir=str(work), out_dir=str(out_dir), duration=duration,
            plan=plan.to_dict(), transcript=transcript, summary=summary, outputs=outputs,
            warnings=self.warnings + list(plan.warnings), errors=self.errors, partial=partial,
            elapsed=elapsed, stats=stats,
        )
        # 工作彙整檔
        write_json(work / "job.json", job_result.to_dict())
        write_json(work / "transcript.json", transcript.to_dict())
        if summary:
            write_json(work / "summary.json", summary.to_dict())
        store.finish(status, message=_status_message(status, outputs), outputs=outputs, errors=self.errors)
        if self.errors and not self.options.allow_partial:
            raise JobError(f"{len(self.errors)} 個 chunk 失敗（允許部分結果請加 --allow-partial）：{self.errors[:3]}")
        return job_result

    # -- ASR 階段 --------------------------------------------------------
    def _transcribe(self, source: Path, work: Path, info, store: JobStateStore) -> tuple[Transcript, Plan, float]:
        cfg = self.cfg
        duration = info.duration or 0.0
        audio_path: Path | None = None
        chunk_source = cfg.audio.chunk_source
        # 中間音訊檔：供靜音偵測與 chunk 切割共用
        est_wav = estimate_wav_bytes(duration, cfg.audio) if duration else None
        use_source_chunks = chunk_source == "source" or (
            chunk_source == "auto" and est_wav is not None and est_wav > cfg.audio.chunk_source_auto_threshold_bytes
        )

        audio_file = work / "audio" / f"audio.{_intermediate_ext(cfg)}"
        if use_source_chunks and not cfg.chunking.skip_silence_scan:
            store.update(stage="audio", progress=0.05, message="中間檔過大：chunk 直接從來源切割")
        if audio_file.is_file() and audio_file.stat().st_size > 1024:
            audio_path = audio_file
            store.update(message=f"沿用已抽取音訊 {audio_file.name}")
        else:
            store.update(stage="audio", progress=0.03, message="抽取並正規化音訊（ffmpeg）", force=True)
            audio_path = normalize_audio(
                source, audio_file, cfg.audio, ffmpeg_bin=cfg.ffmpeg_bin,
                audio_stream=self.options.audio_stream, cancel=self.cancel,
                progress=lambda stage, frac: store.update(progress=0.02 + 0.08 * frac, message=f"正規化音訊 {frac * 100:.0f}%"),
            )
            if not duration:
                duration = probe(audio_path, ffprobe_bin=self.cfg.ffprobe_bin).duration or 0.0

        # ---- 靜音偵測 --------------------------------------------------
        silences: list[Silence] = []
        silence_file = work / "silence.json"
        if cfg.chunking.skip_silence_scan:
            store.update(stage="silence", progress=0.12, message="已關閉靜音掃描（固定長度切塊）", force=True)
        elif silence_file.is_file():
            cached = read_json(silence_file) or {}
            silences = [Silence(float(s[0]), float(s[1])) for s in cached.get("silences", [])]
            store.update(stage="silence", progress=0.12, message=f"沿用靜音分析（{len(silences)} 段）", force=True)
        else:
            store.update(stage="silence", progress=0.08, message="掃描靜音點（長音訊約需數十秒）", force=True)
            silences = detect_silence(
                audio_path, noise_db=cfg.chunking.silence_noise_db,
                min_duration=cfg.chunking.silence_min_duration, ffmpeg_bin=cfg.ffmpeg_bin,
                cancel=self.cancel, total_duration=duration,
                progress=lambda stage, frac: store.update(progress=0.06 + 0.09 * frac, message=f"靜音掃描 {frac * 100:.0f}%"),
            )
            write_json(silence_file, {"count": len(silences), "silences": [[round(s.start, 3), round(s.end, 3)] for s in silences]})

        # ---- 切塊計畫 --------------------------------------------------
        plan = self._plan(duration, silences, work, store)
        chunks: Sequence[ChunkPlan] = plan.chunks
        if self.options.max_chunks:
            chunks = chunks[: self.options.max_chunks]
            self.warnings.append(f"除錯模式：僅處理前 {len(chunks)} 塊")
        if self.options.dry_run:
            store.finish("dry-run", message=f"切塊計畫完成：{plan.count} 塊", outputs={})
            LOG.info("切塊計畫：%d 塊，%s", plan.count, ", ".join(f"{c.start:.0f}-{c.end:.0f}s" for c in plan.chunks[:6]))
            return Transcript(duration=duration, segments=[], meta={"dry_run": True}), plan, duration

        # ---- 並行 ASR --------------------------------------------------
        if not cfg.asr.stub_dir:
            cfg.require_api_key("asr")
        asr = ASRClient(cfg.asr, api_key=self._asr_key(), verify_ssl=self.options.verify_ssl)
        results, chunk_errors, languages = self._run_asr(asr, chunks, silences, work, audio_path, source, store, info)
        self.errors.extend(chunk_errors)
        if not results and chunk_errors:
            store.finish("failed", message="所有 chunk 皆失敗")
            raise JobError(f"ASR 全數失敗（{len(chunk_errors)} 塊），第一段錯誤：{chunk_errors[0].get('error')}")
        if chunk_errors:
            self.warnings.append(f"{len(chunk_errors)} 個 chunk 失敗，逐字稿為部分結果")

        language = _pick_language(languages) or cfg.asr.language
        transcript = merge_chunk_results(
            results, duration=duration, overlap_seconds=cfg.chunking.overlap_seconds, language=language
        )
        transcript.meta.update({
            "source": source.name,
            "asr_model": cfg.asr.model,
            "chunks_planned": plan.count,
            "chunks_ok": len(results),
            "paragraph_gap": cfg.output.paragraph_gap,
            "paragraph_max_chars": cfg.output.paragraph_max_chars,
            "chunk_target_seconds": cfg.chunking.target_seconds,
            "summary_mode_hint": "map-reduce" if plan.count > 1 else "single",
        })
        if not transcript.segments:
            store.finish("failed", message="ASR 沒有回傳任何文字")
            raise JobError("ASR 沒有回傳任何文字（可能來源為純靜音，或 ASR 服務回應格式不符）")
        write_json(work / "transcript.json", transcript.to_dict())
        if not cfg.output.keep_chunks:
            _cleanup_chunks(work / "chunks")
        return transcript, plan, duration

    def _plan(self, duration: float, silences: Sequence[Silence], work: Path, store: JobStateStore) -> Plan:
        plan_file = work / "plan.json"
        cfg = self.cfg
        signature = {
            "duration": round(duration, 2), "target": cfg.chunking.target_seconds,
            "max": cfg.chunking.max_seconds, "min": cfg.chunking.min_seconds,
            "overlap": cfg.chunking.overlap_seconds, "silences": len(silences),
        }
        if plan_file.is_file():
            cached = Plan.from_dict(read_json(plan_file) or {})
            if cached.duration and abs(cached.duration - duration) < 0.5 and len(silences) >= cached.silence_count:
                store.update(stage="plan", progress=0.13, message=f"沿用切塊計畫（{cached.count} 塊）", force=True)
                return cached
            LOG.info("切塊參數已變動，重新規劃")
        plan = plan_chunks(duration, silences, cfg.chunking)
        store.update(stage="plan", progress=0.13, message=f"切塊計畫：{plan.count} 塊（靜音對齊 {plan.snapped}）", force=True)
        write_json(plan_file, {"signature": signature, **plan.to_dict()})
        LOG.info("切塊：%d 塊（目標 %.0fs，靜音對齊 %d）", plan.count, cfg.chunking.target_seconds, plan.snapped)
        return plan

    def _run_asr(
        self, asr: ASRClient, chunks: Sequence[ChunkPlan], silences: Sequence[Silence], work: Path,
        audio_path: Path | None, source: Path, store: JobStateStore, info,
    ) -> tuple[list[tuple[ChunkPlan, list[Segment]]], list[dict], list[str | None]]:
        cfg = self.cfg
        results_dir = work / "results"
        results: list[tuple[ChunkPlan, list[Segment]]] = []
        errors: list[dict] = []
        languages: list[str | None] = []
        pending: list[ChunkPlan] = []
        skipped = 0
        for chunk in chunks:
            cached = self._load_chunk_result(results_dir, chunk)
            if cached is not None:
                segs, language = cached
                if segs:
                    results.append((chunk, segs))
                    languages.append(language)
                skipped += 1
            else:
                pending.append(chunk)
        if skipped:
            LOG.info("resume：沿用 %d 個已完成 chunk", skipped)
            store.update(message=f"resume：已完成 {skipped}/{len(chunks)} 塊")
        if not pending:
            store.update(stage="asr", progress=0.78, message=f"全部 chunk 已快取（{len(chunks)}）", force=True)
            return results, errors, languages

        total = len(chunks)
        done = skipped
        started = time.perf_counter()
        workers = max(1, int(cfg.asr.max_concurrency or 1))
        store.update(stage="asr", progress=_asr_progress(done, total), message=f"ASR 並行 {workers}，待處理 {len(pending)} 塊", force=True)
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="asr")
        futures: dict[Future, ChunkPlan] = {}
        chunk_dir = work / "chunks"
        cut_source = audio_path if _use_intermediate(cfg, info, audio_path) else source
        try:
            for chunk in pending:
                self._check_cancel()
                futures[pool.submit(self._asr_one, asr, chunk, chunk_dir, cut_source, results_dir, silences, 0)] = chunk
            for fut in as_completed(futures):
                chunk = futures[fut]
                try:
                    pairs = fut.result()
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # noqa: BLE001
                    LOG.error("chunk %d (%.0f-%.0fs) 失敗：%s", chunk.index, chunk.start, chunk.end, exc)
                    errors.append({
                        "chunk": chunk.index, "start": round(chunk.start, 2), "end": round(chunk.end, 2),
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                else:
                    results.extend(pairs)
                done += 1
                elapsed = time.perf_counter() - started
                rate = (done - skipped) / elapsed if elapsed > 0 else 0
                eta = (total - done) / rate if rate > 0 else None
                store.update(
                    stage="asr", progress=_asr_progress(done, total),
                    message=f"ASR {done}/{total}（{rate * 60:.0f} 塊/分，ETA {_fmt_eta(eta)}）",
                )
        finally:
            pool.shutdown(wait=not self.cancel.is_set(), cancel_futures=self.cancel.is_set())
        results.sort(key=lambda p: p[0].start)
        languages.extend(_result_language(results_dir, chunk) for chunk, _segs in results)
        LOG.info("ASR 完成：成功 %d 塊、失敗 %d 塊（%.1fs）", len(results), len(errors), time.perf_counter() - started)
        return results, errors, languages

    def _asr_one(
        self, asr: ASRClient, chunk: ChunkPlan, chunk_dir: Path, cut_source: Path, results_dir: Path,
        silences: Sequence[Silence], depth: int,
    ) -> list[tuple[ChunkPlan, list[Segment]]]:
        """單塊：切割 → ASR → 存檔；檔案過大則自適應二分。"""
        self._check_cancel()
        cfg = self.cfg
        path = _chunk_path(chunk_dir, chunk, cfg)
        if not path.is_file() or path.stat().st_size < 256:
            path = extract_chunk(
                cut_source, chunk.read_start, chunk.read_duration, path, cfg.audio,
                ffmpeg_bin=cfg.ffmpeg_bin, audio_stream=self.options.audio_stream, cancel=self.cancel,
            )
        limit = cfg.asr.max_upload_bytes
        if limit and path.stat().st_size > limit and depth < 4:
            LOG.warning("chunk %d 為 %s > 上限 %s，自適應二分", chunk.index, fmt_bytes(path.stat().st_size), fmt_bytes(limit))
            path.unlink(missing_ok=True)
            out = []
            for sub in split_chunk(chunk, silences, cfg.chunking):
                out.extend(self._asr_one(asr, sub, chunk_dir, cut_source, results_dir, silences, depth + 1))
            return out
        try:
            try:
                outcome: ASROutcome = asr.transcribe_file(path, stub_key=f"chunk_{chunk.key}")
            except ASRError as exc:
                parts = split_chunk(chunk, silences, cfg.chunking) if exc.too_large else [chunk]
                if exc.too_large and depth < 4 and len(parts) > 1:
                    LOG.warning("chunk %d（%s）被擋下 413 → 二分重送（%d 塊）",
                                chunk.index, fmt_bytes(path.stat().st_size), len(parts))
                    out: list[tuple[ChunkPlan, list[Segment]]] = []
                    for sub in parts:
                        out.extend(self._asr_one(asr, sub, chunk_dir, cut_source, results_dir,
                                                 silences, depth + 1))
                    return out
                raise
        finally:
            if not cfg.output.keep_chunks:
                path.unlink(missing_ok=True)
        segments = [
            Segment(start=s.start, end=s.end, text=s.text, words=s.words, chunk=chunk.index, confidence=s.confidence)
            for s in outcome.segments
        ]
        # 記錄原始相對時間結果，供 resume 驗證
        write_json(
            results_dir / _result_name(chunk),
            {
                "signature": chunk_signature(chunk),
                "index": chunk.index,
                "start": chunk.start,
                "end": chunk.end,
                "read_start": chunk.read_start,
                "read_end": chunk.read_end,
                "language": outcome.language,
                "elapsed": round(outcome.elapsed, 3),
                "bytes_sent": outcome.bytes_sent,
                "segments": [dataclasses.asdict(s) for s in segments],
                "raw_meta": outcome.raw_meta,
            },
        )
        LOG.debug(
            "chunk %d (%.0f-%.0fs) 完成：%.1fs, %d segments, %.1fx realtime",
            chunk.index, chunk.start, chunk.end, outcome.elapsed, len(segments),
            chunk.read_duration / max(0.01, outcome.elapsed),
        )
        return [(chunk, segments)]

    def _load_chunk_result(self, results_dir: Path, chunk: ChunkPlan) -> tuple[list[Segment], str | None] | None:
        if not self.options.resume:
            return None
        data = read_json(results_dir / _result_name(chunk))
        if not isinstance(data, dict):
            return None
        if data.get("signature") != chunk_signature(chunk):
            LOG.debug("chunk %d 簽名不符（邊界已變），重新轉錄", chunk.index)
            return None
        segs = [
            Segment(start=float(s.get("start", 0)), end=float(s.get("end", 0)), text=str(s.get("text", "")),
                    words=[], chunk=chunk.index, confidence=s.get("confidence"))
            for s in (data.get("segments") or [])
        ]
        return segs, data.get("language")

    # -- 文件輸入 --------------------------------------------------------
    def _load_documents(self, paths: Sequence[str | Path], store: JobStateStore, *, role: str) -> list[Document]:
        docs: list[Document] = []
        dcfg = self.cfg.documents
        for i, raw in enumerate(paths, start=1):
            self._check_cancel()
            path = Path(raw).expanduser()
            store.update(message=f"讀取{role} {i}/{len(paths)}：{path.name}")
            try:
                doc = load_document(path, markitdown_cmd=dcfg.markitdown_cmd,
                                    markitdown_timeout=dcfg.markitdown_timeout,
                                    paragraph_max_chars=dcfg.paragraph_max_chars)
            except DocumentError as exc:
                raise JobError(f"{role}無法讀取：{exc}") from exc
            self.warnings.extend(f"{doc.name}：{w}" for w in doc.warnings)
            docs.append(doc)
        return docs

    def _document_transcript(self, source: Path, store: JobStateStore) -> Transcript:
        paths = [source] + [Path(p) for p in (self.options.extra_inputs or [])]
        store.update(stage="document", progress=0.03, message=f"讀取 {len(paths)} 份文件", force=True)
        docs = self._load_documents(paths, store, role="文件")
        transcript = documents_to_transcript(docs)
        chars = sum(d.chars for d in docs)
        store.update(stage="document", progress=0.1, force=True,
                     message=f"已讀取 {len(docs)} 份文件，共 {len(transcript.segments)} 段、約 {chars} 字",
                     counts={"documents": len(docs), "paragraphs": len(transcript.segments), "chars": chars})
        LOG.info("文件輸入：%s（%d 段，約 %d 字）", "、".join(d.name for d in docs), len(transcript.segments), chars)
        if self._work:
            write_json(self._work / "transcript.json", transcript.to_dict())
        return transcript

    def _want_minutes(self, doc_input: bool) -> bool:
        if self.options.minutes is not None:
            return bool(self.options.minutes)
        return bool(self.references) or doc_input

    def _source_desc(self, transcript: Transcript, source: Path | None, doc_input: bool) -> str:
        if doc_input:
            names = [str(d.get("name")) for d in transcript.meta.get("documents") or [] if isinstance(d, dict)]
            return "文件：" + "、".join(names or [source.name if source else "?"])
        name = source.name if source else transcript.meta.get("source", "")
        return f"影音「{name}」，長度 {fmt_clock(transcript.duration, force_hours=True)}"

    # -- 摘要 ------------------------------------------------------------
    def _summarize(
        self, transcript: Transcript, store: JobStateStore, *, title_hint: str | None,
        doc_input: bool = False, source: Path | None = None,
    ) -> Summary | None:
        cfg = self.cfg
        want_minutes = self._want_minutes(doc_input)
        lo, hi = (0.15, 0.86) if doc_input else (0.8, 0.93 if want_minutes else 0.97)
        source_desc = self._source_desc(transcript, source, doc_input)
        try:
            cfg.require_api_key("llm")
            llm = LLMClient(cfg.llm, api_key=self._llm_key(), verify_ssl=self.options.verify_ssl)
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"跳過摘要：{exc}")
            store.update(stage="summarize", message=f"跳過摘要（{exc}）→ 改用本地簡易摘要", force=True)
            summary = self._fallback_summary(transcript, title_hint, reason=str(exc))
            if self.references:
                self.profile = local_profile(self.references)
                summary.meta["reference"] = self.profile.to_dict()
            if want_minutes:
                self._attach_minutes(summary, None, store, progress=hi, source_desc=source_desc)
            return summary

        if self.references:
            store.update(stage="reference", progress=lo, force=True,
                         message=f"分析 {len(self.references)} 份過往會議紀錄的格式與用語")
            self.profile = analyze_references(
                self.references, llm=llm, cfg=cfg, cache_dir=self._work, warnings=self.warnings,
            )
            lo += 0.03
        span = max(0.01, hi - lo)
        summarizer = Summarizer(
            llm, cfg.summarize, output_language=cfg.summarize.language,
            progress=lambda stage, frac, msg: store.update(
                stage="summarize", progress=lo + span * min(1.0, float(frac)), message=msg
            ),
            cancel=self.cancel,
            reference_hint=self.profile.hint_text() if self.profile else None,
        )
        usable_llm: LLMClient | None = llm
        try:
            summary = summarizer.summarize(transcript, title_hint=title_hint)
        except KeyboardInterrupt:
            raise
        except LLMError as exc:
            self.warnings.append(f"摘要失敗：{exc}")
            LOG.warning("摘要失敗，改用本地簡易摘要：%s", exc)
            store.update(stage="summarize", message=f"摘要失敗改用本地摘要（{exc}）", force=True)
            summary, usable_llm = self._fallback_summary(transcript, title_hint, reason=str(exc)), None
        except Exception as exc:  # pragma: no cover - 防禦未知錯誤
            self.warnings.append(f"摘要失敗：{exc}")
            LOG.warning("摘要未知錯誤，改用本地簡易摘要：%s", exc)
            store.update(stage="summarize", message=f"摘要失敗改用本地摘要（{exc}）", force=True)
            summary, usable_llm = self._fallback_summary(transcript, title_hint, reason=str(exc)), None
        else:
            self.warnings.extend(summarizer.warnings)
        if self.profile:
            summary.meta["reference"] = self.profile.to_dict()
        if want_minutes:
            self._attach_minutes(summary, usable_llm, store, progress=hi + 0.005, source_desc=source_desc)
            if usable_llm is not None:
                summary.meta["llm_usage"] = usable_llm.usage.to_dict()  # 含範本分析與撰寫紀錄的呼叫
        return summary

    def _attach_minutes(
        self, summary: Summary, llm: LLMClient | None, store: JobStateStore, *, progress: float, source_desc: str,
    ) -> None:
        store.update(stage="minutes", progress=progress, force=True,
                     message="依範本撰寫會議紀錄" if self.profile else "撰寫會議紀錄")
        notes: list[str] = []
        text, origin = compose_minutes(
            summary, llm=llm, cfg=self.cfg, profile=self.profile, source_desc=source_desc, warnings=notes,
        )
        summary.minutes_markdown = text
        summary.meta["minutes"] = {
            "source": origin,
            "format": "template" if self.profile else "default",
            "template": list(self.profile.files) if self.profile else [],
        }
        if notes:
            self.warnings.extend(notes)
            summary.meta.setdefault("warnings", []).extend(notes)

    def _fallback_summary(self, transcript: Transcript, title_hint: str | None, *, reason: str) -> Summary:
        """LLM 無法使用時的粗略本地摘要：確保 summary.json/summary.md 仍可產出。"""
        paras = transcript.paragraphs(
            gap=float(transcript.meta.get("paragraph_gap", 0.9)),
            max_chars=int(transcript.meta.get("paragraph_max_chars", 180)),
        )
        overview = [p.text.strip() for p in paras[:4] if p.text.strip()]
        tldr = (overview[0][:140] + "…") if overview else "逐字稿過短，未產生完整摘要。"
        chapters = []
        if paras:
            step = max(1, len(paras) // 4)
            for i in range(0, len(paras), step):
                p = paras[i]
                chapters.append(
                    Chapter(start=p.start, end=p.end, title=f"第 {i//step + 1} 段", summary=p.text[:120],
                            para_start=i + 1, para_end=min(len(paras), i + step))
                )
        meta = {
            "mode": "fallback",
            "warnings": [f"LLM 不可用或失敗：{reason}"] + self.warnings,
            "fallback": True,
            "input_kind": "document" if transcript.meta.get("source_kind") == "document" else "media",
        }
        if meta["input_kind"] == "document":
            meta["documents"] = list(transcript.meta.get("documents") or [])
        return Summary(
            title=title_hint or (transcript.meta.get("title") if isinstance(transcript.meta, dict) else None) or "影音摘要",
            tldr=tldr,
            overview=overview,
            key_points=[],
            chapters=chapters,
            keywords=[],
            decisions=[],
            action_items=[],
            open_questions=[],
            quotes=[],
            notes=[],
            meta=meta,
        )

    # -- key -------------------------------------------------------------
    def _asr_key(self) -> str | None:
        return self.cfg.resolve_api_key("asr") if not self.cfg.asr.stub_dir else None

    def _llm_key(self) -> str | None:
        return self.cfg.resolve_api_key("llm") if not self.cfg.llm.stub_dir else None

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise KeyboardInterrupt("工作已取消")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def run_job(
    input_path: str | Path, *, cfg: Config | None = None, options: JobOptions | None = None,
    progress: ProgressFn | None = None, cancel: threading.Event | None = None,
) -> JobResult:
    """程式化入口：``run_job("movie.mp4", cfg=load_config())``。"""
    return Pipeline(cfg or Config(), options=options, progress=progress, cancel=cancel).run(input_path)


def _asr_progress(done: int, total: int) -> float:
    return 0.13 + 0.63 * (done / max(1, total))


def _intermediate_ext(cfg: Config) -> str:
    return {"wav": "wav", "opus": "ogg", "m4a": "m4a", "mp3": "mp3", "flac": "flac"}.get(
        cfg.audio.intermediate_codec, "wav"
    )


def _use_intermediate(cfg: Config, info, audio_path: Path | None) -> bool:
    mode = cfg.audio.chunk_source
    if mode == "wav":
        return True
    if mode == "source":
        return False
    if audio_path is None:
        return False
    return audio_path.stat().st_size <= cfg.audio.chunk_source_auto_threshold_bytes


def _pick_language(languages: Sequence[str | None]) -> str | None:
    """多數決決定語言標籤（部分服務每塊回傳不同寫法）。"""
    counts: dict[str, int] = {}
    for lang in languages:
        if lang:
            key = str(lang).strip()
            if key:
                counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _result_language(results_dir: Path, chunk: ChunkPlan) -> str | None:
    data = read_json(results_dir / _result_name(chunk))
    return data.get("language") if isinstance(data, dict) else None


def _asr_seconds(results_dir: Path) -> float:
    total = 0.0
    for path in results_dir.glob("chunk_*.json"):
        data = read_json(path)
        if isinstance(data, dict):
            try:
                total += float(data.get("elapsed") or 0.0)
            except (TypeError, ValueError):
                continue
    return total


def _result_name(chunk: ChunkPlan) -> str:
    return f"chunk_{chunk.key}.json"


def _chunk_path(chunk_dir: Path, chunk: ChunkPlan, cfg: Config) -> Path:
    from .media import codec_spec

    _args, ext = codec_spec(cfg.audio.codec, cfg.audio.bitrate)
    return chunk_dir / f"chunk_{chunk.key}_{int(chunk.start * 1000)}.{ext}"


def _fmt_eta(seconds: float | None) -> str:
    if not seconds or seconds < 0:
        return "-"
    return fmt_clock(seconds)


def _cleanup_chunks(chunk_dir: Path) -> None:
    try:
        for p in chunk_dir.glob("chunk_*"):
            if p.is_file():
                p.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover
        LOG.debug("清理 chunk 失敗：%s", exc)


def _load_transcript(path: Path, *, fallback_duration: float) -> Transcript:
    if not path.is_file():
        raise JobError(f"找不到既有逐字稿：{path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".json":
        try:
            return Transcript.from_json(text)
        except Exception as exc:  # noqa: BLE001
            raise JobError(f"無法解析逐字稿 JSON：{exc}") from exc
    return transcript_from_text(text, fallback_duration or max(60.0, len(text) / 2.5))


def _status_message(status: str, outputs: dict) -> str:
    files = "、".join(sorted(outputs)) or "無"
    return f"{status}：{files}"


def _safe_target(path: Path, protect: set[Path]) -> Path:
    """輸出檔若會蓋到輸入檔（例：輸入 notes.txt、輸出也叫 notes.txt），改名為 notes.out.txt。"""
    if path.resolve() not in protect:
        return path
    return path.with_name(f"{path.stem}.out{path.suffix}")


def write_outputs(
    *, out_dir: Path, stem: str, transcript: Transcript, summary: Summary | None, cfg: Config,
    source_name: str | None = None, job_id: str | None = None, protect: Sequence[str | Path] = (),
) -> dict[str, str]:
    """依 ``output.formats`` 寫出檔案，回傳 {格式: 路徑}。``protect`` 是絕不覆寫的輸入檔。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, str] = {}
    fmts = [f.lower() for f in (cfg.output.formats or [])]
    title = (summary.title if summary and summary.title else f"{source_name or stem} 逐字稿")
    guard = {Path(x).expanduser().resolve() for x in protect}
    if "json" in fmts:
        p = _safe_target(out_dir / f"{stem}.transcript.json", guard)
        p.write_text(transcript.to_json(), encoding="utf-8")
        outputs["json"] = str(p)
    if "txt" in fmts:
        p = _safe_target(out_dir / f"{stem}.txt", guard)
        p.write_text(
            transcript.to_text(
                paragraph_gap=cfg.output.paragraph_gap, paragraph_max_chars=cfg.output.paragraph_max_chars
            ) + "\n",
            encoding="utf-8",
        )
        outputs["txt"] = str(p)
    if "md" in fmts:
        p = _safe_target(out_dir / f"{stem}.transcript.md", guard)
        p.write_text(
            transcript.to_markdown(
                title=title, source=source_name, paragraph_gap=cfg.output.paragraph_gap,
                paragraph_max_chars=cfg.output.paragraph_max_chars,
            ),
            encoding="utf-8",
        )
        outputs["md"] = str(p)
    if "srt" in fmts:
        p = _safe_target(out_dir / f"{stem}.srt", guard)
        p.write_text(transcript.to_srt(), encoding="utf-8")
        outputs["srt"] = str(p)
    if "vtt" in fmts:
        p = _safe_target(out_dir / f"{stem}.vtt", guard)
        p.write_text(transcript.to_vtt(), encoding="utf-8")
        outputs["vtt"] = str(p)
    if summary is not None:
        p = _safe_target(out_dir / f"{stem}.summary.md", guard)
        p.write_text(summary.to_markdown(duration=transcript.duration), encoding="utf-8")
        outputs["summary.md"] = str(p)
        p = _safe_target(out_dir / f"{stem}.summary.json", guard)
        p.write_text(summary.to_json() + "\n", encoding="utf-8")
        outputs["summary.json"] = str(p)
        if summary.minutes_markdown:
            p = _safe_target(out_dir / f"{stem}.minutes.md", guard)
            p.write_text(summary.minutes_markdown, encoding="utf-8")
            outputs["minutes.md"] = str(p)
    if job_id:
        p = out_dir / f"{stem}.job.json"
        write_json(p, {"job_id": job_id, "outputs": outputs, "formats": fmts})
        outputs["job.json"] = str(p)
    return outputs
