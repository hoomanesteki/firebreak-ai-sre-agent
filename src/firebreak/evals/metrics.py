"""Turn grade sheets into the metrics SPEC.md Section 9.3 asks for.

One module between the graders, which score a single trial, and the report
writer, which renders. Keeping the aggregation here rather than in the writer
means a number can be computed once and rendered twice, into JSON and into
Markdown, with no chance of the two disagreeing.

**Every rate carries its denominator.** An accuracy of 1.0 over two tasks and
an accuracy of 1.0 over thirty are different claims, and a report that shows
only the rate invites the reader to assume the second. Coverage is reported
beside every rate that can be not applicable, for the same reason.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass

from firebreak.evals.graders import GradeSheet, onset_errors
from firebreak.evals.outcome import InvestigationOutcome
from firebreak.evals.statistics import (
    BOOTSTRAP_RESAMPLES,
    Calibration,
    Interval,
    Reliability,
    SelectiveAccuracy,
    bootstrap_mean,
    calibration,
    reliability,
    selective_accuracy,
)


@dataclass(frozen=True)
class GraderSummary:
    """One grader's rate across a set of trials, with its denominator."""

    grader: str
    applicable: int
    correct: int
    total_trials: int
    interval: Interval | None = None

    @property
    def accuracy(self) -> float | None:
        """None rather than zero when the grader applied to nothing.

        Zero would read as "always wrong" where the truth is "never asked",
        and the difference decides whether a reader goes looking for a bug.
        """
        if self.applicable == 0:
            return None
        return self.correct / self.applicable

    @property
    def coverage(self) -> float:
        """Share of trials this grader could say anything about."""
        return self.applicable / self.total_trials if self.total_trials else 0.0

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "grader": self.grader,
            "applicable": self.applicable,
            "correct": self.correct,
            "trials": self.total_trials,
            "coverage": round(self.coverage, 6),
            "accuracy": round(self.accuracy, 6) if self.accuracy is not None else None,
        }
        if self.interval is not None:
            payload["interval"] = self.interval.as_dict()
        return payload


@dataclass(frozen=True)
class CostSummary:
    """What the investigations cost, per SPEC.md Section 9.3 metric 6."""

    trials: int
    mean_tool_calls: float
    mean_usd: float
    median_usd: float
    total_tokens_in: int
    total_tokens_out: int
    p50_seconds: float
    p95_seconds: float

    def as_dict(self) -> dict[str, object]:
        return {
            "trials": self.trials,
            "mean_tool_calls": round(self.mean_tool_calls, 4),
            "mean_usd": round(self.mean_usd, 6),
            # Median as well as mean, because the eval gate's cost check is on
            # the median (SPEC.md Section 9.4) and one runaway investigation
            # should not move the number the gate reads.
            "median_usd": round(self.median_usd, 6),
            "total_tokens_in": self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
            "p50_seconds": round(self.p50_seconds, 4),
            "p95_seconds": round(self.p95_seconds, 4),
        }


def summarise_cost(outcomes: list[InvestigationOutcome]) -> CostSummary:
    """Cost and speed across trials."""
    if not outcomes:
        return CostSummary(0, 0.0, 0.0, 0.0, 0, 0, 0.0, 0.0)
    usd = sorted(o.usd for o in outcomes)
    seconds = sorted(o.wall_clock_seconds for o in outcomes)
    return CostSummary(
        trials=len(outcomes),
        mean_tool_calls=sum(o.tool_calls for o in outcomes) / len(outcomes),
        mean_usd=sum(usd) / len(usd),
        median_usd=statistics.median(usd),
        total_tokens_in=sum(o.tokens_in for o in outcomes),
        total_tokens_out=sum(o.tokens_out for o in outcomes),
        p50_seconds=_quantile(seconds, 0.50),
        p95_seconds=_quantile(seconds, 0.95),
    )


def _quantile(sorted_values: list[float], fraction: float) -> float:
    """Nearest rank quantile, which is what a latency percentile means.

    Deliberately not the interpolating definition used in
    `statistics.percentile`. A p95 latency should be a duration some request
    actually took, not a weighted average of two requests, and the two
    definitions answer different questions.
    """
    if not sorted_values:
        return 0.0
    index = min(int(fraction * len(sorted_values)), len(sorted_values) - 1)
    return sorted_values[index]


def per_task_scores(sheets: list[GradeSheet], grader: str) -> list[float]:
    """One score per task for one grader, averaged over that task's trials.

    **This aggregation is the whole point and getting it wrong is silent.**
    SPEC.md Section 9.4 puts the bootstrap over tasks, and resampling trials
    instead treats three trials of one task as three independent observations.
    The interval then narrows purely because more trials were run, which is
    exactly backwards for a deterministic configuration: B0 produces identical
    trials, so three of them carry no more information than one, and an
    interval that tightened would be reporting confidence that came from
    nowhere.

    Ordered by bundle id so the bootstrap's resampling is reproducible.
    """
    per_task: dict[str, list[float]] = {}
    for sheet in sheets:
        result = sheet.verdict_for(grader)
        if result is None or not result.applies:
            continue
        per_task.setdefault(sheet.bundle_id, []).append(1.0 if result.correct else 0.0)
    return [sum(per_task[task]) / len(per_task[task]) for task in sorted(per_task)]


def headline_scores(sheets: list[GradeSheet]) -> list[float]:
    """One headline score per task, averaged over that task's trials."""
    per_task: dict[str, list[float]] = {}
    for sheet in sheets:
        per_task.setdefault(sheet.bundle_id, []).append(1.0 if sheet.correct_root_cause else 0.0)
    return [sum(per_task[task]) / len(per_task[task]) for task in sorted(per_task)]


def summarise_graders(
    sheets: list[GradeSheet],
    with_intervals: bool = True,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> dict[str, GraderSummary]:
    """Each grader's rate, with a bootstrap interval over tasks.

    The counts are per trial, because "18 of 20 trials" is what a reader wants
    to see. The interval is over tasks, because that is the unit the sampling
    actually varies over.
    """
    per_grader: dict[str, list[bool]] = defaultdict(list)
    total = len(sheets)
    for sheet in sheets:
        for result in sheet.results:
            if result.applies:
                per_grader[result.grader].append(result.correct)

    summaries: dict[str, GraderSummary] = {}
    for grader in sorted(per_grader):
        outcomes = per_grader[grader]
        task_scores = per_task_scores(sheets, grader)
        interval = None
        if with_intervals and task_scores:
            interval = bootstrap_mean(task_scores, resamples=resamples)
        summaries[grader] = GraderSummary(
            grader=grader,
            applicable=len(outcomes),
            correct=sum(1 for o in outcomes if o),
            total_trials=total,
            interval=interval,
        )
    return summaries


@dataclass(frozen=True)
class SplitMetrics:
    """Everything measured for one configuration on one split."""

    trials: int
    tasks: int
    graders: dict[str, GraderSummary]
    headline: Interval | None
    calibration: Calibration | None
    selective: SelectiveAccuracy | None
    reliability: Reliability | None
    cost: CostSummary
    median_onset_error_seconds: float | None

    def as_dict(self) -> dict[str, object]:
        return {
            "trials": self.trials,
            "tasks": self.tasks,
            "headline_accuracy": self.headline.as_dict() if self.headline else None,
            "graders": {name: s.as_dict() for name, s in self.graders.items()},
            "calibration": self.calibration.as_dict() if self.calibration else None,
            "selective_accuracy": self.selective.as_dict() if self.selective else None,
            "reliability": self.reliability.as_dict() if self.reliability else None,
            "cost": self.cost.as_dict(),
            "median_onset_error_seconds": (
                round(self.median_onset_error_seconds, 3)
                if self.median_onset_error_seconds is not None
                else None
            ),
        }


def compute_metrics(
    sheets: list[GradeSheet],
    outcomes: list[InvestigationOutcome],
    trials_per_task: int = 1,
    reliability_k: int = 3,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> SplitMetrics:
    """Everything SPEC.md Section 9.3 asks for, from one split's trials.

    `sheets` and `outcomes` are parallel: one of each per trial, in the same
    order. They are separate arguments because a grade sheet holds what the
    graders concluded and an outcome holds what the system spent, and mixing
    the two would put cost data where a grader could read it.

    Reliability needs several trials of each task and is omitted rather than
    faked when there is only one. A pass^3 computed from single trials would
    be identical to pass@1 and would read as though reliability had been
    measured.
    """
    if len(sheets) != len(outcomes):
        raise ValueError(
            f"sheets and outcomes must be parallel, got {len(sheets)} and {len(outcomes)}"
        )

    by_task: dict[str, list[bool]] = defaultdict(list)
    for sheet in sheets:
        by_task[sheet.bundle_id].append(sheet.correct_root_cause)

    # Over tasks, not trials. See `per_task_scores`.
    task_scores = headline_scores(sheets)
    headline = bootstrap_mean(task_scores, resamples=resamples) if task_scores else None

    # Calibration needs a stated confidence, and a configuration that states
    # none is reported as having no calibration rather than as perfectly
    # calibrated. Only trials that stated one are included, so a system that
    # is confident sometimes is measured on those occasions.
    confident = [
        (o.confidence, sheet.correct_root_cause)
        for o, sheet in zip(outcomes, sheets, strict=True)
        if o.confidence is not None
    ]
    calibration_result = (
        calibration([c for c, _ in confident], [o for _, o in confident]) if confident else None
    )

    # The risk-coverage curve needs a confidence on every trial. A
    # configuration that states one only sometimes gets the single-point
    # version rather than a curve built from a partial sweep, which would
    # silently drop the trials that stated nothing.
    stated = [o.confidence for o in outcomes]
    all_confidences = (
        [c for c in stated if c is not None] if all(c is not None for c in stated) else None
    )
    selective = (
        selective_accuracy(
            [sheet.correct_root_cause for sheet in sheets],
            [not o.abstained for o in outcomes],
            all_confidences,
        )
        if sheets
        else None
    )

    reliability_result = None
    if trials_per_task >= reliability_k and by_task:
        reliability_result = reliability(dict(by_task), k=reliability_k)

    errors = onset_errors(sheets)
    return SplitMetrics(
        trials=len(sheets),
        tasks=len(by_task),
        graders=summarise_graders(sheets, resamples=resamples),
        headline=headline,
        calibration=calibration_result,
        selective=selective,
        reliability=reliability_result,
        cost=summarise_cost(outcomes),
        median_onset_error_seconds=statistics.median(errors) if errors else None,
    )


def group_by(
    sheets: list[GradeSheet],
    outcomes: list[InvestigationOutcome],
    key_of: dict[str, str],
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> dict[str, SplitMetrics]:
    """Metrics per group, for the per-family and per-split breakdowns.

    `key_of` maps a bundle id to its group. A bundle with no entry is skipped
    rather than filed under an "unknown" bucket, because an unknown bucket in
    a results table is read as a finding about the system rather than as a
    gap in the mapping.
    """
    grouped: dict[str, tuple[list[GradeSheet], list[InvestigationOutcome]]] = defaultdict(
        lambda: ([], [])
    )
    for sheet, outcome in zip(sheets, outcomes, strict=True):
        group = key_of.get(sheet.bundle_id)
        if group is None:
            continue
        grouped[group][0].append(sheet)
        grouped[group][1].append(outcome)

    return {
        group: compute_metrics(group_sheets, group_outcomes, resamples=resamples)
        for group, (group_sheets, group_outcomes) in sorted(grouped.items())
    }
