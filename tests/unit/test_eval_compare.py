"""Tests for comparing two configurations on the same tasks.

The failure this module exists to prevent is a comparison that looks valid and
is not: two runs covering different incidents, paired by position, reporting a
difference between one system's third incident and another's third incident.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from firebreak.evals.compare import (
    Comparison,
    ComparisonError,
    LoadedReport,
    compare,
    load_report,
)

RESAMPLES = 500


def report(configuration: str, per_task: dict[str, dict[str, float]], split="validation"):  # type: ignore[no-untyped-def]
    return LoadedReport(
        configuration=configuration,
        split=split,
        per_task=per_task,
        quotable=False,
        caveats=(),
    )


def grader(result: Comparison, name: str):  # type: ignore[no-untyped-def]
    return next(g for g in result.graders if g.grader == name)


class TestPairingIsByTaskNotPosition:
    def test_only_tasks_both_runs_covered_are_compared(self) -> None:
        """A split fills up as it is recorded, so two runs can differ in coverage."""
        treatment = report("fb", {"inc_a": {"root_cause": 1.0}, "inc_b": {"root_cause": 1.0}})
        control = report("b0", {"inc_b": {"root_cause": 0.0}, "inc_c": {"root_cause": 0.0}})
        result = compare(treatment, control, resamples=RESAMPLES)
        assert result.tasks_in_common == 1
        assert result.treatment_only == ("inc_a",)
        assert result.control_only == ("inc_c",)
        assert grader(result, "root_cause").paired_tasks == 1

    def test_a_reordered_report_gives_the_same_answer(self) -> None:
        """The property that proves the pairing is by id.

        With positional pairing, reversing one side's task order would change the
        difference. With pairing by id it cannot.
        """
        scores_a = {"inc_a": {"g": 1.0}, "inc_b": {"g": 0.0}, "inc_c": {"g": 1.0}}
        scores_b = {"inc_a": {"g": 0.0}, "inc_b": {"g": 1.0}, "inc_c": {"g": 0.0}}
        forwards = compare(report("fb", scores_a), report("b0", scores_b), resamples=RESAMPLES)
        backwards = compare(
            report("fb", dict(reversed(list(scores_a.items())))),
            report("b0", scores_b),
            resamples=RESAMPLES,
        )
        assert grader(forwards, "g").difference.estimate == pytest.approx(
            grader(backwards, "g").difference.estimate
        )

    def test_no_tasks_in_common_is_refused(self) -> None:
        with pytest.raises(ComparisonError, match="no tasks in common"):
            compare(
                report("fb", {"inc_a": {"g": 1.0}}),
                report("b0", {"inc_b": {"g": 0.0}}),
                resamples=RESAMPLES,
            )


class TestWhatCannotBeCompared:
    def test_two_splits_are_refused(self) -> None:
        """A difference across splits would measure the splits."""
        with pytest.raises(ComparisonError, match="across splits"):
            compare(
                report("fb", {"inc_a": {"g": 1.0}}),
                report("b0", {"inc_a": {"g": 0.0}}, split="test_id"),
                resamples=RESAMPLES,
            )

    def test_a_configuration_against_itself_is_refused(self) -> None:
        with pytest.raises(ComparisonError, match="against itself"):
            compare(
                report("fb", {"inc_a": {"g": 1.0}}),
                report("fb", {"inc_a": {"g": 1.0}}),
                resamples=RESAMPLES,
            )

    def test_a_report_with_no_per_task_scores_is_refused(self, tmp_path: Path) -> None:
        """Reports written before per-task scores existed cannot be paired.

        Saying so beats comparing against nothing and reporting a difference of
        zero, which is what a missing key would produce.
        """
        path = tmp_path / "old.json"
        path.write_text(json.dumps({"configuration": "b0", "split": "validation"}))
        with pytest.raises(ComparisonError, match="no per-task scores"):
            load_report(path)


class TestAGraderThatDidNotApplyIsNotAZero:
    def test_a_task_one_side_did_not_score_is_excluded(self) -> None:
        """B0 declines to name a fault class, and scoring that zero would punish
        it for not guessing."""
        treatment = report(
            "fb",
            {
                "inc_a": {"root_cause": 1.0, "fault_class": 1.0},
                "inc_b": {"root_cause": 1.0, "fault_class": 1.0},
            },
        )
        control = report("b0", {"inc_a": {"root_cause": 0.0}, "inc_b": {"root_cause": 0.0}})
        result = compare(treatment, control, resamples=RESAMPLES)
        assert [g.grader for g in result.graders] == ["root_cause"]
        assert grader(result, "root_cause").paired_tasks == 2

    def test_partial_coverage_within_a_grader_is_counted(self) -> None:
        treatment = report("fb", {"inc_a": {"g": 1.0}, "inc_b": {"g": 1.0}})
        control = report("b0", {"inc_a": {"g": 0.0}, "inc_b": {}})
        result = compare(treatment, control, resamples=RESAMPLES)
        compared = grader(result, "g")
        assert compared.paired_tasks == 1
        assert compared.unpaired_tasks == 1


class TestTheVerdict:
    def test_an_interval_spanning_zero_does_not_separate(self) -> None:
        """However far apart the means look."""
        treatment = report("fb", {"inc_a": {"g": 1.0}, "inc_b": {"g": 0.0}})
        control = report("b0", {"inc_a": {"g": 0.0}, "inc_b": {"g": 1.0}})
        assert not grader(compare(treatment, control, resamples=RESAMPLES), "g").separates

    def test_a_consistent_win_separates(self) -> None:
        tasks = {f"inc_{i}": {"g": 1.0} for i in range(12)}
        losses = {f"inc_{i}": {"g": 0.0} for i in range(12)}
        compared = grader(
            compare(report("fb", tasks), report("b0", losses), resamples=RESAMPLES), "g"
        )
        assert compared.separates
        assert compared.difference.estimate == pytest.approx(1.0)

    def test_the_difference_is_signed_towards_the_treatment(self) -> None:
        """Positive means the treatment scored higher, which is the only
        convention that makes a table readable."""
        compared = grader(
            compare(
                report("fb", {"inc_a": {"g": 0.0}}),
                report("b0", {"inc_a": {"g": 1.0}}),
                resamples=RESAMPLES,
            ),
            "g",
        )
        assert compared.difference.estimate == pytest.approx(-1.0)

    def test_it_serialises(self) -> None:
        result = compare(
            report("fb", {"inc_a": {"g": 1.0}}),
            report("b0", {"inc_a": {"g": 0.0}}),
            resamples=RESAMPLES,
        )
        payload = result.as_dict()
        assert payload["treatment"] == "fb"
        assert payload["control"] == "b0"
        assert payload["tasks_in_common"] == 1
        assert payload["graders"][0]["grader"] == "g"


class TestWrittenReportsCarryWhatAComparisonNeeds:
    def test_a_real_report_round_trips(self, tmp_path: Path) -> None:
        """SPEC.md Section 9.6 asks the JSON to carry per-trial results, and a
        comparison against a stored baseline is why."""
        path = tmp_path / "report.json"
        path.write_text(
            json.dumps(
                {
                    "configuration": "fb-v1",
                    "split": "validation",
                    "quotable_as_a_result": False,
                    "caveats": ["a caveat"],
                    "per_task": {"inc_a": {"root_cause": 1.0}},
                }
            )
        )
        loaded = load_report(path)
        assert loaded.configuration == "fb-v1"
        assert loaded.per_task == {"inc_a": {"root_cause": 1.0}}
        assert loaded.caveats == ("a caveat",)
