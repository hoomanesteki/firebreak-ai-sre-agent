"""Code graders: did the investigation get it right.

SPEC.md Section 9.2 lists eight graders and marks six of them as code. Those
six are here. The model-based mechanism judge and the human usefulness rubric
belong to later phases, because a judge needs calibrating against owner labels
before its output means anything.

**Grade the outcome, not the path.** A grader that rewarded a particular
sequence of tool calls would punish a valid investigation that took an
unexpected route, and the whole reason for a multi-agent system is that the
route varies.

**Three results, not two.** A grader returns correct, incorrect, or not
applicable, and the third is not a convenience. "The system made no claim
about the fault mechanism" and "the system claimed the wrong mechanism" are
different findings, and collapsing them would let a system improve its score
by saying less. Coverage is reported beside accuracy for the same reason.

**This module reads ground truth.** It is under `firebreak.evals`, which
`config/leakage.yaml` permits, and it never hands a label back to anything
that produced an outcome. The direction of flow is the whole control: an
outcome goes in, a score comes out, and the label never travels the other way.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from firebreak.evals.outcome import InvestigationOutcome
from firebreak_eval_labels import IncidentLabel

# SPEC.md Section 9.2: presence in the top 3 of ranked candidates.
TOP_K = 3

# How close a reported onset has to be to count, in seconds. Generous on
# purpose: a recording samples metrics every fifteen seconds, so an onset
# cannot be located more precisely than one sample interval however good the
# reasoning is, and a tighter tolerance would be measuring the sample rate.
ONSET_TOLERANCE_SECONDS = 60.0


class Verdict(StrEnum):
    """What a grader concluded.

    `NOT_APPLICABLE` covers both a claim the system did not make and a task
    the grader does not apply to, for example onset error on a no-fault task
    where there is no onset. Both are excluded from accuracy and counted in
    coverage.
    """

    CORRECT = "correct"
    INCORRECT = "incorrect"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class GradeResult:
    """One grader's verdict on one outcome, with why."""

    grader: str
    verdict: Verdict
    detail: str
    # A number the grader measured, where it measured one. Onset error in
    # seconds, or a share for evidence validity. Kept separate from the
    # verdict so a report can show the distribution rather than only the pass
    # rate.
    value: float | None = None

    @property
    def applies(self) -> bool:
        return self.verdict is not Verdict.NOT_APPLICABLE

    @property
    def correct(self) -> bool:
        return self.verdict is Verdict.CORRECT

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "grader": self.grader,
            "verdict": self.verdict.value,
            "detail": self.detail,
        }
        if self.value is not None:
            payload["value"] = round(self.value, 6)
        return payload


def grade_root_cause(outcome: InvestigationOutcome, label: IncidentLabel) -> GradeResult:
    """Exact match at rank 1 against the labelled target service.

    A no-fault task has no target, so this grader does not apply to it. That
    is not a gap: `grade_abstention` is the grader for those tasks, and having
    one grader answer both questions would make a system that abstains
    everywhere look like one that identifies everything.
    """
    if label.target_service is None:
        return GradeResult(
            "root_cause",
            Verdict.NOT_APPLICABLE,
            "no-fault task, so there is no service to identify",
        )
    if outcome.abstained:
        return GradeResult(
            "root_cause",
            Verdict.INCORRECT,
            f"abstained on a real incident whose cause was {label.target_service}",
        )
    if outcome.root_cause_service is None:
        return GradeResult("root_cause", Verdict.INCORRECT, "named no service on a real incident")
    if outcome.root_cause_service == label.target_service:
        return GradeResult(
            "root_cause", Verdict.CORRECT, f"named {label.target_service}", value=1.0
        )
    return GradeResult(
        "root_cause",
        Verdict.INCORRECT,
        f"named {outcome.root_cause_service} but the cause was {label.target_service}",
        value=0.0,
    )


def grade_root_cause_top_k(
    outcome: InvestigationOutcome, label: IncidentLabel, k: int = TOP_K
) -> GradeResult:
    """Presence of the labelled target in the top k ranked candidates.

    Reported alongside rank 1 because the two answer different questions.
    Rank 1 asks whether the system is right; top 3 asks whether it looked in
    the right place. A system whose top-3 is high and top-1 is low has an
    ordering problem, which is a different and much cheaper thing to fix than
    not finding the culprit at all.
    """
    if label.target_service is None:
        return GradeResult(
            f"root_cause_top{k}",
            Verdict.NOT_APPLICABLE,
            "no-fault task, so there is no service to identify",
        )
    rank = outcome.rank_of(label.target_service)
    if rank is not None and rank <= k:
        return GradeResult(
            f"root_cause_top{k}",
            Verdict.CORRECT,
            f"{label.target_service} ranked {rank}",
            value=float(rank),
        )
    if rank is None:
        return GradeResult(
            f"root_cause_top{k}",
            Verdict.INCORRECT,
            f"{label.target_service} was not ranked at all",
        )
    return GradeResult(
        f"root_cause_top{k}",
        Verdict.INCORRECT,
        f"{label.target_service} ranked {rank}, outside the top {k}",
        value=float(rank),
    )


def grade_fault_class(outcome: InvestigationOutcome, label: IncidentLabel) -> GradeResult:
    """Exact match against the labelled fault class.

    Not applicable when the outcome makes no claim. B0 is in that position by
    design: it names a service from anomaly scores and a dependency walk and
    says nothing about mechanism. Scoring that as incorrect would report
    classic triage as wrong about something it never asserted.
    """
    if outcome.fault_class is None:
        return GradeResult(
            "fault_class", Verdict.NOT_APPLICABLE, "the outcome claims no fault class"
        )
    if outcome.fault_class == label.fault_class:
        return GradeResult(
            "fault_class", Verdict.CORRECT, f"claimed {label.fault_class}", value=1.0
        )
    return GradeResult(
        "fault_class",
        Verdict.INCORRECT,
        f"claimed {outcome.fault_class} but the label is {label.fault_class}",
        value=0.0,
    )


def grade_onset(
    outcome: InvestigationOutcome,
    label: IncidentLabel,
    tolerance_seconds: float = ONSET_TOLERANCE_SECONDS,
) -> GradeResult:
    """Absolute error between the reported onset and when the fault was applied.

    `fault_onset` on the label is the moment flagd actually served the fault,
    not the moment it was written, which is why the recorder measures
    convergence. Grading against the write time would charge the system for
    the flag service's propagation delay.
    """
    if label.fault_onset is None:
        return GradeResult("onset", Verdict.NOT_APPLICABLE, "the label records no fault onset")
    if outcome.fault_onset is None:
        return GradeResult("onset", Verdict.NOT_APPLICABLE, "the outcome claims no onset")

    error = abs((outcome.fault_onset - label.fault_onset).total_seconds())
    verdict = Verdict.CORRECT if error <= tolerance_seconds else Verdict.INCORRECT
    return GradeResult(
        "onset",
        verdict,
        f"reported onset is {error:.0f}s from the labelled onset "
        f"(tolerance {tolerance_seconds:.0f}s)",
        value=error,
    )


def grade_abstention(outcome: InvestigationOutcome, label: IncidentLabel) -> GradeResult:
    """On a no-fault task, correct if the report blames nobody.

    This is the grader that stops a system scoring well by being confident
    about everything. SPEC.md Section 6.2 includes a no-fault family precisely
    so that inventing a culprit costs something, and without this grader it
    would cost nothing at all.

    On a task that does have a fault, abstaining is graded as incorrect
    abstention. That is deliberately not the same as a wrong service, and the
    report separates the two: staying silent sends nobody anywhere, while
    naming the wrong service sends an engineer somewhere useless with a
    confident looking report.
    """
    if label.target_service is None:
        if outcome.abstained or outcome.root_cause_service is None:
            return GradeResult("abstention", Verdict.CORRECT, "correctly blamed nobody", value=1.0)
        return GradeResult(
            "abstention",
            Verdict.INCORRECT,
            f"invented a culprit, {outcome.root_cause_service}, on a healthy system",
            value=0.0,
        )
    if outcome.abstained:
        return GradeResult(
            "abstention",
            Verdict.INCORRECT,
            f"abstained on a real incident caused by {label.target_service}",
            value=0.0,
        )
    return GradeResult(
        "abstention", Verdict.CORRECT, "did not abstain on a real incident", value=1.0
    )


def grade_evidence_validity(
    outcome: InvestigationOutcome, gathered_evidence_ids: set[str]
) -> GradeResult:
    """Share of cited evidence that resolves in what was actually gathered.

    The cheapest possible fabrication is a citation to an id nothing produced,
    and this is where it is caught. SPEC.md Section 6.9 has the exit gate
    re-run each citation and match it; this grader checks the weaker and more
    fundamental property first, that the citation refers to something real.

    A report with no citations scores zero rather than not applicable. Making
    no claims is not the same as making unsupported ones, but a report that
    names a root cause and cites nothing has asserted something it cannot
    show, and a grader that excused it would remove the incentive the
    evidence mechanism exists to create.
    """
    if not outcome.cited_evidence:
        if outcome.abstained:
            return GradeResult(
                "evidence_validity",
                Verdict.NOT_APPLICABLE,
                "abstained and cited nothing, which is coherent",
            )
        return GradeResult(
            "evidence_validity",
            Verdict.INCORRECT,
            "named a root cause and cited no evidence for it",
            value=0.0,
        )

    resolved = [ref for ref in outcome.cited_evidence if ref in gathered_evidence_ids]
    share = len(resolved) / len(outcome.cited_evidence)
    missing = sorted(set(outcome.cited_evidence) - gathered_evidence_ids)
    if not missing:
        return GradeResult(
            "evidence_validity",
            Verdict.CORRECT,
            f"all {len(resolved)} citations resolve",
            value=share,
        )
    return GradeResult(
        "evidence_validity",
        Verdict.INCORRECT,
        f"{len(missing)} of {len(outcome.cited_evidence)} citations do not resolve: "
        f"{', '.join(missing[:3])}",
        value=share,
    )


def grade_remediation(
    outcome: InvestigationOutcome, label: IncidentLabel, allowed_ids: set[str]
) -> GradeResult:
    """Did the proposed action match the one the label calls for.

    SPEC.md Section 9.2 gives the example of turning off the right flag, so
    the flag is compared and not just the kind of action: proposing to disable
    some flag on a flag-caused incident is not the same as proposing to
    disable the flag that caused it.

    Paging the owning team is always available and always safe, so it is
    graded as correct only when the label has no better answer. A system that
    always paged a human would otherwise score full marks on remediation
    while never remediating anything.
    """
    if outcome.remediation is None:
        return GradeResult("remediation", Verdict.NOT_APPLICABLE, "the outcome proposes no action")

    proposal = outcome.remediation
    if proposal.remediation_id not in allowed_ids:
        # Outside the allowlist is a safety failure rather than a quality one,
        # and it is graded as incorrect regardless of whether the action would
        # have worked.
        return GradeResult(
            "remediation",
            Verdict.INCORRECT,
            f"proposed {proposal.remediation_id!r}, which is not in the allowlist",
            value=0.0,
        )

    # A flag fault has a specific right answer: return that flag to its
    # previous value on the service that owns it.
    if label.fault_flag is not None:
        if proposal.target_flag == label.fault_flag:
            return GradeResult(
                "remediation",
                Verdict.CORRECT,
                f"proposed returning {label.fault_flag} to its previous value",
                value=1.0,
            )
        if proposal.target_flag is None:
            return GradeResult(
                "remediation",
                Verdict.INCORRECT,
                f"proposed {proposal.remediation_id} naming no flag, but the fault was "
                f"the {label.fault_flag} flag",
                value=0.0,
            )
        return GradeResult(
            "remediation",
            Verdict.INCORRECT,
            f"proposed reversing {proposal.target_flag} but the fault was {label.fault_flag}",
            value=0.0,
        )

    # No flag to reverse, so paging the owning team is the correct answer and
    # it has to name the right service to be useful.
    if label.target_service is None:
        return GradeResult(
            "remediation",
            Verdict.INCORRECT,
            "proposed an action on a healthy system",
            value=0.0,
        )
    if proposal.target_service == label.target_service:
        return GradeResult(
            "remediation",
            Verdict.CORRECT,
            f"proposed {proposal.remediation_id} for {label.target_service}",
            value=1.0,
        )
    return GradeResult(
        "remediation",
        Verdict.INCORRECT,
        f"proposed {proposal.remediation_id} for {proposal.target_service} "
        f"but the cause was {label.target_service}",
        value=0.0,
    )


@dataclass(frozen=True)
class GradeSheet:
    """Every grader's verdict on one trial."""

    bundle_id: str
    results: tuple[GradeResult, ...]

    def by_grader(self) -> dict[str, GradeResult]:
        return {result.grader: result for result in self.results}

    def verdict_for(self, grader: str) -> GradeResult | None:
        return self.by_grader().get(grader)

    @property
    def correct_root_cause(self) -> bool:
        """Whether this trial got the headline question right.

        Used as the single per-trial outcome for reliability and calibration.
        On a no-fault task the headline question is the abstention, because
        there is no service to name, so the two graders stand in for each
        other exactly where the other does not apply.
        """
        root = self.verdict_for("root_cause")
        if root is not None and root.applies:
            return root.correct
        abstention = self.verdict_for("abstention")
        return abstention.correct if abstention is not None else False

    def as_dict(self) -> dict[str, object]:
        return {
            "bundle_id": self.bundle_id,
            "correct": self.correct_root_cause,
            "grades": [result.as_dict() for result in self.results],
        }


def grade_trial(
    outcome: InvestigationOutcome,
    label: IncidentLabel,
    gathered_evidence_ids: set[str],
    allowed_remediation_ids: set[str],
    top_k: int = TOP_K,
    onset_tolerance_seconds: float = ONSET_TOLERANCE_SECONDS,
) -> GradeSheet:
    """Run every code grader over one trial.

    Refuses an outcome and a label that describe different bundles. The two
    arrive from different places, the outcome from the investigation and the
    label from `labels/`, and pairing them wrongly would produce a report full
    of plausible numbers about nothing.
    """
    if outcome.bundle_id != label.bundle_id:
        raise ValueError(
            f"outcome is for bundle {outcome.bundle_id} but the label is for {label.bundle_id}"
        )

    return GradeSheet(
        bundle_id=outcome.bundle_id,
        results=(
            grade_root_cause(outcome, label),
            grade_root_cause_top_k(outcome, label, top_k),
            grade_fault_class(outcome, label),
            grade_onset(outcome, label, onset_tolerance_seconds),
            grade_abstention(outcome, label),
            grade_evidence_validity(outcome, gathered_evidence_ids),
            grade_remediation(outcome, label, allowed_remediation_ids),
        ),
    )


def onset_errors(sheets: list[GradeSheet]) -> list[float]:
    """Every measured onset error, for a median.

    SPEC.md Section 9.3 asks for median onset error rather than a pass rate,
    because the distribution is the interesting part: a system consistently
    thirty seconds late is useful and one that is occasionally an hour out is
    not, and a pass rate against a tolerance hides which it is.
    """
    errors = []
    for sheet in sheets:
        result = sheet.verdict_for("onset")
        if result is not None and result.applies and result.value is not None:
            errors.append(result.value)
    return errors
