"""Validated settings for Firebreak.

Every process reads its configuration from the environment through this
module. Required settings have no default, so a misconfigured service fails
at startup instead of halfway through an investigation.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
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
        # Anchored to the repository rather than the working directory. A
        # relative path means running the CLI from anywhere else silently
        # falls back to defaults, which is the worst kind of configuration
        # bug: it changes behaviour and says nothing.
        env_file=REPO_ROOT / ".env",
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

    # Both the documented name and the prefixed one are accepted, documented
    # name first.
    #
    # SPEC.md Section 6.7 writes `LLM_BASE_URL` and `LLM_API_KEY`, unprefixed,
    # because that is what an OpenAI-compatible gateway expects and what the
    # owner's Tollgate uses. This class prefixes everything with FIREBREAK_, so
    # setting the documented variable did nothing and the process silently stayed
    # in stub mode. Nothing would have reported it: stub mode is a valid mode.
    #
    # This is the seventh time in this project that two places agreed on a concept
    # and disagreed on its name. The remedy is the same every time: accept the
    # documented spelling, and assert in a test that the documented spelling works.
    llm_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_BASE_URL", "FIREBREAK_LLM_BASE_URL"),
    )
    llm_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_API_KEY", "FIREBREAK_LLM_API_KEY"),
    )

    # Neo4j. The password is a local development credential and not a
    # secret: `ops/compose.core.yml` sets the same value, and the graph
    # holds no ground truth and no customer data. It is listed here rather
    # than hard coded so a deployment can point at its own database without
    # editing code.
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "firebreak-local"
    neo4j_database: str = "neo4j"

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
