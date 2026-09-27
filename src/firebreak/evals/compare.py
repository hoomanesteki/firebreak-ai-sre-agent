"""Compare two configurations on the same tasks, with a paired interval.

SPEC.md Section 9.4 and Section 17 Phase 7. Two independent intervals are the
wrong tool: the configurations ran on the same incidents, some incidents are
simply harder than others, and comparing independent intervals throws the
pairing away and reports a difference far less certain than the data is.

**Paired by bundle id, never by position.** Two runs can cover different tasks,
because a split fills up as it is recorded. Pairing positionally would compare
one configuration's third incident to the other's third incident, which are not
the same incident, and nothing in the output would say so. Only the tasks both
runs covered are compared, and the count is reported.

**A grader that did not apply is not a zero.** B0 declines to name a fault
class, so scoring it zero there would punish it for not guessing. Such a task is
excluded from that grader's comparison and counted separately.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from firebreak.evals.statistics import (
    BOOTSTRAP_RESAMPLES,
    Interval,
    paired_bootstrap_difference,
)


class ComparisonError(Exception):
    """Two reports cannot be compared."""


@dataclass(frozen=True)
class GraderComparison:
    """One grader, for two configurations, on the tasks both covered."""

    grader: str
    treatment_mean: float
    control_mean: float
    difference: Interval
    paired_tasks: int
    # Tasks one run scored and the other did not apply to. Reported rather than
    # hidden, because a difference computed on four of eight tasks is a different
    # claim from one computed on all eight.
    unpaired_tasks: int

    @property
    def separates(self) -> bool:
        """Whether the data distinguishes the two configurations at all.

        The only honest reading of "one is better": an interval spanning zero
        means the data does not distinguish them, however far apart the two means
        look. `Interval.excludes_zero` is the one place that rule lives.
        """
        return self.difference.excludes_zero()

    def as_dict(self) -> dict[str, object]:
        return {
            "grader": self.grader,
            "treatment_mean": self.treatment_mean,
            "control_mean": self.control_mean,
            "difference": self.difference.as_dict(),
            "paired_tasks": self.paired_tasks,
            "unpaired_tasks": self.unpaired_tasks,
            "separates": self.separates,
        }


@dataclass(frozen=True)
class Comparison:
    """Everything one pairwise comparison concluded."""

    treatment: str
    control: str
    split: str
    graders: tuple[GraderComparison, ...]
    tasks_in_common: int
    treatment_only: tuple[str, ...] = ()
    control_only: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "treatment": self.treatment,
            "control": self.control,
            "split": self.split,
            "tasks_in_common": self.tasks_in_common,
            "treatment_only_tasks": list(self.treatment_only),
            "control_only_tasks": list(self.control_only),
            "graders": [g.as_dict() for g in self.graders],
        }


@dataclass(frozen=True)
class LoadedReport:
    """The part of a written report a comparison needs."""

    configuration: str
    split: str
    per_task: dict[str, dict[str, float]]
    quotable: bool
    caveats: tuple[str, ...]


def load_report(path: Path) -> LoadedReport:
    """Read a written report, refusing one with no per-task scores.

    A report written before per-task scores existed cannot be compared, and
    saying so is better than comparing it against nothing and reporting a
    difference of zero.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    per_task = raw.get("per_task")
    if not per_task:
        raise ComparisonError(
            f"{path.name} carries no per-task scores, so it cannot be paired; "
            "re-run that configuration to write a comparable report"
        )
    return LoadedReport(
        configuration=str(raw["configuration"]),
        split=str(raw["split"]),
        per_task={str(k): {str(g): float(v) for g, v in s.items()} for k, s in per_task.items()},
        quotable=bool(raw.get("quotable_as_a_result", False)),
        caveats=tuple(str(c) for c in raw.get("caveats") or ()),
    )


def compare(
    treatment: LoadedReport,
    control: LoadedReport,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> Comparison:
    """Compare two runs, grader by grader, on the tasks both covered."""
    if treatment.split != control.split:
        raise ComparisonError(
            f"cannot compare {treatment.configuration} on {treatment.split} against "
            f"{control.configuration} on {control.split}; a difference across splits "
            "would measure the splits"
        )
    if treatment.configuration == control.configuration:
        raise ComparisonError(
            f"{treatment.configuration} compared against itself would report zero"
        )

    shared = sorted(set(treatment.per_task) & set(control.per_task))
    if not shared:
        raise ComparisonError(
            f"{treatment.configuration} and {control.configuration} have no tasks in common"
        )

    graders = sorted(
        {g for task in shared for g in treatment.per_task[task]}
        & {g for task in shared for g in control.per_task[task]}
    )
    comparisons = []
    for grader in graders:
        paired = [
            (treatment.per_task[task][grader], control.per_task[task][grader])
            for task in shared
            if grader in treatment.per_task[task] and grader in control.per_task[task]
        ]
        if not paired:
            continue
        treated = [pair[0] for pair in paired]
        controlled = [pair[1] for pair in paired]
        comparisons.append(
            GraderComparison(
                grader=grader,
                treatment_mean=sum(treated) / len(treated),
                control_mean=sum(controlled) / len(controlled),
                difference=paired_bootstrap_difference(treated, controlled, resamples=resamples),
                paired_tasks=len(paired),
                unpaired_tasks=len(shared) - len(paired),
            )
        )

    return Comparison(
        treatment=treatment.configuration,
        control=control.configuration,
        split=treatment.split,
        graders=tuple(comparisons),
        tasks_in_common=len(shared),
        treatment_only=tuple(sorted(set(treatment.per_task) - set(control.per_task))),
        control_only=tuple(sorted(set(control.per_task) - set(treatment.per_task))),
    )
