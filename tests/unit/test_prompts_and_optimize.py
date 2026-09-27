"""Tests for versioned prompts and for the GEPA optimizer's rules.

Two things are worth testing hardest here. The prompt hash, because it is what ties a
report to the prompts that produced it and a report whose prompts cannot be identified
is a report whose numbers cannot be reproduced. And the train-only rule, because Phase
10's reviewer focus is that no validation or test data reaches optimization.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from firebreak.evals.graders import GradeResult, GradeSheet, Verdict
from firebreak.evals.outcome import InvestigationOutcome
from firebreak.optimize.gepa import (
    WEIGHT_CALIBRATION,
    WEIGHT_CORRECTNESS,
    WEIGHT_EVIDENCE,
    LeakageError,
    OptimizationReport,
    OptimizeError,
    build_trainset,
    optimize_node,
    score,
    write_candidate,
    write_report,
)
from firebreak.prompts import (
    PROMPT_NODES,
    PromptError,
    latest_for,
    load_prompts,
    parse_prompt,
    stamps,
)

A_PROMPT = """---
id: reporter/v1
node: reporter
version: 1
optimized_from: null
notes: a note
---
Write the report.
"""


def verdict(correct: bool) -> Verdict:
    return Verdict.CORRECT if correct else Verdict.INCORRECT


def sheet(root_cause: bool, evidence: bool) -> GradeSheet:
    return GradeSheet(
        bundle_id="inc_000000000000",
        results=(
            GradeResult(grader="root_cause", verdict=verdict(root_cause), detail=""),
            GradeResult(grader="evidence_validity", verdict=verdict(evidence), detail=""),
        ),
    )


def outcome(confidence: float | None) -> InvestigationOutcome:
    return InvestigationOutcome(
        bundle_id="inc_000000000000",
        root_cause_service="payment",
        confidence=confidence,
    )


class TestThePromptHashIdentifiesWhatTheModelSaw:
    def test_every_node_has_a_prompt(self) -> None:
        for node in PROMPT_NODES:
            assert latest_for(node).node == node

    def test_the_stamp_carries_the_id_and_the_hash(self) -> None:
        stamp = latest_for("reporter").stamp
        assert stamp.startswith("reporter/v1@")
        assert len(stamp.split("@")[1]) == 12

    def test_editing_the_body_changes_the_hash(self, tmp_path: Path) -> None:
        first = parse_prompt(A_PROMPT, tmp_path / "reporter.v1.md")
        edited = parse_prompt(
            A_PROMPT.replace("Write the report.", "Write it well."), tmp_path / "x.md"
        )
        assert first.body_hash != edited.body_hash

    def test_editing_a_note_does_not_change_the_hash(self, tmp_path: Path) -> None:
        """A hash that moved for a comment would make every past report look stale after
        a typo fix."""
        first = parse_prompt(A_PROMPT, tmp_path / "a.md")
        renoted = parse_prompt(
            A_PROMPT.replace("notes: a note", "notes: a better note"), tmp_path / "b.md"
        )
        assert first.body_hash == renoted.body_hash

    def test_a_report_can_record_every_prompt_in_use(self) -> None:
        recorded = stamps()
        assert set(recorded) == set(PROMPT_NODES)
        assert all("@" in stamp for stamp in recorded.values())


class TestAHalfEditedCopyIsRefused:
    def test_an_id_disagreeing_with_the_node_fails(self, tmp_path: Path) -> None:
        """It would otherwise ship a critic prompt to the reporter, and the only symptom
        would be a worse report."""
        broken = A_PROMPT.replace("id: reporter/v1", "id: critic/v1")
        with pytest.raises(PromptError, match="half-edited copy"):
            parse_prompt(broken, tmp_path / "reporter.v1.md")

    def test_an_unknown_node_fails(self, tmp_path: Path) -> None:
        broken = A_PROMPT.replace("id: reporter/v1", "id: summariser/v1").replace(
            "node: reporter", "node: summariser"
        )
        with pytest.raises(PromptError, match="not one of"):
            parse_prompt(broken, tmp_path / "summariser.v1.md")

    def test_no_frontmatter_fails(self, tmp_path: Path) -> None:
        with pytest.raises(PromptError, match="no frontmatter"):
            parse_prompt("Just a prompt.\n", tmp_path / "x.md")

    def test_an_empty_body_fails(self, tmp_path: Path) -> None:
        with pytest.raises(PromptError, match="no prompt"):
            parse_prompt(
                "---\nid: reporter/v1\nnode: reporter\nversion: 1\n---\n", tmp_path / "x.md"
            )

    def test_a_missing_field_fails(self, tmp_path: Path) -> None:
        with pytest.raises(PromptError, match="missing 'version'"):
            parse_prompt("---\nid: reporter/v1\nnode: reporter\n---\nBody.\n", tmp_path / "x.md")


class TestTheLatestVersionWins:
    def test_the_highest_version_is_chosen(self, tmp_path: Path) -> None:
        """Highest rather than a pointer file, so shipping a version is adding a file and
        nothing else."""
        (tmp_path / "reporter.v1.md").write_text(A_PROMPT)
        (tmp_path / "reporter.v2.md").write_text(
            A_PROMPT.replace("reporter/v1", "reporter/v2")
            .replace("version: 1", "version: 2")
            .replace("optimized_from: null", "optimized_from: reporter/v1@abc123456789")
        )
        load_prompts.cache_clear()
        chosen = latest_for("reporter", tmp_path)
        assert chosen.version == 2
        assert chosen.optimized_from == "reporter/v1@abc123456789"
        load_prompts.cache_clear()

    def test_a_node_with_no_prompt_says_what_file_it_wants(self, tmp_path: Path) -> None:
        load_prompts.cache_clear()
        with pytest.raises(PromptError, match=re.escape("critic.v1.md")):
            latest_for("critic", tmp_path)
        load_prompts.cache_clear()


class TestOptimizationReadsTrainOnly:
    """Phase 10's reviewer focus."""

    def test_the_allowed_split_matches_the_real_one(self) -> None:
        """`gepa.py` spells the split as a string rather than importing `Split`, because
        `Split` lives beside `target_service` and importing it would give the optimizer
        an import path to ground truth. The leakage scanner flagged that on the first
        attempt. This is the test that stops the two spellings drifting.
        """
        from firebreak.lab.scenario import Split
        from firebreak.optimize.gepa import ALLOWED_SPLIT

        assert Split.TRAIN.value == ALLOWED_SPLIT

    def test_a_train_task_is_accepted(self) -> None:
        built = build_trainset([("inc_a", "train", "symptoms here", "payment")])
        assert [task.bundle_id for task in built] == ["inc_a"]

    @pytest.mark.parametrize("split", ["validation", "test_id", "test_ood"])
    def test_any_other_split_is_refused(self, split: str) -> None:
        with pytest.raises(LeakageError, match="reads train only"):
            build_trainset([("inc_a", split, "symptoms", "payment")])

    def test_the_error_says_why_it_matters(self) -> None:
        """A rejection reading "wrong split" teaches nothing, and somebody will
        eventually be tempted to relax it."""
        with pytest.raises(LeakageError, match="pass it for the wrong reason"):
            build_trainset([("inc_a", "validation", "symptoms", "payment")])

    def test_one_bad_task_refuses_the_whole_set(self) -> None:
        """Filtering it out silently would optimize on a smaller set than the caller
        asked for and report the larger count."""
        with pytest.raises(LeakageError):
            build_trainset(
                [
                    ("inc_a", "train", "symptoms", "payment"),
                    ("inc_b", "test_ood", "symptoms", "cart"),
                ]
            )

    def test_an_empty_train_set_is_refused(self) -> None:
        with pytest.raises(OptimizeError, match="record the train split first"):
            build_trainset([])


class TestTheMetric:
    def test_a_correct_well_cited_confident_report_scores_highest(self) -> None:
        assert score(sheet(True, True), outcome(0.85)) == pytest.approx(
            WEIGHT_CORRECTNESS + WEIGHT_EVIDENCE + WEIGHT_CALIBRATION * (1 - 0.15**2)
        )

    def test_correctness_outweighs_citation(self) -> None:
        """A prompt that cites beautifully and names the wrong service is worse than
        useless."""
        right_but_uncited = score(sheet(True, False), outcome(0.85))
        wrong_but_cited = score(sheet(False, True), outcome(0.3))
        assert right_but_uncited > wrong_but_cited

    def test_a_confident_wrong_answer_scores_below_an_unconfident_one(self) -> None:
        """The calibration term's whole purpose: without it a prompt optimized on
        correctness alone learns to always claim high confidence."""
        confident = score(sheet(False, True), outcome(0.85))
        humble = score(sheet(False, True), outcome(0.3))
        assert humble > confident

    def test_no_stated_confidence_is_not_penalised(self) -> None:
        """A report that declines to claim one is behaving correctly, and penalising it
        would teach the optimizer to always claim something."""
        assert score(sheet(True, True), outcome(None)) == pytest.approx(
            WEIGHT_CORRECTNESS + WEIGHT_EVIDENCE + WEIGHT_CALIBRATION
        )

    def test_the_weights_sum_to_one(self) -> None:
        assert pytest.approx(1.0) == WEIGHT_CORRECTNESS + WEIGHT_EVIDENCE + WEIGHT_CALIBRATION


class TestARunWithNoModelDoesNotClaimToOptimize:
    def trainset(self):  # type: ignore[no-untyped-def]
        return build_trainset([("inc_a", "train", "symptoms here", "payment")])

    def graded(self, body: str, task: object):  # type: ignore[no-untyped-def]
        del body, task
        return sheet(True, True), outcome(0.85)

    def test_it_reports_the_baseline_and_says_gepa_did_not_run(self) -> None:
        report = optimize_node("reporter", self.trainset(), rollouts=10, run_candidate=self.graded)
        assert not report.optimizer_ran
        assert "GEPA did not run" in report.verdict
        assert report.candidate_score == report.baseline_score

    def test_it_claims_no_improvement(self) -> None:
        """`improved` must be false when the optimizer never ran, or a report would say
        a prompt improved when nothing changed it."""
        report = optimize_node("reporter", self.trainset(), rollouts=10, run_candidate=self.graded)
        assert not report.improved
        assert report.candidate_path is None

    def test_an_unknown_node_is_refused(self) -> None:
        with pytest.raises(OptimizeError, match="not a node with a prompt"):
            optimize_node("summariser", self.trainset(), rollouts=1, run_candidate=self.graded)

    def test_the_baseline_score_is_a_real_measurement(self) -> None:
        report = optimize_node("reporter", self.trainset(), rollouts=10, run_candidate=self.graded)
        assert report.baseline_score == pytest.approx(score(sheet(True, True), outcome(0.85)))


class TestWritingACandidate:
    def test_it_writes_the_next_version_with_its_lineage(self, tmp_path: Path) -> None:
        baseline = parse_prompt(A_PROMPT, tmp_path / "reporter.v1.md")
        (tmp_path / "reporter.v1.md").write_text(A_PROMPT)
        path = write_candidate(baseline, "A better prompt.", tmp_path)
        load_prompts.cache_clear()
        written = parse_prompt(path.read_text(), path)
        assert written.version == 2
        assert written.optimized_from == baseline.stamp
        assert written.body == "A better prompt."
        load_prompts.cache_clear()

    def test_it_refuses_to_overwrite(self, tmp_path: Path) -> None:
        """A prompt a report may cite must keep resolving."""
        baseline = parse_prompt(A_PROMPT, tmp_path / "reporter.v1.md")
        (tmp_path / "reporter.v2.md").write_text("already here")
        with pytest.raises(OptimizeError, match="already exists"):
            write_candidate(baseline, "A better prompt.", tmp_path)


class TestTheReport:
    def test_it_says_the_measurement_is_train_only(self, tmp_path: Path) -> None:
        report = OptimizationReport(
            node="reporter",
            baseline_prompt="reporter/v1@abcdef123456",
            baseline_score=0.8,
            candidate_score=0.9,
            rollouts=40,
            train_tasks=39,
            generated_at="2026-09-27T00:00:00+00:00",
            optimizer_ran=True,
        )
        _, markdown = write_report(report, tmp_path)
        text = markdown.read_text()
        assert "train split only" in text
        assert "says nothing about held-out performance" in text

    def test_an_improvement_says_it_must_still_pass_the_gate(self, tmp_path: Path) -> None:
        report = OptimizationReport(
            node="reporter",
            baseline_prompt="reporter/v1@abcdef123456",
            baseline_score=0.8,
            candidate_score=0.9,
            rollouts=40,
            train_tasks=39,
            generated_at="2026-09-27T00:00:00+00:00",
            optimizer_ran=True,
        )
        assert report.improved
        assert "eval gate on validation" in report.verdict
        assert "OOD" in report.verdict

    def test_the_weights_are_explained_in_the_report(self, tmp_path: Path) -> None:
        """They are a judgement, not a measurement, and a reader should not have to read
        the source to learn that."""
        report = OptimizationReport(
            node="critic",
            baseline_prompt="critic/v1@abcdef123456",
            baseline_score=0.8,
            candidate_score=0.8,
            rollouts=0,
            train_tasks=39,
            generated_at="2026-09-27T00:00:00+00:00",
        )
        _, markdown = write_report(report, tmp_path)
        assert "a judgement, not a measurement" in markdown.read_text()


class TestA6RefusesRatherThanFakingARow:
    def test_it_refuses_while_no_optimized_prompt_exists(self) -> None:
        """An A6 row identical to FB would read as "optimization did not help", which is
        a claim about GEPA. The truth is that no optimized prompt exists, which is a
        claim about this repository."""
        from firebreak.evals.runner import run_a6

        expected = re.escape("no prompt in prompts/ carries an optimized_from")
        with pytest.raises(OptimizeError, match=expected):
            run_a6(Path("unused"))

    def test_it_is_still_registered(self) -> None:
        """So the spec conformance check sees every configuration SPEC.md names."""
        from firebreak.evals.runner import CONFIGURATIONS

        assert "a6" in CONFIGURATIONS
