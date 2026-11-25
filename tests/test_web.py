"""Web UI 測試：上傳 → 背景處理 → 輪詢進度 → 下載結果。"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from summery import web
from summery.config import Config


@pytest.fixture
def web_server(base_config, tmp_path, media_factory):
    base_config.chunking.target_seconds = 20.0
    base_config.chunking.min_seconds = 6.0
    base_config.chunking.silence_snap_window = 10.0
    httpd, queue_obj, jobs_root = web.build_server(
        base_config, host="127.0.0.1", port=0, jobs_dir=tmp_path / "jobs", workers=1, max_upload_mb=64
    )
    thread = threading.Thread(target=httpd.serve_forever, name="test-web", daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, queue_obj, jobs_root
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.status, resp.read()


def post(url: str, body: bytes, headers: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw or b"{}")
    return 200, json.loads(raw or b"{}")


def wait_for_done(base: str, job_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        status, raw = get(f"{base}/api/jobs/{job_id}")
        last = json.loads(raw)
        if last.get("status") in ("done", "partial", "failed", "cancelled"):
            return last
        time.sleep(0.4)
    raise AssertionError(f"工作未在 {timeout}s 內完成：{last}")


# ------------------------------------------------------------------ 基本頁面
def raw_request(url: str, *, headers: dict | None = None) -> tuple[int, dict, bytes]:
    """可自訂標頭的 GET，用來檢查 Range 與快取標頭。"""
    parsed = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=30)
    conn.request("GET", parsed.path or "/", headers=headers or {})
    resp = conn.getresponse()
    body = resp.read()
    fields = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, fields, body


def test_index_serves_built_app(web_server):
    """根路徑應提供 Vite 建置好的前端（含 #root 與 /assets/ 資源）。"""
    base, _q, _jobs = web_server
    status, raw = get(base + "/")
    assert status == 200
    html = raw.decode("utf-8")
    assert "<title>summery" in html
    assert '<div id="root">' in html
    assert "/assets/" in html
    assert 'lang="zh-Hant"' in html


def test_built_assets_are_served_with_immutable_cache(web_server):
    base, _q, _jobs = web_server
    html = get(base + "/")[1].decode("utf-8")
    asset = next(
        part.split('"')[0]
        for part in html.split('src="')[1:]
        if part.startswith("/assets/")
    )
    status, headers, body = raw_request(base + asset)
    assert status == 200 and len(body) > 1000
    assert headers["content-type"].startswith(("text/javascript", "application/javascript", "text/css"))
    assert "immutable" in headers.get("cache-control", "")
    assert headers.get("accept-ranges") == "bytes"


def test_deep_link_falls_back_to_app_shell(web_server):
    """SPA 的深連結（不是 API 也不是實體檔）應回到 index.html。"""
    base, _q, _jobs = web_server
    status, headers, body = raw_request(base + "/jobs/some-deep-link")
    assert status == 200
    assert headers["content-type"].startswith("text/html")
    assert b'<div id="root">' in body


def test_static_traversal_is_rejected(web_server):
    base, _q, _jobs = web_server
    status, _h, body = raw_request(base + "/%2e%2e%2f%2e%2e%2fetc/passwd")
    assert status in (400, 404)
    assert b"root:" not in body


def test_legacy_static_dir_still_selectable(base_config, tmp_path):
    """打包成 wheel 沒有 frontend/dist 時，仍可指定內建 web_static 陽春介面。"""
    legacy = Path(web.__file__).parent / "web_static"
    if not (legacy / "index.html").is_file():
        pytest.skip("此安裝未內附 web_static")
    httpd, _queue_obj, _root = web.build_server(
        base_config, host="127.0.0.1", port=0, jobs_dir=tmp_path / "jobs",
        workers=1, max_upload_mb=64, static_dir=legacy,
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        html = get(base + "/")[1].decode("utf-8")
        assert "1. 上傳影音" in html
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_config_endpoint_redacts(web_server):
    base, _q, _jobs = web_server
    data = json.loads(get(base + "/api/config")[1])
    assert data["asr"]["key_configured"] is True
    assert data["chunking"]["target_seconds"] == 20.0
    assert "api_key" not in json.dumps(data)


def test_health_and_unknown_route(web_server):
    base, _q, _jobs = web_server
    assert json.loads(get(base + "/api/health")[1])["status"] == "ok"
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base + "/api/nope")
    assert exc.value.code == 404


# ------------------------------------------------------------------ 上傳與處理
def test_upload_process_and_download(web_server, media_factory):
    base, _queue, jobs_root = web_server
    media = media_factory(minutes=1.0, segments=8)
    blob = media.read_bytes()
    status, payload = post(
        f"{base}/api/jobs?style=concise&summary_language=zh-TW",
        blob,
        {"X-Filename": urllib.parse.quote("例會 記錄.mkv"), "Content-Type": "application/octet-stream"},
    )
    assert status in (200, 202), payload  # 202 Accepted；部分 client 歸一為 200
    job_id = payload["job_id"]
    assert payload["size"] == len(blob)

    detail = wait_for_done(base, job_id)
    assert detail["status"] == "done", detail
    assert detail["progress"] == 1.0
    names = [f["format"] for f in detail["files"]]
    assert {"summary.md", "summary.json", "md", "json", "txt", "srt", "vtt"} <= set(names)

    summary_md = next(f for f in detail["files"] if f["format"] == "summary.md")
    body = get(base + summary_md["url"])[1].decode("utf-8")
    assert "TL;DR" in body and "章節摘要" in body

    result = detail["result"]
    assert result["summary"]["meta"]["style"] == "concise"
    assert result["transcript"]["segments"] >= 6
    assert "[0:0" in result["transcript"]["text"]

    # 列表可看到此工作
    listing = json.loads(get(base + "/api/jobs")[1])["jobs"]
    assert any(job["job_id"] == job_id and job["status"] == "done" for job in listing)
    assert (jobs_root / job_id).is_dir()


def test_upload_rejects_unknown_extension(web_server):
    base, _q, _jobs = web_server
    status, payload = post(f"{base}/api/jobs", b"hello",
                           {"X-Filename": urllib.parse.quote("notes.pdf"), "Content-Type": "application/octet-stream"})
    assert status == 400
    assert "不支援" in payload["error"]


def test_upload_rejects_empty_body(web_server):
    base, _q, _jobs = web_server
    status, payload = post(f"{base}/api/jobs", b"", {"X-Filename": "a.mp3"})
    assert status == 400


def test_no_summary_option(web_server, media_factory):
    base, _q, _jobs = web_server
    media = media_factory(minutes=0.6, segments=4)
    _s, payload = post(f"{base}/api/jobs?no_summary=1&chunk_seconds=15", media.read_bytes(),
                       {"X-Filename": urllib.parse.quote("voice.mp3")})
    detail = wait_for_done(base, payload["job_id"])
    assert detail["status"] == "done"
    formats = {f["format"] for f in detail["files"]}
    assert "md" in formats and "summary.md" not in formats


def test_download_rejects_path_traversal(web_server, media_factory):
    base, _q, _jobs = web_server
    media = media_factory(minutes=0.6, segments=4)
    _s, payload = post(f"{base}/api/jobs?no_summary=1&chunk_seconds=15", media.read_bytes(),
                       {"X-Filename": urllib.parse.quote("clip.mp3")})
    job_id = wait_for_done(base, payload["job_id"])["job_id"]
    conn = http.client.HTTPConnection("127.0.0.1", urllib.parse.urlparse(base).port, timeout=20)
    conn.request("GET", f"/api/jobs/{job_id}/files/..%2F..%2F..%2Fetc%2Fpasswd")
    response = conn.getresponse()
    body = response.read()
    conn.close()
    assert response.status in (400, 404)
    assert b"root:" not in body


def test_cancel_unknown_job(web_server):
    base, _q, _jobs = web_server
    status, payload = post(f"{base}/api/jobs/does-not-exist/cancel", b"")
    assert status == 404
    assert "找不到" in payload["error"]


def test_detail_unknown_job(web_server):
    base, _q, _jobs = web_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base + "/api/jobs/nope")
    assert exc.value.code == 404


# ------------------------------------------------------------ 影音串流
def test_media_stream_and_range(web_server, media_factory):
    """上傳後的原始檔案可串流播放，並支援 Range（瀏覽器拖曳進度條需要）。"""
    base, _queue, _jobs = web_server
    media = media_factory(minutes=0.5, segments=4)
    blob = media.read_bytes()
    _status, payload = post(
        f"{base}/api/jobs",
        blob,
        {"X-Filename": urllib.parse.quote("採訪.mp3"), "Content-Type": "application/octet-stream"},
    )
    detail = wait_for_done(base, payload["job_id"])
    assert detail["media_url"] and detail["duration"]

    status, headers, body = raw_request(base + detail["media_url"])
    assert status == 200 and len(body) == len(blob)
    assert headers["accept-ranges"] == "bytes"

    status, headers, part = raw_request(base + detail["media_url"], headers={"Range": "bytes=0-1023"})
    assert status == 206, (status, headers)
    assert headers["content-range"] == f"bytes 0-1023/{len(blob)}"
    assert part == blob[:1024]

    tail_start = len(blob) - 100
    status, _headers, tail = raw_request(base + detail["media_url"], headers={"Range": f"bytes={tail_start}-"})
    assert status == 206 and tail == blob[tail_start:]

    status, headers, _body = raw_request(base + detail["media_url"], headers={"Range": f"bytes={len(blob) + 10}-"})
    assert status == 416 and headers["content-range"].startswith("bytes */")


def test_media_missing_returns_404(web_server):
    base, queue_obj, _jobs = web_server
    _status, payload = post(
        f"{base}/api/jobs",
        b"\x00" * 2048,
        {"X-Filename": urllib.parse.quote("x.mp3"), "Content-Type": "application/octet-stream"},
    )
    job_id = payload["job_id"]
    rec = queue_obj.get(job_id)
    assert rec is not None
    Path(rec.input_path).unlink(missing_ok=True)
    status, _headers, body = raw_request(base + f"/api/jobs/{job_id}/media")
    assert status == 404
    assert "無法播放" in body.decode("utf-8")
