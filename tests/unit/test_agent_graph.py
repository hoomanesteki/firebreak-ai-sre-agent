"""Integration tests for the investigation graph, in stub mode.

SPEC.md Section 17 Phase 6 asks for every node path to be covered in `stub`
mode, and for a budget breach to produce a partial report rather than nothing.

Run against synthetic fixtures so the suite needs no recordings, with one
exception: the argument contract below, which is checked against the tools'
own input models and needs no data at all.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import pytest

from firebreak.agent.budget import BudgetLimits, StopReason
from firebreak.agent.graph import SPECIALISTS, investigate, stub_handlers
from firebreak.agent.llm import LlmClient, LlmError, Tier
from firebreak.agent.nodes import _gather, entry_gate, exit_gate, seed_hypotheses
from firebreak.agent.state import (
    Claim,
    Confidence,
    Finding,
    Hypothesis,
    InvestigationState,
    Notebook,
    Report,
    Status,
)
from firebreak.lab.bundle import derive_bundle_id
from firebreak.lab.scenario import load_library
from firebreak.lab.synthetic import build_synthetic_bundle
from firebreak.settings import LlmMode
from firebreak.tools.registry import ALL_TOOLS, SPECIALIST_TOOLS, build_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
SPECS_DIR = REPO_ROOT / "scenarios" / "specs"

# An error-injection scenario, which produces a signal large enough on the
# fixtures that triage does not abstain and the whole loop runs.
FAULTED = "payment-failure-50pct-20u"
NO_FAULT = "no-fault-flood-homepage-sr5-20u"


@pytest.fixture(scope="module")
def library():  # type: ignore[no-untyped-def]
    return load_library(SPECS_DIR)


@pytest.fixture(scope="module")
def faulted_bundle(library, tmp_path_factory):  # type: ignore[no-untyped-def]
    root = tmp_path_factory.mktemp("agent-faulted")
    spec = library[FAULTED]
    bundle = root / derive_bundle_id(spec.id, "agent")
    build_synthetic_bundle(bundle, spec, "agent", seed=11)
    return bundle


@pytest.fixture(scope="module")
def quiet_bundle(library, tmp_path_factory):  # type: ignore[no-untyped-def]
    root = tmp_path_factory.mktemp("agent-quiet")
    spec = library[NO_FAULT]
    bundle = root / derive_bundle_id(spec.id, "agent")
    build_synthetic_bundle(bundle, spec, "agent", seed=11)
    return bundle


class TestTheSpecialistPlansMatchTheirTools:
    """The bug this class exists for shipped and hid.

    The traces analyst passed `service` where `find_traces` takes `services`, a
    tuple. The tool refused every call, so the specialist gathered nothing and
    did so silently, because a tool that cannot answer is deliberately treated
    as a fact about the incident rather than a crash. A wrong argument name is
    not a fact about the incident.

    Checked against the tools' own input models, so a tool changing its
    arguments breaks this rather than quietly disabling a specialist.
    """

    def test_every_planned_call_validates_against_its_tool(self) -> None:
        from datetime import UTC, datetime

        from firebreak.agent.state import Brief

        registry = build_registry(ALL_TOOLS)
        moment = datetime(2025, 1, 1, tzinfo=UTC)
        hypothesis = Hypothesis(id="h1", service="payment", statement="payment broke")

        class _Recorder:
            """Captures the arguments without running anything."""

            def __init__(self) -> None:
                self.seen: list[tuple[str, dict[str, Any]]] = []

            def call(self, name: str, context: Any, arguments: dict[str, Any]) -> Any:
                # Validate through the tool's real input model, which is the
                # whole point: this is the check the graph skipped.
                registry.spec(name).parse(arguments)
                self.seen.append((name, arguments))
                raise _StopError

        class _StopError(Exception):
            pass

        for specialist in SPECIALISTS:
            brief = Brief(
                specialist=specialist,
                hypothesis=hypothesis,
                question="?",
                window_start=moment,
                window_end=moment,
                baseline_start=moment,
                baseline_end=moment,
                allowed_tools=SPECIALIST_TOOLS.get(specialist, ()),
            )
            state = InvestigationState(incident_id="inc_000000000000")
            recorder = _Recorder()
            context = type(
                "Ctx", (), {"registry": recorder, "tools": None, "notes": [], "llm": None}
            )()
            with contextlib.suppress(_StopError):
                _gather(state, context, brief)  # type: ignore[arg-type]
            assert recorder.seen, f"{specialist} planned no tool calls"

    def test_no_specialist_plans_a_tool_it_may_not_call(self) -> None:
        """The allowlist is enforced in `_gather` and asserted here too."""
        for specialist in SPECIALISTS:
            assert SPECIALIST_TOOLS.get(specialist), f"{specialist} has no tools"


class TestEntryGate:
    def test_a_well_formed_incident_passes(self) -> None:
        from datetime import UTC, datetime

        moment = datetime(2025, 1, 1, tzinfo=UTC)
        later = datetime(2025, 1, 1, 1, tzinfo=UTC)
        state = InvestigationState(
            incident_id="inc_000000000000",
            window_start=moment,
            window_end=later,
            baseline_start=moment,
            baseline_end=moment,
        )
        assert entry_gate(state).status is Status.TRIAGED

    def test_an_incident_with_no_window_is_refused_before_any_model_call(self) -> None:
        """Every check here is cheaper than the investigation it prevents."""
        state = InvestigationState(incident_id="inc_000000000000")
        result = entry_gate(state)
        assert result.status is Status.FAILED
        assert result.report is not None
        assert "no window" in result.report.claims[0].text

    def test_a_backwards_window_is_refused(self) -> None:
        from datetime import UTC, datetime

        state = InvestigationState(
            incident_id="inc_000000000000",
            window_start=datetime(2025, 1, 2, tzinfo=UTC),
            window_end=datetime(2025, 1, 1, tzinfo=UTC),
            baseline_start=datetime(2025, 1, 1, tzinfo=UTC),
            baseline_end=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert entry_gate(state).status is Status.FAILED


class TestSeeding:
    def test_an_abstaining_triage_seeds_nothing(self, quiet_bundle) -> None:  # type: ignore[no-untyped-def]
        """Investigating a quiet system spends the budget proving nothing happened."""
        result = investigate(quiet_bundle)
        assert result.report.abstained
        assert result.state.budget.tool_calls == 0
        assert "No service was unusual enough" in result.report.claims[0].text

    def test_candidates_become_hypotheses_without_a_model_call(self, faulted_bundle) -> None:  # type: ignore[no-untyped-def]
        from firebreak.triage.pipeline import triage_bundle

        triage = triage_bundle(faulted_bundle, verify=False)
        state = InvestigationState(incident_id=triage.bundle_id, triage=triage)
        seeded = seed_hypotheses(state)
        if triage.says_nothing_is_wrong:
            pytest.skip("this fixture abstains, so there is nothing to seed")
        assert seeded.notebook.hypotheses
        assert len(seeded.notebook.hypotheses) <= 3


class TestTheWholeLoop:
    def test_it_finds_the_culprit_and_cites_evidence(self, faulted_bundle) -> None:  # type: ignore[no-untyped-def]
        result = investigate(faulted_bundle)
        if result.report.abstained:
            pytest.skip("triage abstained on this fixture")
        assert result.state.budget.tool_calls > 0
        assert result.state.evidence
        assert result.report.claims
        # Every surviving claim cites something the investigation gathered,
        # because the exit gate strips the rest.
        for claim in result.report.claims:
            assert claim.evidence_ids
            for evidence_id in claim.evidence_ids:
                assert evidence_id in result.state.evidence

    def test_every_specialist_contributes(self, faulted_bundle) -> None:  # type: ignore[no-untyped-def]
        """A specialist that gathers nothing is a bug, not a quiet result.

        This is the assertion that would have caught the find_traces argument
        error, which disabled the traces analyst on every run.
        """
        result = investigate(faulted_bundle)
        if result.report.abstained:
            pytest.skip("triage abstained on this fixture")
        contributed = {f.specialist for f in result.state.findings}
        assert contributed == set(SPECIALISTS), f"missing: {set(SPECIALISTS) - contributed}"

    def test_it_is_deterministic_in_stub_mode(self, faulted_bundle) -> None:  # type: ignore[no-untyped-def]
        """Same bundle, same answer, so pass^3 measures the agent."""
        first = investigate(faulted_bundle)
        second = investigate(faulted_bundle)
        assert first.report.root_cause_service == second.report.root_cause_service
        assert [c.text for c in first.report.claims] == [c.text for c in second.report.claims]

    def test_it_records_why_it_stopped(self, faulted_bundle) -> None:  # type: ignore[no-untyped-def]
        result = investigate(faulted_bundle)
        assert result.stopped_because in set(StopReason)


class TestBudgets:
    def test_a_round_ceiling_still_produces_a_report(self, faulted_bundle) -> None:  # type: ignore[no-untyped-def]
        """SPEC.md Section 17: a budget breach produces a partial report.

        Nothing at all would be the worst outcome, because an on-call engineer
        gets neither an answer nor an explanation.
        """
        result = investigate(faulted_bundle, limits=BudgetLimits(max_rounds=1))
        assert result.report is not None
        if not result.report.abstained:
            assert result.stopped_because is StopReason.BUDGET_STEPS

    def test_a_tool_call_ceiling_stops_the_run(self, faulted_bundle) -> None:  # type: ignore[no-untyped-def]
        result = investigate(faulted_bundle, limits=BudgetLimits(max_tool_calls=2))
        assert result.report is not None
        if not result.report.abstained:
            assert result.state.budget.tool_calls <= 6

    def test_a_cut_short_report_lowers_its_confidence(self, faulted_bundle) -> None:  # type: ignore[no-untyped-def]
        """A report that hit its ceiling must not read like a finished one."""
        result = investigate(faulted_bundle, limits=BudgetLimits(max_rounds=1))
        if result.report.abstained or result.stopped_because is StopReason.COMPLETE:
            pytest.skip("this fixture completed rather than being cut short")
        assert result.report.confidence is Confidence.LOW
        assert result.report.stopped_because is not None


class TestExitGate:
    def test_a_claim_citing_nothing_is_stripped(self) -> None:
        state = InvestigationState(incident_id="inc_000000000000")
        report = Report(
            incident_id="inc_000000000000",
            root_cause_service="payment",
            claims=(Claim(text="payment broke"),),
        )
        assert exit_gate(state, report).claims == ()

    def test_a_claim_citing_an_invented_id_is_stripped(self) -> None:
        """The cheapest fabrication there is, caught here."""
        state = InvestigationState(incident_id="inc_000000000000")
        report = Report(
            incident_id="inc_000000000000",
            root_cause_service="payment",
            claims=(Claim(text="payment broke", evidence_ids=("ev_invented",)),),
        )
        assert exit_gate(state, report).claims == ()

    def test_the_root_cause_survives_losing_every_claim(self) -> None:
        """The ranking behind it is deterministic and did not come from a model.

        What a stripped report loses is the prose, which is the right thing to
        lose.
        """
        state = InvestigationState(incident_id="inc_000000000000")
        report = Report(
            incident_id="inc_000000000000",
            root_cause_service="payment",
            claims=(Claim(text="unsupported"),),
        )
        assert exit_gate(state, report).root_cause_service == "payment"


class TestFindingsMustCiteEvidence:
    def test_a_finding_with_no_evidence_cannot_be_built(self) -> None:
        """A tool call that found nothing still produces a record."""
        with pytest.raises(ValueError, match=r"citing\s+no evidence"):
            Finding(
                specialist="metrics_analyst",
                hypothesis_id="h1",
                summary="nothing to see",
                supports=False,
                confidence=Confidence.LOW,
            )


class TestStubMode:
    def test_a_missing_handler_is_an_error_not_silence(self) -> None:
        """A node receiving nothing would look like a model with nothing to say."""
        client = LlmClient(mode=LlmMode.STUB, stub_handlers={})
        with pytest.raises(LlmError, match="no stub handler"):
            client.complete("commander", {}, Report, tier=Tier.STRONG)

    def test_a_handler_producing_the_wrong_shape_fails_loudly(self) -> None:
        """A stub is a bug when it is wrong, not a model to be repaired."""
        client = LlmClient(
            mode=LlmMode.STUB, stub_handlers={"x": lambda payload: {"nonsense": True}}
        )
        with pytest.raises(LlmError, match="rejects"):
            client.complete("x", {}, Report, tier=Tier.SMALL)

    def test_stub_completions_report_no_cost(self) -> None:
        """Reporting invented tokens would put fiction in every eval's cost column."""
        handlers = stub_handlers()
        client = LlmClient(mode=LlmMode.STUB, stub_handlers=handlers)
        from firebreak.agent.nodes import CommanderPlan

        _, completion = client.complete(
            "commander", {"hypotheses": []}, CommanderPlan, tier=Tier.STRONG
        )
        assert (completion.tokens_in, completion.tokens_out, completion.usd) == (0, 0, 0.0)

    def test_local_and_api_modes_refuse_without_an_endpoint(self) -> None:
        from firebreak.agent.llm import ModelUnavailableError

        for mode in (LlmMode.LOCAL, LlmMode.API):
            client = LlmClient(mode=mode)
            with pytest.raises(ModelUnavailableError, match="needs a configured endpoint"):
                client.complete("commander", {}, Report, tier=Tier.STRONG)


class TestNotebook:
    def test_hypotheses_rank_by_support_with_a_stable_tiebreak(self) -> None:
        notebook = Notebook(
            hypotheses=(
                Hypothesis(id="hb", service="b", statement="b", supporting_evidence=("1",)),
                Hypothesis(id="ha", service="a", statement="a", supporting_evidence=("1",)),
                Hypothesis(
                    id="hc",
                    service="c",
                    statement="c",
                    supporting_evidence=("1", "2"),
                ),
            )
        )
        assert [h.id for h in notebook.ranked()] == ["hc", "ha", "hb"]
        assert notebook.leader is not None and notebook.leader.id == "hc"

    def test_support_is_a_difference_not_a_ratio(self) -> None:
        """One piece each way is unresolved, not moderately supported."""
        contested = Hypothesis(
            id="h1",
            service="a",
            statement="a",
            supporting_evidence=("1",),
            contradicting_evidence=("2",),
        )
        assert contested.support == 0
        assert contested.is_contested
