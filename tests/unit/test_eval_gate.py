"""Tests for firebreak.evals.gate, including the determinism SPEC.md requires.

SPEC.md Section 17 Phase 5 asks for a gate determinism test. The reason is
practical rather than theoretical: a gate that occasionally changes its mind
about the same two reports is a gate people learn to re-run until it passes,
and at that point it is worse than having none.

The other half of these tests is direction. A non-inferiority check reads one
end of a paired interval, and reading the wrong end would pass exactly the
regressions the gate exists to catch, silently and while looking like it
worked.
"""

from __future__ import annotations

import pytest

from firebreak.evals.gate import (
    GateConfig,
    GateError,
    Side,
    Verdict,
    evaluate_gate,
    load_gate,
)
from firebreak.evals.graders import grade_trial
from firebreak.evals.outcome import InvestigationOutcome
from firebreak_eval_labels import IncidentLabel, new_canary

# Small enough to be fast, large enough that the interval is not degenerate.
RESAMPLES = 400
TASKS = 30
ALLOWED = {"page-owning-team"}


def _bundle_id(index: int) -> str:
    return f"inc_{index:012x}"


def _label(index: int) -> IncidentLabel:
    return IncidentLabel(
        scenario_id=f"scenario-{index}",
        run_id="r1",
        bundle_id=_bundle_id(index),
        split="test_id",
        target_service="payment",
        fault_class="error_injection",
        canary=new_canary(),
    )


def _side(
    correct_count: int,
    tasks: int = TASKS,
    confidence: float | None = 0.6,
    usd: float = 0.0,
) -> Side:
    """A configuration's sheets and outcomes, with `correct_count` right.

    Correct tasks are the low indices, so two sides built with different
    counts are nested: whatever the smaller one gets right, the larger one
    does too. That is what a real improvement looks like and it keeps the
    paired comparison meaningful.
    """
    sheets: list = []
    outcomes: list = []
    for index in range(tasks):
        named = "payment" if index < correct_count else "cart"
        outcome = InvestigationOutcome(
            bundle_id=_bundle_id(index),
            root_cause_service=named,
            ranked_candidates=(named, "payment"),
            cited_evidence=("ev_metric_aaaaaaaaaaaa",),
            confidence=confidence,
            usd=usd,
        )
        sheets.append(grade_trial(outcome, _label(index), {"ev_metric_aaaaaaaaaaaa"}, ALLOWED))
        outcomes.append(outcome)
    return Side.of(sheets, outcomes)


@pytest.fixture(scope="module")
def config() -> GateConfig:
    return load_gate()


class TestTheShippedConfiguration:
    def test_it_loads(self, config: GateConfig) -> None:
        assert config.metrics
        assert config.minimum_tasks >= 1

    def test_evidence_validity_has_no_tolerance(self, config: GateConfig) -> None:
        """A citation that does not resolve is a fabrication, not a regression."""
        assert config.metrics["evidence_validity"].margin == 0.0

    def test_abstention_is_tighter_than_accuracy(self, config: GateConfig) -> None:
        """Inventing a culprit is not something to trade away for cost."""
        assert config.metrics["abstention"].margin < config.metrics["root_cause_top1"].margin

    def test_every_rule_names_a_real_grader(self, config: GateConfig) -> None:
        """A rule naming a grader nothing produces would pass silently.

        `evaluate_gate` treats a grader with no shared tasks as passing, which
        is right for a genuinely inapplicable grader and wrong for a typo, so
        the names are checked against what the graders actually emit.
        """
        produced = set(_side(TASKS).sheets[0].by_grader())
        for name, rule in config.metrics.items():
            assert rule.grader in produced, f"{name} names unknown grader {rule.grader}"

    def test_a_malformed_file_is_refused(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        path = tmp_path / "gate.yaml"
        path.write_text("metrics: not-a-mapping\n", encoding="utf-8")
        with pytest.raises(GateError, match="not a valid gate config"):
            load_gate(path)

    def test_a_missing_file_is_refused(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(GateError, match="cannot read"):
            load_gate(tmp_path / "absent.yaml")


class TestDeterminism:
    def test_the_same_inputs_always_give_the_same_verdict(self, config: GateConfig) -> None:
        """The property SPEC.md Section 17 asks for by name."""
        candidate = _side(27)
        baseline = _side(26)
        first = evaluate_gate(candidate, baseline, config=config, resamples=RESAMPLES)
        second = evaluate_gate(candidate, baseline, config=config, resamples=RESAMPLES)
        assert first == second

    def test_it_is_deterministic_over_many_repeats(self, config: GateConfig) -> None:
        """Once could be luck. A gate people re-run until it passes is useless."""
        candidate = _side(24)
        baseline = _side(26)
        verdicts = {
            evaluate_gate(candidate, baseline, config=config, resamples=RESAMPLES).verdict
            for _ in range(8)
        }
        assert len(verdicts) == 1

    def test_the_interval_bounds_are_reproducible_not_just_the_verdict(
        self, config: GateConfig
    ) -> None:
        """A verdict that is stable while its numbers move is still not citable."""
        candidate = _side(27)
        baseline = _side(26)
        first = evaluate_gate(candidate, baseline, config=config, resamples=RESAMPLES)
        second = evaluate_gate(candidate, baseline, config=config, resamples=RESAMPLES)
        assert [c.difference.as_dict() for c in first.checks] == [
            c.difference.as_dict() for c in second.checks
        ]


class TestDirection:
    def test_an_identical_candidate_passes(self, config: GateConfig) -> None:
        side = _side(26)
        result = evaluate_gate(side, side, config=config, resamples=RESAMPLES)
        assert result.verdict is Verdict.PASS

    def test_a_clear_improvement_passes(self, config: GateConfig) -> None:
        result = evaluate_gate(_side(30), _side(20), config=config, resamples=RESAMPLES)
        assert result.verdict is Verdict.PASS

    def test_a_large_regression_fails_on_quality(self, config: GateConfig) -> None:
        """The case the gate exists for."""
        result = evaluate_gate(_side(15), _side(29), config=config, resamples=RESAMPLES)
        assert result.verdict is Verdict.FAIL_QUALITY
        assert "root_cause_top1" in result.summary

    def test_one_flipped_task_in_thirty_already_exceeds_the_margin(
        self, config: GateConfig
    ) -> None:
        """A finding about library size rather than about the gate.

        SPEC.md Section 9.4 sets a three point margin for top-1 accuracy. One
        task in thirty is 3.33 points, so on a split this small the finest
        distinction available is already coarser than the margin, and a single
        flipped task is a genuine regression by that definition. The margin
        does not become usable by loosening it; the library has to get bigger.
        """
        result = evaluate_gate(_side(29), _side(30), config=config, resamples=RESAMPLES)
        assert result.verdict is Verdict.FAIL_QUALITY

    def test_a_regression_inside_the_margin_but_unresolvable_is_inconclusive(
        self, config: GateConfig
    ) -> None:
        """Underpowered non-inferiority is inconclusive, not negative.

        A hundred tasks with one flipped moves the estimate by 0.01, inside the
        three point margin, while the bootstrap interval still reaches past it.
        No regression was observed and none can be ruled out, so neither PASS
        nor FAIL_QUALITY is true.
        """
        result = evaluate_gate(
            _side(99, tasks=100),
            _side(100, tasks=100),
            config=config,
            resamples=RESAMPLES,
        )
        assert result.verdict is Verdict.INCONCLUSIVE
        assert "cannot rule one out" in result.summary

    def test_a_measured_regression_is_a_quality_failure_not_inconclusive(
        self, config: GateConfig
    ) -> None:
        """The two must not collapse into each other.

        Here the point estimate itself is well past the margin, so a
        regression was observed rather than merely not excluded.
        """
        result = evaluate_gate(_side(18), _side(30), config=config, resamples=RESAMPLES)
        assert result.verdict is Verdict.FAIL_QUALITY

    def test_the_failing_check_is_recorded_with_its_numbers(self, config: GateConfig) -> None:
        result = evaluate_gate(_side(10), _side(30), config=config, resamples=RESAMPLES)
        failed = [check for check in result.checks if not check.passed]
        assert failed
        for check in failed:
            assert check.difference.estimate < 0
            assert "margin" in check.reason


class TestInconclusive:
    def test_too_few_tasks_is_inconclusive_rather_than_a_pass(self, config: GateConfig) -> None:
        """A gate that passes for lack of evidence produces a signed statement
        that nothing was checked, which is worse than no gate."""
        candidate = _side(2, tasks=2)
        baseline = _side(2, tasks=2)
        result = evaluate_gate(candidate, baseline, config=config, resamples=RESAMPLES)
        assert result.verdict is Verdict.INCONCLUSIVE
        assert "below the minimum" in result.summary


class TestCalibration:
    def test_a_calibration_regression_fails_on_calibration(self, config: GateConfig) -> None:
        """Both sides equally accurate, but the candidate is badly overconfident."""
        candidate = _side(15, confidence=0.99)
        baseline = _side(15, confidence=0.5)
        result = evaluate_gate(candidate, baseline, config=config, resamples=RESAMPLES)
        assert result.verdict is Verdict.FAIL_CALIBRATION

    def test_no_stated_confidence_skips_the_check(self, config: GateConfig) -> None:
        """A configuration making no confidence claim cannot regress on one."""
        candidate = _side(26, confidence=None)
        baseline = _side(26, confidence=None)
        result = evaluate_gate(candidate, baseline, config=config, resamples=RESAMPLES)
        assert result.verdict is Verdict.PASS
        assert not any(check.name == "expected_calibration_error" for check in result.checks)


class TestCost:
    def test_a_large_cost_increase_fails_on_cost(self, config: GateConfig) -> None:
        candidate = _side(26, usd=1.00)
        baseline = _side(26, usd=0.50)
        result = evaluate_gate(candidate, baseline, config=config, resamples=RESAMPLES)
        assert result.verdict is Verdict.FAIL_COST
        assert "median cost" in result.summary

    def test_an_increase_inside_the_ceiling_passes(self, config: GateConfig) -> None:
        candidate = _side(26, usd=0.55)
        baseline = _side(26, usd=0.50)
        result = evaluate_gate(candidate, baseline, config=config, resamples=RESAMPLES)
        assert result.verdict is Verdict.PASS

    def test_a_negligible_absolute_cost_is_ignored(self, config: GateConfig) -> None:
        """Doubling a hundredth of a cent is a 100% increase nobody cares about.

        Failing a release for it would train people to bypass the gate.
        """
        candidate = _side(26, usd=0.0001)
        baseline = _side(26, usd=0.00001)
        result = evaluate_gate(candidate, baseline, config=config, resamples=RESAMPLES)
        assert result.verdict is Verdict.PASS

    def test_quality_is_reported_before_cost(self, config: GateConfig) -> None:
        """A system that is both wrong and expensive has a quality problem.

        The order of the checks is which problem the reader is told to fix.
        """
        candidate = _side(10, usd=10.0)
        baseline = _side(30, usd=0.50)
        result = evaluate_gate(candidate, baseline, config=config, resamples=RESAMPLES)
        assert result.verdict is Verdict.FAIL_QUALITY
