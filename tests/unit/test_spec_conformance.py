"""Tests for the spec conformance checker.

A checker that guides the work has to be trustworthy in both directions. A false
failure teaches people to ignore it, and a false pass is worse than no checker at
all because it reads as an assurance.

So the tests here are mostly about the distinction the checker exists to draw:
late work fails, planned work reports.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_spec_conformance import (  # noqa: E402
    ablation_due_phase,
    check_adrs,
    check_configurations,
    check_phase_reports,
    current_phase,
    due_phase,
    phase_tasks,
    read_spec,
    spec_adrs,
    spec_configurations,
)

SPEC_TEXT = read_spec()
TASKS = phase_tasks(SPEC_TEXT)


class TestItReadsTheSpecRatherThanACopyOfIt:
    def test_it_finds_every_phase(self) -> None:
        """Thirteen, numbered 0 to 12. A parser that found some of them would
        silently stop checking the rest."""
        assert sorted(TASKS) == list(range(13))

    def test_it_finds_the_configurations(self) -> None:
        found = spec_configurations(SPEC_TEXT)
        assert found == ["B0", "B1", "B2", "FB", "A1", "A2", "A3", "A4", "A5", "A6"]

    def test_it_finds_the_decision_records(self) -> None:
        found = spec_adrs(SPEC_TEXT)
        assert found[0] == "0001"
        assert "0010" in found
        assert len(found) == 14

    def test_a_missing_section_fails_loudly(self) -> None:
        """A stale checker must say so rather than pass by finding nothing."""
        from check_spec_conformance import _section

        with pytest.raises(SystemExit, match="no section headed"):
            _section("# nothing here", "### 9.5 Baselines and ablations")


class TestTheCurrentPhaseComesFromTheReports:
    def test_it_is_the_first_phase_without_a_report(self) -> None:
        """The same artefact the review gate produces, so there is no version
        string anybody has to remember to bump."""
        phase = current_phase()
        assert (REPO_ROOT / "docs" / "phase-reports" / f"P{phase - 1:02d}.md").is_file()
        assert not (REPO_ROOT / "docs" / "phase-reports" / f"P{phase:02d}.md").is_file()


class TestARangeClaimsTheAblationsInside:
    """Section 17 writes "ablations A2 to A4", so A3 is claimed without appearing.

    Reading the literal id only reported A3 as claimed by nobody, which is the
    first thing this checker got wrong.
    """

    def test_a3_is_claimed_by_the_phase_whose_range_covers_it(self) -> None:
        assert ablation_due_phase("A3", TASKS) == ablation_due_phase("A2", TASKS)

    def test_a_directly_named_ablation_still_wins(self) -> None:
        assert ablation_due_phase("A1", TASKS) == 7

    def test_a_range_does_not_claim_an_ablation_outside_it(self) -> None:
        tasks = {8: "ablations A2 to A4"}
        assert ablation_due_phase("A6", tasks) is None

    def test_something_that_is_not_an_ablation_is_not_range_matched(self) -> None:
        tasks = {8: "ablations A2 to A4"}
        assert ablation_due_phase("B1", tasks) is None

    def test_due_phase_returns_the_earliest_mention(self) -> None:
        tasks = {2: "nothing", 5: "builds the thing", 8: "the thing again"}
        assert due_phase(tasks, "the thing") == 5


class TestLateWorkFailsAndPlannedWorkReports:
    """Every configuration SPEC.md names is now registered, so these tests empty the
    registry rather than borrowing a real gap. Relying on one meant the tests broke each
    time an ablation was built, which is the wrong signal: the checker's own logic did
    not change."""

    @pytest.fixture(autouse=True)
    def _empty_registry(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from firebreak.evals import runner

        monkeypatch.setattr(runner, "CONFIGURATIONS", {})

    def test_a_configuration_due_in_a_past_phase_is_late(self) -> None:
        tasks = {3: "build A6 here"}
        found = check_configurations(_spec_with_configuration("A6"), tasks, phase=7)
        assert any("Phase 3 was meant to build it" in item for item in found.late)
        assert not found.ok

    def test_a_configuration_due_in_a_future_phase_is_planned(self) -> None:
        tasks = {9: "build A6 here"}
        found = check_configurations(_spec_with_configuration("A6"), tasks, phase=7)
        assert found.ok
        assert any("due in Phase 9" in item for item in found.planned)

    def test_a_configuration_due_this_phase_is_planned_not_late(self) -> None:
        """The phase in progress has not finished, so its work is not yet late."""
        tasks = {7: "build A6 here"}
        found = check_configurations(_spec_with_configuration("A6"), tasks, phase=7)
        assert found.ok

    def test_a_configuration_no_phase_claims_is_late(self) -> None:
        found = check_configurations(_spec_with_configuration("A6"), {3: "nothing"}, phase=7)
        assert any("no phase claims it" in item for item in found.late)

    def test_an_id_the_checker_has_no_runner_name_for_is_late(self) -> None:
        """A new configuration added to the spec must be mapped here deliberately.

        Guessing the runner id would reintroduce the exact failure this project
        keeps hitting: two files agreeing on a type and disagreeing on a name.
        """
        found = check_configurations(_spec_with_configuration("B9"), {3: "build B9"}, phase=7)
        assert any("no runner id for" in item for item in found.late)


class TestTheRealRepository:
    """Outside the class above, because that one empties the registry to test the
    checker's logic and this one needs the real thing."""

    def test_nothing_a_completed_phase_left_out(self) -> None:
        """The assertion that makes this suite worth running in CI."""
        phase = current_phase()
        for found in (
            check_configurations(SPEC_TEXT, TASKS, phase),
            check_adrs(SPEC_TEXT, TASKS, phase),
            check_phase_reports(SPEC_TEXT, phase),
        ):
            assert found.ok, found.late


class TestTheLastPhaseIsNotFollowedByAnInventedOne:
    """`current_phase()` counts reports, so once every phase has one it returns one past the
    last. The checker printed "Phase 13 is in progress" of a thirteen-phase spec, which invents
    a phase and leaves every later comparison measuring against nothing.

    This matters precisely at the end of the project, which is when nobody is looking at the
    tool any more.
    """

    def test_every_report_written_means_no_phase_is_in_progress(self) -> None:
        last = max(TASKS)
        assert current_phase() > last, (
            "not every phase has a report yet, so this boundary is not reachable; "
            "remove this test only when SPEC.md gains a phase"
        )

    def test_the_checker_still_checks_every_phase_at_the_boundary(self) -> None:
        """Clamping the phase number must not turn the checks off. Every obligation of every
        phase is still due, because every phase is complete."""
        last = max(TASKS)
        for found in (
            check_configurations(SPEC_TEXT, TASKS, last),
            check_adrs(SPEC_TEXT, TASKS, last),
            check_phase_reports(SPEC_TEXT, last),
        ):
            assert found.ok, found.late

    def test_the_phase_count_and_the_last_phase_number_differ_by_one(self) -> None:
        """The off-by-one itself. Phases are numbered from zero, so a thirteen-phase spec ends
        at twelve, and reporting the count as a phase number is the mistake."""
        assert max(TASKS) == len(TASKS) - 1
        assert 0 in TASKS


class TestAnUnindexedRecordIsLate:
    def test_every_written_record_is_in_the_index(self) -> None:
        """A record nobody browsing docs/adr/README.md can find may as well not
        be written."""
        found = check_adrs(SPEC_TEXT, TASKS, current_phase())
        assert not [item for item in found.late if "README.md" in item]


def _spec_with_configuration(spec_id: str) -> str:
    """A minimal spec whose Section 9.5 names one configuration."""
    return (
        "### 9.5 Baselines and ablations\n\n"
        "| ID | Configuration | Question |\n"
        "|---|---|---|\n"
        f"| {spec_id} | something | why |\n\n"
        "## 10. Next\n"
    )
