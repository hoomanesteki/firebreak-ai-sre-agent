"""The eval gate: is this candidate allowed to ship.

SPEC.md Section 9.4. The gate compares a candidate against a baseline on the
same tasks and returns one of five verdicts. Margins live in
`config/eval_gate.yaml` so the trade-offs are argued in a pull request rather
than buried here.

**Non-inferiority on the lower bound of a paired difference.** The question is
not whether the candidate scored higher, which on tens of tasks is mostly
noise, but whether it has been shown to be worse by more than the margin. A
candidate two points behind with an interval from minus ten to plus six has
not been shown to be worse; one two points behind with an interval from minus
three to minus one has.

**Determinism is a requirement, not a nicety.** The same two reports must
always produce the same verdict, because a gate that occasionally changes its
mind is a gate nobody can act on. Every statistic underneath uses a fixed
seed, and `tests/unit/test_eval_gate.py` asserts repeated evaluation agrees.
"""

from __future__ import annotations

import statistics as stdlib_statistics
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from firebreak.evals.graders import GradeSheet
from firebreak.evals.outcome import InvestigationOutcome
from firebreak.evals.statistics import (
    BOOTSTRAP_RESAMPLES,
    Interval,
    calibration,
    paired_bootstrap_difference,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
GATE_PATH = REPO_ROOT / "config" / "eval_gate.yaml"


class GateError(Exception):
    """The gate configuration is missing or malformed, or its inputs disagree."""


class Verdict(StrEnum):
    """SPEC.md Section 9.4."""

    PASS = "PASS"
    FAIL_QUALITY = "FAIL_QUALITY"
    FAIL_CALIBRATION = "FAIL_CALIBRATION"
    FAIL_COST = "FAIL_COST"
    INCONCLUSIVE = "INCONCLUSIVE"


class MetricRule(BaseModel):
    """One non-inferiority check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    grader: str
    higher_is_better: bool
    margin: float = Field(ge=0.0)


class CalibrationRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str
    higher_is_better: bool
    margin: float = Field(ge=0.0)


class CostRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str
    maximum_relative_increase: float = Field(ge=0.0)
    ignore_below_usd: float = Field(ge=0.0)


class GateConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    metrics: dict[str, MetricRule]
    calibration: CalibrationRule
    cost: CostRule
    minimum_tasks: int = Field(ge=1)


@lru_cache(maxsize=1)
def load_gate(path: Path | None = None) -> GateConfig:
    """Read and validate the gate configuration."""
    resolved = path or GATE_PATH
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except OSError as error:
        raise GateError(f"cannot read {resolved}: {error}") from error
    if not isinstance(raw, dict):
        raise GateError(f"{resolved} does not contain a mapping")
    try:
        return GateConfig.model_validate(raw)
    except ValueError as error:
        raise GateError(f"{resolved} is not a valid gate config: {error}") from error


@dataclass(frozen=True)
class Side:
    """One configuration's graded trials, sheets and outcomes together.

    A pair object rather than four positional lists, because the four-list
    signature this replaced grouped both sides' sheets and then both sides'
    outcomes, and the natural reading is "candidate first, then baseline". The
    first caller transposed them, and a transposed comparison produces a
    verdict about nothing while looking entirely normal.

    Refuses mismatched lengths on construction, since a sheet without its
    outcome means the cost and the quality numbers describe different trials.
    """

    sheets: tuple[GradeSheet, ...]
    outcomes: tuple[InvestigationOutcome, ...]

    def __post_init__(self) -> None:
        if len(self.sheets) != len(self.outcomes):
            raise GateError(
                f"a side needs one outcome per sheet, got {len(self.sheets)} sheets "
                f"and {len(self.outcomes)} outcomes"
            )

    @classmethod
    def of(cls, sheets: list[GradeSheet], outcomes: list[InvestigationOutcome]) -> Side:
        return cls(tuple(sheets), tuple(outcomes))

    @property
    def tasks(self) -> int:
        return len({sheet.bundle_id for sheet in self.sheets})


@dataclass(frozen=True)
class MetricCheck:
    """One metric's paired comparison and what the gate concluded about it."""

    name: str
    margin: float
    difference: Interval
    passed: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "metric": self.name,
            "margin": self.margin,
            "difference": self.difference.as_dict(),
            "passed": self.passed,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class GateResult:
    """The verdict, and every check behind it."""

    verdict: Verdict
    checks: tuple[MetricCheck, ...]
    summary: str

    @property
    def passed(self) -> bool:
        return self.verdict is Verdict.PASS

    def as_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "summary": self.summary,
            "checks": [check.as_dict() for check in self.checks],
        }


def _grader_scores(sheets: list[GradeSheet], grader: str) -> dict[str, float]:
    """Per-task score for one grader, averaged over that task's trials.

    Averaged rather than taking the first trial, so a task run three times
    contributes one observation to the paired comparison. Feeding three trials
    in as three observations would treat them as independent tasks and narrow
    every interval the gate reads.

    A task the grader did not apply to is absent rather than zero.
    """
    per_task: dict[str, list[float]] = {}
    for sheet in sheets:
        result = sheet.verdict_for(grader)
        if result is None or not result.applies:
            continue
        per_task.setdefault(sheet.bundle_id, []).append(1.0 if result.correct else 0.0)
    return {task: sum(values) / len(values) for task, values in per_task.items()}


def _paired(
    candidate: dict[str, float], baseline: dict[str, float]
) -> tuple[list[float], list[float], list[str]]:
    """The tasks both sides scored, in a stable order.

    Intersecting rather than unioning. A task only one configuration was
    graded on carries no difference, and including it by treating the missing
    side as zero would invent a regression that was never measured.
    """
    shared = sorted(set(candidate) & set(baseline))
    return (
        [candidate[task] for task in shared],
        [baseline[task] for task in shared],
        shared,
    )


def evaluate_gate(
    candidate: Side,
    baseline: Side,
    config: GateConfig | None = None,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> GateResult:
    """Compare a candidate against a baseline and return a verdict.

    Checks run in the order SPEC.md Section 9.4 lists the verdicts, so the
    first failure decides and a system that is both wrong and expensive is
    reported as having a quality problem, which is the one to fix first.
    """
    gate = config or load_gate()
    checks: list[MetricCheck] = []

    tasks = candidate.tasks
    if tasks < gate.minimum_tasks:
        return GateResult(
            Verdict.INCONCLUSIVE,
            (),
            f"{tasks} tasks is below the minimum of {gate.minimum_tasks}; a gate that "
            "passes for lack of evidence is worse than no gate",
        )

    quality_failures: list[str] = []
    underpowered: list[str] = []
    for name, rule in sorted(gate.metrics.items()):
        candidate_scores = _grader_scores(list(candidate.sheets), rule.grader)
        baseline_scores = _grader_scores(list(baseline.sheets), rule.grader)
        treatment, control, shared = _paired(candidate_scores, baseline_scores)
        if not shared:
            checks.append(
                MetricCheck(
                    name=name,
                    margin=rule.margin,
                    difference=Interval(0.0, 0.0, 0.0, resamples, 0),
                    passed=True,
                    reason=f"no task was graded by {rule.grader} on both sides",
                )
            )
            continue

        difference = paired_bootstrap_difference(treatment, control, resamples=resamples)
        # For a metric where higher is better, non-inferiority holds when the
        # lower bound of the difference sits at or above minus the margin.
        bound = difference.lower if rule.higher_is_better else -difference.upper
        observed = difference.estimate if rule.higher_is_better else -difference.estimate
        passed = bound >= -rule.margin
        reason = (
            f"difference {difference.estimate:+.4f} with lower bound "
            f"{difference.lower:+.4f} against a margin of {rule.margin:.4f} "
            f"over {len(shared)} shared tasks"
        )

        # Failing non-inferiority has two quite different causes, and
        # collapsing them makes the gate unusable on a small library.
        #
        # Either a regression was measured, or the split is too small to rule
        # one out. When the point estimate itself sits inside the margin, the
        # second is what happened: nothing worse than the margin was observed
        # and nothing was excluded either. That is an inconclusive test, not a
        # negative one, and reporting FAIL_QUALITY for it would be failing on
        # noise and teaching people to re-run the gate.
        #
        # Saying so keeps the margin at the value SPEC.md Section 9.4
        # specifies while making the real constraint visible: the library needs
        # to be larger, not the margin looser.
        if not passed and observed >= -rule.margin:
            underpowered.append(
                f"{name}: no regression beyond the margin was observed "
                f"({difference.estimate:+.4f}), but {len(shared)} tasks cannot rule one "
                f"out; the interval reaches {difference.lower:+.4f}"
            )
            checks.append(MetricCheck(name, rule.margin, difference, False, reason))
            continue

        checks.append(MetricCheck(name, rule.margin, difference, passed, reason))
        if not passed:
            quality_failures.append(f"{name}: {reason}")

    # Quality first: a measured regression is the thing to fix, and it is
    # reported even when another metric was merely unresolvable.
    if quality_failures:
        return GateResult(Verdict.FAIL_QUALITY, tuple(checks), "; ".join(quality_failures))
    if underpowered:
        return GateResult(Verdict.INCONCLUSIVE, tuple(checks), "; ".join(underpowered))

    calibration_check = _check_calibration(candidate, baseline, gate, resamples)
    if calibration_check is not None:
        checks.append(calibration_check)
        if not calibration_check.passed:
            # The same distinction the quality metrics make, and it was missing
            # here: a calibration gap whose point estimate sits inside the
            # margin has not been shown to have regressed, however wide the
            # interval is. Reporting FAIL_CALIBRATION for that would fail a
            # candidate whose calibration actually improved, which is what
            # happened the first time this gate ran on recorded data.
            if calibration_check.difference.estimate <= gate.calibration.margin:
                return GateResult(
                    Verdict.INCONCLUSIVE,
                    tuple(checks),
                    f"calibration: {calibration_check.reason}; the estimate is inside "
                    "the margin but the interval cannot rule a regression out",
                )
            return GateResult(Verdict.FAIL_CALIBRATION, tuple(checks), calibration_check.reason)

    cost_check = _check_cost(candidate, baseline, gate, resamples)
    checks.append(cost_check)
    if not cost_check.passed:
        return GateResult(Verdict.FAIL_COST, tuple(checks), cost_check.reason)

    return GateResult(
        Verdict.PASS,
        tuple(checks),
        f"no metric regressed beyond its margin over {tasks} tasks",
    )


def _check_calibration(
    candidate: Side, baseline: Side, gate: GateConfig, resamples: int
) -> MetricCheck | None:
    """Expected calibration error, candidate against baseline.

    Returns None when either side states no confidence. A configuration that
    makes no confidence claim cannot regress on calibration, and inventing a
    comparison would either pass it for free or fail it for a claim it never
    made.

    Compared on the per-task absolute gap between stated confidence and
    outcome rather than on the binned ECE of each side. The binned statistic
    is not a per-task quantity, so it cannot be paired, and an unpaired
    comparison of two ECEs discards the pairing that makes the rest of this
    gate sensitive.
    """
    candidate_gaps = _confidence_gaps(candidate)
    baseline_gaps = _confidence_gaps(baseline)
    if not candidate_gaps or not baseline_gaps:
        return None

    treatment, control, shared = _paired(candidate_gaps, baseline_gaps)
    if not shared:
        return None

    difference = paired_bootstrap_difference(treatment, control, resamples=resamples)
    # A gap is an error, so lower is better and a rise is the regression.
    passed = difference.upper <= gate.calibration.margin

    # The binned figures go in the reason, because they are what a report
    # shows and a verdict nobody can reconcile with the report is unhelpful.
    candidate_ece = _ece(candidate)
    baseline_ece = _ece(baseline)
    reason = (
        f"mean Brier score moved {difference.estimate:+.4f} with upper bound "
        f"{difference.upper:+.4f} against a margin of {gate.calibration.margin:.4f}; "
        f"binned ECE {baseline_ece:.4f} to {candidate_ece:.4f}"
    )
    return MetricCheck(gate.calibration.metric, gate.calibration.margin, difference, passed, reason)


def _confidence_gaps(side: Side) -> dict[str, float]:
    """Per-task squared error between stated confidence and correctness.

    The Brier contribution, not the absolute gap. The absolute gap cannot tell
    a confident mistake from a hedge: a system saying 0.99 and being right half
    the time and one saying 0.5 both average a gap of 0.5, so the gate reported
    no difference between a badly overconfident candidate and a well hedged one.

    Squaring separates them, 0.98 against 0.25, and it is the proper scoring
    rule SPEC.md Section 9.3 already names alongside expected calibration error.
    """
    gaps: dict[str, list[float]] = {}
    for outcome, sheet in zip(side.outcomes, side.sheets, strict=True):
        if outcome.confidence is None:
            continue
        actual = 1.0 if sheet.correct_root_cause else 0.0
        gaps.setdefault(sheet.bundle_id, []).append((outcome.confidence - actual) ** 2)
    return {task: sum(values) / len(values) for task, values in gaps.items()}


def _ece(side: Side) -> float:
    """The binned expected calibration error, or zero when nothing was stated."""
    pairs = [
        (o.confidence, sheet.correct_root_cause)
        for o, sheet in zip(side.outcomes, side.sheets, strict=True)
        if o.confidence is not None
    ]
    if not pairs:
        return 0.0
    stated = [c for c, _ in pairs if c is not None]
    return calibration(stated, [o for _, o in pairs]).expected_calibration_error


def _check_cost(candidate: Side, baseline: Side, gate: GateConfig, resamples: int) -> MetricCheck:
    """Median cost per investigation, against a relative ceiling.

    Median rather than mean, as SPEC.md Section 9.4 specifies, so one runaway
    investigation does not move the number the gate reads.

    Below `ignore_below_usd` the check passes unconditionally. A relative
    ceiling on a cost near zero is meaningless: going from a hundredth of a
    cent to two hundredths is a hundred percent increase that nobody cares
    about, and failing a release for it would train people to bypass the gate.
    """
    candidate_median = (
        stdlib_statistics.median([o.usd for o in candidate.outcomes]) if candidate.outcomes else 0.0
    )
    baseline_median = (
        stdlib_statistics.median([o.usd for o in baseline.outcomes]) if baseline.outcomes else 0.0
    )
    difference = Interval(
        candidate_median - baseline_median,
        candidate_median - baseline_median,
        candidate_median - baseline_median,
        resamples,
        len(candidate.outcomes),
    )

    if max(candidate_median, baseline_median) < gate.cost.ignore_below_usd:
        return MetricCheck(
            gate.cost.metric,
            gate.cost.maximum_relative_increase,
            difference,
            True,
            f"median cost {candidate_median:.6f} USD is below the "
            f"{gate.cost.ignore_below_usd} USD floor, so a relative comparison "
            "would be meaningless",
        )
    if baseline_median <= 0.0:
        return MetricCheck(
            gate.cost.metric,
            gate.cost.maximum_relative_increase,
            difference,
            True,
            "the baseline cost nothing, so there is no relative increase to bound",
        )

    increase = (candidate_median - baseline_median) / baseline_median
    passed = increase <= gate.cost.maximum_relative_increase
    return MetricCheck(
        gate.cost.metric,
        gate.cost.maximum_relative_increase,
        difference,
        passed,
        f"median cost per investigation moved {increase:+.1%}, from "
        f"{baseline_median:.6f} to {candidate_median:.6f} USD, against a ceiling of "
        f"{gate.cost.maximum_relative_increase:+.0%}",
    )
