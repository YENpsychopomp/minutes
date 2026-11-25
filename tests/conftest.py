"""pytest 共用 fixtures：把 src 加入 path、啟動 mock 伺服器、產生小型測試媒體。"""

from __future__ import annotations

import os

# 測試期間絕不自動載入開發者本機的 .env（避免真实金鑰/網路進入測試）
os.environ["SUMMERY_DOTENV"] = "0"
import os

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from summery.config import Config  # noqa: E402
from summery.mock_provider import start_mock_server  # noqa: E402


@pytest.fixture(scope="session")
def ffmpeg_bin() -> str:
    from shutil import which

    path = which("ffmpeg")
    if not path:
        pytest.skip("此環境沒有 ffmpeg")
    return path


@pytest.fixture(scope="session")
def mock_server():
    server, _thread, url = start_mock_server(port=0)
    yield url
    server.shutdown()


@pytest.fixture(scope="session")
def media_factory(tmp_path_factory, ffmpeg_bin):
    """產生「語音段 + 靜音」測試媒體；相同參數會快取重覆使用。"""
    cache = tmp_path_factory.mktemp("media")
    made: dict[tuple, Path] = {}

    def make(minutes: float = 1.0, segments: int = 8, video: bool = False) -> Path:
        key = (minutes, segments, video)
        if key in made:
            return made[key]
        out = cache / f"clip_{minutes:g}m_{segments}s_{'v' if video else 'a'}.mkv"
        if not out.is_file():
            seconds = minutes * 60.0
            cycle = seconds / segments
            speech = cycle * 0.85
            expr = f"0.3*sin(2*PI*(210+130*sin(2*PI*t/23))*t)*lt(mod(t\\,{cycle:.4f})\\,{speech:.4f})"
            cmd = [ffmpeg_bin, "-hide_banner", "-v", "error", "-y",
                   "-f", "lavfi", "-i", f"aevalsrc='{expr}':d={seconds:.3f}:s=16000:c=1"]
            if video:
                cmd += ["-f", "lavfi", "-i", f"testsrc=size=160x90:rate=2:duration={seconds:.3f}",
                        "-map", "0:a", "-map", "1:v", "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", "-g", "30"]
            else:
                cmd += ["-map", "0:a"]
            cmd += ["-c:a", "pcm_s16le", str(out)]
            res = subprocess.run(cmd, capture_output=True, text=True)
            assert res.returncode == 0, f"無法產生測試媒體：{res.stderr[-500:]}"
        made[key] = out
        return out

    return make


@pytest.fixture
def base_config(tmp_path, mock_server) -> Config:
    """指向 mock 伺服器的設定；工作目錄落在 tmp_path。"""
    cfg = Config()
    cfg.work_dir = str(tmp_path / ".summery_work")
    for section in (cfg.asr, cfg.llm):
        section.base_url = mock_server
        section.api_key = "test-key"
        section.timeout = 30.0
        section.max_retries = 2
        section.retry_backoff = 1.0
        section.max_concurrency = 3
    cfg.chunking.target_seconds = 20.0
    cfg.chunking.min_seconds = 5.0
    cfg.chunking.silence_snap_window = 12.0
    cfg.audio.codec = "wav"
    cfg.llm.max_concurrency = 3
    return cfg


@pytest.fixture
def mock_requests() -> list:
    """給需要檢視請求序的測試使用。"""
    return []


@pytest.fixture
def mock_server_inject():
    """需要自訂 mock 行為（延遲、失敗率）的測試自己啟動。"""
    servers = []

    def start(**kwargs):
        server, _thread, url = start_mock_server(port=0, **kwargs)
        servers.append(server)
        return url

    yield start
    for server in servers:
        server.shutdown()


def env_without_keys(monkeypatch) -> None:
    for name in ("ASR_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                 "SUMMERY_CONFIG", "SUMMERY_ASR_BASE_URL", "SUMMERY_LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def clean_env(monkeypatch):
    env_without_keys(monkeypatch)
    for name in list(os.environ):
        if name.startswith("SUMMERY_"):
            monkeypatch.delenv(name, raising=False)
    # 清完一定要重新關掉 .env 自動載入，否則 repo 根目錄的真實 .env 會混進測試
    monkeypatch.setenv("SUMMERY_DOTENV", "0")


# ------------------------------------------------------------------ 文件
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{_esc(text)}</w:t></w:r>'


def build_docx(path: Path, blocks: list[tuple]) -> Path:
    """以標準函式庫寫出最小可用的 .docx。

    blocks：("h1"|"h2", 文字)、("p", 文字)、("num", 文字, 層級)（Word 自動編號「一、」「(1)」）、
    ("bullet", 文字)、("table", [[儲存格, ...], ...])。
    """
    import zipfile

    body: list[str] = []
    for block in blocks:
        kind = block[0]
        if kind in ("h1", "h2"):
            style = "1" if kind == "h1" else "2"  # 中文版 Word 的標題樣式 ID 就是 1、2
            body.append(f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>{_run(block[1])}</w:p>')
        elif kind == "p":
            body.append(f"<w:p>{_run(block[1])}</w:p>")
        elif kind == "num":
            level = block[2] if len(block) > 2 else 0
            body.append(
                f'<w:p><w:pPr><w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="1"/></w:numPr></w:pPr>{_run(block[1])}</w:p>'
            )
        elif kind == "bullet":
            body.append(
                f'<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr></w:pPr>{_run(block[1])}</w:p>'
            )
        elif kind == "table":
            rows = "".join(
                "<w:tr>" + "".join(f"<w:tc><w:p>{_run(cell)}</w:p></w:tc>" for cell in row) + "</w:tr>"
                for row in block[1]
            )
            body.append(f"<w:tbl>{rows}</w:tbl>")
    document = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{_W_NS}"><w:body>{"".join(body)}</w:body></w:document>'
    styles = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:styles xmlns:w="{_W_NS}">'
        '<w:style w:type="paragraph" w:styleId="1"><w:name w:val="heading 1"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="2"><w:name w:val="heading 2"/></w:style>'
        "</w:styles>"
    )
    numbering = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:numbering xmlns:w="{_W_NS}">'
        '<w:abstractNum w:abstractNumId="0">'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="taiwaneseCountingThousand"/><w:lvlText w:val="%1、"/></w:lvl>'
        '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="(%2)"/></w:lvl>'
        "</w:abstractNum>"
        '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/><w:lvlText w:val="●"/></w:lvl></w:abstractNum>'
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
        '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>'
        "</w:numbering>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", styles)
        zf.writestr("word/numbering.xml", numbering)
    return path


#: 一份典型的「過往會議紀錄」範本
TEMPLATE_BLOCKS = [
    ("h1", "資訊工程系第 12 次系務會議紀錄"),
    ("p", "時間：2026 年 10 月 7 日（三）中午 12 時"),
    ("p", "地點：工程館 B201"),
    ("p", "主席：王大明主任"),
    ("p", "出席人員：王大明、李小華、陳志強"),
    ("p", "記錄：林雅婷"),
    ("num", "報告事項"),
    ("num", "AI 中心機房擴充進度", 1),
    ("num", "討論事項"),
    ("num", "決議事項"),
    ("table", [["事項", "負責人", "期限"], ["採購 GPU 伺服器", "陳志強", "11 月底"]]),
    ("num", "臨時動議"),
]


@pytest.fixture
def make_docx(tmp_path):
    def make(name: str = "template.docx", blocks: list[tuple] | None = None) -> Path:
        return build_docx(tmp_path / name, blocks or TEMPLATE_BLOCKS)

    return make
