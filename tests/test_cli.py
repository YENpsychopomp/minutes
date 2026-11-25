"""命令列介面測試。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from summery import cli


def point_env(monkeypatch, url: str, tmp_path: Path) -> None:
    monkeypatch.setenv("ASR_API_KEY", "cli-key")
    monkeypatch.setenv("LLM_API_KEY", "cli-key")
    monkeypatch.setenv("SUMMERY__ASR__BASE_URL", url)
    monkeypatch.setenv("SUMMERY__LLM__BASE_URL", url)
    monkeypatch.setenv("SUMMERY_WORK_DIR", str(tmp_path / ".work"))
    monkeypatch.setenv("SUMMERY__LLM__MAX_RETRIES", "1")


# ------------------------------------------------------------------ parser
@pytest.mark.parametrize("command", ["run", "transcribe", "summarize", "minutes", "plan", "probe", "serve",
                                     "mock-server", "make-media", "jobs"])
def test_subcommand_help_works(command):
    with pytest.raises(SystemExit) as exc:
        cli.main([command, "--help"])
    assert exc.value.code == 0


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "summery" in capsys.readouterr().out


def test_parser_accepts_documented_flags():
    parser = cli.build_parser()
    args = parser.parse_args(
        ["run", "a.mp4", "--chunk-seconds", "300", "--overlap", "3", "--concurrency", "8",
         "--formats", "md,srt", "--style", "concise", "--instruction", "聚焦風險", "--no-summary",
         "--dry-run", "--set", "llm.model=x", "--set", "chunking.min_seconds=30", "--json"]
    )
    assert args.chunk_seconds == 300 and args.concurrency == 8
    assert args.formats == "md,srt" and args.sets == ["llm.model=x", "chunking.min_seconds=30"]


def test_dotenv_is_not_loaded_when_disabled(monkeypatch, tmp_path, capsys):
    """SUMMERY_DOTENV=0 / --no-env-file 必須完全阻擋 .env 自動載入。"""
    (tmp_path / ".env").write_text("ASR_API_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUMMERY_DOTENV", "0")
    monkeypatch.delenv("ASR_API_KEY", raising=False)
    from summery import config as cfg_mod

    assert cfg_mod.load_config(env={}).asr.api_key is None
    monkeypatch.delenv("SUMMERY_DOTENV")
    assert cfg_mod.load_config(env=os.environ).asr.api_key is None  # 尚未載入
    assert cli.main(["--no-env-file", "jobs", "--work-dir", str(tmp_path / "none")]) == cli.EXIT_OK
    assert os.environ.get("ASR_API_KEY") is None


def test_dotenv_file_is_loaded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        'export SUMMERY__ASR__MODEL="from-dot-file"\nASR_API_KEY=sk-from-file\n', encoding="utf-8")
    monkeypatch.delenv("SUMMERY_DOTENV", raising=False)
    monkeypatch.delenv("ASR_API_KEY", raising=False)
    monkeypatch.delenv("SUMMERY__ASR__MODEL", raising=False)
    assert cli.main(["jobs", "--work-dir", str(tmp_path / "none")]) == cli.EXIT_OK
    assert os.environ["ASR_API_KEY"] == "sk-from-file"
    assert os.environ["SUMMERY__ASR__MODEL"] == "from-dot-file"


def test_existing_env_wins_over_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ASR_API_KEY=sk-from-file\n", encoding="utf-8")
    monkeypatch.setenv("ASR_API_KEY", "sk-from-shell")
    monkeypatch.delenv("SUMMERY_DOTENV", raising=False)
    cli.main(["jobs", "--work-dir", str(tmp_path / "none")])
    assert os.environ["ASR_API_KEY"] == "sk-from-shell"


def test_missing_env_file_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--env-file", str(tmp_path / "nope.env"), "jobs"]) == cli.EXIT_ERROR
    assert "找不到" in capsys.readouterr().err


# ------------------------------------------------------------------ 單一命令
def test_probe_json(media_factory, tmp_path, capsys, clean_env):
    media = media_factory(minutes=0.2, segments=2)
    assert cli.main(["probe", str(media), "--json", "--work-dir", str(tmp_path / "w")]) == cli.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["duration"] == pytest.approx(12.0, abs=1.0)
    assert data["has_video"] is False and len(data["audio_streams"]) == 1


def test_make_media_creates_file(tmp_path):
    out = tmp_path / "tiny.mkv"
    assert cli.main(["make-media", "--out", str(out), "--minutes", "0.3", "--segments", "3"]) == cli.EXIT_OK
    assert out.is_file() and out.stat().st_size > 5000


def test_jobs_lists_nothing(clean_env, tmp_path, capsys):
    assert cli.main(["jobs", "--work-dir", str(tmp_path / "nowhere")]) == cli.EXIT_OK
    assert "尚無工作目錄" in capsys.readouterr().out


# ------------------------------------------------------------------ 完整流程
def test_cli_run_end_to_end(media_factory, tmp_path, monkeypatch, mock_server_inject, capsys):
    requests: list = []
    url = mock_server_inject(requests_log=requests)
    point_env(monkeypatch, url, tmp_path)
    media = media_factory(minutes=1.0, segments=8)
    out_dir = tmp_path / "out"
    code = cli.main(["run", str(media), "--out-dir", str(out_dir), "--chunk-seconds", "20",
                     "--concurrency", "2", "--json", "--quiet"])
    assert code == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["plan"]["count"] >= 2
    assert payload["summary"]["title"]
    assert payload["stats"]["llm_usage"]["calls"] >= 1
    assert {Path(p).suffix for p in payload["outputs"].values()} >= {".md", ".json", ".srt", ".vtt", ".txt"}

    # jobs 應能看到剛完成的工作
    capsys.readouterr()
    assert cli.main(["jobs"]) == cli.EXIT_OK
    assert "done" in capsys.readouterr().out


def test_cli_dry_run_does_not_call_api(media_factory, tmp_path, monkeypatch, mock_server_inject, capsys):
    requests: list = []
    url = mock_server_inject(requests_log=requests)
    point_env(monkeypatch, url, tmp_path)
    media = media_factory(minutes=1.0, segments=8)
    code = cli.main(["run", str(media), "--dry-run", "--work-dir", str(tmp_path / "w"), "--json", "--quiet"])
    assert code == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["stats"]["dry_run"] is True
    assert requests == []


def test_cli_transcribe_skips_summary(media_factory, tmp_path, monkeypatch, mock_server, capsys):
    point_env(monkeypatch, mock_server, tmp_path)
    media = media_factory(minutes=0.6, segments=4)
    code = cli.main(["transcribe", str(media), "--out-dir", str(tmp_path / "o"), "--formats", "txt,srt",
                     "--chunk-seconds", "15", "--quiet"])
    assert code == cli.EXIT_OK
    files = [p.name for p in (tmp_path / "o").iterdir()]
    assert any(name.endswith(".txt") for name in files) and any(name.endswith(".srt") for name in files)
    assert not any("summary" in name for name in files)


def test_cli_missing_key_returns_error(media_factory, tmp_path, clean_env, capsys):
    media = media_factory(minutes=0.2, segments=2)
    code = cli.main(["run", str(media), "--work-dir", str(tmp_path / "w"), "--formats", "md"])
    assert code == cli.EXIT_ERROR
    assert "API key" in capsys.readouterr().err


def test_cli_bad_config_value_errors(media_factory, tmp_path, clean_env, capsys):
    media = media_factory(minutes=0.2, segments=2)
    code = cli.main(["run", str(media), "--set", "chunking.target_seconds=oops", "--work-dir", str(tmp_path)])
    assert code == cli.EXIT_ERROR
    assert "設定" in capsys.readouterr().err


def test_cli_summarize_existing_transcript(media_factory, tmp_path, monkeypatch, mock_server, capsys):
    point_env(monkeypatch, mock_server, tmp_path)
    transcript = tmp_path / "talk.transcript.json"
    payload = {
        "schema": "summery.transcript/1", "duration": 600, "language": "zh", "meta": {},
        "segments": [{"start": i * 20, "end": i * 20 + 19, "text": f"第 {i} 段發言內容，包含決定與待辦。"} for i in range(30)],
    }
    transcript.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    code = cli.main(["summarize", str(transcript), "--out-dir", str(tmp_path / "out"), "--style", "concise", "--quiet"])
    assert code == cli.EXIT_OK
    assert (tmp_path / "out" / "talk.summary.md").is_file()
    out = capsys.readouterr().out
    assert "map-reduce" in out or "direct" in out


def test_cli_summarize_from_json_does_not_duplicate_transcript(media_factory, tmp_path, monkeypatch, mock_server, capsys):
    """輸入已經是 transcript.json 時，不應再寫出一份重複的逐字稿。"""
    point_env(monkeypatch, mock_server, tmp_path)
    transcript = tmp_path / "talk.transcript.json"
    transcript.write_text(json.dumps({
        "schema": "summery.transcript/1", "duration": 300, "language": "zh", "meta": {},
        "segments": [{"start": i * 10, "end": i * 10 + 9, "text": f"第 {i} 段會議內容。"} for i in range(12)],
    }, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    assert cli.main(["summarize", str(transcript), "--out-dir", str(out), "--quiet"]) == cli.EXIT_OK
    names = sorted(p.name for p in out.iterdir())
    assert names == ["talk.summary.json", "talk.summary.md"], names


def test_cli_summarize_from_txt_also_writes_normalized_transcript(tmp_path, monkeypatch, mock_server, capsys):
    point_env(monkeypatch, mock_server, tmp_path)
    src = tmp_path / "手寫逐字稿.txt"
    src.write_text(
        "開場說明本季目標。\n"
        "各組回報進度與風險。\n"
        "結論是調整時程並指派負責人。\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    assert cli.main(["summarize", str(src), "--out-dir", str(out), "--duration", "120", "--quiet"]) == cli.EXIT_OK
    names = sorted(p.name for p in out.iterdir())
    assert "手寫逐字稿.summary.md" in names
    assert "手寫逐字稿.transcript.md" in names and "手寫逐字稿.transcript.json" in names
    assert not any("summary-less" in n for n in names)


def test_cli_plan_shows_chunk_budget(media_factory, tmp_path, clean_env, capsys):
    media = media_factory(minutes=1.0, segments=8)
    code = cli.main(["plan", str(media), "--chunk-seconds", "20", "--work-dir", str(tmp_path / "plan")])
    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "切塊" in out and "chunk 檔" in out and "預估 ASR 請求" in out


# ------------------------------------------------------------------ 文件與範本
def test_cli_minutes_from_documents_with_template(tmp_path, monkeypatch, mock_server, make_docx, capsys):
    point_env(monkeypatch, mock_server, tmp_path)
    notes = tmp_path / "例會筆記.md"
    notes.write_text("# 預算\n\n同意送簽採購案。\n", encoding="utf-8")
    extra = tmp_path / "補充.txt"
    extra.write_text("陳志強負責 11 月底前完成。\n", encoding="utf-8")
    out = tmp_path / "out"
    code = cli.main(["minutes", str(notes), str(extra), "--template", str(make_docx()),
                     "--out-dir", str(out), "--quiet"])
    assert code == cli.EXIT_OK, capsys.readouterr().err
    names = sorted(p.name for p in out.iterdir())
    assert "例會筆記.minutes.md" in names and "例會筆記.summary.md" in names
    assert "例會筆記.transcript.md" in names and not any(n.endswith((".srt", ".vtt")) for n in names)
    printed = capsys.readouterr().out
    assert "輸入文件  : 2 份" in printed and "範本      : template.docx" in printed and "依範本格式" in printed


def test_cli_minutes_rejects_media_and_missing_files(tmp_path, clean_env, capsys):
    media = tmp_path / "a.mp4"
    media.write_bytes(b"x")
    assert cli.main(["minutes", str(media)]) == cli.EXIT_ERROR
    assert "summery run" in capsys.readouterr().err
    assert cli.main(["minutes", str(tmp_path / "nope.docx")]) == cli.EXIT_ERROR


def test_cli_summarize_with_template_writes_minutes(tmp_path, monkeypatch, mock_server, make_docx, capsys):
    point_env(monkeypatch, mock_server, tmp_path)
    transcript = tmp_path / "talk.transcript.json"
    transcript.write_text(json.dumps({
        "schema": "summery.transcript/1", "duration": 120, "language": "zh", "meta": {},
        "segments": [{"start": i * 10, "end": i * 10 + 9, "text": f"第 {i} 段會議內容。"} for i in range(8)],
    }, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    code = cli.main(["summarize", str(transcript), "--template", str(make_docx()), "--out-dir", str(out), "--quiet"])
    assert code == cli.EXIT_OK
    assert sorted(p.name for p in out.iterdir()) == ["talk.minutes.md", "talk.summary.json", "talk.summary.md"]
    summary = json.loads((out / "talk.summary.json").read_text(encoding="utf-8"))
    assert summary["meta"]["minutes"]["format"] == "template" and summary["meta"]["input_kind"] == "media"


def test_cli_summarize_reads_docx(tmp_path, monkeypatch, mock_server, make_docx, capsys):
    point_env(monkeypatch, mock_server, tmp_path)
    out = tmp_path / "out"
    assert cli.main(["summarize", str(make_docx("old.docx")), "--out-dir", str(out), "--quiet"]) == cli.EXIT_OK
    summary = json.loads((out / "old.summary.json").read_text(encoding="utf-8"))
    assert summary["meta"]["input_kind"] == "document"
    assert (out / "old.minutes.md").is_file()  # 輸入是文件：跟 run／minutes 一樣自動產出會議紀錄
    other = tmp_path / "out2"
    assert cli.main(["summarize", str(make_docx("old.docx")), "--no-minutes", "--out-dir", str(other), "--quiet"]) == cli.EXIT_OK
    assert not (other / "old.minutes.md").exists()


def test_cli_run_accepts_a_document(tmp_path, monkeypatch, mock_server, capsys):
    point_env(monkeypatch, mock_server, tmp_path)
    notes = tmp_path / "notes.md"
    notes.write_text("# 決議\n\n通過。\n", encoding="utf-8")
    assert cli.main(["run", str(notes), "--out-dir", str(tmp_path / "o"), "--quiet"]) == cli.EXIT_OK
    assert (tmp_path / "o" / "notes.minutes.md").is_file()
