"""A scenario's split must never change, and a recorded one must never be held out.

Two invariants, both leakage controls, and neither was enforced until a real
regeneration broke the first one.

Splits used to be derived on every run from each family's sorted ids, so
removing one scenario shifted the position of every later one. Removing the
twelve `imageSlowLoad` scenarios moved two latency scenarios out of validation.
Nothing leaked because both destinations were tunable, and the same mechanism
would as happily have moved an already analysed scenario into `test_id`, which
would void the held out guarantee that is the only thing making its numbers
worth anything.

So the assignment is recorded in `scenarios/splits.yaml` rather than derived, and
these tests hold it there.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from firebreak.evals.runner import load_labels
from firebreak.evals.splits import HELD_OUT_SPLITS, TUNABLE_SPLITS
from firebreak.lab.bundle import iter_bundles
from firebreak.lab.scenario import Split, load_library

REPO_ROOT = Path(__file__).resolve().parents[2]
SPECS_DIR = REPO_ROOT / "scenarios" / "specs"
SPLITS_FILE = REPO_ROOT / "scenarios" / "splits.yaml"
BUNDLES_DIR = REPO_ROOT / "bundles"

sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture(scope="module")
def library():  # type: ignore[no-untyped-def]
    return load_library(SPECS_DIR)


@pytest.fixture(scope="module")
def recorded_assignment() -> dict[str, str]:
    raw = yaml.safe_load(SPLITS_FILE.read_text(encoding="utf-8")) or {}
    return {str(k): str(v) for k, v in (raw.get("splits") or {}).items()}


class TestTheAssignmentIsRecorded:
    def test_the_file_exists(self) -> None:
        """Derived splits are unstable, so the assignment is data."""
        assert SPLITS_FILE.is_file(), f"{SPLITS_FILE} is missing"

    def test_every_scenario_has_a_recorded_split(
        self, library, recorded_assignment: dict[str, str]
    ) -> None:  # type: ignore[no-untyped-def]
        missing = sorted(set(library) - set(recorded_assignment))
        assert not missing, f"scenarios with no recorded split: {missing}"

    def test_every_spec_matches_its_recorded_split(
        self, library, recorded_assignment: dict[str, str]
    ) -> None:  # type: ignore[no-untyped-def]
        """The file is the authority, so a spec disagreeing with it is a bug.

        It would mean somebody edited a spec's split by hand, and the next
        regeneration would silently revert it.
        """
        for scenario_id, spec in sorted(library.items()):
            assert spec.split.value == recorded_assignment[scenario_id], (
                f"{scenario_id} is {spec.split.value} in its spec and "
                f"{recorded_assignment[scenario_id]} in splits.yaml"
            )

    def test_every_recorded_split_is_a_real_split(
        self, recorded_assignment: dict[str, str]
    ) -> None:
        known = {split.value for split in Split}
        unknown = sorted({v for v in recorded_assignment.values() if v not in known})
        assert not unknown, f"unknown splits in the file: {unknown}"

    def test_a_removed_scenario_keeps_its_entry(
        self, library, recorded_assignment: dict[str, str]
    ) -> None:  # type: ignore[no-untyped-def]
        """Entries outlive the scenarios that earned them, on purpose.

        Dropping an entry would let a removed scenario come back later under a
        different split, which is the hazard this file exists to prevent. This
        asserts the behaviour rather than a count, so it passes whether or not
        any scenario has been removed yet.
        """
        stale = set(recorded_assignment) - set(library)
        for scenario_id in stale:
            assert recorded_assignment[scenario_id] in {s.value for s in Split}


class TestRegenerationMovesNothing:
    def test_regenerating_leaves_every_assignment_alone(
        self, recorded_assignment: dict[str, str]
    ) -> None:
        """The property the whole file exists for.

        `assign_splits` is called with the current specs and must return exactly
        what the file already says, never what the hash would say. At the time
        this was written 82 of 114 assignments differed from their hash, so a
        regeneration that consulted the hash would have moved most of the
        library.
        """
        from generate_scenarios import assign_splits, load_split_assignment, split_for_id

        before = load_split_assignment()
        assert before, "the recorded assignment is empty"

        # How many would move if the hash were consulted, which is what makes
        # this test meaningful rather than vacuous.
        would_differ = sum(1 for name, split in before.items() if split != split_for_id(name))
        assert would_differ > 0, (
            "no assignment differs from its hash, so this test cannot show that "
            "the file is being honoured rather than recomputed"
        )

        library = load_library(SPECS_DIR)
        assigned = assign_splits(list(library.values()))
        for spec in assigned:
            assert spec.split.value == recorded_assignment[spec.id], (
                f"regeneration moved {spec.id} from "
                f"{recorded_assignment[spec.id]} to {spec.split.value}"
            )

    def test_a_new_scenario_gets_a_split_from_its_own_id(self) -> None:
        """Deterministic, and independent of what else exists."""
        from generate_scenarios import split_for_id

        first = split_for_id("a-scenario-that-does-not-exist-20u")
        second = split_for_id("a-scenario-that-does-not-exist-20u")
        assert first is second
        assert first in set(Split)

    def test_the_hash_spreads_across_the_tunable_and_held_out_splits(self) -> None:
        """An assignment that sent everything one way would be useless.

        Checked over many synthetic ids rather than the library, so it measures
        the function rather than this particular set of names.
        """
        from collections import Counter

        from generate_scenarios import split_for_id

        counts = Counter(split_for_id(f"synthetic-scenario-{i}-20u") for i in range(600))
        assert set(counts) == {Split.TRAIN, Split.VALIDATION, Split.TEST_ID}
        # Roughly the SPEC.md Section 8.2 proportions, loosely bounded because
        # the point is that none of the three is starved.
        for split in (Split.TRAIN, Split.VALIDATION, Split.TEST_ID):
            assert counts[split] > 30, f"{split.value} got only {counts[split]} of 600"


class TestNoRecordedScenarioIsHeldOut:
    """The invariant that actually protects a number.

    A scenario that has been recorded has been available to look at, and
    everything in this project's development has looked at the recordings. Such a
    scenario can sit in a tunable split for ever; it can never become held out,
    because a held out split's whole value is that nothing has seen it.

    Skips when nothing is recorded, so a fresh clone does not fail.
    """

    def test_every_recorded_scenario_is_in_a_tunable_split_or_was_always_held_out(
        self, library, recorded_assignment: dict[str, str]
    ) -> None:  # type: ignore[no-untyped-def]
        if not BUNDLES_DIR.is_dir():
            pytest.skip("no bundles recorded")
        labels = load_labels()
        offenders = []
        for bundle in iter_bundles(BUNDLES_DIR):
            label = labels.get(bundle.name)
            if label is None:
                continue
            recorded_as = label.split
            now = recorded_assignment.get(label.scenario_id, recorded_as)
            # Recorded as tunable and now held out is the failure. Recorded as
            # held out and still held out is fine: a held out recording is how
            # a release candidate is measured.
            if recorded_as in {s.value for s in TUNABLE_SPLITS} and now in {
                s.value for s in HELD_OUT_SPLITS
            }:
                offenders.append((label.scenario_id, recorded_as, now))
        assert not offenders, (
            "these were recorded while tuning and are now held out, which voids "
            f"them as held out data: {offenders}"
        )

    def test_a_label_agrees_with_the_library_about_its_split(self, library) -> None:  # type: ignore[no-untyped-def]
        """Otherwise the eval and the library disagree about what a number means.

        The runner groups by the label's split, so a label saying validation
        while the library says train would put a bundle in a report about the
        wrong split.
        """
        if not BUNDLES_DIR.is_dir():
            pytest.skip("no bundles recorded")
        labels = load_labels()
        disagreements = []
        for bundle in iter_bundles(BUNDLES_DIR):
            label = labels.get(bundle.name)
            if label is None or label.scenario_id not in library:
                continue
            spec_split = library[label.scenario_id].split.value
            if spec_split != label.split:
                disagreements.append((label.scenario_id, label.split, spec_split))
        assert not disagreements, f"label and library disagree: {disagreements}"
