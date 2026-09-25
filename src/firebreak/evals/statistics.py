"""The statistics every reported number goes through.

SPEC.md Section 9.4 asks for bootstrap intervals on every headline metric, a
paired bootstrap for comparisons, calibration error, and pass^k reliability.
This module is all of it, in pure Python.

**Why pure Python rather than numpy.** Two reasons, and the second is the
real one.

The first is that these functions are small and the data is small: a split is
tens of tasks, not millions of rows, and 10,000 resamples of 31 items costs
milliseconds either way.

The second is that a reported number has to be reproducible, and numpy's
reductions do not promise a fixed summation order across platforms or build
configurations. That is a tolerable risk for most work and not for this: the
whole claim of the project is that a figure in a report can be re-derived.
Sorting and summing in an order this module controls gives bit-identical
results everywhere.

**Reference implementations are still used, in the tests.** scipy and
scikit-learn are dev dependencies for exactly that, because "I wrote a Brier
score" is worth nothing next to "my Brier score agrees with sklearn's to
twelve decimal places on a hundred random vectors". SPEC.md Section 17 makes
that an acceptance criterion for this phase.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from fractions import Fraction

# SPEC.md Section 9.4: 10,000 resamples with a fixed seed. Fixed rather than
# random because an interval that moves between runs of the same data is not a
# number anybody can cite.
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20260924

# A 95% interval, taken as percentiles of the resample distribution.
CONFIDENCE_LEVEL = 0.95

# SPEC.md Section 9.3: expected calibration error over 10 bins.
CALIBRATION_BINS = 10


class StatisticsError(Exception):
    """A statistic was asked for on data that cannot support it."""


@dataclass(frozen=True)
class Interval:
    """A point estimate and its bootstrap interval."""

    estimate: float
    lower: float
    upper: float
    resamples: int
    observations: int

    @property
    def width(self) -> float:
        return self.upper - self.lower

    def excludes_zero(self) -> bool:
        """True when the interval does not contain zero.

        For a paired difference this is the question being asked: whether the
        two configurations differ at all, before asking by how much.
        """
        return self.lower > 0.0 or self.upper < 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "estimate": round(self.estimate, 6),
            "lower": round(self.lower, 6),
            "upper": round(self.upper, 6),
            "resamples": self.resamples,
            "observations": self.observations,
        }


def mean(values: list[float]) -> float:
    """The arithmetic mean, summed in the order given.

    Not `statistics.mean`, which is slower and, more to the point, not what
    the bootstrap below calls tens of thousands of times.
    """
    if not values:
        raise StatisticsError("the mean of no observations is undefined")
    return math.fsum(values) / len(values)


def percentile(sorted_values: list[float], fraction: float) -> float:
    """Linear interpolated percentile of an already sorted list.

    The interpolating definition, which is numpy's default and scipy's, so
    the tests can compare against them directly. A nearest rank definition
    would disagree by up to one observation's worth on small samples, and a
    split here has tens of observations rather than thousands.
    """
    if not sorted_values:
        raise StatisticsError("the percentile of no observations is undefined")
    if not 0.0 <= fraction <= 1.0:
        raise StatisticsError(f"fraction must be in [0, 1], got {fraction}")
    if len(sorted_values) == 1:
        return sorted_values[0]

    position = fraction * (len(sorted_values) - 1)
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return sorted_values[lower_index]
    weight = position - lower_index
    return sorted_values[lower_index] * (1.0 - weight) + sorted_values[upper_index] * weight


def bootstrap_mean(
    values: list[float],
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    confidence: float = CONFIDENCE_LEVEL,
) -> Interval:
    """A percentile bootstrap interval for the mean of `values`.

    Resampling tasks with replacement, which is the unit SPEC.md Section 9.4
    names. Resampling trials instead would treat three trials of one task as
    three independent observations and report an interval far narrower than
    the evidence supports.

    A single observation gets a degenerate interval rather than an error. One
    task is a real situation while developing, and refusing to report it would
    push a caller into special casing that is easy to get wrong.
    """
    if not values:
        raise StatisticsError("cannot bootstrap an empty sample")
    if resamples < 1:
        raise StatisticsError(f"resamples must be at least 1, got {resamples}")

    point = mean(values)
    if len(values) == 1:
        return Interval(point, point, point, resamples, 1)

    rng = random.Random(seed)
    size = len(values)
    means: list[float] = []
    for _ in range(resamples):
        # `rng.choices` rather than a comprehension of `rng.choice`, so the
        # sequence of random draws is one documented call per resample and
        # stays stable if this loop is ever rewritten.
        sample = rng.choices(values, k=size)
        means.append(math.fsum(sample) / size)
    means.sort()

    tail = (1.0 - confidence) / 2.0
    return Interval(
        estimate=point,
        lower=percentile(means, tail),
        upper=percentile(means, 1.0 - tail),
        resamples=resamples,
        observations=size,
    )


def paired_bootstrap_difference(
    treatment: list[float],
    control: list[float],
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    confidence: float = CONFIDENCE_LEVEL,
) -> Interval:
    """A bootstrap interval for the mean per-task difference.

    Paired, so the same resampled task indices are used for both
    configurations. That is the whole point: the two systems ran on the same
    tasks, and some tasks are simply harder, so comparing two independent
    intervals throws away the pairing and reports a difference far less
    certain than the data actually is.

    Positive means the treatment scored higher.
    """
    if len(treatment) != len(control):
        raise StatisticsError(
            f"paired samples must be the same length, got {len(treatment)} and {len(control)}"
        )
    if not treatment:
        raise StatisticsError("cannot bootstrap an empty sample")

    differences = [t - c for t, c in zip(treatment, control, strict=True)]
    return bootstrap_mean(differences, resamples=resamples, seed=seed, confidence=confidence)


def brier_score(confidences: list[float], outcomes: list[bool]) -> float:
    """Mean squared error between stated confidence and what happened.

    Lower is better, and zero is perfect. Unlike accuracy it punishes a
    confident mistake more than a hesitant one, which is the property worth
    having: a report claiming 95% certainty and being wrong is a worse
    failure than one claiming 55% and being wrong, and accuracy cannot tell
    them apart.
    """
    if len(confidences) != len(outcomes):
        raise StatisticsError(
            f"confidences and outcomes must match, got {len(confidences)} and {len(outcomes)}"
        )
    if not confidences:
        raise StatisticsError("the Brier score of no predictions is undefined")
    for value in confidences:
        if not 0.0 <= value <= 1.0:
            raise StatisticsError(f"a confidence must be in [0, 1], got {value}")
    return math.fsum(
        (c - (1.0 if o else 0.0)) ** 2 for c, o in zip(confidences, outcomes, strict=True)
    ) / len(confidences)


@dataclass(frozen=True)
class CalibrationBin:
    """One bin of a reliability diagram."""

    lower: float
    upper: float
    count: int
    mean_confidence: float
    accuracy: float

    @property
    def gap(self) -> float:
        """How far this bin's confidence was from its outcome."""
        return self.mean_confidence - self.accuracy

    def as_dict(self) -> dict[str, float | int]:
        return {
            "lower": round(self.lower, 6),
            "upper": round(self.upper, 6),
            "count": self.count,
            "mean_confidence": round(self.mean_confidence, 6),
            "accuracy": round(self.accuracy, 6),
            "gap": round(self.gap, 6),
        }


@dataclass(frozen=True)
class Calibration:
    """Expected calibration error and the diagram behind it."""

    expected_calibration_error: float
    maximum_calibration_error: float
    brier: float
    bins: tuple[CalibrationBin, ...]
    predictions: int

    def as_dict(self) -> dict[str, object]:
        return {
            "expected_calibration_error": round(self.expected_calibration_error, 6),
            "maximum_calibration_error": round(self.maximum_calibration_error, 6),
            "brier_score": round(self.brier, 6),
            "predictions": self.predictions,
            # SPEC.md Section 9.3 asks for a reliability diagram in every
            # report. The bins are the diagram: a renderer needs no more, and
            # a number nobody can plot is not a diagram.
            "reliability_diagram": [b.as_dict() for b in self.bins],
        }


def calibration(
    confidences: list[float], outcomes: list[bool], bins: int = CALIBRATION_BINS
) -> Calibration:
    """Expected calibration error over equal width bins, with the diagram.

    ECE is the average gap between stated confidence and observed accuracy,
    weighted by how many predictions fall in each bin. A system that says 70%
    and is right 70% of the time scores zero however wrong its individual
    answers are, which is the point: this measures honesty rather than skill.

    Equal width bins rather than equal count, matching the definition in the
    calibration literature and in SPEC.md Section 9.3. The trade is that a
    system whose confidences cluster leaves most bins empty, so
    `maximum_calibration_error` is reported alongside: it is the worst single
    bin, and it catches a large error hiding in a bin holding few predictions
    that the weighted average washes out.

    Bin edges are half open, `[lower, upper)`, except the last which includes
    1.0. Without that a prediction of exactly 1.0 falls outside every bin, and
    a perfectly confident prediction is precisely the one worth scoring.
    """
    if len(confidences) != len(outcomes):
        raise StatisticsError(
            f"confidences and outcomes must match, got {len(confidences)} and {len(outcomes)}"
        )
    if not confidences:
        raise StatisticsError("the calibration of no predictions is undefined")
    if bins < 1:
        raise StatisticsError(f"bins must be at least 1, got {bins}")
    for value in confidences:
        if not 0.0 <= value <= 1.0:
            raise StatisticsError(f"a confidence must be in [0, 1], got {value}")

    width = 1.0 / bins
    grouped: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for confidence, outcome in zip(confidences, outcomes, strict=True):
        index = min(int(confidence / width), bins - 1)
        grouped[index].append((confidence, outcome))

    total = len(confidences)
    built: list[CalibrationBin] = []
    weighted_gap = 0.0
    worst_gap = 0.0
    for index, members in enumerate(grouped):
        lower = index * width
        upper = (index + 1) * width
        if not members:
            # Kept in the output with a count of zero, because an empty bin is
            # information: it says the system never expressed that much
            # confidence, and a diagram missing its empty bins misleads.
            built.append(CalibrationBin(lower, upper, 0, 0.0, 0.0))
            continue
        bin_confidence = math.fsum(c for c, _ in members) / len(members)
        bin_accuracy = sum(1 for _, o in members if o) / len(members)
        gap = abs(bin_confidence - bin_accuracy)
        weighted_gap += (len(members) / total) * gap
        worst_gap = max(worst_gap, gap)
        built.append(CalibrationBin(lower, upper, len(members), bin_confidence, bin_accuracy))

    return Calibration(
        expected_calibration_error=weighted_gap,
        maximum_calibration_error=worst_gap,
        brier=brier_score(confidences, outcomes),
        bins=tuple(built),
        predictions=total,
    )


@dataclass(frozen=True)
class SelectivePoint:
    """One point on a risk-coverage curve."""

    threshold: float
    coverage: float
    accuracy: float

    @property
    def risk(self) -> float:
        return 1.0 - self.accuracy

    def as_dict(self) -> dict[str, float]:
        return {
            "threshold": round(self.threshold, 6),
            "coverage": round(self.coverage, 6),
            "accuracy": round(self.accuracy, 6),
            "risk": round(self.risk, 6),
        }


@dataclass(frozen=True)
class SelectiveAccuracy:
    """Accuracy on what was answered, and how much was answered."""

    coverage: float
    selective_accuracy: float
    overall_accuracy: float
    answered: int
    total: int
    curve: tuple[SelectivePoint, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "coverage": round(self.coverage, 6),
            "selective_accuracy": round(self.selective_accuracy, 6),
            "overall_accuracy": round(self.overall_accuracy, 6),
            "answered": self.answered,
            "total": self.total,
            "risk_coverage_curve": [p.as_dict() for p in self.curve],
        }


def selective_accuracy(
    outcomes: list[bool], answered: list[bool], confidences: list[float] | None = None
) -> SelectiveAccuracy:
    """Accuracy over the reports that did not abstain, plus the curve.

    SPEC.md Section 9.3 metric 3. The pair matters more than either number:
    a system can reach any selective accuracy it likes by abstaining more, so
    quoting it without coverage is a way of reporting a better result than was
    achieved. Overall accuracy, which counts an abstention as not correct, is
    reported alongside for the same reason.

    The risk-coverage curve sweeps a confidence threshold, answering "if this
    system only spoke when it was at least this sure, how often would it be
    right and how often would it speak". Without confidences there is one
    point, which is the system's own abstention decision.
    """
    if len(outcomes) != len(answered):
        raise StatisticsError(
            f"outcomes and answered must match, got {len(outcomes)} and {len(answered)}"
        )
    if not outcomes:
        raise StatisticsError("selective accuracy over no tasks is undefined")

    answered_outcomes = [o for o, a in zip(outcomes, answered, strict=True) if a]
    total = len(outcomes)
    coverage = len(answered_outcomes) / total
    selective = (
        sum(1 for o in answered_outcomes if o) / len(answered_outcomes)
        if answered_outcomes
        else 0.0
    )
    overall = sum(1 for o in outcomes if o) / total

    curve: list[SelectivePoint] = []
    if confidences is not None:
        if len(confidences) != total:
            raise StatisticsError(
                f"confidences must match outcomes, got {len(confidences)} and {total}"
            )
        # Sweeping the observed confidences rather than a fixed grid, so every
        # point on the curve is one the system actually reached. A grid would
        # invent thresholds between two adjacent values where nothing changes.
        for threshold in sorted({0.0, *(round(c, 6) for c in confidences)}):
            kept = [
                o
                for o, c, a in zip(outcomes, confidences, answered, strict=True)
                if a and c >= threshold
            ]
            curve.append(
                SelectivePoint(
                    threshold=threshold,
                    coverage=len(kept) / total,
                    accuracy=sum(1 for o in kept if o) / len(kept) if kept else 0.0,
                )
            )
    else:
        curve.append(SelectivePoint(0.0, coverage, selective))

    return SelectiveAccuracy(
        coverage=coverage,
        selective_accuracy=selective,
        overall_accuracy=overall,
        answered=len(answered_outcomes),
        total=total,
        curve=tuple(curve),
    )


def pass_at_k(correct: int, trials: int, k: int) -> float:
    """Probability that at least one of k trials drawn from `trials` passes.

    The unbiased estimator, `1 - C(n - c, k) / C(n, k)`, rather than the naive
    share of passing trials. With n trials observed and c of them correct, the
    question "how often would k attempts contain a success" is a sampling
    question, and the combinatorial form answers it exactly.

    Computed as an exact fraction and converted to float once at the end.
    Evaluating `1 - 4/5` in binary floating point gives 0.19999999999999996
    where `1/5` gives 0.2, so the two estimators disagreed at k=1 where they
    are mathematically identical. The numbers here are small enough that exact
    arithmetic costs nothing, and a metric that is off in the last few bits is
    a metric that cannot be compared between two runs by equality.
    """
    _check_trials(correct, trials, k)
    failures = trials - correct
    if failures < k:
        return 1.0
    return float(1 - Fraction(math.comb(failures, k), math.comb(trials, k)))


def pass_hat_k(correct: int, trials: int, k: int) -> float:
    """Probability that all k trials drawn from `trials` pass.

    SPEC.md Section 9.3 metric 4 calls for pass^3, and this is the reliability
    measure rather than the capability one. An agent that solves a task one
    time in three has demonstrated it can; an on-call engineer needs to know
    it will. The estimator is `C(c, k) / C(n, k)`.

    This is why determinism has been enforced so carefully everywhere else in
    this codebase: a ranking that reshuffled between identical runs would
    depress pass^3 while measuring nothing about the agent.
    """
    _check_trials(correct, trials, k)
    if correct < k:
        return 0.0
    return float(Fraction(math.comb(correct, k), math.comb(trials, k)))


def _check_trials(correct: int, trials: int, k: int) -> None:
    if trials < 1:
        raise StatisticsError(f"trials must be at least 1, got {trials}")
    if not 0 <= correct <= trials:
        raise StatisticsError(f"correct must be in [0, {trials}], got {correct}")
    if not 1 <= k <= trials:
        raise StatisticsError(f"k must be in [1, {trials}], got {k}")


@dataclass(frozen=True)
class Reliability:
    """pass@1 and pass^k across a set of tasks."""

    pass_at_1: float
    pass_hat_k: float
    k: int
    tasks: int
    trials_per_task: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            "pass_at_1": round(self.pass_at_1, 6),
            f"pass_hat_{self.k}": round(self.pass_hat_k, 6),
            "tasks": self.tasks,
            "trials_per_task": self.trials_per_task,
        }


def reliability(results_by_task: dict[str, list[bool]], k: int = 3) -> Reliability:
    """Average pass@1 and pass^k over tasks.

    Averaged per task rather than pooled over all trials, so a task that
    happened to be run more often does not weigh more heavily than one run the
    minimum number of times.
    """
    if not results_by_task:
        raise StatisticsError("reliability over no tasks is undefined")

    counts = {len(trials) for trials in results_by_task.values()}
    if len(counts) != 1:
        raise StatisticsError(
            f"every task needs the same number of trials for pass^{k}, saw {sorted(counts)}"
        )
    trials_per_task = counts.pop()
    if trials_per_task < k:
        raise StatisticsError(f"pass^{k} needs at least {k} trials per task, got {trials_per_task}")

    at_1 = []
    hat_k = []
    for task in sorted(results_by_task):
        outcomes = results_by_task[task]
        correct = sum(1 for outcome in outcomes if outcome)
        at_1.append(pass_at_k(correct, trials_per_task, 1))
        hat_k.append(pass_hat_k(correct, trials_per_task, k))

    return Reliability(
        pass_at_1=mean(at_1),
        pass_hat_k=mean(hat_k),
        k=k,
        tasks=len(results_by_task),
        trials_per_task=trials_per_task,
    )
