"""Validated settings for Firebreak.

Every process reads its configuration from the environment through this
module. Required settings have no default, so a misconfigured service fails
at startup instead of halfway through an investigation.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class DataMode(StrEnum):
    """Where evidence comes from."""

    BUNDLE = "bundle"
    LIVE = "live"


class LlmMode(StrEnum):
    """How model calls are served."""

    STUB = "stub"
    REPLAY = "replay"
    LOCAL = "local"
    API = "api"


class Settings(BaseSettings):
    """Process configuration read from the environment and .env."""

    model_config = SettingsConfigDict(
        env_prefix="FIREBREAK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    data_mode: DataMode = DataMode.BUNDLE
    llm_mode: LlmMode = LlmMode.STUB

    bundles_dir: Path = Field(default=REPO_ROOT / "bundles")
    reports_dir: Path = Field(default=REPO_ROOT / "reports")
    recordings_dir: Path = Field(default=REPO_ROOT / "recordings")
    config_dir: Path = Field(default=REPO_ROOT / "config")

    llm_base_url: str | None = None
    llm_api_key: str | None = None

    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return upper

    @field_validator("llm_base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("llm_base_url must start with http:// or https://")
        return value.rstrip("/")

    def require_api_credentials(self) -> None:
        """Fail fast when API mode is selected without credentials."""
        if self.llm_mode is not LlmMode.API:
            return
        missing = [
            name
            for name, value in (
                ("llm_base_url", self.llm_base_url),
                ("llm_api_key", self.llm_api_key),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"llm_mode=api needs {', '.join(missing)}")


def load_settings() -> Settings:
    """Build settings from the environment, raising on invalid values."""
    return Settings()
