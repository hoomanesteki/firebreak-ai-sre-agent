"""Model tiers, cascade rules, and fallback policy, read from configuration.

SPEC.md Section 6.7. Three tiers, each an ordered list of models, plus the rules
for moving between them and for retrying inside one.

**A price without a source is rejected.** SPEC.md Section 17 Phase 8's reviewer
focus is honest cost numbers with sourced prices, and the only way to make that
hold is to refuse the alternative. A price is a number that multiplies every cost
figure in every report, so an unsourced one is not a small inaccuracy: it makes
the whole cost column unverifiable while looking precise.

**An empty tier is valid, and cannot serve a real call.** The repository ships
with no models listed, because inventing an id would fail at the first call with
a confusing error. `stub` and `replay` need no models at all, so the harness, the
tests and the offline demo all run against an empty file. Asking an empty tier for
a model raises, naming what to put in the file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from firebreak.agent.llm import Tier

REPO_ROOT = Path(__file__).resolve().parents[3]
MODELS_PATH = REPO_ROOT / "config" / "models.yaml"

# Prices are quoted per million tokens because that is how every provider quotes
# them. Converting at the point of use rather than storing per-token avoids a file
# full of numbers like 0.000003, which nobody can check against a price page.
TOKENS_PER_PRICE_UNIT = 1_000_000


class ModelConfigError(Exception):
    """The model configuration is missing, malformed, or unusable."""


class Price(BaseModel):
    """What one model costs, and where the figure came from.

    `source` is required. A price with no source cannot be checked, and a cost
    table built from unverifiable prices is worse than one with no prices in it,
    because it looks like a measurement.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_per_million_usd: float = Field(ge=0.0)
    output_per_million_usd: float = Field(ge=0.0)
    source: str = Field(min_length=8)
    # When the price was read. Prices move, and a cost figure quoted from a
    # year-old price page is a guess presented as arithmetic.
    checked_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    def cost_usd(self, tokens_in: int, tokens_out: int) -> float:
        """What a call of this size costs, in dollars."""
        if tokens_in < 0 or tokens_out < 0:
            raise ValueError("token counts cannot be negative")
        return (
            tokens_in * self.input_per_million_usd + tokens_out * self.output_per_million_usd
        ) / TOKENS_PER_PRICE_UNIT


class ModelSpec(BaseModel):
    """One model in one tier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    # Absent is allowed and means the cost of this model is unknown, which an
    # eval report says rather than guessing. A local model through Ollama has no
    # per-token price, and pretending it costs zero would hide the hardware it
    # needs, so `price: null` reads as "not priced here".
    price: Price | None = None
    # Which family this model belongs to, so the critic can be given a different
    # one. SPEC.md Section 6.6 and [R8]: a critic sharing a family with the
    # writer agrees with it.
    family: str = ""

    @property
    def priced(self) -> bool:
        return self.price is not None


class TierConfig(BaseModel):
    """One tier: an ordered list, first tried and the rest as fallbacks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    models: tuple[ModelSpec, ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.models)


class CascadeRules(BaseModel):
    """When a specialist call moves from small to strong."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    escalate_after_schema_failures: int = Field(ge=1)
    escalate_below_confidence: float = Field(ge=0.0, le=1.0)
    escalate_on_contradiction: bool


class FallbackRules(BaseModel):
    """How a failing call is retried, and when the next model is tried."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts_per_model: int = Field(ge=1)
    base_delay_seconds: float = Field(gt=0.0)
    max_delay_seconds: float = Field(gt=0.0)
    retry_on_status: tuple[int, ...] = ()
    retry_on_timeout: bool = True
    deterministic_floor: bool = True

    @model_validator(mode="after")
    def _delays_are_ordered(self) -> FallbackRules:
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError(
                f"max_delay_seconds {self.max_delay_seconds} is below base_delay_seconds "
                f"{self.base_delay_seconds}, so backoff would shrink"
            )
        return self

    @model_validator(mode="after")
    def _a_schema_error_is_never_retried(self) -> FallbackRules:
        """SPEC.md Section 6.7 is explicit, and this is the rule most easily lost.

        A 422 or a 400 in the retry list would mean a malformed request was sent
        again unchanged, which fails the same way and spends the budget learning
        nothing. Schema failures escalate a tier instead.
        """
        forbidden = sorted(
            status for status in self.retry_on_status if 400 <= status < 500 and status != 429
        )
        if forbidden:
            raise ValueError(
                f"retry_on_status includes {forbidden}, which a retry cannot fix; a "
                "schema or request error escalates a tier instead of being resent"
            )
        return self


class ModelConfig(BaseModel):
    """Everything `config/models.yaml` says."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = 1
    tiers: dict[str, TierConfig]
    cascade: CascadeRules
    fallback: FallbackRules

    @model_validator(mode="after")
    def _every_tier_is_declared(self) -> ModelConfig:
        """The file must name exactly the tiers the code knows about.

        An extra tier would be configuration nothing reads, and a missing one
        would be a tier whose calls have nowhere to go. Both have happened in this
        project as a vocabulary mismatch between two files, six times now, so the
        set is asserted rather than assumed.
        """
        expected = {tier.value for tier in Tier}
        actual = set(self.tiers)
        if actual != expected:
            missing = sorted(expected - actual)
            unknown = sorted(actual - expected)
            raise ValueError(
                f"tiers must be exactly {sorted(expected)}; missing {missing}, unknown {unknown}"
            )
        return self

    def tier(self, tier: Tier) -> TierConfig:
        return self.tiers[tier.value]

    def models_for(self, tier: Tier) -> tuple[ModelSpec, ...]:
        """The ordered candidates for one tier, refusing an empty one.

        Raises rather than returning nothing, because a caller that received an
        empty list would have to invent its own error, and the useful error names
        the file to edit.
        """
        candidates = self.tier(tier).models
        if not candidates:
            raise ModelConfigError(
                f"tier {tier.value!r} has no models configured; add one to "
                f"{MODELS_PATH.name} with its id, provider and a sourced price, or run "
                "in stub or replay mode"
            )
        return candidates

    def any_configured(self) -> bool:
        """Whether a real model call is possible at all."""
        return any(tier.configured for tier in self.tiers.values())

    def unpriced(self) -> tuple[str, ...]:
        """Models whose cost is unknown, so a report can say so.

        A cost figure that silently omits a model is the failure SPEC.md Section
        17 Phase 8 is guarding against, so the names come back rather than the
        omission being hidden in an average.
        """
        return tuple(
            sorted(
                model.id
                for tier in self.tiers.values()
                for model in tier.models
                if not model.priced
            )
        )


@lru_cache(maxsize=1)
def load_model_config(path: Path | None = None) -> ModelConfig:
    """Read and validate the model configuration.

    Cached, because it is read on every model call and does not change during a
    run. A test needing a different file passes an explicit path, which gets its
    own cache entry.
    """
    resolved = path or MODELS_PATH
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except OSError as error:
        raise ModelConfigError(f"cannot read {resolved}: {error}") from error
    if not isinstance(raw, dict):
        raise ModelConfigError(f"{resolved} does not contain a mapping")
    try:
        return ModelConfig.model_validate(raw)
    except ValueError as error:
        raise ModelConfigError(f"{resolved} is not a valid model configuration: {error}") from error
