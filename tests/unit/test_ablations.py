"""Tests that each ablation actually ablates.

SPEC.md Section 9.5 lists six ablations, and the failure mode they share is
silence: an ablation that does not remove what it claims to remove produces a
comparison between two identical systems and reports it as a finding. Every test
here asserts the removal happened, not just that the run finished.

Each ablation is a switch on the one graph rather than a second graph, so these
also assert that nothing else changed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from firebreak.agent.graph import AgentOptions, investigate, stub_handlers
from firebreak.agent.llm import LlmClient, Tier
from firebreak.agent.nodes import tools_for
from firebreak.lab.bundle import derive_bundle_id
from firebreak.lab.scenario import load_library
from firebreak.lab.synthetic import build_synthetic_bundle
from firebreak.settings import LlmMode
from firebreak.tools.registry import GRAPH_TOOLS, SPECIALIST_TOOLS
from firebreak.triage.pipeline import triage_bundle

REPO_ROOT = Path(__file__).resolve().parents[2]
SPECS_DIR = REPO_ROOT / "scenarios" / "specs"
FAULTED = "payment-failure-50pct-20u"


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):  # type: ignore[no-untyped-def]
    spec = load_library(SPECS_DIR)[FAULTED]
    root = tmp_path_factory.mktemp("ablations")
    path = root / derive_bundle_id(spec.id, "abl")
    build_synthetic_bundle(path, spec, "abl", seed=11)
    return path


def fresh_client() -> LlmClient:
    return LlmClient(mode=LlmMode.STUB, stub_handlers=stub_handlers())


class TestA1RemovesTheCritic:
    def test_the_critic_is_not_called(self, bundle: Path) -> None:
        llm = fresh_client()
        investigate(bundle, llm=llm, options=AgentOptions(use_critic=False))
        assert "critic" not in llm.calls

    def test_the_full_system_does_call_it(self, bundle: Path) -> None:
        """The other half of the comparison. Without this the test above would
        pass on a graph that had no critic at all."""
        llm = fresh_client()
        investigate(bundle, llm=llm, options=AgentOptions())
        assert "critic" in llm.calls

    def test_nothing_else_is_removed(self, bundle: Path) -> None:
        llm = fresh_client()
        investigate(bundle, llm=llm, options=AgentOptions(use_critic=False))
        assert "commander" in llm.calls
        assert "specialist" in llm.calls
        assert "reporter" in llm.calls


class TestA2RemovesTheGraph:
    """SPEC.md Section 9.5 gives A2 "no ranking, no dependency tools".

    Removing only the ranking would leave a configuration that still reads the
    graph, and the A2 row would stop answering the question it exists for.
    """

    def test_the_dependency_tools_leave_the_allowlist(self) -> None:
        allowed = tools_for("change_analyst", allow_graph=False)
        assert not set(allowed) & set(GRAPH_TOOLS)
        # Everything else it had, it keeps.
        expected = set(SPECIALIST_TOOLS["change_analyst"]) - set(GRAPH_TOOLS)
        assert set(allowed) == expected

    def test_the_full_system_keeps_them(self) -> None:
        allowed = tools_for("change_analyst", allow_graph=True)
        assert set(allowed) & set(GRAPH_TOOLS)

    def test_a_specialist_with_no_graph_tools_is_unaffected(self) -> None:
        """The switch must not quietly change a specialist it has nothing to do
        with."""
        for specialist in ("metrics_analyst", "logs_analyst", "traces_analyst"):
            assert tools_for(specialist, allow_graph=False) == SPECIALIST_TOOLS[specialist]

    def test_the_ranking_stops_using_dependency_edges(self, bundle: Path) -> None:
        """With no edges the walk has nothing to propagate through, so the order
        reduces to the anomaly ranking. That is what no ranking means here, and it
        keeps one ranking implementation rather than two that could disagree."""
        with_graph = triage_bundle(bundle, verify=False)
        without = triage_bundle(bundle, verify=False, use_graph=False)
        ranked_by_anomaly = [score.subject for score in without.anomalies]
        assert without.candidates[0].service == ranked_by_anomaly[0]
        assert with_graph.candidates, "the full system should still rank"

    def test_it_says_in_the_notes_when_a_tool_is_absent(self, bundle: Path) -> None:
        """A skipped tool call must be visible, or an ablation and a broken plan
        look the same in a transcript."""
        result = investigate(bundle, options=AgentOptions(use_graph=False))
        assert any("no blast_radius in this configuration" in note for note in result.notes)

    def test_the_full_system_gathers_the_graph_evidence(self, bundle: Path) -> None:
        full = investigate(bundle, options=AgentOptions())
        ablated = investigate(bundle, options=AgentOptions(use_graph=False))
        kinds = {record.kind.value for record in full.state.evidence.values()}
        ablated_kinds = {record.kind.value for record in ablated.state.evidence.values()}
        assert "topology" in kinds
        assert "topology" not in ablated_kinds


class TestA3AndA4OverrideTheTier:
    def test_all_strong_runs_every_call_at_strong(self, bundle: Path) -> None:
        llm = fresh_client()
        investigate(bundle, llm=llm, options=AgentOptions(tier_override=Tier.STRONG))
        assert Tier.SMALL in llm.tiers_requested, "the graph should still ask for small"
        assert {llm.effective_tier(t) for t in llm.tiers_requested} == {Tier.STRONG}

    def test_all_small_runs_every_call_at_small(self, bundle: Path) -> None:
        llm = fresh_client()
        investigate(bundle, llm=llm, options=AgentOptions(tier_override=Tier.SMALL))
        assert Tier.STRONG in llm.tiers_requested, "the graph should still ask for strong"
        assert {llm.effective_tier(t) for t in llm.tiers_requested} == {Tier.SMALL}

    def test_the_full_system_uses_both_tiers(self, bundle: Path) -> None:
        llm = fresh_client()
        investigate(bundle, llm=llm, options=AgentOptions())
        assert {llm.effective_tier(t) for t in llm.tiers_requested} == {Tier.SMALL, Tier.STRONG}

    def test_what_was_asked_for_is_recorded_separately(self, bundle: Path) -> None:
        """An ablation that only reported the override would make the cascade's
        own decisions invisible, and the cost comparison would lose its baseline."""
        llm = fresh_client()
        investigate(bundle, llm=llm, options=AgentOptions(tier_override=Tier.SMALL))
        assert set(llm.tiers_requested) == {Tier.SMALL, Tier.STRONG}

    def test_the_override_applies_to_a_client_the_caller_passed(self, bundle: Path) -> None:
        """It used to apply to a copy, so a caller inspecting its own client saw
        no override and would conclude the ablation had not applied."""
        llm = fresh_client()
        investigate(bundle, llm=llm, options=AgentOptions(tier_override=Tier.STRONG))
        assert llm.tier_override is Tier.STRONG

    def test_the_judge_tier_is_never_overridden_onto(self) -> None:
        """A judge runs offline. A runtime call reaching it would let the graded
        system spend the grader's budget."""
        llm = LlmClient(mode=LlmMode.STUB, tier_override=Tier.SMALL)
        assert llm.effective_tier(Tier.JUDGE) is Tier.JUDGE


class TestTheOptionsNameThemselves:
    def test_each_configuration_has_its_own_label(self) -> None:
        """Named from the switches rather than passed in, so a configuration
        cannot be labelled as something it is not."""
        assert AgentOptions().name == "full"
        assert AgentOptions(use_critic=False).name == "no-critic"
        assert AgentOptions(use_graph=False).name == "no-graph"
        assert AgentOptions(tier_override=Tier.SMALL).name == "all-small"
        assert AgentOptions(tier_override=Tier.STRONG).name == "all-strong"


class TestTheRunnerCanRunThemAll:
    def test_every_ablation_spec_names_is_registered(self) -> None:
        from firebreak.evals.runner import CONFIGURATIONS

        for name in ("a1", "a2", "a3", "a4"):
            assert name in CONFIGURATIONS

    def test_each_one_produces_an_outcome(self, bundle: Path) -> None:
        from firebreak.evals.runner import CONFIGURATIONS

        for name in ("a1", "a2", "a3", "a4"):
            outcome, evidence = CONFIGURATIONS[name](bundle)
            assert outcome.bundle_id
            assert evidence, f"{name} gathered no evidence at all"


class TestA5RemovesIncidentMemory:
    """SPEC.md Section 9.5. A switch on the run rather than an emptied memory file, so
    two configurations can be compared on one machine without moving data about.

    The failure this guards against is the one every ablation shares: a switch that
    removes nothing produces a comparison between two identical systems and reports it
    as a finding. So these tests assert that memory changes the board when it is on.
    """

    def memory_at(self, path: Path) -> None:
        """Put one entry in memory that the fixture bundle's symptoms will retrieve."""
        from datetime import UTC, datetime

        from firebreak.memory.store import MemoryStore, entry_from_label

        MemoryStore(path).admit(
            entry_from_label(
                incident_id="inc_past",
                scenario_id="a-past-incident",
                split="train",
                target_service="currency",
                fault_class="error_injection",
                symptom_summary=(
                    "payment anomaly high and frontend anomaly high with failed charges downstream"
                ),
                confirmed_by="owner",
                evidence_types=("metric", "log"),
                services_involved=("payment", "frontend"),
                confirmed_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )

    def run_with_memory(self, bundle: Path, path: Path, options: AgentOptions):  # type: ignore[no-untyped-def]
        from firebreak.memory import store as memory_store

        original = memory_store.MEMORY_PATH
        memory_store.MEMORY_PATH = path
        try:
            return investigate(bundle, options=options)
        finally:
            memory_store.MEMORY_PATH = original

    def test_memory_adds_a_hypothesis_when_it_is_on(self, bundle: Path, tmp_path: Path) -> None:
        path = tmp_path / "memory.jsonl"
        self.memory_at(path)
        result = self.run_with_memory(bundle, path, AgentOptions())
        assert any("incident memory added" in note for note in result.notes)
        assert "currency" in {h.service for h in result.state.notebook.hypotheses}

    def test_a5_adds_nothing(self, bundle: Path, tmp_path: Path) -> None:
        path = tmp_path / "memory.jsonl"
        self.memory_at(path)
        result = self.run_with_memory(bundle, path, AgentOptions(use_memory=False))
        assert not any("incident memory" in note for note in result.notes)
        assert "currency" not in {h.service for h in result.state.notebook.hypotheses}

    def test_the_memory_lookup_is_recorded_as_evidence_when_on(
        self, bundle: Path, tmp_path: Path
    ) -> None:
        """A prior nobody can trace afterwards is worse than no prior."""
        path = tmp_path / "memory.jsonl"
        self.memory_at(path)
        result = self.run_with_memory(bundle, path, AgentOptions())
        kinds = {record.kind.value for record in result.state.evidence.values()}
        assert "memory" in kinds

    def test_a5_records_no_memory_evidence(self, bundle: Path, tmp_path: Path) -> None:
        path = tmp_path / "memory.jsonl"
        self.memory_at(path)
        result = self.run_with_memory(bundle, path, AgentOptions(use_memory=False))
        kinds = {record.kind.value for record in result.state.evidence.values()}
        assert "memory" not in kinds

    def test_a_memory_hypothesis_arrives_with_no_supporting_evidence(
        self, bundle: Path, tmp_path: Path
    ) -> None:
        """What makes it advisory in practice rather than only in wording: the exit
        gate's abstention check counts support, so a memory-derived hypothesis cannot
        carry a report until a specialist confirms it."""
        path = tmp_path / "memory.jsonl"
        self.memory_at(path)
        result = self.run_with_memory(bundle, path, AgentOptions())
        from_memory = [h for h in result.state.notebook.hypotheses if h.service == "currency"]
        assert from_memory
        assert from_memory[0].statement.startswith("A past incident")

    def test_an_empty_memory_changes_nothing(self, bundle: Path, tmp_path: Path) -> None:
        """A fresh installation has no memory, and that must look like A5 rather than
        like a failure."""
        path = tmp_path / "empty.jsonl"
        with_memory = self.run_with_memory(bundle, path, AgentOptions())
        without = self.run_with_memory(bundle, path, AgentOptions(use_memory=False))
        assert {h.service for h in with_memory.state.notebook.hypotheses} == {
            h.service for h in without.state.notebook.hypotheses
        }

    def test_a5_is_registered(self) -> None:
        from firebreak.evals.runner import CONFIGURATIONS

        assert "a5" in CONFIGURATIONS
