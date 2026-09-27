"""No held-out incident may be in memory, at any layer.

SPEC.md Section 10.3 and Section 17 Phase 10's reviewer focus. This is the leakage
control that differs from all the others, and the difference is why it gets three
layers and its own test file.

**Every other leakage control guards a path a person could take.** The split
assignment is recorded so a regeneration cannot move a scenario. Bundle directories
are opaque so a name cannot state the answer. The runner's signature has no parameter
a label could arrive through. All of those guard against somebody doing the wrong
thing.

**Memory is written by the system itself.** So the way this one fails is not a person
editing a file. It is a run confirming an incident, the confirmation being admitted,
and every later run on that incident reading the answer out of memory instead of
investigating. The report would cite real evidence and reach the right conclusion for
the wrong reason, the score would measure recall, and nothing downstream could tell.

That is unrecoverable in the way that matters: a number that has been quoted cannot be
un-quoted. Hence the model validator, the store's own check, and this test over the
file on disk.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from firebreak.evals.splits import HELD_OUT_SPLITS, TUNABLE_SPLITS
from firebreak.lab.scenario import Split
from firebreak.memory.store import (
    ALLOWED_SPLIT,
    LeakageError,
    MemoryEntry,
    MemoryStore,
    entry_from_label,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def an_entry(split: str = ALLOWED_SPLIT, incident_id: str = "inc_000000000000") -> MemoryEntry:
    return entry_from_label(
        incident_id=incident_id,
        scenario_id="payment-failure-50pct-20u",
        split=split,
        target_service="payment",
        fault_class="error_injection",
        symptom_summary="payment error rate rose sharply while checkout reported failed charges",
        confirmed_by="owner",
        evidence_types=("metric", "log"),
        services_involved=("payment", "checkout"),
        confirmed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


class TestTheTypeRefusesAHeldOutSplit:
    """Layer one. A caller that bypassed the store still cannot build the entry."""

    @pytest.mark.parametrize("split", sorted(s.value for s in HELD_OUT_SPLITS))
    def test_a_held_out_split_cannot_be_constructed(self, split: str) -> None:
        with pytest.raises(ValidationError, match="holds train only"):
            an_entry(split=split)

    def test_validation_cannot_be_avoided_by_the_dict_form(self) -> None:
        """`model_validate` runs the same validator, so a round trip through JSON is
        not a way in."""
        payload = json.loads(an_entry().model_dump_json())
        payload["split"] = "test_ood"
        with pytest.raises(ValidationError, match="holds train only"):
            MemoryEntry.model_validate(payload)

    def test_validation_is_on_the_split_and_not_on_a_list_of_names(self) -> None:
        """An unknown split is refused too, so a typo in a label cannot pass as
        train."""
        with pytest.raises(ValidationError, match="holds train only"):
            an_entry(split="trian")

    def test_the_error_says_why_it_matters(self) -> None:
        """A rejection reading "invalid split" teaches nothing, and somebody will
        eventually be tempted to relax it."""
        with pytest.raises(ValidationError, match="recall rather than of investigation"):
            an_entry(split="test_id")

    def test_the_allowed_split_is_the_one_spec_names(self) -> None:
        assert Split.TRAIN.value == ALLOWED_SPLIT
        assert Split(ALLOWED_SPLIT) in TUNABLE_SPLITS


class TestTheStoreRefusesAHeldOutSplit:
    """Layer two, and it raises its own type.

    A malformed entry is a bug to fix. This is a measurement that would have been
    silently invalidated, and a caller should be able to tell the difference.
    """

    def test_admitting_a_held_out_entry_raises_leakage_error(self, tmp_path: Path) -> None:
        store = MemoryStore(tmp_path / "memory.jsonl")
        # Built past the validator on purpose, which is what a bug elsewhere would do.
        smuggled = MemoryEntry.model_construct(**json.loads(an_entry().model_dump_json()))
        object.__setattr__(smuggled, "split", "test_ood")
        with pytest.raises(LeakageError, match="memory holds train only"):
            store.admit(smuggled)

    def test_nothing_is_written_when_an_entry_is_refused(self, tmp_path: Path) -> None:
        """A refusal that had already appended would defeat the point."""
        path = tmp_path / "memory.jsonl"
        store = MemoryStore(path)
        smuggled = MemoryEntry.model_construct(**json.loads(an_entry().model_dump_json()))
        object.__setattr__(smuggled, "split", "test_id")
        with pytest.raises(LeakageError):
            store.admit(smuggled)
        assert not path.exists() or path.read_text() == ""

    def test_a_train_entry_is_admitted(self, tmp_path: Path) -> None:
        """The other half. Without this the tests above would pass on a store that
        accepted nothing at all."""
        store = MemoryStore(tmp_path / "memory.jsonl")
        store.admit(an_entry())
        assert [entry.incident_id for entry in store.entries()] == ["inc_000000000000"]

    def test_re_confirming_an_incident_does_not_duplicate_it(self, tmp_path: Path) -> None:
        """Re-confirming is a normal thing to do, and the first confirmation counts."""
        store = MemoryStore(tmp_path / "memory.jsonl")
        store.admit(an_entry())
        store.admit(an_entry())
        assert len(store.entries()) == 1


class TestTheFileOnDiskHoldsOnlyTrain:
    """Layer three. The only layer that catches an entry that got in some other way.

    Reads the file without validating, because an entry written by a tool that did not
    exist when the validator was added is exactly the case the first two layers cannot
    see.
    """

    def test_the_shipped_memory_file_has_no_held_out_entry(self) -> None:
        store = MemoryStore()
        offenders = store.held_out_entries()
        assert not offenders, (
            "these entries are from held-out splits and invalidate every score on "
            f"those incidents: {[(e.incident_id, e.split) for e in offenders]}"
        )

    def test_the_check_would_catch_one(self, tmp_path: Path) -> None:
        """A checker that found nothing because it was broken would be worse than
        none, so it is pointed at a file that does contain a violation."""
        path = tmp_path / "memory.jsonl"
        payload = json.loads(an_entry().model_dump_json())
        payload["split"] = "test_ood"
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        offenders = MemoryStore(path).held_out_entries()
        assert [entry.split for entry in offenders] == ["test_ood"]

    def test_an_absent_memory_file_is_not_a_violation(self, tmp_path: Path) -> None:
        """A fresh clone has no memory, and that must not fail the build."""
        assert MemoryStore(tmp_path / "nothing.jsonl").held_out_entries() == []

    def test_entries_reading_the_shipped_file_all_validate(self) -> None:
        """So the strict reader and the permissive one agree about what is in there."""
        store = MemoryStore()
        assert all(entry.split == ALLOWED_SPLIT for entry in store.entries())


class TestRetrievalCannotReachAHeldOutIncident:
    def test_a_query_matching_a_held_out_incident_returns_nothing(self, tmp_path: Path) -> None:
        """The end-to-end version: even a perfectly matching held-out entry is
        unreachable, because it could not be stored."""
        store = MemoryStore(tmp_path / "memory.jsonl")
        smuggled = MemoryEntry.model_construct(**json.loads(an_entry().model_dump_json()))
        object.__setattr__(smuggled, "split", "test_ood")
        with pytest.raises(LeakageError):
            store.admit(smuggled)
        assert (
            store.similar("payment error rate rose sharply while checkout reported failures") == []
        )

    def test_a_train_incident_is_reachable(self, tmp_path: Path) -> None:
        store = MemoryStore(tmp_path / "memory.jsonl")
        store.admit(an_entry())
        matches = store.similar(
            "payment error rate rose while checkout reported failed charges",
            services=("payment",),
        )
        assert [match.entry.incident_id for match in matches] == ["inc_000000000000"]


class TestTheToolCannotSeeHeldOutMemory:
    def test_the_tool_reads_the_store_and_nothing_else(self) -> None:
        """A second read path would be a second place for the invariant to be missing,
        which is how the recurring vocabulary defect in this project happens."""
        source = (REPO_ROOT / "src" / "firebreak" / "tools" / "memory.py").read_text(
            encoding="utf-8"
        )
        assert "MemoryStore()" in source
        assert "json.loads" not in source, "the tool must not parse the memory file itself"

    def test_the_tool_description_states_the_rule(self) -> None:
        """The description is what a model reads, and the rule is part of what makes
        the tool safe to offer."""
        from firebreak.tools.memory import SIMILAR_INCIDENTS

        assert "held-out" in SIMILAR_INCIDENTS.description
        assert "lead to test" in SIMILAR_INCIDENTS.description
        assert "never as evidence" in SIMILAR_INCIDENTS.description
