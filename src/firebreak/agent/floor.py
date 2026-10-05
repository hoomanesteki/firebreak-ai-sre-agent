"""The deterministic floor: what an on-call engineer gets when the models do not.

SPEC.md Section 6.7. If every LLM tier fails, or the budget runs out before a
report exists, Firebreak publishes the B0 triage report labelled "automated
triage only, no AI analysis".

**Why this is a feature and not an error path.** At three in the morning the
useful behaviour is a ranked list of suspicious services with re-runnable
evidence, which B0 produces without a model at all. The alternative is a stack
trace, and an operator who gets a stack trace once stops opening the tool.

**Why the label is not optional and not a caveat at the bottom.** A report with no
model behind it that looks like one with a model behind it is the single most
misleading thing this system could publish: the claims are weaker, the mechanism
is absent, and the confidence means something different. The label goes in
`Report.notes`, which the exit gate does not check and the reporter model cannot
write, so it cannot be dropped by a model trying to sound confident.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from firebreak.agent.budget import StopReason
from firebreak.agent.state import CitedNumber, Claim, ClaimType, Confidence, Report
from firebreak.triage.report import B0Report, build_b0_report

# The exact wording SPEC.md Section 6.7 requires. A constant because it appears
# in the report, in the console and in the eval's mode field, and three
# paraphrases would let a reader think they were three different situations.
FLOOR_LABEL = "Automated triage only, no AI analysis."


class FloorReason(StrEnum):
    """Why the floor was used, which changes what an operator should do next."""

    # Every model in every tier failed. Retrying later may well work.
    MODELS_UNAVAILABLE = "models_unavailable"
    # The budget ran out before a report existed. Retrying identically will run
    # out again, so the budget or the scope has to change.
    BUDGET_EXHAUSTED = "budget_exhausted"
    # The graph raised before producing a report. A bug, and worth saying so
    # rather than implying the models were down.
    INVESTIGATION_FAILED = "investigation_failed"

    @property
    def guidance(self) -> str:
        """What to do about it, in one sentence for the report."""
        return {
            "models_unavailable": (
                "No model was reachable, so no analysis was attempted. The ranking "
                "below is deterministic and the evidence can be re-run."
            ),
            "budget_exhausted": (
                "The investigation ran out of budget before writing a report. Raising "
                "the budget or narrowing the window would let it finish."
            ),
            "investigation_failed": (
                "The investigation failed before writing a report. This is a defect "
                "rather than a model or budget problem."
            ),
        }[self.value]


@dataclass(frozen=True)
class FloorResult:
    """The floor report and what produced it."""

    report: Report
    reason: FloorReason
    b0: B0Report

    @property
    def labelled(self) -> bool:
        """Whether the published report carries the label.

        Checked rather than assumed, because the label is the whole difference
        between this and a report somebody might act on as if a model had looked.
        """
        return any(FLOOR_LABEL in note for note in self.report.notes)


def floor_reason_for(stop: StopReason | None) -> FloorReason:
    """Which floor reason a budget stop corresponds to.

    Declared here rather than at the call site so the two vocabularies are mapped
    once. A stop reason that is not a budget stop reaches the floor only because
    something failed, which is a different message.
    """
    if stop is not None and stop.is_budget:
        return FloorReason.BUDGET_EXHAUSTED
    return FloorReason.INVESTIGATION_FAILED


def build_floor_report(
    bundle_dir: Path,
    reason: FloorReason,
    verify: bool = False,
    b0: B0Report | None = None,
) -> FloorResult:
    """Publish B0's findings as a labelled report.

    `b0` is accepted so a caller that already ran triage does not run it twice:
    the floor is reached on a bad day, and making that day slower by re-querying
    the whole bundle would be the wrong instinct.

    The claims come from B0's sections and cite B0's evidence, so every number in
    the floor report is as re-runnable as one in a full report. What is missing is
    the mechanism and the ruled-out alternatives, which is exactly what no model
    means.
    """
    report = b0 or build_b0_report(bundle_dir, verify=verify)
    claims = tuple(
        Claim(
            text=f"{fact.subject or report.named_service or 'Signal'}: {fact.field} = "
            f"{fact.value} {fact.unit}.",
            claim_type=ClaimType.MEASUREMENT,
            evidence_ids=(record.id,),
            numbers=(
                CitedNumber(
                    value=fact.value, unit=fact.unit, evidence_id=record.id, field=fact.field
                ),
            ),
        )
        for record in report.evidence.values()
        for fact in record.facts[:1]
    )
    published = Report(
        incident_id=report.bundle_id,
        root_cause_service=report.named_service,
        # Never above low. A deterministic ranking with no analysis behind it is
        # a starting point, and SPEC.md Section 9.4 scores calibration, so a floor
        # report claiming medium confidence would be punished for good reason.
        confidence=None if report.abstained else Confidence.LOW,
        claims=claims,
        notes=(FLOOR_LABEL, reason.guidance),
    )
    return FloorResult(report=published, reason=reason, b0=report)
