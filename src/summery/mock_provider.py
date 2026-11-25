"""本機 mock：OpenAI 相容的 /audio/transcriptions 與 /chat/completions。

用途：
1. 沒有 API key 也能端到端驗證長影音管線（切塊 → 並行 ASR → 合併 → map-reduce 摘要）。
2. 可注入延遲／失敗率，驗證重試、部分失敗與 resume 行為。

啟動：``summery mock-server --port 8899``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_TOPICS = [
    "長影音切塊策略", "靜音點對齊", "並行轉錄與重試", "逐字稿時間戳合併",
    "map-reduce 摘要", "章節對齊", "專有名詞一致性", "輸出格式與字幕",
    "效能與成本估算", "錯誤處理與可觀測性", "模型選擇", "隱私與資料留存",
]
_FILLERS = [
    "也就是說", "這邊要補充一點", "我們可以看到", "重點在於",
    "實際上的做法是", "換個角度想",
]

_MAP_MARKERS = ("結構化筆記", "區塊筆記", "影音內容分析助手")
_REDUCE_MARKERS = ("彙整助手", "多份區塊筆記")
_FINAL_MARKERS = ("資深影音內容編輯",)


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def parse_multipart(body: bytes, content_type: str) -> dict[str, list[tuple[dict[str, str], bytes]]]:
    """解析 multipart/form-data（自己送的內容，用邊界精確切分）。"""
    match = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not match:
        raise ValueError("Content-Type 缺少 boundary")
    boundary = match.group(1).encode()
    out: dict[str, list[tuple[dict[str, str], bytes]]] = {}
    for raw in body.split(b"--" + boundary)[1:]:
        if raw.startswith(b"--"):
            break
        raw = raw[2:] if raw.startswith(b"\r\n") else raw
        head, sep, data = raw.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers: dict[str, str] = {}
        for line in head.decode("utf-8", "replace").splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
        name = re.search(r'name="([^"]*)"', headers.get("content-disposition", ""))
        if not name:
            continue
        out.setdefault(name.group(1), []).append((headers, data))
    return out


def probe_duration(path: str | Path, *, ffprobe_bin: str = "ffprobe") -> float | None:
    try:
        res = subprocess.run(
            [ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(res.stdout.strip()) if res.returncode == 0 and res.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def synth_sentences(seed_text: str, duration: float, *, language: str | None = None) -> list[dict]:
    """依音訊長度產生確定性的模擬 segments（文字內含絕對秒數方便驗證順序）。"""
    rng = random.Random(int(hashlib.sha256(seed_text.encode()).hexdigest()[:8], 16))
    seg_len = 6.0 + rng.random() * 4.0
    segments: list[dict] = []
    start = 0.0
    index = 0
    zh = not (language or "").lower().startswith("en")
    while start < max(duration, 1.0):
        end = min(duration, start + seg_len)
        topic = _TOPICS[(index + rng.randint(0, 3)) % len(_TOPICS)]
        filler = rng.choice(_FILLERS)
        sec = int(start)
        if zh:
            text = (
                f"模擬語音 {sec // 60:02d} 分 {sec % 60:02d} 秒：{filler}，"
                f"這一段討論{topic}，並給出第 {index + 1} 項具體做法。"
            )
        else:
            text = (
                f"Mock speech at {sec // 60:02d}:{sec % 60:02d}: {filler}, this segment "
                f"covers {topic} and lists step {index + 1}."
            )
        words = []
        n_words = max(3, int((end - start) / 0.45))
        for w in range(n_words):
            ws = start + (end - start) * w / n_words
            words.append({"word": text.split()[w] if not zh else text[w * len(text) // n_words : (w + 1) * len(text) // n_words],
                          "start": round(ws, 3), "end": round(start + (end - start) * (w + 1) / n_words, 3)})
        segments.append({
            "id": index, "seek": 0, "start": round(start, 3), "end": round(end, 3),
            "text": text, "words": words, "temperature": 0.0, "avg_logprob": -0.25,
            "compression_ratio": 1.6, "no_speech_prob": 0.01,
        })
        start = end + rng.choice([0.0, 0.2, 0.5])
        index += 1
        seg_len = 6.0 + rng.random() * 4.0
    return segments


# --------------------------------------------------------------------------
# LLM 模擬回應
# --------------------------------------------------------------------------


def _para_indices(text: str) -> list[int]:
    return [int(m) for m in re.findall(r"^\[(\d+)\]", text, re.M)]


def _note_indices(text: str) -> list[int]:
    return [int(m) for m in re.findall(r"\[筆記 (\d+)", text)]


def mock_chat_completion(body: dict[str, Any], *, rng: random.Random | None = None) -> dict[str, Any]:
    messages = body.get("messages") or []
    system = "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
    user = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
    marker = hashlib.sha256(user.encode()).hexdigest()[:6]
    paras = _para_indices(user)
    notes = _note_indices(user)
    json_mode = bool((body.get("response_format") or {}).get("type") == "json_object")

    if any(k in system for k in _MAP_MARKERS) or "逐字稿片段" in system:
        span = f"{paras[0]}-{paras[-1]}" if paras else "1-1"
        payload = {
            "summary": f"區塊 {marker}（段落 {span}）的重點：說明長影音處理的做法與注意事項。",
            "key_points": [f"區塊 {marker} 重點 A", f"區塊 {marker} 重點 B"],
            "terms": ["逐字稿", "切塊", "時間戳"],
            "issues": [],
        }
    elif any(k in system for k in _REDUCE_MARKERS):
        span = f"{notes[0]}-{notes[-1]}" if notes else "1-2"
        payload = {
            "summary": f"彙整筆記 {span}（標記 {marker}）：合併重複論點並保留數據。",
            "key_points": [f"彙整 {marker} 重點 1", f"彙整 {marker} 重點 2"],
            "terms": ["map-reduce", "摘要"],
            "issues": [],
        }
    elif any(k in system for k in _FINAL_MARKERS):
        total = max(paras) if paras else 10
        chunks = 4 if total >= 8 else 2
        size = max(1, total // chunks)
        chapters = []
        for i in range(chunks):
            start = i * size + 1
            end = total if i == chunks - 1 else min(total, (i + 1) * size)
            if start > total:
                break
            chapters.append({
                "para_start": start, "para_end": end,
                "title": f"{_TOPICS[i % len(_TOPICS)]}（段落 {start}-{end}）",
                "summary": f"這一章節涵蓋段落 {start} 到 {end}，說明對應流程與取捨。",
                "points": [f"段落 {start} 起的重點整理"],
            })
        payload = {
            "title": f"影音內容摘要（標記 {marker}）",
            "tldr": f"本內容由 mock LLM 產生，涵蓋 {total} 個編號段落。",
            "overview": [f"概要根据 {total} 個段落整理，標記 {marker}。", "第二段概要：說明切塊、轉錄與摘要三階段。"],
            "key_points": [f"重點 {i + 1}（標記 {marker}）" for i in range(4)],
            "chapters": chapters,
            "keywords": ["影音摘要", "逐字稿", "map-reduce", "mock"],
            "decisions": ["採用靜音點對齊切塊"],
            "action_items": [{"task": "以真實 API 驗證", "owner": "未標明", "due": "未標明"}],
            "open_questions": ["真實模型的斷句品質？"],
            "quotes": [{"text": "摘要必鬚忠於原文", "speaker": "未標明", "para": 1}],
        }
    else:
        payload = {"summary": f"mock 回應 {marker}", "key_points": []}

    text = json.dumps(payload, ensure_ascii=False)
    usage = {
        "prompt_tokens": max(1, len(user) // 4),
        "completion_tokens": max(1, len(text) // 4),
        "total_tokens": max(2, (len(user) + len(text)) // 4),
    }
    return {
        "id": f"chatcmpl-mock-{marker}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": (body.get("model") or "mock-llm"),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": usage,
        "_mock_json_mode": json_mode,
    }


def mock_transcription(body_fields: dict[str, list[tuple[dict[str, str], bytes]]], *, ffprobe_bin: str = "ffprobe") -> dict[str, Any]:
    file_parts = body_fields.get("file") or []
    if not file_parts:
        raise ValueError("缺少 file 欄位")
    _headers, blob = file_parts[0]
    with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as tmp:
        tmp.write(blob)
        tmp_path = tmp.name
    try:
        duration = probe_duration(tmp_path, ffprobe_bin=ffprobe_bin) or len(blob) / 3000.0
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    model = (body_fields.get("model") or [("", b"mock")])[0][1].decode()
    language = ((body_fields.get("language") or [("", b"zh")])[0][1].decode() or "zh")
    want_words = any(
        (v[1].decode() == "word") for v in body_fields.get("timestamp_granularity[]", [])
    )
    segments = synth_sentences(f"{len(blob)}-{model}-{language}", duration, language=language)
    if not want_words:
        segments = [{k: v for k, v in s.items() if k != "words"} for s in segments]
    text = "".join(s["text"] for s in segments)
    return {
        "task": "transcribe", "language": language, "duration": round(duration, 3),
        "text": text, "segments": segments, "model": model,
    }


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------


def make_handler(*, seed: int = 0, fail_rate: float = 0.0, slow_ms: int = 0, asr_error_first: int = 0, requests_log: list | None = None):
    counter = {"n": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "summery-mock/1"

        def log_message(self, fmt: str, *args: Any) -> None:  # 安靜
            pass

        # -- helpers ---------------------------------------------------
        def _send(self, code: int, payload: dict | list, *, extra_headers: dict | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            chunks = []
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        # -- routes ----------------------------------------------------
        def do_GET(self):  # noqa: N802
            if self.path in ("/health", "/v1/health", "/healthz"):
                self._send(200, {"status": "ok", "service": "summery-mock"})
            elif self.path == "/v1/models":
                self._send(200, {"object": "list", "data": [
                    {"id": "mock-whisper", "object": "model"}, {"id": "mock-llm", "object": "model"}]})
            else:
                self._send(404, {"error": {"message": f"未知路徑 {self.path}"}})

        def do_POST(self):  # noqa: N802
            with lock:
                counter["n"] += 1
                seq = counter["n"]
            body = self._read_body()
            if requests_log is not None:
                with lock:
                    requests_log.append({"seq": seq, "path": self.path, "bytes": len(body)})
            if slow_ms:
                time.sleep(slow_ms / 1000.0)
            if fail_rate and random.Random(seed + seq).random() < fail_rate:
                self._send(429, {"error": {"message": "mock rate limited"}}, extra_headers={"Retry-After": "0"})
                return
            try:
                if self.path.endswith("/audio/transcriptions"):
                    if asr_error_first and seq <= asr_error_first:
                        self._send(503, {"error": {"message": "mock temporary failure"}})
                        return
                    fields = parse_multipart(body, self.headers.get("Content-Type", ""))
                    self._send(200, mock_transcription(fields))
                elif self.path.endswith("/chat/completions"):
                    payload = json.loads(body.decode("utf-8"))
                    self._send(200, mock_chat_completion(payload))
                elif self.path.endswith("/audio/translations"):
                    fields = parse_multipart(body, self.headers.get("Content-Type", ""))
                    self._send(200, mock_transcription(fields))
                else:
                    self._send(404, {"error": {"message": f"未知路徑 {self.path}"}})
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"error": {"message": f"mock 內部錯誤：{exc}"}})

    return Handler


def start_mock_server(
    *, host: str = "127.0.0.1", port: int = 0, seed: int = 0, fail_rate: float = 0.0,
    slow_ms: int = 0, asr_error_first: int = 0, requests_log: list | None = None,
) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    handler = make_handler(seed=seed, fail_rate=fail_rate, slow_ms=slow_ms, asr_error_first=asr_error_first, requests_log=requests_log)
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="summery-mock", daemon=True)
    thread.start()
    actual = server.server_address[1]
    return server, thread, f"http://{host}:{actual}/v1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="summery 本機 mock ASR/LLM 伺服器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--fail-rate", type=float, default=0.0, help="0~1，隨機回 429 的機率")
    parser.add_argument("--slow-ms", type=int, default=0, help="每個請求延遲毫秒")
    parser.add_argument("--asr-error-first", type=int, default=0, help="前 N 個請求回 503（測重試）")
    parser.add_argument("--requests-log", default=None, help="把請求序號寫入指定 JSON 檔")
    args = parser.parse_args(argv)
    log: list = []
    server, thread, url = start_mock_server(
        host=args.host, port=args.port, seed=args.seed, fail_rate=args.fail_rate,
        slow_ms=args.slow_ms, asr_error_first=args.asr_error_first,
        requests_log=log if args.requests_log else None,
    )
    print(f"mock server: {url}")
    print(f"  export SUMMERY__ASR__BASE_URL={url}")
    print(f"  export SUMMERY__LLM__BASE_URL={url}")
    print(f"  export ASR_API_KEY=mock LLM_API_KEY=mock")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("bye")
    finally:
        if args.requests_log:
            Path(args.requests_log).write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        server.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
