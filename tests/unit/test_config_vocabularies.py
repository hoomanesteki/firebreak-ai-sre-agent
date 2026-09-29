"""Configuration files may not contain keys nothing reads.

**The failure mode this closes is the one that recurred all through this project:** two
components agreeing on a type and disagreeing on a vocabulary, silently. Here it was a file and
a model. `GateConfig` and the threshold models allowed unknown keys, so
`config/eval_gate.yaml` carried a `verdicts` list that no field declared. Editing it changed
nothing, and its comment described a decision order the code does not use.

The threshold file had the same hole for a different reason: it needed to carry the measurement
that justified each number, and allowing unknown keys was the cheap way to let it. The cost was
that a threshold nobody had implemented would be accepted and dropped, which matters most
exactly where the file's own comment invites one, saying the abstention rule "needs a second
signal, not a better number".

**Both are now declared and both models refuse the unknown.** These tests check that the
declarations still match what the files hold, and that nothing has quietly gone back to ignore.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from firebreak.evals.gate import GateConfig, Verdict, load_gate
from firebreak.triage.thresholds import (
    AbstentionThresholds,
    Measurement,
    RankingThresholds,
    Thresholds,
    WindowThresholds,
    load_thresholds,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = REPO_ROOT / "config" / "eval_gate.yaml"
THRESHOLDS_PATH = REPO_ROOT / "config" / "thresholds.yaml"

# Every model that reads a committed configuration file. A file has a closed vocabulary, unlike
# an environment, so each of these must refuse a key it does not know.
FILE_BACKED_MODELS: tuple[type[BaseModel], ...] = (
    GateConfig,
    Thresholds,
    AbstentionThresholds,
    RankingThresholds,
    WindowThresholds,
    Measurement,
)


class TestEveryFileBackedModelRefusesTheUnknown:
    @pytest.mark.parametrize("model", FILE_BACKED_MODELS, ids=lambda m: m.__name__)
    def test_it_forbids_extra_keys(self, model: type[BaseModel]) -> None:
        """Not `ignore`. A key nobody reads is a key somebody will set and believe in, and the
        belief is the damage: the value looks configured and the behaviour does not change."""
        assert model.model_config.get("extra") == "forbid", (
            f"{model.__name__} allows unknown keys, so a config typo is silent"
        )

    @pytest.mark.parametrize("model", FILE_BACKED_MODELS, ids=lambda m: m.__name__)
    def test_it_is_frozen(self, model: type[BaseModel]) -> None:
        """Configuration read once and mutated later is two configurations."""
        assert model.model_config.get("frozen") is True


class TestTheGateFileAndTheEnumAgree:
    """The defect: `verdicts` was in the file, absent from the model, and silently dropped."""

    def test_the_file_declares_every_verdict_the_code_can_return(self) -> None:
        declared = load_gate().verdicts
        assert list(declared) == list(Verdict), (
            "config/eval_gate.yaml's verdicts list and the Verdict enum have drifted"
        )

    def test_the_declaration_is_not_empty(self) -> None:
        """The field defaults to empty so an older file still loads. That default must not be
        what the committed file produces, or this whole class proves nothing."""
        assert load_gate().verdicts

    def test_an_unknown_gate_key_is_refused(self) -> None:
        raw = yaml.safe_load(GATE_PATH.read_text(encoding="utf-8"))
        raw["maximum_relative_increase"] = 0.5
        with pytest.raises(ValidationError, match="maximum_relative_increase"):
            GateConfig.model_validate(raw)

    def test_the_committed_file_still_loads(self) -> None:
        """Forbidding the unknown is only safe if the real file has no unknowns left."""
        assert load_gate().minimum_tasks >= 1

    def test_the_comment_no_longer_claims_the_list_is_an_order(self) -> None:
        """It claimed the verdicts were checked in the order listed. They are not: the code
        reports INCONCLUSIVE second and reaches PASS only by falling through. A comment that
        describes control flow the code does not have is worse than no comment."""
        text = GATE_PATH.read_text(encoding="utf-8")
        assert "A vocabulary, not an order" in text
        assert "in the order they are checked" not in text


class TestTheThresholdFileDeclaresItsProvenance:
    def test_the_measurement_block_is_a_declared_field(self) -> None:
        """It used to be an unknown key that `extra="ignore"` allowed through, which is what
        made every other unknown key allowed too."""
        assert "measured" in AbstentionThresholds.model_fields
        measured = load_thresholds().abstention.measured
        assert measured is not None
        assert measured.rule

    def test_an_invented_threshold_is_refused(self) -> None:
        """The case the file's own comment invites. It says the abstention rule needs a second
        signal rather than a better number, so the next edit is plausibly a signal that does not
        exist yet, and it used to be accepted and dropped."""
        with pytest.raises(ValidationError, match="latency_signal_z"):
            AbstentionThresholds(
                minimum_top_anomaly_z=22.717,
                minimum_hypothesis_support=2,
                latency_signal_z=4.0,
            )

    def test_a_misspelled_threshold_is_refused_rather_than_defaulted(self) -> None:
        """This half was already safe, because every threshold is required, so a typo makes the
        real key missing. Pinned anyway: adding a default to any of them would silently
        reintroduce the hazard."""
        with pytest.raises(ValidationError):
            AbstentionThresholds(minimum_top_anomaly_Z=13.4, minimum_hypothesis_support=2)

    def test_the_committed_file_still_loads(self) -> None:
        thresholds = load_thresholds()
        assert thresholds.abstention.minimum_top_anomaly_z > 0
        assert thresholds.ranking.restart_alpha > 0
        assert thresholds.windows.baseline_fraction > 0

    def test_every_key_in_the_file_is_known_to_a_model(self) -> None:
        """Belt and braces over the loader: asserted against the raw YAML, so a section the
        loader does not read at all would still be caught."""
        raw: dict[str, Any] = yaml.safe_load(THRESHOLDS_PATH.read_text(encoding="utf-8"))
        assert set(raw) == set(Thresholds.model_fields), "a whole section is unread"
        for section, model in (
            ("abstention", AbstentionThresholds),
            ("ranking", RankingThresholds),
            ("windows", WindowThresholds),
        ):
            unknown = set(raw[section]) - set(model.model_fields)
            assert not unknown, f"{section} holds keys nothing reads: {sorted(unknown)}"


class TestSettingsIsDeliberatelyDifferent:
    """`Settings` reads the environment rather than a file, and the environment is not a closed
    vocabulary. Forbidding the unknown there would crash the process over an unrelated
    `FIREBREAK_` variable somebody else set, so it stays permissive on purpose.

    Recorded as a test rather than a comment, because the audit that produced this file flagged
    it alongside the real problems and the next audit will too.
    """

    def test_settings_does_not_forbid_extra(self) -> None:
        from firebreak.settings import Settings

        assert Settings.model_config.get("extra") != "forbid"

    def test_the_variables_the_docs_name_are_still_read(self) -> None:
        """The compensating control. A typo'd environment variable is silently ignored here, so
        what has to be guaranteed instead is that every name the documentation gives is a name
        the code actually reads. That bug happened once already, with `LLM_BASE_URL`."""
        from firebreak.settings import Settings

        readable: set[str] = set()
        for name, field in Settings.model_fields.items():
            readable.add(f"FIREBREAK_{name.upper()}")
            alias = field.validation_alias
            for choice in getattr(alias, "choices", []) or ([alias] if alias else []):
                if isinstance(choice, str):
                    readable.add(choice)
        assert {"LLM_BASE_URL", "LLM_API_KEY"} <= readable
