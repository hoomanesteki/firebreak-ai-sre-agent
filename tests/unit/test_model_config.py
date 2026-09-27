"""Tests for the model tier configuration.

The rule worth testing hardest is the one SPEC.md Section 17 Phase 8 makes its
reviewer focus: prices with sources. A price is a number that multiplies every
cost figure in every report, so an unsourced one is not a small inaccuracy. It
makes the whole cost column unverifiable while looking precise.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from firebreak.agent.llm import Tier
from firebreak.agent.models import (
    MODELS_PATH,
    ModelConfig,
    ModelConfigError,
    ModelSpec,
    Price,
    load_model_config,
)

PRICE = {
    "input_per_million_usd": 3.0,
    "output_per_million_usd": 15.0,
    "source": "https://example.invalid/pricing",
    "checked_on": "2026-09-26",
}


def config_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "version": 1,
        "tiers": {"small": {"models": []}, "strong": {"models": []}, "judge": {"models": []}},
        "cascade": {
            "escalate_after_schema_failures": 2,
            "escalate_below_confidence": 0.3,
            "escalate_on_contradiction": True,
        },
        "fallback": {
            "max_attempts_per_model": 3,
            "base_delay_seconds": 0.5,
            "max_delay_seconds": 20.0,
            "retry_on_status": [429, 500],
            "retry_on_timeout": True,
        },
    }
    body.update(overrides)
    return body


class TestAPriceNeedsASource:
    """SPEC.md Section 17 Phase 8's reviewer focus, enforced rather than intended."""

    def test_a_sourced_price_is_accepted(self) -> None:
        price = Price(**PRICE)  # type: ignore[arg-type]
        assert price.source.startswith("https://")

    def test_a_price_with_no_source_is_rejected(self) -> None:
        without = {k: v for k, v in PRICE.items() if k != "source"}
        with pytest.raises(ValidationError):
            Price(**without)  # type: ignore[arg-type]

    def test_a_token_source_is_rejected(self) -> None:
        """ "n/a" or "docs" is not a source anybody can check."""
        with pytest.raises(ValidationError):
            Price(**{**PRICE, "source": "docs"})  # type: ignore[arg-type]

    def test_a_price_needs_a_date_it_was_checked(self) -> None:
        """Prices move. A figure quoted from a year old page is a guess presented
        as arithmetic."""
        without = {k: v for k, v in PRICE.items() if k != "checked_on"}
        with pytest.raises(ValidationError):
            Price(**without)  # type: ignore[arg-type]

    def test_a_malformed_date_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Price(**{**PRICE, "checked_on": "last week"})  # type: ignore[arg-type]

    def test_cost_is_per_million_tokens(self) -> None:
        price = Price(**PRICE)  # type: ignore[arg-type]
        assert price.cost_usd(1_000_000, 0) == pytest.approx(3.0)
        assert price.cost_usd(0, 1_000_000) == pytest.approx(15.0)
        assert price.cost_usd(500_000, 100_000) == pytest.approx(1.5 + 1.5)

    def test_negative_tokens_are_refused(self) -> None:
        price = Price(**PRICE)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="cannot be negative"):
            price.cost_usd(-1, 0)


class TestAnUnpricedModelIsAllowedAndReported:
    def test_a_model_may_have_no_price(self) -> None:
        """A local model through Ollama has no per-token price, and calling it
        zero would hide the hardware it needs."""
        spec = ModelSpec(id="local-thing", provider="ollama")
        assert not spec.priced

    def test_the_config_names_its_unpriced_models(self) -> None:
        """So a cost figure can say what it omits instead of hiding it in an
        average."""
        body = config_body(
            tiers={
                "small": {"models": [{"id": "cheap", "provider": "ollama"}]},
                "strong": {"models": [{"id": "dear", "provider": "x", "price": PRICE}]},
                "judge": {"models": []},
            }
        )
        config = ModelConfig.model_validate(body)
        assert config.unpriced() == ("cheap",)


class TestTheTiersMustMatchTheCode:
    def test_the_shipped_file_declares_exactly_the_known_tiers(self) -> None:
        config = load_model_config()
        assert set(config.tiers) == {tier.value for tier in Tier}

    def test_a_missing_tier_is_rejected(self) -> None:
        body = config_body(tiers={"small": {"models": []}, "strong": {"models": []}})
        with pytest.raises(ValidationError, match="missing"):
            ModelConfig.model_validate(body)

    def test_an_unknown_tier_is_rejected(self) -> None:
        """Configuration nothing reads is worse than none: somebody will edit it
        and expect an effect."""
        body = config_body(
            tiers={
                "small": {"models": []},
                "strong": {"models": []},
                "judge": {"models": []},
                "enormous": {"models": []},
            }
        )
        with pytest.raises(ValidationError, match="unknown"):
            ModelConfig.model_validate(body)


class TestAnEmptyTierIsValidAndCannotServeACall:
    def test_the_shipped_file_lists_no_models(self) -> None:
        """Deliberate. A plausible model id fails at the first call with a
        confusing error, and this repository's rules forbid inventing one."""
        config = load_model_config()
        assert not config.any_configured()

    def test_asking_an_empty_tier_names_the_file_to_edit(self) -> None:
        config = load_model_config()
        with pytest.raises(ModelConfigError, match="no models configured"):
            config.models_for(Tier.SMALL)
        with pytest.raises(ModelConfigError, match=re.escape("models.yaml")):
            config.models_for(Tier.STRONG)

    def test_a_configured_tier_returns_its_models_in_order(self) -> None:
        body = config_body(
            tiers={
                "small": {
                    "models": [
                        {"id": "first", "provider": "x"},
                        {"id": "second", "provider": "y"},
                    ]
                },
                "strong": {"models": []},
                "judge": {"models": []},
            }
        )
        config = ModelConfig.model_validate(body)
        assert [m.id for m in config.models_for(Tier.SMALL)] == ["first", "second"]


class TestLoadingTheFile:
    def test_the_shipped_file_is_valid(self) -> None:
        config = load_model_config()
        assert config.cascade.escalate_after_schema_failures >= 1
        assert config.fallback.deterministic_floor is True

    def test_a_missing_file_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(ModelConfigError, match="cannot read"):
            load_model_config(tmp_path / "absent.yaml")

    def test_a_file_that_is_not_a_mapping_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "list.yaml"
        path.write_text("- one\n- two\n")
        with pytest.raises(ModelConfigError, match="does not contain a mapping"):
            load_model_config(path)

    def test_an_unknown_key_is_rejected(self, tmp_path: Path) -> None:
        """extra=forbid, so a typo is an error rather than a setting that does
        nothing."""
        path = tmp_path / "typo.yaml"
        path.write_text(yaml.safe_dump(config_body(cascaed={})))
        with pytest.raises(ModelConfigError, match="not a valid model configuration"):
            load_model_config(path)


class TestTheShippedFileSaysWhyItIsEmpty:
    def test_it_explains_rather_than_just_being_blank(self) -> None:
        """An empty config with no comment reads as an oversight, and the next
        person fills it in with a guessed model id."""
        text = MODELS_PATH.read_text(encoding="utf-8")
        assert "DELIBERATE" in text
        assert "verify names" in text
