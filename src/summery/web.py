"""最小 Web UI（stdlib http.server，零相依）。

* 上傳影片/音訊 → 背景執行管線 → 輪詢進度 → 顯示逐字稿與摘要、下載各格式。
* 上傳探用 raw body（``X-Filename`` 帶檔名），避免 multipart 解析的邊角問題。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import mimetypes
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import Config, ConfigError
from .pipeline import JobError, JobOptions, Pipeline
from .util import LOG, fmt_clock, read_json, slugify, write_json

STATIC_DIR = Path(__file__).parent / "web_static"
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".ts", ".mpg", ".mpeg"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma", ".amr", ".aiff", ".caf"}
MAX_INLINE_CHARS = 200_000


def resolve_static_dir(explicit: str | Path | None = None) -> Path:
    """尋找前端靜態檔位置，依優先序：參數 → SUMMERY_WEB_STATIC → frontend/dist → 套件內 web_static。

    ``frontend/dist`` 是 Vite 的建置輸出（開發時用）；包裝成 wheel 後則由
    ``web_static`` 提供（``deploy.sh`` 會把建置結果同步過去）。
    """
    here = Path(__file__).resolve()
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env = os.environ.get("SUMMERY_WEB_STATIC", "").strip()
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(here.parent.parent.parent / "frontend" / "dist")
    candidates.append(here.parent / "web_static")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return STATIC_DIR


@dataclass
class JobRecord:
    job_id: str
    name: str
    input_path: str
    work_dir: str
    out_dir: str
    cancel: threading.Event = field(default_factory=threading.Event)
    status: str = "queued"
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    options: dict = field(default_factory=dict)
    historical: bool = False

    def input_path_for_web(self) -> str | None:
        """可供瀏覽器播放的原始檔路徑（歷史工作的檔案可能已被移走）。"""
        path = Path(self.input_path)
        return str(path) if path.is_file() else None

    def to_dict(self, state: dict | None = None) -> dict:
        data = {
            "job_id": self.job_id, "name": self.name, "status": self.status,
            "created_at": self.created_at, "error": self.error,
            "input": Path(self.input_path).name, "out_dir": self.out_dir,
            "options": self.options,
        }
        data.update({
            "stage": (state or {}).get("stage"),
            "progress": (state or {}).get("progress", 1.0 if self.status in ("done", "partial") else 0.0),
            "message": (state or {}).get("message", ""),
            "counts": (state or {}).get("counts", {}),
            "outputs": (state or {}).get("outputs", {}),
            "errors": (state or {}).get("errors", []),
            "updated_at": (state or {}).get("updated_at"),
            "finished_at": (state or {}).get("finished_at"),
            "historical": self.historical,
        })
        return data


class JobQueue:
    """單执行序的背景佇列（長影音很吃頻寬與 CPU，預設一次跑一件）。"""

    def __init__(self, cfg: Config, jobs_dir: Path, *, workers: int = 1):
        self.cfg = cfg
        self.jobs_dir = jobs_dir
        self.queue: "queue.Queue[str]" = queue.Queue()
        self.jobs: dict[str, JobRecord] = {}
        self.lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.workers = max(1, workers)
        self._load_existing()

    def _load_existing(self) -> None:
        """把磁碟上的舊工作登陸為唯讀紀錄（服務重啟後 UI 仍可看歷史與下載）。"""
        if not self.jobs_dir.is_dir():
            return
        for child in sorted(self.jobs_dir.iterdir(), reverse=True):
            if not child.is_dir():
                continue
            state = read_json(Path(self.cfg.work_dir).expanduser() / child.name / "state.json")
            state = state if isinstance(state, dict) else {}
            inputs = sorted(p for p in child.iterdir() if p.suffix and not p.name.startswith("."))
            if not state and not inputs:
                continue
            options = dict(state.get("options") or {})
            options["job_id"] = child.name
            options.setdefault("stem", slugify(child.name, 60))
            rec = JobRecord(
                job_id=child.name,
                name=Path(state.get("input") or (inputs[0].name if inputs else child.name)).name,
                input_path=str(state.get("input") or (inputs[0] if inputs else child)),
                work_dir=str(Path(self.cfg.work_dir).expanduser() / child.name),
                out_dir=str(child),
                status=str(state.get("status") or "unknown"),
                created_at=child.stat().st_mtime,
                options=options,
                historical=True,
            )
            self.jobs[rec.job_id] = rec

    def start(self) -> None:
        for i in range(self.workers):
            t = threading.Thread(target=self._worker, name=f"summery-worker-{i}", daemon=True)
            t.start()
            self.threads.append(t)

    def add(self, record: JobRecord) -> None:
        with self.lock:
            self.jobs[record.job_id] = record
        self.queue.put(record.job_id)

    def get(self, job_id: str) -> JobRecord | None:
        with self.lock:
            return self.jobs.get(job_id)

    def list(self) -> list[dict]:
        with self.lock:
            records = list(self.jobs.values())
        out = []
        for rec in sorted(records, key=lambda r: r.created_at, reverse=True):
            state = read_json(Path(rec.work_dir) / "state.json") or {}
            out.append(rec.to_dict(state if isinstance(state, dict) else None))
        return out

    def cancel(self, job_id: str) -> bool:
        rec = self.get(job_id)
        if not rec or rec.historical or rec.status not in ("queued", "running", "cancelling"):
            return False
        rec.cancel.set()
        rec.status = "cancelling"
        return True

    def _worker(self) -> None:
        while True:
            job_id = self.queue.get()
            rec = self.get(job_id)
            if rec is None:
                self.queue.task_done()
                continue
            rec.status = "running"
            LOG.info("開始工作 %s（%s）", job_id, rec.name)
            try:
                valid = {f.name for f in dataclasses.fields(JobOptions)}
                options = JobOptions(**{k: v for k, v in rec.options.items() if k in valid})
                pipeline = Pipeline(self.cfg, options=options, cancel=rec.cancel)
                result = pipeline.run(rec.input_path)
                rec.status = "partial" if result.partial else "done"
                LOG.info("工作 %s 完成：%s", job_id, rec.status)
            except KeyboardInterrupt:
                rec.status = "cancelled"
                rec.error = "已取消"
            except Exception as exc:  # noqa: BLE001
                rec.status = "failed"
                rec.error = f"{type(exc).__name__}: {exc}"
                LOG.error("工作 %s 失敗：%s", job_id, exc)
            finally:
                self.queue.task_done()


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------


def make_handler(cfg: Config, queue_obj: JobQueue, jobs_dir: Path, *, max_upload_mb: int = 4096):
    limit_bytes = int(max_upload_mb * 1024 * 1024)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "summery-web/1"

        def log_message(self, fmt: str, *args: Any) -> None:
            LOG.debug("web %s - %s", self.address_string(), fmt % args)

        # -- 回應輔助 ---------------------------------------------------
        def _json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _text(self, text: str, status: int = 200, *, content_type: str = "text/plain; charset=utf-8") -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path, *, download: bool = True, immutable: bool = False) -> None:
            """送出檔案；支援 Range 讓瀏覽器可以串流與拖曳進度條。"""
            if not path.is_file():
                self._text("檔案不存在", 404)
                return
            size = path.stat().st_size
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if path.suffix in {".md", ".txt", ".srt", ".vtt"}:
                ctype = "text/plain; charset=utf-8"
            elif path.suffix == ".js":
                ctype = "text/javascript; charset=utf-8"
            elif path.suffix == ".css":
                ctype = "text/css; charset=utf-8"
            elif path.suffix == ".svg":
                ctype = "image/svg+xml"

            start, end, partial = 0, size - 1, False
            match = RANGE_RE.match(self.headers.get("Range") or "")
            if match and (match.group(1) or match.group(2)):
                head = int(match.group(1) or 0)
                tail = int(match.group(2)) if match.group(2) else size - 1
                if head > tail or head >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, end, partial = head, min(tail, size - 1), True

            length = end - start + 1
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if immutable:
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            if download:
                quoted = urllib.parse.quote(path.name)
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with path.open("rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    remaining -= len(chunk)

        def _static_root(self) -> Path:
            return getattr(self.server, "static_dir", STATIC_DIR)

        def _serve_static(self, path: str) -> bool:
            """提供前端靜態檔；找不到對應檔案時回到 index.html（SPA 深連結）。"""
            root = self._static_root().resolve()
            rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
            target = (root / rel).resolve()
            if target != root and not str(target).startswith(str(root) + os.sep):
                self._text("不接受的路徑", 400)
                return True
            if not target.is_file():
                target = root / "index.html"
                if not target.is_file():
                    return False
            self._file(target, download=False, immutable=str(root / "assets") in str(target))
            return True

        def _params(self) -> dict[str, str]:
            query = urllib.parse.urlparse(self.path).query
            return {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}

        def _route(self) -> tuple[str, dict[str, str]]:
            parsed = urllib.parse.urlparse(self.path)
            return parsed.path.rstrip("/") or "/", self._params()

        # -- GET --------------------------------------------------------
        def do_GET(self):  # noqa: N802
            path, params = self._route()
            if path == "/api/health":
                self._json({"status": "ok", "version": _version(), "time": time.time()})
                return
            if path == "/api/config":
                self._json(_config_summary(cfg))
                return
            if path == "/api/jobs":
                self._json({"jobs": queue_obj.list(), "config": _config_summary(cfg)})
                return
            parts = [p for p in path.split("/") if p]
            if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                self._job_detail(parts[2])
                return
            if len(parts) == 5 and parts[:2] == ["api", "jobs"] and parts[3] == "files":
                self._job_file(parts[2], parts[4])
                return
            if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "media":
                self._job_media(parts[2])
                return
            if path.startswith("/api/"):
                self._json({"error": f"未知路徑 {path}"}, 404)
                return
            if self._serve_static(path):
                return
            self._json({"error": f"未知路徑 {path}"}, 404)

        def _job_media(self, job_id: str) -> None:
            """串流上傳的原始影音，讓逐字稿的時間戳可以跳著聽。"""
            rec = queue_obj.get(job_id)
            if not rec:
                self._text("找不到該工作", 404)
                return
            source = rec.input_path_for_web()
            if not source:
                self._text("原始檔案已不在原本的位置，無法播放", 404)
                return
            self._file(Path(source), download=False)

        def _job_detail(self, job_id: str) -> None:
            rec = queue_obj.get(job_id)
            if not rec:
                self._json({"error": "找不到該工作"}, 404)
                return
            state = read_json(Path(rec.work_dir) / "state.json") or {}
            detail = rec.to_dict(state if isinstance(state, dict) else None)
            stem = Path(rec.options.get("stem") or slugify(Path(rec.name).stem, 60))
            out_dir = Path(rec.out_dir)
            files = []
            for fmt in ("summary.md", "summary.json", "md", "json", "txt", "srt", "vtt"):
                found = _find_output(out_dir, stem, fmt)
                if found:
                    files.append({"format": fmt, "name": found.name, "size": found.stat().st_size,
                                  "url": f"/api/jobs/{job_id}/files/{urllib.parse.quote(found.name)}"})
            detail["files"] = files
            detail["result"] = _load_result(out_dir, stem)
            transcript_path = _find_output(out_dir, stem, "json")
            duration = None
            if transcript_path:
                data = read_json(transcript_path)
                if isinstance(data, dict):
                    duration = data.get("duration")
            detail["duration"] = duration
            detail["media_url"] = f"/api/jobs/{job_id}/media" if rec.input_path_for_web() else None
            self._json(detail)

        def _job_file(self, job_id: str, name: str) -> None:
            rec = queue_obj.get(job_id)
            if not rec:
                self._text("找不到該工作", 404)
                return
            target = (Path(rec.out_dir) / urllib.parse.unquote(name)).resolve()
            allowed = Path(rec.out_dir).resolve()
            if not str(target).startswith(str(allowed)):
                self._text("不接受的路徑", 400)
                return
            self._file(target)

        # -- POST -------------------------------------------------------
        def do_POST(self):  # noqa: N802
            path, params = self._route()
            parts = [p for p in path.split("/") if p]
            if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
                if queue_obj.cancel(parts[2]):
                    self._json({"ok": True})
                else:
                    self._json({"error": "找不到該工作（可能服務已重啟）"}, 404)
                return
            if path == "/api/jobs":
                self._create_job(params)
                return
            self._json({"error": f"未知路徑 {path}"}, 404)

        def _create_job(self, params: dict[str, str]) -> None:
            raw_name = self.headers.get("X-Filename") or params.get("name") or "input.mp4"
            filename = urllib.parse.unquote(raw_name)
            ext = Path(filename).suffix.lower()
            if ext not in VIDEO_EXT | AUDIO_EXT:
                self._json({"error": f"不支援的檔案類型 {ext or '(無副檔名)'}"}, 400)
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                self._json({"error": "上傳內容為空（需要 Content-Length）"}, 400)
                return
            if length > limit_bytes:
                self._json({"error": f"檔案過大 {length / 1e6:.0f}MB，上限 {max_upload_mb}MB"}, 413)
                return
            safe_stem = slugify(Path(filename).stem, 48) or "upload"
            # job_id 會出現在 URL 路徑上 → 僅使用 ASCII，原始檔名仍記錄在 record.name
            tag = slugify(Path(filename).stem, 24, ascii_only=True)
            tag = "upload" if tag in ("", "job") else tag  # 純中文檔名沒有 ASCII 可轉
            fingerprint = hashlib.sha1(f"{filename}:{length}".encode("utf-8")).hexdigest()[:6]
            nonce = f"{time.time_ns() % 1_000_000:06d}"  # 避免同一秒重複上傳時 job_id 撞號
            rec_job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}-{fingerprint}-{nonce}"
            job_dir = jobs_dir / rec_job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            input_path = job_dir / f"{safe_stem}{ext}"
            received = 0
            with input_path.open("wb") as fh:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 1 << 20))
                    if not chunk:
                        break
                    fh.write(chunk)
                    received += len(chunk)
                    remaining -= len(chunk)
            if received != length:
                self._json({"error": f"上傳不完整（{received}/{length} bytes）"}, 400)
                return
            options = _options_from_params(params, stem=safe_stem)
            options["job_id"] = rec_job_id  # 讓 state.json 落在 record.work_dir，UI 才看得到進度
            rec = JobRecord(
                job_id=job_dir.name, name=filename, input_path=str(input_path),
                work_dir=str(Path(cfg.work_dir).expanduser() / job_dir.name),
                out_dir=str(job_dir), options=options,
            )
            queue_obj.add(rec)
            self._json({"job_id": rec.job_id, "name": rec.name, "status": rec.status, "size": received}, 202)

    return Handler


def _options_from_params(params: dict[str, str], *, stem: str) -> dict[str, Any]:
    options: dict[str, Any] = {"resume": True, "allow_partial": True}
    if params.get("style"):
        options["summary_style"] = params["style"]
    if params.get("summary_language"):
        options["summary_language"] = params["summary_language"]
    if params.get("asr_language"):
        options["asr_language"] = params["asr_language"]
    if params.get("instruction"):
        options["custom_instruction"] = params["instruction"]
    if params.get("chunk_seconds"):
        try:
            options["chunk_seconds"] = float(params["chunk_seconds"])
        except ValueError:
            pass
    if params.get("concurrency"):
        try:
            options["concurrency"] = max(1, int(params["concurrency"]))
        except ValueError:
            pass
    if params.get("no_summary") in ("1", "true", "yes"):
        options["skip_summary"] = True
    if params.get("no_chapters") in ("1", "true", "yes"):
        options["chapters"] = False
    if params.get("strict") in ("1", "true", "yes"):
        options["allow_partial"] = False
    options["stem"] = stem
    return options


def _find_output(out_dir: Path, stem: str, fmt: str) -> Path | None:
    candidates = {
        "md": [f"{stem}.transcript.md"], "json": [f"{stem}.transcript.json"],
        "txt": [f"{stem}.txt"], "srt": [f"{stem}.srt"], "vtt": [f"{stem}.vtt"],
        "summary.md": [f"{stem}.summary.md"], "summary.json": [f"{stem}.summary.json"],
    }.get(fmt, [f"{stem}.{fmt}"])
    for name in candidates:
        path = out_dir / name
        if path.is_file():
            return path
    # 名稱可能因 slug 差異而不同 → 退而求其次比對後綴
    suffix = {"md": ".transcript.md", "json": ".transcript.json", "txt": ".txt", "srt": ".srt",
              "vtt": ".vtt", "summary.md": ".summary.md", "summary.json": ".summary.json"}.get(fmt)
    if suffix:
        matches = sorted(out_dir.glob(f"*{suffix}"), key=lambda p: p.stat().st_mtime, reverse=True)
        if matches:
            return matches[0]
    return None


def _load_result(out_dir: Path, stem: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    summary_path = _find_output(out_dir, stem, "summary.json")
    if summary_path:
        data = read_json(summary_path)
        if isinstance(data, dict):
            result["summary"] = data
    transcript_path = _find_output(out_dir, stem, "json")
    if transcript_path:
        data = read_json(transcript_path)
        if isinstance(data, dict):
            segments = data.get("segments") or []
            text = "\n".join(
                f"[{_clock(s.get('start', 0))}] {s.get('text', '')}" for s in segments
            )
            result["transcript"] = {
                "duration": data.get("duration"), "stats": data.get("stats"),
                "segments": len(segments), "text": text[:MAX_INLINE_CHARS],
                "truncated": len(text) > MAX_INLINE_CHARS,
            }
    md_path = _find_output(out_dir, stem, "summary.md")
    if md_path:
        result["summary_markdown"] = md_path.read_text(encoding="utf-8")[:MAX_INLINE_CHARS]
    return result


def _clock(seconds: Any) -> str:
    try:
        return fmt_clock(float(seconds), force_hours=True)
    except (TypeError, ValueError):
        return "00:00:00"


def _limits_summary(cfg: Config) -> dict[str, Any]:
    """把「上傳上限 → 每塊最長秒數」的推算結果給前端顯示，避免使用者選到會被自動縮短的長度。"""
    from .chunking import max_seconds_for_bytes

    budget = max_seconds_for_bytes(
        cfg.asr.max_upload_bytes, cfg.audio.sample_rate, cfg.audio.channels, cfg.audio.codec, cfg.audio.bitrate
    )
    effective = min(float(cfg.chunking.target_seconds), budget)
    return {
        "max_upload_bytes": int(cfg.asr.max_upload_bytes),
        "chunk_seconds_cap": None if budget == float("inf") else round(budget, 1),
        "effective_chunk_seconds": round(effective, 1),
        "audio_codec": cfg.audio.codec,
        "audio_bitrate": cfg.audio.bitrate,
    }


def _config_summary(cfg: Config) -> dict[str, Any]:
    return {
        "asr": {"base_url": cfg.asr.base_url, "model": cfg.asr.model, "stub": bool(cfg.asr.stub_dir),
                "key_configured": bool(cfg.resolve_api_key("asr"))},
        "llm": {"base_url": cfg.llm.base_url, "model": cfg.llm.model, "stub": bool(cfg.llm.stub_dir),
                "key_configured": bool(cfg.resolve_api_key("llm"))},
        "chunking": dataclasses.asdict(cfg.chunking),
        "limits": _limits_summary(cfg),
        "summarize": dataclasses.asdict(cfg.summarize),
        "work_dir": str(Path(cfg.work_dir).expanduser().resolve()),
    }


def _version() -> str:
    from . import __version__

    return __version__


# --------------------------------------------------------------------------
# 啟動
# --------------------------------------------------------------------------


class _Server(ThreadingHTTPServer):
    """標準多线程 HTTP 伺服器，但對「客戶端自己斷線」保持安靜。

    瀏覽器關閉分頁、重新整理或取消下載時會直接送 RST，標準函式庫會把
    ConnectionResetError 整段 traceback 打到終端機，看起來像服務出錯，
    其實是完全正常的行為，只有真正的錯誤值得打出來。
    """

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):  # noqa: ANN001, D102
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def build_server(
    cfg: Config, *, host: str = "127.0.0.1", port: int = 8765, jobs_dir: str | Path | None = None,
    workers: int = 1, max_upload_mb: int = 4096, static_dir: str | Path | None = None,
) -> tuple[ThreadingHTTPServer, JobQueue, Path]:
    """建立（但未啟動）HTTP server 與工作佇列。"""
    jobs_root = Path(jobs_dir or "summery_jobs").expanduser()
    jobs_root.mkdir(parents=True, exist_ok=True)
    Path(cfg.work_dir).expanduser().mkdir(parents=True, exist_ok=True)
    queue_obj = JobQueue(cfg, jobs_root, workers=workers)
    queue_obj.start()
    handler = make_handler(cfg, queue_obj, jobs_root, max_upload_mb=max_upload_mb)
    httpd = _Server((host, port), handler)
    httpd.static_dir = resolve_static_dir(static_dir)  # type: ignore[attr-defined]
    return httpd, queue_obj, jobs_root


def serve(
    cfg: Config, *, host: str = "127.0.0.1", port: int = 8765, jobs_dir: str | Path | None = None,
    workers: int = 1, max_upload_mb: int = 4096, open_browser: bool = False,
    static_dir: str | Path | None = None,
) -> None:
    httpd, _queue_obj, jobs_root = build_server(
        cfg, host=host, port=port, jobs_dir=jobs_dir, workers=workers,
        max_upload_mb=max_upload_mb, static_dir=static_dir,
    )
    url = f"http://{host}:{httpd.server_address[1]}/"
    summary = _config_summary(cfg)
    static_root: Path = httpd.static_dir  # type: ignore[attr-defined]
    print(f"summery Web UI → {url}")
    print(f"  前端：{static_root}")
    if static_root.resolve() == STATIC_DIR.resolve() and not (STATIC_DIR / "assets").is_dir():
        print("  （目前是陽春介面；在 frontend/ 執行 pnpm run build 可啟用新版頁面）")
    print(f"  工作目錄：{summary['work_dir']}｜上傳目錄：{jobs_root}")
    if not summary["asr"]["key_configured"] and not summary["asr"]["stub"]:
        print("  ⚠ 未設定 ASR API key（SUMMERY__ASR__API_KEY / ASR_API_KEY），工作會失敗")
    if not summary["llm"]["key_configured"] and not summary["llm"]["stub"]:
        print("  ⚠ 未設定 LLM API key（SUMMERY__LLM__API_KEY / LLM_API_KEY），摘要會跳過")
    print("  Ctrl+C 停止")
    if open_browser:
        threading.Timer(0.6, lambda: _open_browser(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()


def _open_browser(url: str) -> None:
    import webbrowser

    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
