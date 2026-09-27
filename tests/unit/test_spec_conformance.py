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

    def test_the_real_repository_has_nothing_late(self) -> None:
        """The assertion that makes this suite worth running in CI."""
        phase = current_phase()
        for found in (
            check_configurations(SPEC_TEXT, TASKS, phase),
            check_adrs(SPEC_TEXT, TASKS, phase),
            check_phase_reports(SPEC_TEXT, phase),
        ):
            assert found.ok, found.late


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
