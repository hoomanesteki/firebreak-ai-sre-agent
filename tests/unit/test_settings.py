"""Tests for validated settings (SPEC.md Section 16)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from firebreak.settings import REPO_ROOT, DataMode, LlmMode, Settings, load_settings


def build(**overrides: object) -> Settings:
    defaults: dict[str, object] = {"_env_file": None}
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def test_settings_default_to_offline_modes():
    settings = build()
    assert settings.data_mode is DataMode.BUNDLE
    assert settings.llm_mode is LlmMode.STUB


def test_settings_are_frozen():
    settings = build()
    with pytest.raises(ValidationError):
        settings.log_level = "DEBUG"  # type: ignore[misc]


def test_settings_read_modes_from_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FIREBREAK_DATA_MODE", "live")
    monkeypatch.setenv("FIREBREAK_LLM_MODE", "local")
    settings = load_settings()
    assert settings.data_mode is DataMode.LIVE
    assert settings.llm_mode is LlmMode.LOCAL


def test_settings_reject_unknown_data_mode():
    with pytest.raises(ValidationError):
        build(data_mode="guess")


def test_settings_reject_unknown_llm_mode():
    with pytest.raises(ValidationError):
        build(llm_mode="magic")


def test_log_level_is_upper_cased():
    assert build(log_level="debug").log_level == "DEBUG"


def test_log_level_rejects_unknown_value():
    with pytest.raises(ValidationError, match="log_level must be one of"):
        build(log_level="chatty")


def test_base_url_requires_a_scheme():
    with pytest.raises(ValidationError, match="must start with"):
        build(llm_base_url="localhost:11434/v1")


def test_base_url_drops_a_trailing_slash():
    assert build(llm_base_url="http://localhost:11434/v1/").llm_base_url == (
        "http://localhost:11434/v1"
    )


def test_base_url_may_be_unset():
    assert build().llm_base_url is None


def test_require_api_credentials_passes_in_stub_mode():
    build().require_api_credentials()


def test_require_api_credentials_reports_every_missing_field():
    settings = build(llm_mode="api")
    with pytest.raises(ValueError) as error:
        settings.require_api_credentials()
    message = str(error.value)
    assert "llm_base_url" in message
    assert "llm_api_key" in message


def test_require_api_credentials_reports_only_the_missing_field():
    settings = build(llm_mode="api", llm_base_url="https://gateway.example/v1")
    with pytest.raises(ValueError) as error:
        settings.require_api_credentials()
    assert "llm_api_key" in str(error.value)
    assert "llm_base_url" not in str(error.value)


def test_require_api_credentials_passes_when_configured():
    build(
        llm_mode="api",
        llm_base_url="https://gateway.example/v1",
        llm_api_key="secret",
    ).require_api_credentials()


def test_paths_point_inside_the_repository():
    settings = build()
    assert settings.bundles_dir.name == "bundles"
    assert settings.reports_dir.name == "reports"
    assert settings.config_dir.name == "config"


def test_env_file_is_anchored_to_the_repository():
    """Running the CLI from another directory must not change configuration.

    A relative env_file resolves against the working directory, so a command
    run from anywhere but the repository root silently fell back to defaults
    and said nothing about it.
    """
    configured = Settings.model_config["env_file"]

    assert isinstance(configured, Path)
    assert configured.is_absolute()
    assert configured == REPO_ROOT / ".env"


def test_settings_read_the_repository_env_file_from_another_directory(tmp_path, monkeypatch):
    env_file = REPO_ROOT / ".env"
    original = env_file.read_text(encoding="utf-8") if env_file.exists() else None
    env_file.write_text("FIREBREAK_LOG_LEVEL=WARNING\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FIREBREAK_LOG_LEVEL", raising=False)
    try:
        assert load_settings().log_level == "WARNING"
    finally:
        if original is None:
            env_file.unlink()
        else:
            env_file.write_text(original, encoding="utf-8")
