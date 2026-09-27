"""Tests for the recording priority order.

Two properties, both of which cost real hours when they are wrong: the order
must be stable so an interrupted run resumes rather than restarting somewhere
new, and it must record the no-fault scenarios first because they are what the
abstention threshold needs and there are few of them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from firebreak.lab.scenario import FaultKind, Split  # noqa: E402
from record_library import SPLIT_ORDER, ordered_scenarios  # noqa: E402


@pytest.fixture(scope="module")
def order():  # type: ignore[no-untyped-def]
    return ordered_scenarios()


class TestNoFaultComesFirst:
    """The scarce resource, and the only data the abstention threshold can use.

    Measured on the first eight clean recordings, with one no-fault among them,
    the shipped threshold abstains on five of seven real faults and no defensible
    replacement can be fitted from a single negative example.
    """

    def test_within_each_split_the_no_fault_scenarios_lead(self) -> None:
        for split in SPLIT_ORDER:
            in_split = ordered_scenarios((split,))
            if not in_split:
                continue
            kinds = [s.fault.kind is FaultKind.NONE for s in in_split]
            # Every True before every False: no faulted scenario precedes a
            # no-fault one.
            assert kinds == sorted(kinds, reverse=True), (
                f"{split.value} interleaves faulted and no-fault scenarios"
            )

    def test_validation_records_its_negatives_before_anything_else(self) -> None:
        first = ordered_scenarios((Split.VALIDATION,))[0]
        assert first.fault.kind is FaultKind.NONE

    def test_a_split_with_no_negatives_still_works(self) -> None:
        """test_ood has none, and an empty leading group must not reorder it."""
        specs = ordered_scenarios((Split.TEST_OOD,))
        assert specs
        assert [s.id for s in specs] == sorted(s.id for s in specs)


class TestTheOrderIsStable:
    def test_two_calls_agree(self, order) -> None:  # type: ignore[no-untyped-def]
        """An interrupted run has to continue, not start somewhere new."""
        assert [s.id for s in order] == [s.id for s in ordered_scenarios()]

    def test_tunable_splits_come_before_held_out_ones(self, order) -> None:  # type: ignore[no-untyped-def]
        """A held out recording made before the thresholds are re-tuned scores
        zero and measures nothing."""
        positions = {}
        for index, spec in enumerate(order):
            positions.setdefault(spec.split, index)
        assert positions[Split.VALIDATION] < positions[Split.TEST_ID]
        assert positions[Split.TRAIN] < positions[Split.TEST_OOD]

    def test_every_scenario_appears_exactly_once(self, order) -> None:  # type: ignore[no-untyped-def]
        ids = [s.id for s in order]
        assert len(ids) == len(set(ids))
