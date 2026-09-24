"""Tests for the scripts that produce the numbers this project quotes.

These are not gatekeepers the way `check_leakage.py` is, so the bar is
different, but it is not zero. A measurement script that crashes is noticed
the next time someone runs it; a measurement script that silently computes
the wrong summary is quoted in a report and in an ADR and believed. The
scoring functions are where that would happen, so they are tested directly
against hand built inputs with answers that can be checked by eye.

Each script also gets one small end to end run, because a report file nobody
can regenerate is the thing this whole project exists to avoid.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import compare_log_templates as clt  # noqa: E402
import measure_ranking as mr  # noqa: E402
import run_baseline_b0 as b0  # noqa: E402


class TestRankingSummary:
    def _trial(self, rank: int | None, target: str | None = "payment", **kwargs: object):  # type: ignore[no-untyped-def]
        return mr.Trial(
            scenario_id="s",
            family="f",
            target=target,
            rank=rank,
            **kwargs,  # type: ignore[arg-type]
        )

    def test_top1_and_top3_count_what_they_say(self) -> None:
        summary = mr._summarise(
            [self._trial(1), self._trial(2), self._trial(3), self._trial(4), self._trial(None)]
        )
        assert summary["trials"] == 5
        assert summary["top1"] == 1
        assert summary["top3"] == 3

    def test_mean_reciprocal_rank_is_the_usual_definition(self) -> None:
        summary = mr._summarise([self._trial(1), self._trial(2), self._trial(4)])
        assert summary["mean_reciprocal_rank"] == pytest.approx((1 + 0.5 + 0.25) / 3, abs=1e-4)

    def test_no_fault_trials_are_excluded_from_rank_scoring(self) -> None:
        """A scenario with no culprit has no rank, and averaging one in would be nonsense."""
        summary = mr._summarise([self._trial(1), self._trial(None, target=None)])
        assert summary["trials"] == 1

    def test_an_unranked_culprit_is_recorded_as_unranked(self) -> None:
        summary = mr._summarise([self._trial(None)])
        assert summary["rank_distribution"] == {"unranked": 1}
        assert summary["mean_reciprocal_rank"] == 0.0

    def test_rank_in_is_one_based(self) -> None:
        assert mr._rank_in(["a", "b", "c"], "b") == 2
        assert mr._rank_in(["a", "b"], "missing") is None
        assert mr._rank_in(["a"], None) is None


class TestAbstentionThresholdChoice:
    def _trial(self, top_anomaly: float, target: str | None):  # type: ignore[no-untyped-def]
        return mr.Trial(scenario_id="s", family="f", target=target, rank=1, top_anomaly=top_anomaly)

    def test_a_separating_gap_puts_the_threshold_in_the_middle(self) -> None:
        """Not on either edge. The edge is the most fragile point in the range.

        Every threshold inside the gap scores a perfect Youden J, so the tie
        has to be broken on robustness rather than on whichever the sort
        happened to reach first.
        """
        trials = [self._trial(4.0, None), self._trial(100.0, "payment")]
        result = mr._abstention(trials)
        assert result["separable"] is True
        assert result["chosen"]["threshold"] == pytest.approx(20.0, abs=0.01)
        assert 4.0 < result["chosen"]["threshold"] < 100.0

    def test_a_perfect_split_reports_perfect_sensitivity_and_specificity(self) -> None:
        trials = [self._trial(4.0, None), self._trial(100.0, "payment")]
        chosen = mr._abstention(trials)["chosen"]
        assert chosen["sensitivity"] == 1.0
        assert chosen["specificity"] == 1.0

    def test_overlapping_classes_fall_back_to_the_best_tradeoff(self) -> None:
        """No gap to sit in the middle of, so it picks a point off the curve."""
        trials = [
            self._trial(10.0, None),
            self._trial(50.0, None),
            self._trial(20.0, "payment"),
            self._trial(60.0, "payment"),
        ]
        result = mr._abstention(trials)
        assert result["separable"] is False
        assert result["chosen"]["rule"] == "best Youden J"

    def test_it_refuses_to_answer_without_both_kinds_of_trial(self) -> None:
        assert mr._abstention([self._trial(10.0, "payment")])["usable"] is False
        assert mr._abstention([self._trial(10.0, None)])["usable"] is False


class TestBaselineScoring:
    def _scored(self, truth: str | None, named: str | None, abstained: bool):  # type: ignore[no-untyped-def]
        return b0.Scored(
            bundle_id="inc_000000000000",
            scenario_id="s",
            family="f",
            split="train",
            truth=truth,
            named=named,
            abstained=abstained,
            rank=1 if named == truth else None,
            evidence_count=3,
            tool_calls=3,
            cited=3,
        )

    def test_naming_the_culprit_is_correct(self) -> None:
        assert self._scored("payment", "payment", False).correct

    def test_naming_the_wrong_service_is_not(self) -> None:
        assert not self._scored("payment", "cart", False).correct

    def test_staying_silent_on_a_quiet_system_is_correct(self) -> None:
        """The no fault family is answered by naming nobody."""
        assert self._scored(None, None, True).correct

    def test_inventing_a_culprit_on_a_quiet_system_is_not(self) -> None:
        assert not self._scored(None, "cart", False).correct

    def test_the_summary_separates_the_two_kinds_of_failure(self) -> None:
        """Naming the wrong service and missing an incident are not the same.

        One sends an engineer somewhere useless with a confident report; the
        other sends nobody anywhere. Folding them into one number would hide
        which a change had traded for the other.
        """
        summary = b0._summarise(
            [
                self._scored("payment", "payment", False),
                self._scored("payment", "cart", False),
                self._scored("payment", None, True),
                self._scored(None, None, True),
                self._scored(None, "cart", False),
            ]
        )
        assert summary["faulted"]["named_wrong_service"] == 1
        assert summary["faulted"]["abstained_on_a_real_incident"] == 1
        assert summary["no_fault"]["correctly_said_nothing"] == 1
        assert summary["no_fault"]["invented_a_culprit"] == 1
        assert summary["correct"] == 2


class TestLogTemplateComparison:
    def test_the_corpus_is_labelled_and_varied(self) -> None:
        corpus = clt.build_corpus()
        assert len(corpus) > 100
        assert len({item.template_id for item in corpus}) > 10
        # Two lines from one event must differ, or masking is being tested
        # against data that cannot tell it apart from doing nothing.
        for template_id in list({item.template_id for item in corpus})[:5]:
            bodies = [i.body for i in corpus if i.template_id == template_id]
            assert len(set(bodies)) > 1, f"{template_id} generated identical lines"

    def test_a_pinned_entity_actually_appears_in_the_line(self) -> None:
        """The label has to describe the line, or the whole score is fiction."""
        corpus = clt.build_corpus()
        for item in corpus:
            _, _, service, route, peer = item.template_id.split(":")
            assert item.body.startswith(service)
            if route != "-":
                assert route in item.body
            if peer != "-":
                assert peer in item.body

    def test_scoring_a_perfect_clustering(self) -> None:
        truth = ["a", "a", "b", "b"]
        result = clt.score(["x", "x", "y", "y"], truth)
        assert result["purity"] == 1.0
        assert result["merged_clusters"] == 0
        assert result["fragmented_templates"] == 0

    def test_scoring_notices_a_merge(self) -> None:
        result = clt.score(["x", "x", "x", "x"], ["a", "a", "b", "b"])
        assert result["merged_clusters"] == 1
        assert result["purity"] == 0.5

    def test_scoring_notices_fragmentation(self) -> None:
        result = clt.score(["x", "y", "z", "w"], ["a", "a", "a", "a"])
        assert result["fragmented_templates"] == 1
        assert result["purity"] == 1.0

    def test_the_masker_is_stable_and_this_asserts_it(self) -> None:
        """The property ADR-0006 turns on."""
        corpus = clt.build_corpus()[:200]
        assert clt.stability(clt.cluster_with_masker, corpus)["stable"] is True


class TestScriptsRunEndToEnd:
    """A report nobody can regenerate is an assertion, not a measurement."""

    @pytest.mark.slow
    def test_measure_ranking_writes_its_reports(self, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(mr, "REPORT_PATH", tmp_path / "ablation.json")
        monkeypatch.setattr(mr, "BASELINE_PATH", tmp_path / "baseline.json")
        monkeypatch.setattr(mr, "SEEDS", (1,))
        monkeypatch.setattr(mr, "BASELINE_FRACTIONS", (0.3333333333333333,))
        monkeypatch.setattr(sys, "argv", ["measure_ranking.py", "--limit", "2"])

        assert mr.main() == 0
        report = json.loads((tmp_path / "ablation.json").read_text())
        assert "graph_default" in report["configurations"]
        assert "synthetic" in report["caveat"].lower()

    @pytest.mark.slow
    def test_run_baseline_b0_writes_its_report(self, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(b0, "REPORT_PATH", tmp_path / "b0.json")
        monkeypatch.setattr(sys, "argv", ["run_baseline_b0.py", "--limit", "2"])

        assert b0.main() == 0
        report = json.loads((tmp_path / "b0.json").read_text())
        assert report["overall"]["bundles"] == 2
        # Without a recorded library the report must say so in its own
        # fields, or a synthetic number gets quoted as a result.
        assert report["data_source"] == "synthetic fixtures"
        assert "must not be quoted as a result" in report["caveat"]

    @pytest.mark.slow
    def test_compare_log_templates_writes_its_report(self, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(clt, "REPORT_PATH", tmp_path / "logs.json")
        monkeypatch.setattr(clt, "SERVICES", ("frontend", "cart"))
        monkeypatch.setattr(clt, "LINES_PER_EVENT", 2)

        assert clt.main() == 0
        report = json.loads((tmp_path / "logs.json").read_text())
        assert report["masker"]["stability"]["stable"] is True
        assert "errors_only" in report["masker"]
