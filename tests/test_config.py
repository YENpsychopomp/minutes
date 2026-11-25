"""設定載入與驗證測試。"""

from __future__ import annotations

import pytest

from summery.config import Config, ConfigError, apply_cli_overrides, load_config


def test_defaults_are_valid():
    cfg = load_config(env={})
    assert cfg.chunking.target_seconds > 0
    assert cfg.chunking.overlap_seconds < cfg.chunking.target_seconds
    assert cfg.audio.sample_rate == 16000
    assert "md" in cfg.output.formats


def test_toml_file_overrides(tmp_path):
    ini = tmp_path / "cfg.toml"
    ini.write_text(
        """
work_dir = "/tmp/custom-work"
[asr]
base_url = "https://asr.internal/v1"
model = "large-v3"
max_concurrency = 9
[chunking]
target_seconds = 240
overlap_seconds = 0
[summarize]
style = "concise"
max_input_tokens = 4000
""",
        encoding="utf-8",
    )
    cfg = load_config(ini, env={})
    assert cfg.asr.model == "large-v3"
    assert cfg.asr.max_concurrency == 9
    assert cfg.chunking.target_seconds == 240
    assert cfg.chunking.overlap_seconds == 0
    assert cfg.summarize.style == "concise"
    assert cfg.work_dir == "/tmp/custom-work"


def test_unknown_key_in_file_raises(tmp_path):
    ini = tmp_path / "bad.toml"
    ini.write_text('[asr]\nnope = 1\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="未知設定"):
        load_config(ini, env={})


@pytest.mark.parametrize("name", ["SUMMERY_ASR_MODEL", "SUMMERY__ASR__MODEL"])
def test_env_forms(name):
    cfg = load_config(env={name: "from-env", "SUMMERY__CHUNKING__TARGET_SECONDS": "180"})
    assert cfg.asr.model == "from-env"
    assert cfg.chunking.target_seconds == 180


def test_env_top_level_and_aliases(clean_env):
    cfg = load_config(
        env={"OPENAI_BASE_URL": "https://gateway.local/v1", "SUMMERY_WORK_DIR": "/tmp/w",
             "ASR_API_KEY": "k1", "LLM_API_KEY": "k2", "SUMMERY_LOG_LEVEL": "DEBUG"}
    )
    assert cfg.asr.base_url == "https://gateway.local/v1"
    assert cfg.llm.base_url == "https://gateway.local/v1"
    assert cfg.work_dir == "/tmp/w"
    assert cfg.log_level == "DEBUG"
    assert cfg.resolve_api_key("asr") == "k1"
    assert cfg.resolve_api_key("llm") == "k2"


def test_missing_key_error_mentions_env(clean_env):
    cfg = load_config(env={})
    with pytest.raises(ConfigError, match="ASR_API_KEY"):
        cfg.require_api_key("asr")


def test_stub_dir_counts_as_key(clean_env, tmp_path):
    stub = tmp_path / "stub"
    stub.mkdir()
    cfg = load_config(env={"SUMMERY__LLM__STUB_DIR": str(stub)})
    assert cfg.require_api_key("llm") is None  # 不拋錯


def test_cli_overrides_and_validation():
    cfg = Config()
    apply_cli_overrides(cfg, "chunking", ["target_seconds=300", "overlap_seconds=0"])
    assert cfg.chunking.target_seconds == 300
    cfg2 = Config()
    with pytest.raises(ConfigError):
        apply_cli_overrides(cfg2, "chunking", ["target_seconds=abc"])
    with pytest.raises(ConfigError):
        apply_cli_overrides(Config(), "chunking", ["typo=1"])
    with pytest.raises(ConfigError):
        apply_cli_overrides(Config(), "nonsense", ["a=1"])


def test_validate_rejects_bad_values():
    cfg = Config()
    cfg.audio.codec = "divx"
    with pytest.raises(ConfigError, match="codec"):
        load_config(overrides={"audio": {"codec": "divx"}}, env={})
    with pytest.raises(ConfigError, match="formats"):
        load_config(overrides={"output": {"formats": ["md", "doc"]}}, env={})
    bad = Config()
    bad.chunking.overlap_seconds = bad.chunking.target_seconds  # 重疊過大
    with pytest.raises(ConfigError, match="overlap"):
        from summery.config import _validate

        _validate(bad)
    bad2 = Config()
    bad2.asr.base_url = "ftp://x"
    with pytest.raises(ConfigError, match="http"):
        from summery.config import _validate

        _validate(bad2)


def test_redacted_hides_key():
    cfg = Config()
    cfg.asr.api_key = "super-secret"
    assert cfg.redacted()["asr"]["api_key"] == "***"
    assert "super-secret" not in str(cfg.redacted())


def test_shipped_example_configs_load():
    """repo 附的範例設定必須真的能用（避免文件與欄位名走鐘）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "config"
    files = sorted(root.glob("*.toml"))
    assert files, "缺少 config/*.toml"
    for path in files:
        cfg = load_config(path, env={"ASR_API_KEY": "x"})
        assert cfg.chunking.target_seconds <= cfg.chunking.max_seconds


def test_empty_string_means_unset():
    cfg = load_config(env={"SUMMERY__ASR__LANGUAGE": "", "SUMMERY__OUTPUT__STEM": "  "})
    assert cfg.asr.language is None and cfg.output.stem is None


def test_list_and_bool_coercion():
    cfg = load_config(
        env={"SUMMERY__OUTPUT__FORMATS": "md, srt", "SUMMERY__ASR__SEND_TEMPERATURE": "true",
             "SUMMERY__ASR__EXTRA_PARAMS": "task=transcribe,language_target=zh"}
    )
    assert cfg.output.formats == ["md", "srt"]
    assert cfg.asr.send_temperature is True
    assert cfg.asr.extra_params == {"task": "transcribe", "language_target": "zh"}
