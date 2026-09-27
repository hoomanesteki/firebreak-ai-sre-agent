"""Tests for the deterministic floor.

SPEC.md Section 17 Phase 8 names one acceptance criterion for this: the floor
report appears when all LLMs fail. The rest of these tests are about the two ways
the floor can fail while looking like it worked, which are worse than not having
it: publishing without the label, and publishing with nothing in it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from firebreak.agent.budget import StopReason
from firebreak.agent.floor import (
    FLOOR_LABEL,
    FloorReason,
    build_floor_report,
    floor_reason_for,
)
from firebreak.agent.graph import investigate
from firebreak.agent.llm import LlmClient
from firebreak.agent.state import Confidence, Notebook
from firebreak.lab.bundle import derive_bundle_id
from firebreak.lab.scenario import load_library
from firebreak.lab.synthetic import build_synthetic_bundle
from firebreak.settings import LlmMode

REPO_ROOT = Path(__file__).resolve().parents[2]
SPECS_DIR = REPO_ROOT / "scenarios" / "specs"

# An error-injection scenario, which on the fixtures produces a signal large
# enough that triage does not abstain and the graph actually reaches a model.
FAULTED = "payment-failure-50pct-20u"


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):  # type: ignore[no-untyped-def]
    spec = load_library(SPECS_DIR)[FAULTED]
    root = tmp_path_factory.mktemp("floor")
    path = root / derive_bundle_id(spec.id, "floor")
    build_synthetic_bundle(path, spec, "floor", seed=11)
    return path


def no_models() -> LlmClient:
    """A client that cannot answer anything, which is every tier failing."""
    return LlmClient(mode=LlmMode.STUB, stub_handlers={})


class TestTheFloorAppearsWhenAllModelsFail:
    """SPEC.md Section 17 Phase 8's acceptance criterion, by name."""

    def test_the_investigation_returns_a_report_rather_than_raising(self, bundle: Path) -> None:
        """A stack trace at three in the morning is how a tool stops being
        opened."""
        result = investigate(bundle, llm=no_models())
        assert result.report is not None

    def test_it_carries_the_label_spec_requires(self, bundle: Path) -> None:
        result = investigate(bundle, llm=no_models())
        assert any(FLOOR_LABEL in note for note in result.report.notes)

    def test_it_says_why_the_floor_was_used(self, bundle: Path) -> None:
        """Models down and budget exhausted need different responses from an
        operator, so a report that only said "floor" would be unactionable."""
        result = investigate(bundle, llm=no_models())
        assert any("No model was reachable" in note for note in result.report.notes)

    def test_it_still_names_a_service(self, bundle: Path) -> None:
        """The ranking is deterministic and did not need a model. Losing it would
        leave the floor delivering nothing, which is the failure mode that makes a
        fallback worse than an error."""
        result = investigate(bundle, llm=no_models())
        assert result.report.root_cause_service is not None

    def test_it_cites_evidence_that_can_be_re_run(self, bundle: Path) -> None:
        """The claims come from B0's sections, so every number in a floor report
        is as checkable as one in a full report."""
        result = investigate(bundle, llm=no_models())
        assert result.report.cited_evidence
        assert result.gate.passed, result.gate.failed_checks

    def test_its_confidence_is_never_above_low(self, bundle: Path) -> None:
        """A deterministic ranking with no analysis behind it is a starting point.
        Calibration is scored, so claiming more would be punished for good
        reason."""
        result = investigate(bundle, llm=no_models())
        assert result.report.confidence in {None, Confidence.LOW}

    def test_the_run_notes_say_a_fallback_happened(self, bundle: Path) -> None:
        result = investigate(bundle, llm=no_models())
        assert any("falling back to deterministic triage" in note for note in result.notes)


class TestTheGateStillRuns:
    def test_all_six_checks_run_on_a_floor_report(self, bundle: Path) -> None:
        """The floor is a route to a published report, so it is a route through
        the gate. An exception for it would be the bypass the gate exists to not
        have."""
        result = investigate(bundle, llm=no_models())
        assert len(result.gate.results) == 6

    def test_the_seeded_notebook_does_not_strip_the_named_service(self, bundle: Path) -> None:
        """The bug this test exists for.

        `seed_hypotheses` fills the notebook from triage with hypotheses no
        specialist has supported yet. Handing that notebook to the gate made check
        6 see support of zero and abstain, so the floor published no service at
        all. The floor passes the notebook its own report implies instead.
        """
        result = investigate(bundle, llm=no_models())
        assert result.report.root_cause_service is not None
        assert result.state.notebook.hypotheses, "the seeded notebook should be non-empty"
        assert result.state.notebook.leader is not None
        assert result.state.notebook.leader.support == 0


class TestBuildingTheFloorReportDirectly:
    def test_it_reuses_a_b0_report_when_given_one(self, bundle: Path) -> None:
        """The floor is reached on a bad day; re-querying the whole bundle to
        produce the same answer twice would be the wrong instinct."""
        from firebreak.triage.report import build_b0_report

        b0 = build_b0_report(bundle, verify=False)
        floor = build_floor_report(bundle, FloorReason.MODELS_UNAVAILABLE, b0=b0)
        assert floor.b0 is b0

    def test_every_claim_cites_something(self, bundle: Path) -> None:
        """A section with no evidence is dropped rather than published uncited,
        because the gate would strip it and the notice would then misdescribe what
        happened."""
        floor = build_floor_report(bundle, FloorReason.MODELS_UNAVAILABLE)
        assert floor.report.claims
        assert all(claim.evidence_ids for claim in floor.report.claims)

    def test_the_label_is_reported_as_present(self, bundle: Path) -> None:
        floor = build_floor_report(bundle, FloorReason.MODELS_UNAVAILABLE)
        assert floor.labelled

    def test_each_reason_gives_different_guidance(self) -> None:
        guidance = {reason.guidance for reason in FloorReason}
        assert len(guidance) == len(list(FloorReason))

    def test_a_budget_stop_maps_to_the_budget_reason(self) -> None:
        assert floor_reason_for(StopReason.BUDGET_STEPS) is FloorReason.BUDGET_EXHAUSTED

    def test_a_non_budget_stop_does_not_claim_the_budget_ran_out(self) -> None:
        """Saying the budget ran out when it did not would send an operator to
        raise a limit that was never the problem."""
        assert floor_reason_for(StopReason.COMPLETE) is FloorReason.INVESTIGATION_FAILED
        assert floor_reason_for(None) is FloorReason.INVESTIGATION_FAILED


class TestTheImpliedNotebook:
    def test_it_supports_the_named_service_with_the_cited_evidence(self, bundle: Path) -> None:
        floor = build_floor_report(bundle, FloorReason.MODELS_UNAVAILABLE)
        notebook = Notebook.from_report(floor.report)
        leader = notebook.leader
        assert leader is not None
        assert leader.service == floor.report.root_cause_service
        assert leader.support == len(floor.report.cited_evidence)

    def test_an_abstaining_report_implies_nothing(self) -> None:
        from firebreak.agent.state import Report

        empty = Report(incident_id="inc_000000000000", root_cause_service=None)
        assert Notebook.from_report(empty).hypotheses == ()
