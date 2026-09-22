"""Held out sets, and the checks that keep them held out.

SPEC.md Section 8.2 asks for two different kinds of held out test, because
they answer different questions. The in distribution test asks whether the
system works on fault types it has seen at variants and loads it has not.
The out of distribution test holds entire fault families back and asks
whether it generalises or memorised.

Both are worthless the moment something tuned on train or validation has
seen the test data. That is not a thing to be careful about: it is checked,
here, and the check runs in CI.

This module lives under `firebreak.evals`, which is one of the few packages
allowed to import scenario specifications at all. See `config/leakage.yaml`.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from firebreak.lab.scenario import ScenarioSpec, Split

# Splits that anything may be tuned on. Everything else is read once, per
# release candidate, and never fitted to.
TUNABLE_SPLITS = frozenset({Split.TRAIN, Split.VALIDATION})
HELD_OUT_SPLITS = frozenset({Split.TEST_ID, Split.TEST_OOD})


class SplitError(Exception):
    """The library's split assignment is not sound."""


@dataclass(frozen=True)
class SplitSummary:
    """How a library divides, and whether the division holds up."""

    counts: dict[str, int]
    families_by_split: dict[str, tuple[str, ...]]
    ood_families: tuple[str, ...]
    scenarios: int

    @property
    def tunable_scenarios(self) -> int:
        return sum(self.counts.get(split.value, 0) for split in TUNABLE_SPLITS)

    @property
    def held_out_scenarios(self) -> int:
        return sum(self.counts.get(split.value, 0) for split in HELD_OUT_SPLITS)


def summarise(library: dict[str, ScenarioSpec]) -> SplitSummary:
    """Describe how a library divides across splits and families."""
    counts = Counter(spec.split.value for spec in library.values())
    families: dict[str, set[str]] = {}
    for spec in library.values():
        families.setdefault(spec.split.value, set()).add(spec.family)
    ood = tuple(sorted(families.get(Split.TEST_OOD.value, set())))
    return SplitSummary(
        counts=dict(sorted(counts.items())),
        families_by_split={k: tuple(sorted(v)) for k, v in sorted(families.items())},
        ood_families=ood,
        scenarios=len(library),
    )


def check_splits(library: dict[str, ScenarioSpec]) -> list[str]:
    """Every way a split assignment can be wrong, checked in one place.

    Returns problems rather than raising, so a caller can report all of them
    at once instead of one per run.
    """
    problems: list[str] = []
    if not library:
        return ["the library is empty"]

    summary = summarise(library)

    # The point of the out of distribution split is that these families were
    # never trained or tuned on. One scenario from an OOD family sitting in
    # train quietly turns the OOD number into another in distribution number.
    ood = set(summary.ood_families)
    for split in TUNABLE_SPLITS:
        overlap = ood & set(summary.families_by_split.get(split.value, ()))
        if overlap:
            problems.append(
                f"families held out as {Split.TEST_OOD.value} also appear in "
                f"{split.value}: {', '.join(sorted(overlap))}"
            )

    # An in distribution test on fault types never seen in training measures
    # generalisation, not in distribution performance, and would be reported
    # under the wrong name.
    trained = set()
    for split in TUNABLE_SPLITS:
        trained |= set(summary.families_by_split.get(split.value, ()))
    id_families = set(summary.families_by_split.get(Split.TEST_ID.value, ()))
    unseen = id_families - trained
    if unseen:
        problems.append(
            f"families in {Split.TEST_ID.value} that appear in no tunable split: "
            f"{', '.join(sorted(unseen))}"
        )

    for split in (Split.TRAIN, Split.VALIDATION, Split.TEST_ID):
        if summary.counts.get(split.value, 0) == 0:
            problems.append(f"no scenarios assigned to {split.value}")

    return problems


def require_sound_splits(library: dict[str, ScenarioSpec]) -> SplitSummary:
    """Summarise a library, refusing to proceed if its splits are unsound."""
    problems = check_splits(library)
    if problems:
        raise SplitError("; ".join(problems))
    return summarise(library)


def select(library: dict[str, ScenarioSpec], split: Split) -> dict[str, ScenarioSpec]:
    """Every scenario in one split, keyed by id."""
    return {name: spec for name, spec in library.items() if spec.split is split}


def tunable(library: dict[str, ScenarioSpec]) -> dict[str, ScenarioSpec]:
    """Scenarios anything may be fitted to: train and validation only."""
    return {name: spec for name, spec in library.items() if spec.split in TUNABLE_SPLITS}
