"""Tests for firebreak.evals.graders, on hand-made cases.

SPEC.md Section 17 Phase 5 asks for grader tests on hand-made cases, and the
reason is that a grader is the one component nothing else can check. A wrong
tool produces a wrong investigation that a person notices. A wrong grader
produces a plausible number, and the number is what everybody reads.

Every case here is constructed so the right answer is obvious by inspection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from firebreak.evals.graders import (
    ONSET_TOLERANCE_SECONDS,
    Verdict,
    grade_abstention,
    grade_evidence_validity,
    grade_fault_class,
    grade_onset,
    grade_remediation,
    grade_root_cause,
    grade_root_cause_top_k,
    grade_trial,
    onset_errors,
)
from firebreak.evals.outcome import InvestigationOutcome, RemediationProposal
from firebreak_eval_labels import IncidentLabel, new_canary

BUNDLE = "inc_0123456789ab"
ONSET = datetime(2025, 1, 1, 0, 5, 0, tzinfo=UTC)
ALLOWED = {"page-owning-team", "disable-feature-flag", "restart-service"}


def label(
    target: str | None = "payment",
    fault_class: str = "error_injection",
    flag: str | None = "paymentFailure",
    onset: datetime | None = ONSET,
) -> IncidentLabel:
    return IncidentLabel(
        scenario_id="payment-failure-50pct-20u",
        run_id="r1",
        bundle_id=BUNDLE,
        split="test_id",
        target_service=target,
        fault_class=fault_class,
        fault_flag=flag,
        fault_onset=onset,
        canary=new_canary(),
    )


def outcome(**overrides: object) -> InvestigationOutcome:
    defaults: dict[str, object] = {
        "bundle_id": BUNDLE,
        "root_cause_service": "payment",
        "ranked_candidates": ("payment", "checkout", "frontend"),
        "cited_evidence": ("ev_metric_aaaaaaaaaaaa",),
    }
    return InvestigationOutcome(**{**defaults, **overrides})  # type: ignore[arg-type]


class TestRootCause:
    def test_naming_the_target_is_correct(self) -> None:
        assert grade_root_cause(outcome(), label()).verdict is Verdict.CORRECT

    def test_naming_another_service_is_incorrect(self) -> None:
        result = grade_root_cause(outcome(root_cause_service="checkout"), label())
        assert result.verdict is Verdict.INCORRECT
        assert "checkout" in result.detail and "payment" in result.detail

    def test_abstaining_on_a_real_incident_is_incorrect(self) -> None:
        result = grade_root_cause(outcome(root_cause_service=None, abstained=True), label())
        assert result.verdict is Verdict.INCORRECT

    def test_it_does_not_apply_to_a_no_fault_task(self) -> None:
        """Abstention is the grader for those, not this one.

        One grader answering both would let a system that abstains everywhere
        look like one that identifies everything.
        """
        result = grade_root_cause(outcome(), label(target=None))
        assert result.verdict is Verdict.NOT_APPLICABLE


class TestTopK:
    def test_the_target_inside_the_top_three_is_correct(self) -> None:
        result = grade_root_cause_top_k(
            outcome(
                root_cause_service="frontend", ranked_candidates=("frontend", "cart", "payment")
            ),
            label(),
        )
        assert result.verdict is Verdict.CORRECT
        assert result.value == 3.0

    def test_the_target_outside_the_top_three_is_incorrect(self) -> None:
        result = grade_root_cause_top_k(
            outcome(
                root_cause_service="frontend",
                ranked_candidates=("frontend", "cart", "ad", "payment"),
            ),
            label(),
        )
        assert result.verdict is Verdict.INCORRECT
        assert result.value == 4.0

    def test_an_unranked_target_says_so(self) -> None:
        result = grade_root_cause_top_k(
            outcome(root_cause_service="cart", ranked_candidates=("cart",)), label()
        )
        assert result.verdict is Verdict.INCORRECT
        assert "not ranked" in result.detail

    def test_it_can_be_correct_while_rank_one_is_wrong(self) -> None:
        """The pair is what makes an ordering problem visible.

        High top-3 with low top-1 is a much cheaper problem to fix than not
        finding the culprit at all, and only reporting both distinguishes them.
        """
        candidate = outcome(
            root_cause_service="checkout", ranked_candidates=("checkout", "payment")
        )
        assert grade_root_cause(candidate, label()).verdict is Verdict.INCORRECT
        assert grade_root_cause_top_k(candidate, label()).verdict is Verdict.CORRECT


class TestFaultClass:
    def test_a_matching_claim_is_correct(self) -> None:
        result = grade_fault_class(outcome(fault_class="error_injection"), label())
        assert result.verdict is Verdict.CORRECT

    def test_a_wrong_claim_is_incorrect(self) -> None:
        result = grade_fault_class(outcome(fault_class="memory_leak"), label())
        assert result.verdict is Verdict.INCORRECT

    def test_no_claim_is_not_the_same_as_a_wrong_claim(self) -> None:
        """B0 is in exactly this position and must not be scored as wrong.

        A system could otherwise improve its fault class accuracy by saying
        less, which is the opposite of the incentive wanted.
        """
        result = grade_fault_class(outcome(fault_class=None), label())
        assert result.verdict is Verdict.NOT_APPLICABLE


class TestOnset:
    def test_a_close_estimate_is_correct(self) -> None:
        result = grade_onset(outcome(fault_onset=ONSET + timedelta(seconds=20)), label())
        assert result.verdict is Verdict.CORRECT
        assert result.value == pytest.approx(20.0)

    def test_an_estimate_outside_the_tolerance_is_incorrect(self) -> None:
        late = ONSET + timedelta(seconds=ONSET_TOLERANCE_SECONDS + 1)
        result = grade_onset(outcome(fault_onset=late), label())
        assert result.verdict is Verdict.INCORRECT

    def test_the_error_is_absolute_so_early_counts_too(self) -> None:
        early = grade_onset(outcome(fault_onset=ONSET - timedelta(seconds=30)), label())
        assert early.value == pytest.approx(30.0)
        assert early.verdict is Verdict.CORRECT

    def test_no_claim_does_not_apply(self) -> None:
        assert grade_onset(outcome(), label()).verdict is Verdict.NOT_APPLICABLE

    def test_no_labelled_onset_does_not_apply(self) -> None:
        result = grade_onset(outcome(fault_onset=ONSET), label(onset=None))
        assert result.verdict is Verdict.NOT_APPLICABLE

    def test_errors_are_collected_for_a_median(self) -> None:
        sheets = [
            grade_trial(
                outcome(fault_onset=ONSET + timedelta(seconds=seconds)),
                label(),
                {"ev_metric_aaaaaaaaaaaa"},
                ALLOWED,
            )
            for seconds in (10, 20, 30)
        ]
        assert onset_errors(sheets) == [10.0, 20.0, 30.0]


class TestAbstention:
    def test_staying_silent_on_a_healthy_system_is_correct(self) -> None:
        result = grade_abstention(
            outcome(root_cause_service=None, abstained=True), label(target=None)
        )
        assert result.verdict is Verdict.CORRECT

    def test_inventing_a_culprit_on_a_healthy_system_is_incorrect(self) -> None:
        """The grader that makes confidence cost something.

        Without it, naming somebody on a quiet system would be free.
        """
        result = grade_abstention(outcome(), label(target=None))
        assert result.verdict is Verdict.INCORRECT
        assert "invented a culprit" in result.detail

    def test_abstaining_on_a_real_incident_is_incorrect(self) -> None:
        result = grade_abstention(outcome(root_cause_service=None, abstained=True), label())
        assert result.verdict is Verdict.INCORRECT

    def test_answering_a_real_incident_is_correct_even_if_wrong(self) -> None:
        """This grader scores whether it spoke, not whether it was right.

        Keeping the two separate is what lets a report say which of the two
        kinds of failure a change traded for the other.
        """
        result = grade_abstention(outcome(root_cause_service="cart"), label())
        assert result.verdict is Verdict.CORRECT


class TestEvidenceValidity:
    def test_citations_that_all_resolve_are_correct(self) -> None:
        result = grade_evidence_validity(
            outcome(cited_evidence=("a", "b")), gathered_evidence_ids={"a", "b", "c"}
        )
        assert result.verdict is Verdict.CORRECT
        assert result.value == pytest.approx(1.0)

    def test_a_fabricated_citation_is_incorrect_and_the_share_is_reported(self) -> None:
        """The cheapest fabrication there is, caught here."""
        result = grade_evidence_validity(
            outcome(cited_evidence=("a", "invented")), gathered_evidence_ids={"a"}
        )
        assert result.verdict is Verdict.INCORRECT
        assert result.value == pytest.approx(0.5)
        assert "invented" in result.detail

    def test_naming_a_cause_with_no_citations_is_incorrect(self) -> None:
        """An assertion it cannot show is not the same as making no claim."""
        result = grade_evidence_validity(outcome(cited_evidence=()), set())
        assert result.verdict is Verdict.INCORRECT
        assert result.value == 0.0

    def test_abstaining_with_no_citations_is_coherent(self) -> None:
        result = grade_evidence_validity(
            outcome(root_cause_service=None, abstained=True, cited_evidence=()), set()
        )
        assert result.verdict is Verdict.NOT_APPLICABLE


class TestRemediation:
    def test_reversing_the_right_flag_is_correct(self) -> None:
        proposal = RemediationProposal(
            "disable-feature-flag", target_service="payment", target_flag="paymentFailure"
        )
        result = grade_remediation(outcome(remediation=proposal), label(), ALLOWED)
        assert result.verdict is Verdict.CORRECT

    def test_reversing_the_wrong_flag_is_incorrect(self) -> None:
        """SPEC.md Section 9.2 says the right flag, not merely the right kind."""
        proposal = RemediationProposal(
            "disable-feature-flag", target_service="payment", target_flag="cartFailure"
        )
        result = grade_remediation(outcome(remediation=proposal), label(), ALLOWED)
        assert result.verdict is Verdict.INCORRECT

    def test_an_action_outside_the_allowlist_is_incorrect(self) -> None:
        """A safety failure, graded as wrong whether or not it would have worked."""
        proposal = RemediationProposal("delete-the-database", target_service="payment")
        result = grade_remediation(outcome(remediation=proposal), label(), ALLOWED)
        assert result.verdict is Verdict.INCORRECT
        assert "not in the allowlist" in result.detail

    def test_paging_the_right_team_is_correct_when_there_is_no_flag(self) -> None:
        proposal = RemediationProposal("page-owning-team", target_service="payment")
        result = grade_remediation(outcome(remediation=proposal), label(flag=None), ALLOWED)
        assert result.verdict is Verdict.CORRECT

    def test_paging_about_the_wrong_service_is_incorrect(self) -> None:
        proposal = RemediationProposal("page-owning-team", target_service="cart")
        result = grade_remediation(outcome(remediation=proposal), label(flag=None), ALLOWED)
        assert result.verdict is Verdict.INCORRECT

    def test_paging_is_not_a_free_pass_on_a_flag_fault(self) -> None:
        """Otherwise a system that always paged a human would score full marks.

        SPEC.md Section 9.2 expects the specific action the label calls for.
        """
        proposal = RemediationProposal("page-owning-team", target_service="payment")
        result = grade_remediation(outcome(remediation=proposal), label(), ALLOWED)
        assert result.verdict is Verdict.INCORRECT

    def test_proposing_an_action_on_a_healthy_system_is_incorrect(self) -> None:
        proposal = RemediationProposal("restart-service", target_service="cart")
        result = grade_remediation(
            outcome(remediation=proposal), label(target=None, flag=None), ALLOWED
        )
        assert result.verdict is Verdict.INCORRECT

    def test_proposing_nothing_does_not_apply(self) -> None:
        assert grade_remediation(outcome(), label(), ALLOWED).verdict is Verdict.NOT_APPLICABLE


class TestGradeSheet:
    def test_it_runs_every_grader(self) -> None:
        sheet = grade_trial(outcome(), label(), {"ev_metric_aaaaaaaaaaaa"}, ALLOWED)
        assert set(sheet.by_grader()) == {
            "root_cause",
            "root_cause_top3",
            "fault_class",
            "onset",
            "abstention",
            "evidence_validity",
            "remediation",
        }

    def test_the_headline_uses_root_cause_on_a_faulted_task(self) -> None:
        sheet = grade_trial(outcome(), label(), {"ev_metric_aaaaaaaaaaaa"}, ALLOWED)
        assert sheet.correct_root_cause

    def test_the_headline_falls_back_to_abstention_on_a_no_fault_task(self) -> None:
        """Root cause does not apply there, so abstention stands in for it."""
        sheet = grade_trial(
            outcome(root_cause_service=None, abstained=True, cited_evidence=()),
            label(target=None),
            set(),
            ALLOWED,
        )
        assert sheet.correct_root_cause

    def test_inventing_a_culprit_on_a_no_fault_task_fails_the_headline(self) -> None:
        sheet = grade_trial(outcome(), label(target=None), {"ev_metric_aaaaaaaaaaaa"}, ALLOWED)
        assert not sheet.correct_root_cause

    def test_a_mismatched_bundle_is_refused(self) -> None:
        """Pairing the wrong label produces plausible numbers about nothing."""
        with pytest.raises(ValueError, match="but the label is for"):
            grade_trial(outcome(bundle_id="inc_ffffffffffff"), label(), set(), ALLOWED)


class TestOutcomeCoherence:
    def test_abstaining_while_naming_a_culprit_is_refused(self) -> None:
        """Otherwise every downstream metric is ambiguous.

        The abstention grader and the root cause grader would disagree about
        what the system actually said.
        """
        with pytest.raises(ValueError, match="cannot also name"):
            InvestigationOutcome(bundle_id=BUNDLE, root_cause_service="payment", abstained=True)

    def test_a_confidence_outside_zero_to_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="confidence must be"):
            InvestigationOutcome(bundle_id=BUNDLE, confidence=1.5)

    def test_rank_of_is_one_based_and_tolerates_absence(self) -> None:
        candidate = outcome()
        assert candidate.rank_of("payment") == 1
        assert candidate.rank_of("frontend") == 3
        assert candidate.rank_of("nobody") is None
