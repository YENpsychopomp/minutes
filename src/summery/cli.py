"""summery 命令列介面。

    summery run 講座.mp4 --chunk-seconds 600
    summery transcribe podcast.mp3 --formats srt,txt
    summery summarize out/podcast.transcript.json --style concise
    summery plan 講座.mp4                      # 只看切塊計畫，不打 API
    summery probe 講座.mp4                     # 媒體資訊
    summery serve --port 8765                  # 網頁上傳介面
    summery mock-server                        # 本機模擬 ASR/LLM（免 API key 試跑）
    summery make-media --minutes 130 --out test.mkv   # 產生長測試檔
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import Config, ConfigError, apply_cli_overrides, load_config
from .media import MediaError, detect_silence, probe
from .pipeline import JobError, JobOptions, JobResult, Pipeline, write_outputs
from .util import LOG, fmt_bytes, fmt_clock, setup_logging

EXIT_OK, EXIT_ERROR, EXIT_PARTIAL, EXIT_CANCELLED = 0, 1, 2, 130


# --------------------------------------------------------------------------
# 參數
# --------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-c", "--config", help="TOML 設定檔（預設讀 SUMMERY_CONFIG）")
    parser.add_argument("--set", dest="sets", action="append", default=[], metavar="KEY=VALUE",
                        help="覆蓋任一設定，例：--set chunking.target_seconds=300 --set llm.model=gpt-4o")
    parser.add_argument("--work-dir", help="中間檔／快取目錄（預設 .summery_work）")
    parser.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--quiet", action="store_true", help="只輸出必要資訊")
    parser.add_argument("--no-ssl-verify", action="store_true", help="自簽憑證的內部 Gateway 使用")


def _add_run_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-o", "--out-dir", help="輸出目錄（預設與來源同目錄）")
    parser.add_argument("--formats", help="輸出格式：md,txt,json,srt,vtt（逗號分隔）")
    parser.add_argument("--job-id", help="指定工作 ID（相同來源+參數會自動沿用快取）")
    parser.add_argument("--chunk-seconds", type=float, help="目標切塊長度（秒），預設 600")
    parser.add_argument("--chunk-max-seconds", type=float, help="單塊上限（秒）")
    parser.add_argument("--overlap", type=float, help="回溯重疊秒數（預設 2）")
    parser.add_argument("--no-silence-scan", action="store_true", help="不用靜音點對齊（固定長度切塊）")
    parser.add_argument("--concurrency", type=int, help="ASR/摘要並行數（預設 4）")
    parser.add_argument("--asr-language", help="ASR 語言提示，例：zh、en（預設自動偵測）")
    parser.add_argument("--audio-stream", type=int, help="選取第幾軌音訊（0 起）")
    parser.add_argument("--keep-chunks", action="store_true", help="保留切割出的 chunk 檔")
    parser.add_argument("--no-resume", action="store_true", help="忽略既有快取，全部重跑")
    parser.add_argument("--strict", action="store_true", help="任何 chunk 失敗就整個工作失敗")
    parser.add_argument("--max-chunks", type=int, help="除錯：只處理前 N 塊")
    parser.add_argument("--dry-run", action="store_true", help="只產生切塊計畫，不打 API")


def _add_summary_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--no-summary", action="store_true", help="只做逐字稿，不做摘要")
    parser.add_argument("--summary-language", help="摘要語言（預設 zh-TW；auto = 追隨逐字稿）")
    parser.add_argument("--style", choices=["concise", "standard", "comprehensive", "bullets"], help="摘要風格")
    parser.add_argument("--instruction", help="額外的摘要指示（例：聚焦法遵風險）")
    parser.add_argument("--no-chapters", action="store_true", help="不輸出彩章摘要")
    parser.add_argument("--title", help="標題提示（用於摘要與逐字稿標題）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="summery",
        description="影片/音訊 → 逐字稿 + 摘要（OpenAI 相容 ASR/LLM，支援長影音）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"summery {__version__}")
    parser.add_argument("--no-env-file", action="store_true", help="不要自動載入目前目錄的 .env")
    parser.add_argument(
        "--env-file", metavar="PATH", default=None,
        help="額外載入的 .env 檔案（會覆蓋既有環境變數）；預設自動載入目前目錄的 .env",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="完整流程：轉錄 + 摘要")
    run.add_argument("input", help="影片或音訊檔案（mp4/mov/mkv/mp3/wav/…）")
    _add_common(run)
    _add_run_flags(run)
    _add_summary_flags(run)
    run.add_argument("--json", action="store_true", help="結束時輸出結果 JSON（便於腳本處理）")

    tr = sub.add_parser("transcribe", help="只產逐字稿")
    tr.add_argument("input")
    _add_common(tr)
    _add_run_flags(tr)
    tr.add_argument("--json", action="store_true")

    sm = sub.add_parser("summarize", help="從既有逐字稿產生摘要")
    sm.add_argument("transcript", help="summery 產出的 .transcript.json 或純文字逐字稿")
    sm.add_argument("--duration", type=float, help="純文字逐字稿請提供總長度（秒）")
    _add_common(sm)
    _add_summary_flags(sm)
    sm.add_argument("-o", "--out-dir", help="輸出目錄（預設與逐字稿同目錄）")
    sm.add_argument("--formats", help="輸出格式（預設 md,json）")
    sm.add_argument("--json", action="store_true")

    pl = sub.add_parser("plan", help="顯示切塊計畫（不打 API）")
    pl.add_argument("input")
    _add_common(pl)
    _add_run_flags(pl)
    pl.add_argument("--json", action="store_true")

    pr = sub.add_parser("probe", help="顯示媒體資訊")
    pr.add_argument("input")
    pr.add_argument("--silence", action="store_true", help="同時執行靜音偵測")
    _add_common(pr)
    pr.add_argument("--json", action="store_true")

    sv = sub.add_parser("serve", help="啟動網頁上傳介面")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--jobs-dir", help="上傳與輸出目錄（預設 ./summery_jobs）")
    sv.add_argument("--max-upload-mb", type=int, default=4096)
    sv.add_argument("--open", action="store_true", help="啟動後開啟瀏覽器")
    sv.add_argument("--static", help="前端靜態檔目錄（預設自動尋找 frontend/dist 或內建 web_static）")
    _add_common(sv)

    mk = sub.add_parser("mock-server", help="啟動本機模擬 ASR/LLM 伺服器（免 API key 試跑）")
    mk.add_argument("--host", default="127.0.0.1")
    mk.add_argument("--port", type=int, default=8899)
    mk.add_argument("--seed", type=int, default=0)
    mk.add_argument("--fail-rate", type=float, default=0.0)
    mk.add_argument("--slow-ms", type=int, default=0)
    mk.add_argument("--asr-error-first", type=int, default=0)

    mm = sub.add_parser("make-media", help="產生測試用影音（含靜音段，可用於驗證切塊）")
    mm.add_argument("--out", default="summery_test.mkv")
    mm.add_argument("--minutes", type=float, default=2.0)
    mm.add_argument("--segments", type=int, default=0, help="語音段數（預設依長度自動）")
    mm.add_argument("--video", action="store_true", help="附視頻軌（預設含色彩測試圖）")
    mm.add_argument("--no-video", action="store_true")
    mm.add_argument("--speech-tone", action="store_true", help="用正弦代替靜音段之間的「語音」")
    _add_common(mm)

    rs = sub.add_parser("jobs", help="列出已知工作與進度")
    _add_common(rs)
    return parser


# --------------------------------------------------------------------------
# 進度顯示
# --------------------------------------------------------------------------


class ProgressPrinter:
    def __init__(self, enabled: bool = True, *, tty: bool | None = None):
        self.enabled = enabled
        self.tty = sys.stdout.isatty() if tty is None else tty
        self._last = ""
        self._t0 = time.perf_counter()

    def __call__(self, stage: str, fraction: float, message: str) -> None:
        if not self.enabled:
            return
        frac = max(0.0, min(1.0, float(fraction)))
        bar = ""
        if self.tty:
            filled = int(frac * 28)
            bar = f"[{'#' * filled}{'.' * (28 - filled)}] {frac * 100:5.1f}%"
        line = f"{bar} {message}".strip()
        if self.tty:
            pad = max(0, len(self._last) - len(line))
            sys.stdout.write("\r" + line + " " * pad)
            sys.stdout.flush()
            self._last = line
            if frac >= 1.0:
                sys.stdout.write("\n")
                self._last = ""
        elif line and line != self._last:
            sys.stderr.write(f"{line}\n")
            self._last = line


# --------------------------------------------------------------------------
# 命令實作
# --------------------------------------------------------------------------


def _cfg_from_args(args: argparse.Namespace) -> Config:
    cfg = load_config(args.config)
    for pair in getattr(args, "sets", []) or []:
        apply_cli_overrides(cfg, "asr", [pair])
    if getattr(args, "work_dir", None):
        cfg.work_dir = args.work_dir
    chunk_seconds = getattr(args, "chunk_seconds", None)
    chunk_max = getattr(args, "chunk_max_seconds", None)
    if chunk_seconds:
        cfg.chunking.target_seconds = float(chunk_seconds)
    if chunk_max:
        cfg.chunking.max_seconds = float(chunk_max)
    if chunk_seconds or chunk_max:
        cfg.chunking.max_seconds = max(cfg.chunking.max_seconds, cfg.chunking.target_seconds)
    overlap = getattr(args, "overlap", None)
    if overlap is not None:
        cfg.chunking.overlap_seconds = float(overlap)
    if getattr(args, "no_silence_scan", None):
        cfg.chunking.skip_silence_scan = True
    if getattr(args, "log_level", None):
        cfg.log_level = args.log_level
    return cfg


def _options_from_args(args: argparse.Namespace) -> JobOptions:
    formats = [f.strip().lower() for f in str(getattr(args, "formats", "") or "").split(",") if f.strip()] or None
    return JobOptions(
        out_dir=getattr(args, "out_dir", None),
        job_id=getattr(args, "job_id", None),
        formats=formats,
        keep_chunks=True if getattr(args, "keep_chunks", False) else None,
        resume=not getattr(args, "no_resume", False),
        allow_partial=not getattr(args, "strict", False),
        skip_summary=getattr(args, "no_summary", False),
        dry_run=getattr(args, "dry_run", False) or getattr(args, "command", "") == "plan",
        asr_language=getattr(args, "asr_language", None),
        summary_language=getattr(args, "summary_language", None),
        summary_style=getattr(args, "style", None),
        custom_instruction=getattr(args, "instruction", None),
        chapters=False if getattr(args, "no_chapters", False) else None,
        audio_stream=getattr(args, "audio_stream", None),
        max_chunks=getattr(args, "max_chunks", None),
        concurrency=getattr(args, "concurrency", None),
        title_hint=getattr(args, "title", None),
        verify_ssl=not getattr(args, "no_ssl_verify", False),
    )


def _print_result(result: JobResult, *, as_json: bool = False) -> int:
    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print()
        print(f"工作 ID   : {result.job_id}")
        print(f"媒體長度  : {fmt_clock(result.duration, force_hours=True)}（{result.duration:.1f}s）")
        print(f"切塊      : {result.plan.get('count', 0)} 塊（靜音對齊 {result.plan.get('snapped', 0)}）")
        print(f"逐字稿    : {len(result.transcript.segments)} 段 / {result.transcript.char_count()} 字")
        if result.summary:
            print(f"摘要      : {result.summary.title}")
            print(f"            TL;DR {result.summary.tldr[:80]}")
            print(f"            章節 {len(result.summary.chapters)}、重點 {len(result.summary.key_points)}")
        print(f"耗時      : {result.elapsed:.1f}s"
              + (f"（{result.stats.get('realtime_factor')}x realtime）" if result.stats.get("realtime_factor") else ""))
        if result.stats.get("llm_usage"):
            usage = result.stats["llm_usage"]
            print(f"LLM 用量  : {usage.get('calls')} 次呼叫、{usage.get('total_tokens')} tokens")
        print("輸出檔案  :")
        for fmt, path in sorted(result.outputs.items()):
            print(f"  {fmt:11s} {path}")
        for warn in result.warnings:
            print(f"⚠ {warn}")
        for err in result.errors[:8]:
            print(f"✗ chunk {err.get('chunk')} ({err.get('start')}–{err.get('end')}s)：{err.get('error')}")
        if len(result.errors) > 8:
            print(f"  …另有 {len(result.errors) - 8} 個 chunk 失敗")
    if result.errors:
        return EXIT_PARTIAL
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _cfg_from_args(args)
    options = _options_from_args(args)
    printer = ProgressPrinter(enabled=not args.quiet)
    cancel = threading.Event()
    pipeline = Pipeline(cfg, options=options, progress=printer, cancel=cancel)
    _install_sigint(cancel)
    started = time.perf_counter()
    try:
        result = pipeline.run(args.input)
    except KeyboardInterrupt:
        sys.stderr.write("\n已取消（快取保留，可直接重跑續傳）\n")
        return EXIT_CANCELLED
    except (JobError, MediaError, ConfigError) as exc:
        sys.stderr.write(f"\n✗ 失敗：{exc}\n")
        return EXIT_ERROR
    printer("done", 1.0, f"完成（總計 {time.perf_counter() - started:.1f}s）")
    return _print_result(result, as_json=getattr(args, "json", False))


def cmd_transcribe(args: argparse.Namespace) -> int:
    args.no_summary = True
    if not args.formats:
        args.formats = "md,txt,json,srt,vtt"
    return cmd_run(args)


def cmd_summarize(args: argparse.Namespace) -> int:
    cfg = _cfg_from_args(args)
    path = Path(args.transcript).expanduser()
    if not path.is_file():
        sys.stderr.write(f"✗ 找不到逐字稿：{path}\n")
        return EXIT_ERROR
    from .transcript import Transcript, transcript_from_text
    from .llm import LLMClient
    from .summarizer import Summarizer

    raw = path.read_text(encoding="utf-8", errors="replace")
    transcript = Transcript.from_json(raw) if path.suffix.lower() == ".json" else transcript_from_text(
        raw, args.duration or max(60.0, len(raw) / 2.5)
    )
    options = _options_from_args(args)
    cfg = options.apply_to(cfg)
    printer = ProgressPrinter(enabled=not args.quiet)
    printer("summarize", 0.05, f"逐字稿 {transcript.char_count()} 字，開始摘要")
    cfg.require_api_key("llm")
    llm = LLMClient(cfg.llm, api_key=cfg.resolve_api_key("llm"), verify_ssl=not args.no_ssl_verify)
    summarizer = Summarizer(llm, cfg.summarize, progress=lambda stage, frac, msg: printer(stage, frac, msg))
    try:
        summary = summarizer.summarize(transcript, title_hint=args.title or path.stem)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"✗ 摘要失敗：{exc}\n")
        return EXIT_ERROR
    out_dir = Path(args.out_dir or path.parent).expanduser()
    cfg.output.formats = [f.strip().lower() for f in (args.formats or "md,json").split(",") if f.strip()]
    stem = cfg.output.stem or f"{slugify_stem(path.stem)}"
    summary_md = out_dir / f"{stem}.summary.md"
    summary_md.parent.mkdir(parents=True, exist_ok=True)
    summary_md.write_text(summary.to_markdown(duration=transcript.duration), encoding="utf-8")
    summary_json = out_dir / f"{stem}.summary.json"
    summary_json.write_text(summary.to_json() + "\n", encoding="utf-8")
    outputs = {"summary.md": str(summary_md), "summary.json": str(summary_json)}
    if path.suffix.lower() != ".json":
        # 輸入是純文字逐字稿：順便輸出正規化的 md/json，讓時間戳與格式與其他指令一致
        outputs.update(
            write_outputs(out_dir=out_dir, stem=stem, transcript=transcript,
                          summary=None, cfg=cfg, source_name=path.name)
        )
    if getattr(args, "json", False):
        print(json.dumps({"summary": summary.to_dict(), "outputs": outputs}, ensure_ascii=False, indent=2))
    else:
        print()
        print(f"標題      : {summary.title}")
        print(f"TL;DR     : {summary.tldr}")
        print(f"章節      : {len(summary.chapters)}")
        print(f"LLM 用量  : {summary.meta.get('llm_usage')}")
        print(f"模式      : {summary.meta.get('mode')}（{summary.meta.get('estimated_input_tokens')} tokens 估算）")
        print("輸出檔案  :")
        for fmt, p in sorted(outputs.items()):
            print(f"  {fmt:12s} {p}")
        for warn in summary.meta.get("warnings", []):
            print(f"⚠ {warn}")
    return EXIT_OK


def slugify_stem(name: str) -> str:
    from .util import slugify

    base = name
    for suffix in (".transcript", ".summary-less", ".summary"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return slugify(base or "transcript", 60)


def cmd_plan(args: argparse.Namespace) -> int:
    args.dry_run = True
    cfg = _cfg_from_args(args)
    # 與真正執行時走同一條路（含依 asr.max_upload_bytes 自動縮塊），預覽才不會和實跑不一致
    cfg = JobOptions().apply_to(cfg)
    info = probe(args.input, ffprobe_bin=cfg.ffprobe_bin)
    duration = info.duration or 0.0
    silences = []
    if not cfg.chunking.skip_silence_scan:
        printer = ProgressPrinter(enabled=not args.quiet)
        audio = Path(cfg.work_dir).expanduser() / "plan-audio" / "audio.wav"
        from .media import normalize_audio

        printer("audio", 0.1, "抽取音訊以估算靜音點")
        audio = normalize_audio(args.input, audio, cfg.audio, ffmpeg_bin=cfg.ffmpeg_bin,
                                audio_stream=args.audio_stream,
                                progress=lambda s, f: printer(s, 0.1 + 0.5 * f, f"抽取音訊 {f * 100:.0f}%"))
        printer("silence", 0.7, "掃描靜音點")
        silences = detect_silence(audio, noise_db=cfg.chunking.silence_noise_db,
                                  min_duration=cfg.chunking.silence_min_duration, ffmpeg_bin=cfg.ffmpeg_bin,
                                  total_duration=duration,
                                  progress=lambda s, f: printer(s, 0.7 + 0.3 * f, f"靜音掃描 {f * 100:.0f}%"))
    from .chunking import estimate_chunk_bytes, plan_chunks

    plan = plan_chunks(duration, silences, cfg.chunking)
    per_bytes = estimate_chunk_bytes(cfg.chunking.target_seconds, cfg.audio.sample_rate,
                                     cfg.audio.channels, cfg.audio.codec, cfg.audio.bitrate)
    if getattr(args, "json", False):
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"來源      : {args.input}")
    print(f"長度      : {fmt_clock(duration, force_hours=True)}（{duration:.1f}s）")
    print(f"音軌      : {len(info.audio_streams)} 條（{'含视频' if info.has_video else '純音訊'}，{fmt_bytes(info.size_bytes)}）")
    print(f"靜音點    : {len(silences)} 段可用")
    print(f"切塊      : {plan.count} 塊，目標 {cfg.chunking.target_seconds:.0f}s，上限 {cfg.chunking.max_seconds:.0f}s")
    print(f"chunk 檔  : 約 {fmt_bytes(per_bytes)}/塊（編碼 {cfg.audio.codec} {cfg.audio.bitrate}）")
    print(f"靜音對齊  : {plan.snapped} / {plan.count}")
    if plan.count:
        preview = ", ".join(f"{c.start:.0f}–{c.end:.0f}s" for c in plan.chunks[:8])
        print(f"區塊預覽  : {preview}" + (" …" if plan.count > 8 else ""))
    for warn in plan.warnings:
        print(f"⚠ {warn}")
    print(f"\n預估 ASR 請求：{plan.count} 次（並行 {cfg.asr.max_concurrency}）")
    return EXIT_OK


def cmd_probe(args: argparse.Namespace) -> int:
    cfg = _cfg_from_args(args)
    info = probe(args.input, ffprobe_bin=cfg.ffprobe_bin)
    silences = None
    if args.silence:
        silences = detect_silence(args.input, noise_db=cfg.chunking.silence_noise_db,
                                  min_duration=cfg.chunking.silence_min_duration, ffmpeg_bin=cfg.ffmpeg_bin,
                                  total_duration=info.duration)
    if getattr(args, "json", False):
        payload = {
            "path": info.path, "duration": info.duration, "format": info.format_name,
            "has_video": info.has_video, "size": info.size_bytes,
            "audio_streams": [vars(a) for a in info.audio_streams],
            "silences": len(silences) if silences is not None else None,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK
    from .media import describe

    print(describe(info, cfg))
    for a in info.audio_streams:
        print(f"  音軌 #{a.index}: {a.codec} {a.sample_rate}Hz {a.channels}ch {a.language or ''} {a.title or ''}")
    if silences is not None:
        usable = [s for s in silences if s.duration >= cfg.chunking.silence_min_duration]
        print(f"  靜音段: {len(silences)}（可用切點 {len(usable)}，d>= {cfg.chunking.silence_min_duration}s）")
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    from .web import serve

    cfg = _cfg_from_args(args)
    setup_logging(cfg.log_level)
    try:
        serve(cfg, host=args.host, port=args.port, jobs_dir=args.jobs_dir,
              max_upload_mb=args.max_upload_mb, open_browser=args.open, static_dir=args.static)
    except KeyboardInterrupt:
        print("\n已停止 Web 介面")
    return EXIT_OK


def cmd_mock_server(args: argparse.Namespace) -> int:
    from .mock_provider import main as mock_main

    return mock_main([
        "--host", args.host, "--port", str(args.port), "--seed", str(args.seed),
        "--fail-rate", str(args.fail_rate), "--slow-ms", str(args.slow_ms),
        "--asr-error-first", str(args.asr_error_first),
    ])


def cmd_make_media(args: argparse.Namespace) -> int:
    """以 ffmpeg 合成「語音段 + 靜音」的測試媒體（不依賴外部素材）。"""
    out = Path(args.out).expanduser()
    seconds = max(4.0, args.minutes * 60.0)
    seg_count = args.segments or max(2, int(round(seconds / 45)))
    seg_count = max(1, min(seg_count, 5000))
    cycle = seconds / seg_count
    speech = cycle * 0.85
    if speech < 0.5:
        raise SystemExit("語音段過短：請增加分鐘數或減少 --segments")
    expr = (
        f"0.30*sin(2*PI*(210+130*sin(2*PI*t/23))*t)"
        f"*lt(mod(t\\,{cycle:.4f})\\,{speech:.4f})"
    )
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y",
           "-f", "lavfi", "-i", f"aevalsrc='{expr}':d={seconds:.3f}:s=16000:c=1"]
    with_video = args.video or not args.no_video
    if with_video:
        cmd += ["-f", "lavfi", "-i", f"testsrc=size=320x180:rate=5:duration={seconds:.3f}",
                "-map", "0:a", "-map", "1:v", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-g", "60"]
    else:
        cmd += ["-map", "0:a"]
    cmd += ["-c:a", "pcm_s16le", str(out)]
    print(f"產生測試媒體：{out}（{seconds:.0f}s、{seg_count} 個語音段、每段 {speech:.1f}s + 靜音 {cycle - speech:.1f}s、video={'yes' if with_video else 'no'}）")
    if args.speech_tone:
        print("（提示：--speech-tone 僅為相容保留，預設已使用變頻正弦模擬語音）")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.stderr.write((res.stderr or "")[-1500:] + "\n")
        return EXIT_ERROR
    try:
        info = probe(out)
        print(f"完成：{out}｜{fmt_clock(info.duration or 0, force_hours=True)}｜{fmt_bytes(info.size_bytes)}")
    except MediaError as exc:
        print(f"完成但無法探測：{exc}")
    return EXIT_OK


def cmd_jobs(args: argparse.Namespace) -> int:
    cfg = _cfg_from_args(args)
    root = Path(cfg.work_dir).expanduser()
    if not root.is_dir():
        print(f"尚無工作目錄：{root}")
        return EXIT_OK
    rows = []
    for state_file in sorted(root.glob("*/state.json")):
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.append(data)
    rows.sort(key=lambda d: d.get("updated_at", 0), reverse=True)
    if not rows:
        print(f"尚無工作：{root}")
        return EXIT_OK
    print(f"{'狀態':10s} {'進度':>6s} {'階段':12s} {'更新':19s} 工作 ID")
    for data in rows[:40]:
        progress = float(data.get("progress") or 0.0)
        updated = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(data.get("updated_at", 0)))
        print(f"{data.get('status', '?'):10s} {progress * 100:5.1f}% {data.get('stage', '?'):12s} "
              f"{updated}  {data.get('job_id', '?')}")
        if data.get("message"):
            print(f"{'':31s}{data['message'][:110]}")
    return EXIT_OK


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def _install_sigint(cancel: threading.Event) -> None:
    state = {"count": 0}

    def handler(signum, frame):  # noqa: ANN001
        state["count"] += 1
        if state["count"] == 1:
            cancel.set()
            sys.stderr.write("\n正在取消（完成中的請求會先結束；再按一次立即中止）…\n")
        else:
            sys.stderr.write("\n強制中止\n")
            raise SystemExit(130)

    try:
        import signal

        signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):  # 非主執行緒
        pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # .env：目前目錄的 .env 自動載入（不覆蓋已存在的環境變數）；--env-file 會覆蓋
    # 關閉方式：--no-env-file，或環境變數 SUMMERY_DOTENV=0（測試 CI 常用）
    from .util import load_dotenv

    disabled = args.no_env_file or os.environ.get("SUMMERY_DOTENV", "").strip().lower() in {
        "0", "false", "no", "off"}
    auto = {} if disabled else load_dotenv()
    extra = load_dotenv(args.env_file, override=True) if args.env_file else {}
    if extra or (auto and os.environ.get("SUMMERY_DOTENV_VERBOSE")):
        print(f"[dotenv] 套用 {len(auto) + len(extra)} 個變數"
              + (f"（--env-file {args.env_file}）" if args.env_file else "（./.env）"), file=sys.stderr)
    if args.env_file and not Path(args.env_file).is_file():
        print(f"錯誤：找不到 --env-file 指定的檔案：{args.env_file}", file=sys.stderr)
        return EXIT_ERROR
    quiet = getattr(args, "quiet", False)
    level = getattr(args, "log_level", None) or ("WARNING" if quiet else "INFO")
    setup_logging(level)
    if hasattr(args, "log_level"):
        args.log_level = level
    handlers = {
        "run": cmd_run, "transcribe": cmd_transcribe, "summarize": cmd_summarize,
        "plan": cmd_plan, "probe": cmd_probe, "serve": cmd_serve,
        "mock-server": cmd_mock_server, "make-media": cmd_make_media, "jobs": cmd_jobs,
    }
    try:
        return handlers[args.command](args)
    except ConfigError as exc:
        sys.stderr.write(f"✗ 設定錯誤：{exc}\n")
        return EXIT_ERROR
    except KeyboardInterrupt:
        return EXIT_CANCELLED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
