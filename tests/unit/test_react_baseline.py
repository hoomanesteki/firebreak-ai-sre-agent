"""Tests for baselines B1 and B2, and for ablation A1.

SPEC.md Section 9.5. A baseline only answers its question if it is a fair
implementation of the obvious alternative, so most of these tests are about
fairness: B1 gets every tool but the graph ones, the same caps, the same budget,
and the same windows, and it differs from B2 in the exit gate and in nothing
else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from firebreak.agent.budget import BudgetLimits, StopReason
from firebreak.agent.graph import AgentOptions, investigate
from firebreak.agent.llm import LlmClient
from firebreak.agent.react import (
    GRAPH_TOOLS,
    investigate_react,
    notebook_from,
    react_stub_handlers,
    react_tools,
)
from firebreak.agent.state import Claim, Confidence, Report
from firebreak.lab.bundle import derive_bundle_id
from firebreak.lab.scenario import load_library
from firebreak.lab.synthetic import build_synthetic_bundle
from firebreak.settings import LlmMode
from firebreak.tools.registry import build_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
SPECS_DIR = REPO_ROOT / "scenarios" / "specs"

FAULTED = "payment-failure-50pct-20u"


@pytest.fixture(scope="module")
def library():  # type: ignore[no-untyped-def]
    return load_library(SPECS_DIR)


@pytest.fixture(scope="module")
def bundle(tmp_path_factory, library):  # type: ignore[no-untyped-def]
    root = tmp_path_factory.mktemp("react")
    spec = library[FAULTED]
    path = root / derive_bundle_id(spec.id, "react")
    build_synthetic_bundle(path, spec, "react", seed=11)
    return path


class TestTheToolListIsTheDifference:
    """SPEC.md Section 9.5 gives B1 "no graph ranking" and A2 "no ranking, no
    dependency tools". Leaving the dependency tools in B1 would make it a weaker
    A2, and the two rows would stop answering different questions."""

    def test_b1_gets_every_tool_except_the_graph_ones(self) -> None:
        registry = build_registry()
        allowed = react_tools(registry)
        assert set(allowed) == set(registry.names()) - set(GRAPH_TOOLS)
        assert allowed, "B1 with no tools would be a strawman"

    def test_every_excluded_name_is_a_real_tool(self) -> None:
        """A renamed graph tool would silently rejoin the baseline."""
        registry = build_registry()
        for name in GRAPH_TOOLS:
            assert name in registry, f"{name} is not a tool, so excluding it does nothing"

    def test_an_unknown_excluded_name_is_refused(self) -> None:
        from firebreak.tools.base import ToolError, ToolRegistry

        with pytest.raises(ToolError, match="excludes graph tools"):
            react_tools(ToolRegistry([]))


class TestB1Investigates:
    def test_it_names_a_service_and_cites_evidence(self, bundle: Path) -> None:
        result = investigate_react(bundle)
        assert result.report.root_cause_service is not None
        assert result.report.cited_evidence
        assert result.budget.tool_calls > 0

    def test_it_has_no_gate(self, bundle: Path) -> None:
        """By definition. B2 is what shows the gate's value, so a gate here
        would mean B1 and B2 measured the same thing."""
        assert investigate_react(bundle).gate is None

    def test_it_is_deterministic_in_stub_mode(self, bundle: Path) -> None:
        first = investigate_react(bundle)
        second = investigate_react(bundle)
        assert first.report.root_cause_service == second.report.root_cause_service
        assert first.steps == second.steps

    def test_it_stops_when_the_budget_runs_out(self, bundle: Path) -> None:
        result = investigate_react(bundle, limits=BudgetLimits(max_rounds=1))
        assert result.stopped_because is StopReason.BUDGET_STEPS
        assert result.steps == 1

    def test_a_budget_stop_lowers_the_confidence(self, bundle: Path) -> None:
        """SPEC.md Section 6.6, and it applies to a baseline too: a report
        produced under a budget stop says so and lowers its confidence."""
        result = investigate_react(bundle, limits=BudgetLimits(max_rounds=1))
        assert result.report.confidence is Confidence.LOW
        assert result.report.stopped_because is StopReason.BUDGET_STEPS

    def test_a_repeated_call_is_refused_rather_than_run_again(self, bundle: Path) -> None:
        """The same cap Firebreak has. Without it the comparison would be
        between a system with loop protection and one without."""
        result = investigate_react(bundle)
        fingerprints = set(result.budget.call_counts)
        assert len(fingerprints) == result.budget.tool_calls

    def test_a_model_that_cannot_choose_a_step_ends_the_run(self, bundle: Path) -> None:
        """A single agent with no fallback stops, and says why in its notes."""
        result = investigate_react(bundle, llm=LlmClient(mode=LlmMode.STUB, stub_handlers={}))
        assert result.steps == 0
        assert any("could not choose" in note for note in result.notes)
        assert result.report.root_cause_service is None

    def test_a_bad_tool_argument_is_an_observation_not_a_crash(self, bundle: Path) -> None:
        """A single agent that died on its first bad argument would be a
        strawman. The failure is information: it is how the agent learns the
        tool's shape."""

        def handlers():  # type: ignore[no-untyped-def]
            calls = {"n": 0}

            def step(payload):  # type: ignore[no-untyped-def]
                calls["n"] += 1
                if calls["n"] == 1:
                    return {"tool": "list_anomalies", "arguments": {"nonsense": True}}
                return {"tool": ""}

            base = react_stub_handlers()
            return {"react_step": step, "react_report": base["react_report"]}

        result = investigate_react(
            bundle, llm=LlmClient(mode=LlmMode.STUB, stub_handlers=handlers())
        )
        assert result.steps == 1
        assert result.report.root_cause_service is None


class TestB2IsB1PlusTheGate:
    def test_it_runs_the_gate(self, bundle: Path) -> None:
        result = investigate_react(bundle, gate=True)
        assert result.gate is not None
        assert len(result.gate.results) == 6

    def test_the_gate_re_runs_the_cited_evidence(self, bundle: Path) -> None:
        """Check 2 is the one that needs a live backend, so this is also the
        test that the backend is still open when the gate runs."""
        result = investigate_react(bundle, gate=True)
        assert result.gate is not None
        re_execution = next(r for r in result.gate.results if r.name.value == "re_execution")
        assert re_execution.passed, re_execution.detail
        assert "re-ran identically" in re_execution.detail

    def test_it_differs_from_b1_in_nothing_but_the_gate(self, bundle: Path) -> None:
        b1 = investigate_react(bundle)
        b2 = investigate_react(bundle, gate=True)
        assert b1.steps == b2.steps
        assert b1.budget.tool_calls == b2.budget.tool_calls
        assert set(b1.evidence) == set(b2.evidence)

    def test_a_fabricated_citation_is_removed(self, bundle: Path) -> None:
        """What B2 exists to measure, forced rather than waited for."""

        def report(payload):  # type: ignore[no-untyped-def]
            del payload
            return {
                "root_cause_service": "payment",
                "confidence": "medium",
                "claims": [
                    {"text": "invented", "evidence_ids": ["ev_metric_deadbeefdead"]},
                ],
            }

        base = react_stub_handlers()
        result = investigate_react(
            bundle,
            gate=True,
            llm=LlmClient(
                mode=LlmMode.STUB,
                stub_handlers={"react_step": base["react_step"], "react_report": report},
            ),
        )
        assert result.gate is not None
        assert result.gate.removed == 1
        assert result.report.claims == ()
        assert any("could not be verified" in note for note in result.report.notes)


class TestTheNotebookB2sGateNeeds:
    """Checks 4 and 6 read a notebook, and a single agent has none.

    An empty one would make check 6 abstain on every incident, so B2 would score
    zero and measure nothing. The report is translated instead, and this is a
    judgement worth testing rather than burying.
    """

    def test_it_supports_the_service_the_report_named(self) -> None:
        report = Report(
            incident_id="inc_000000000000",
            root_cause_service="payment",
            claims=(Claim(text="a", evidence_ids=("ev_metric_000000000000",)),),
        )
        notebook = notebook_from(report)
        leader = notebook.leader
        assert leader is not None
        assert leader.service == "payment"
        assert leader.support == 1

    def test_support_counts_distinct_citations(self) -> None:
        """Two claims citing the same record are one piece of evidence."""
        report = Report(
            incident_id="inc_000000000000",
            root_cause_service="payment",
            claims=(
                Claim(text="a", evidence_ids=("ev_metric_000000000000",)),
                Claim(text="b", evidence_ids=("ev_metric_000000000000",)),
                Claim(text="c", evidence_ids=("ev_trace_000000000000",)),
            ),
        )
        leader = notebook_from(report).leader
        assert leader is not None
        assert leader.support == 2

    def test_an_abstaining_report_gets_an_empty_notebook(self) -> None:
        report = Report(incident_id="inc_000000000000", root_cause_service=None)
        assert notebook_from(report).hypotheses == ()


class TestA1IsFbWithoutTheCritic:
    def test_the_critic_runs_by_default(self, bundle: Path) -> None:
        llm = LlmClient(mode=LlmMode.STUB, stub_handlers=_stub_handlers())
        investigate(bundle, llm=llm)
        assert "critic" in llm.calls

    def test_a1_never_calls_the_critic(self, bundle: Path) -> None:
        """The ablation has to actually remove it, not just ignore its verdict.

        Ignoring the verdict would leave the cost in and take the value out,
        which would make A1 answer a question nobody asked.
        """
        llm = LlmClient(mode=LlmMode.STUB, stub_handlers=_stub_handlers())
        investigate(bundle, llm=llm, options=AgentOptions(use_critic=False))
        assert "critic" not in llm.calls

    def test_a1_still_produces_a_report(self, bundle: Path) -> None:
        result = investigate(bundle, options=AgentOptions(use_critic=False))
        assert result.report is not None
        assert result.gate is not None


def _stub_handlers():  # type: ignore[no-untyped-def]
    from firebreak.agent.graph import stub_handlers

    return stub_handlers()


def test_the_runner_knows_every_configuration_phase_7_compares() -> None:
    """SPEC.md Section 17 Phase 7: the validation report compares FB, B1, B2 and A1."""
    from firebreak.evals.runner import CONFIGURATIONS

    for required in ("b0", "b1", "b2", "fb-v1", "a1"):
        assert required in CONFIGURATIONS, f"{required} is not runnable"


def test_b1_reports_no_ranked_candidates(bundle: Path) -> None:
    """Deliberate. B1 has no ranking, so filling a ranked list in from the
    anomaly scores it happened to see would hand it a top-3 metric it did not
    earn. The graders score an empty ranking as a miss beyond rank 1, which is
    the honest reading of a configuration that names one service and offers no
    alternatives.
    """
    from firebreak.evals.runner import run_b1

    outcome, evidence = run_b1(bundle)
    assert outcome.ranked_candidates == ()
    assert outcome.root_cause_service is not None
    assert evidence


def test_fb_reports_the_ranking_it_actually_has(bundle: Path) -> None:
    """The other half of the comparison: FB earns its top-3 from a real ranking."""
    from firebreak.evals.runner import run_fb_v1

    outcome, _ = run_fb_v1(bundle)
    assert outcome.ranked_candidates
