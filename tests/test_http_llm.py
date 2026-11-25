"""HTTP 層、ASR 客戶端與 LLM 客戶端測試（含重試與降級路徑）。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from summery import _http
from summery.asr import ASRClient, ASRError
from summery.config import ASRConfig, LLMConfig
from summery.llm import LLMClient, LLMError, extract_json, parse_chat_response


class ScriptedServer:
    """依腳本回應的本地伺服器：records 記錄每次請求。"""

    def __init__(self, routes: dict):
        # path -> [responses]，response = (status, body, headers) 或 dict；單一 dict 會自動包成清單
        normalised = {}
        for key, value in routes.items():
            if isinstance(value, list):
                normalised[key] = value
            else:
                normalised[key] = [value]
        self.routes = normalised
        self.records: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args, **kwargs):
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                script = outer.routes.get(self.path)
                if not script:
                    self.send_error(404, "no route")
                    return
                prior = len([r for r in outer.records if r["path"] == self.path])
                entry = script[min(prior, len(script) - 1)]
                outer.records.append({"path": self.path, "body": body, "headers": dict(self.headers)})
                status, payload, headers = entry if isinstance(entry, tuple) else (200, entry, {})
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Content-Type", "application/json")
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------- URL / 工具
@pytest.mark.parametrize("base,path,expected", [
    ("https://x/v1", "chat/completions", "https://x/v1/chat/completions"),
    ("https://x/v1/", "chat/completions", "https://x/v1/chat/completions"),
    ("https://x/v1/chat/completions", "chat/completions", "https://x/v1/chat/completions"),
    ("https://x", "audio/transcriptions", "https://x/audio/transcriptions"),
])
def test_endpoint_joining(base, path, expected):
    assert _http.endpoint(base, path) == expected


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json("說明文字\n```json\n{\"a\": 2}\n```\n尾巴") == {"a": 2}
    assert extract_json('前言 {"a": [1, 2,], } 後語'.replace(", }", "}")) == {"a": [1, 2]}
    assert extract_json('[{"a": 3}]') == [{"a": 3}]
    with pytest.raises(ValueError):
        extract_json("完全沒有 JSON")


def test_parse_chat_response_shapes():
    res = parse_chat_response({"model": "m", "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 3, "completion_tokens": 4}})
    assert res.text == "hi" and res.input_tokens == 3 and res.output_tokens == 4
    blocks = parse_chat_response({"choices": [{"message": {"content": [{"text": "甲"}, {"text": "乙"}]}}]})
    assert blocks.text == "甲乙"
    with pytest.raises(LLMError):
        parse_chat_response({"choices": []})
    with pytest.raises(LLMError):
        parse_chat_response({"choices": [{"message": {"content": "   "}}]})


# ---------------------------------------------------------------- 重試
def test_retries_then_succeeds():
    server = ScriptedServer({
        "/v1/chat/completions": [
            (503, {"error": "down"}, {}),
            (429, {"error": "slow down"}, {"Retry-After": "0"}),
            {"choices": [{"message": {"content": "ok"}}]},
        ]
    })
    try:
        client = LLMClient(LLMConfig(base_url=server.url, max_retries=3, retry_backoff=1.0), api_key="k")
        result = client.complete("sys", "user")
        assert result.text == "ok"
        assert len(server.records) == 3
        assert client.usage.calls == 1 and client.usage.input_tokens > 0
    finally:
        server.close()


def test_gives_up_after_max_retries():
    server = ScriptedServer({"/v1/chat/completions": [(500, {"error": "boom"}, {})]})
    try:
        client = LLMClient(LLMConfig(base_url=server.url, max_retries=2, retry_backoff=1.0), api_key="k")
        with pytest.raises(LLMError, match="HTTP 500"):
            client.complete("sys", "user")
        assert len(server.records) == 2
    finally:
        server.close()


def test_client_error_is_not_retried():
    server = ScriptedServer({"/v1/chat/completions": [(401, {"error": "bad key"}, {})]})
    try:
        client = LLMClient(LLMConfig(base_url=server.url, max_retries=4, retry_backoff=1.0), api_key="k")
        with pytest.raises(LLMError):
            client.complete("s", "u")
        assert len(server.records) == 1
    finally:
        server.close()


def test_response_format_falls_back_when_unsupported():
    server = ScriptedServer({
        "/v1/chat/completions": [
            (400, {"error": {"message": "Unsupported parameter: 'response_format' is not supported by this model"}}, {}),
            {"choices": [{"message": {"content": "{\"ok\": true}"}}]},
        ]
    })
    try:
        client = LLMClient(LLMConfig(base_url=server.url, max_retries=2, retry_backoff=1.0), api_key="k")
        assert client.complete_json("s", "u") == {"ok": True}
        assert client._disable_response_format is True
        payloads = [json.loads(r["body"]) for r in server.records]
        assert "response_format" in payloads[0] and "response_format" not in payloads[-1]
    finally:
        server.close()


def test_json_repair_retry_when_model_returns_prose():
    server = ScriptedServer({
        "/v1/chat/completions": [
            {"choices": [{"message": {"content": "我無法產生 JSON，但重點如下"}}]},
            {"choices": [{"message": {"content": "```json\n{\"summary\": \"重試後成功\"}\n```"}}]},
        ]
    })
    try:
        client = LLMClient(LLMConfig(base_url=server.url, max_retries=1), api_key="k")
        assert client.complete_json("s", "u")["summary"] == "重試後成功"
        assert len(server.records) == 2
    finally:
        server.close()


def test_truncated_output_is_retried_with_larger_budget():
    server = ScriptedServer({
        "/v1/chat/completions": [
            {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
            {"choices": [{"message": {"content": "{\"ok\": 1}"}, "finish_reason": "stop"}]},
        ]
    })
    try:
        client = LLMClient(LLMConfig(base_url=server.url, max_output_tokens=1000, retry_backoff=1.0), api_key="k")
        assert client.complete_json("s", "u") == {"ok": 1}
        budgets = [json.loads(r["body"])["max_tokens"] for r in server.records]
        assert budgets[0] == 1000 and budgets[1] > budgets[0]
        assert client.usage.calls == 2  # 截斷那次也要計入成本
    finally:
        server.close()


def test_persistent_truncation_raises_actionable_error():
    server = ScriptedServer({
        "/v1/chat/completions": [
            {"choices": [{"message": {"content": "一半"}, "finish_reason": "length"}]}
        ]
    })
    try:
        client = LLMClient(LLMConfig(base_url=server.url, max_output_tokens=1000, retry_backoff=1.0), api_key="k")
        with pytest.raises(LLMError, match="調高 llm.max_output_tokens"):
            client.complete_json("s", "u")
        assert len(server.records) == 2  # 原預算 + 加大預算各一次
    finally:
        server.close()


# ---------------------------------------------------------------- ASR
def test_asr_multipart_fields_and_auth(tmp_path):
    server = ScriptedServer({
        "/v1/audio/transcriptions": [{"text": "轉錄結果", "language": "chinese",
                                      "segments": [{"start": 0, "end": 2, "text": "轉錄結果"}]}]
    })
    try:
        cfg = ASRConfig(base_url=server.url, model="large-v3", response_format="verbose_json",
                        timestamp_granularity=["segment", "word"])
        client = ASRClient(cfg, api_key="secret-key")
        audio = tmp_path / "chunk.m4a"
        audio.write_bytes(b"FAKEAUDIO" * 40)
        outcome = client.transcribe_file(audio, language="zh")
        assert outcome.text == "轉錄結果" and outcome.language == "chinese"
        body = server.records[0]["body"].decode("utf-8", "replace")
        assert 'name="model"' in body and "large-v3" in body
        assert body.count('name="timestamp_granularity[]"') == 2
        assert 'name="language"' in body and "zh" in body
        assert 'name="file"' in body and "chunk.m4a" in body
        assert server.records[0]["headers"]["Authorization"] == "Bearer secret-key"
    finally:
        server.close()


def test_asr_rejects_oversized_chunk(tmp_path):
    cfg = ASRConfig(base_url="http://127.0.0.1:1/v1", max_upload_bytes=100)
    client = ASRClient(cfg, api_key="k")
    audio = tmp_path / "big.wav"
    audio.write_bytes(b"x" * 500)
    with pytest.raises(ASRError, match="超過上傳上限"):
        client.transcribe_file(audio)


def test_asr_stub_dir(tmp_path):
    stub = tmp_path / "stubs"
    stub.mkdir()
    (stub / "chunk_00003.json").write_text(json.dumps({"text": "離線結果"}), encoding="utf-8")
    client = ASRClient(ASRConfig(stub_dir=str(stub)), api_key=None)
    audio = tmp_path / "chunk_3.wav"
    audio.write_bytes(b"x" * 10)
    assert client.transcribe_file(audio, stub_key="chunk_00003").text == "離線結果"


def test_asr_bad_payload_is_not_retryable():
    server = ScriptedServer({"/v1/audio/transcriptions": {"unexpected": 1}})
    try:
        client = ASRClient(ASRConfig(base_url=server.url), api_key="k")
        import pathlib, tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            fh.write(b"x" * 100)
            name = fh.name
        try:
            with pytest.raises(ASRError, match="無法解析"):
                client.transcribe_file(pathlib.Path(name))
            assert len(server.records) == 1
        finally:
            os.unlink(name)
    finally:
        server.close()


def test_asr_error_recognises_payload_too_large():
    """413 要能被辨識出來：縮小同一塊重送有意義，原封不動重送沒有意義。"""
    from summery.asr import ASRError

    nginx = ASRError('HTTP 413：<html><h1>413 Request Entity Too Large</h1></html>')
    assert nginx.too_large is True

    cause = RuntimeError("gateway said no")
    cause.status = 413  # 準 http 錯誤物件會帶 status
    with_status = ASRError("上傳失敗")
    with_status.__cause__ = cause
    assert with_status.too_large is True

    for message in ("HTTP 400: Invalid or unsupported audio file", "HTTP 429: rate limited", "連線逾時"):
        assert ASRError(message).too_large is False
