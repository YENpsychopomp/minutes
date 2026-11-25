"""部署腳本的承諾：一鍵啟動、前台執行、關掉終端機就整套停止。"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy.sh"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def health(port: int, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def listener_pids(port: int) -> list[int]:
    """用 lsof 找出在該埠監聽的行程（沒有 lsof 時回空列表）。"""
    if not shutil.which("lsof"):
        return []
    out = subprocess.run(
        ["lsof", "-nP", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
        capture_output=True, text=True, check=False,
    ).stdout.split()
    return [int(pid) for pid in out if pid.isdigit()]


@pytest.mark.skipif(not SCRIPT.is_file(), reason="deploy.sh 不存在")
def test_script_runs_in_foreground_and_traps_exit_signals():
    raw = SCRIPT.read_text(encoding="utf-8")
    # 註解裡出現「沒有 nohup」這類說明是正常的，只檢查實際指令
    source = "\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("#"))
    assert os.access(SCRIPT, os.X_OK), "deploy.sh 需要可執行位元"
    for signal_name in ("INT", "TERM", "HUP", "EXIT"):
        assert signal_name in source, f"未捕獲 {signal_name}，關閉終端機可能留下孤兒行程"
    # 明確排除會把服務丟到背景、關掉終端也不會死的做法
    for forbidden in ("nohup", "disown", "systemctl", "launchctl", "setsid"):
        assert forbidden not in source, f"部署腳本不應使用 {forbidden}（會讓服務脫離終端機）"
    if shutil.which("bash"):
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


@pytest.mark.skipif(not SCRIPT.is_file(), reason="deploy.sh 不存在")
def test_hup_stops_server_and_releases_port(tmp_path):
    """模擬關閉終端機視窗（SIGHUP）：服務要跟著停止並釋放埠號。"""
    port = free_port()
    log = tmp_path / "deploy.log"
    with log.open("wb") as handle:
        proc = subprocess.Popen(
            [str(SCRIPT), "--port", str(port), "--no-build", "--jobs-dir", str(tmp_path / "jobs")],
            cwd=str(ROOT),
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # 自成一組，避免訊號誤傷測試行程
        )

    deadline = time.time() + 60
    while time.time() < deadline and not health(port):
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    assert health(port), f"服務未在期限內啟動：\n{log.read_text(encoding='utf-8')[-1500:]}"

    watchers = listener_pids(port)
    proc.send_signal(signal.SIGHUP)

    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:  # noqa: PERF203
        proc.kill()
        pytest.fail("送出 SIGHUP 後部署腳本仍未結束")

    time.sleep(0.5)
    assert not health(port), "關閉終端機後服務仍然可用"
    for pid in watchers:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        pytest.fail(f"殘留仍在監聽的行程 {pid}（port {port}）")
    if watchers:
        assert not listener_pids(port), "埠號未釋放"


PS_SCRIPT = ROOT / "deploy.ps1"


@pytest.mark.skipif(not PS_SCRIPT.is_file(), reason="deploy.ps1 不存在")
def test_powershell_script_runs_in_foreground_and_cleans_up():
    raw_bytes = PS_SCRIPT.read_bytes()
    # Windows PowerShell 5.1 沒有 BOM 時會用系統代碼頁讀檔，中文訊息全變亂碼
    assert raw_bytes.startswith(b"\xef\xbb\xbf"), "deploy.ps1 需以 UTF-8（含 BOM）儲存"
    raw = raw_bytes.decode("utf-8-sig")
    source = "\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("#"))
    # 子行程要共用主控台（關視窗即停），收尾時連同子孫行程一起結束
    assert "-NoNewWindow" in source
    assert "finally" in source and "taskkill" in source and "/T" in source
    for forbidden in ("Start-Job", "Register-ScheduledTask", "New-Service", "-WindowStyle Hidden", "nohup"):
        assert forbidden not in source, f"部署腳本不應使用 {forbidden}（會讓服務脫離視窗）"
    # 參數要與 deploy.sh 對齊
    for flag in ("dev", "build", "no-build", "port", "host", "jobs-dir", "python", "open", "help"):
        assert f"'{flag}'" in source, f"deploy.ps1 缺少 --{flag}"


@pytest.mark.skipif(not PS_SCRIPT.is_file() or not shutil.which("pwsh"), reason="需要 pwsh 才能檢查語法")
def test_powershell_script_parses():
    command = (
        "$e = $null; [System.Management.Automation.Language.Parser]::ParseFile("
        f"'{PS_SCRIPT}', [ref]$null, [ref]$e) | Out-Null; "
        "if ($e) { $e | ForEach-Object { Write-Output $_.ToString() }; exit 1 }"
    )
    result = subprocess.run(["pwsh", "-NoProfile", "-Command", command], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
