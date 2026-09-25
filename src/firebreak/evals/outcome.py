"""What an investigation concluded, in one shape every configuration produces.

The graders score an outcome, not a path (SPEC.md Section 9.2), so there has
to be one definition of what an outcome is. B0 produces one today; the agent
will produce one in Phase 6; the ablations in Section 9.5 produce one each.
If any of them produced a slightly different shape, the graders would score
them by slightly different rules and the comparison between configurations
would be measuring the shape rather than the system.

This is the Phase 3 lesson stated as a design: the recurring failure in this
codebase is two components agreeing on a type and disagreeing on a
vocabulary, so the vocabulary is declared once, here.

**A claim not made is not a claim that is wrong.** Every field an outcome can
leave unstated is optional, and the graders report "not claimed" separately
from "claimed and incorrect". B0 names a service and makes no claim about the
mechanism, which is a fair description of classic automated triage rather
than a failure; scoring it as zero percent on fault class would be a
different and false statement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class RemediationProposal:
    """An action an investigation proposed, if it proposed one."""

    remediation_id: str
    target_service: str | None = None
    # The flag a `disable_feature_flag` action would return to its previous
    # value. Named here rather than inferred from the id, because the grader
    # in SPEC.md Section 9.2 checks the action matches "turning off the right
    # flag" and needs the flag to compare.
    target_flag: str | None = None


@dataclass(frozen=True)
class InvestigationOutcome:
    """The conclusion of one trial, and everything a grader needs.

    Deliberately holds no transcript and no tool call log. Those belong to a
    trial record, and mixing them in here would invite a grader to score the
    path, which SPEC.md Section 9.2 specifically warns against: a valid but
    unexpected investigation should not be penalised for taking an unexpected
    route.
    """

    # The opaque bundle this investigated. Not the scenario, which would name
    # the fault.
    bundle_id: str

    # The service blamed, or None when the investigation declined to blame
    # anybody. None is a real answer and on a no-fault task it is the correct
    # one, so it is never treated as a missing value.
    root_cause_service: str | None = None

    # Suspects in order, best first, for the top-3 metric. An abstaining
    # outcome may still carry candidates: "here is what I looked at, and none
    # of it convinced me" is more useful than silence.
    ranked_candidates: tuple[str, ...] = ()

    abstained: bool = False

    # Claims that may simply not be made. See the module docstring.
    fault_class: str | None = None
    fault_onset: datetime | None = None
    confidence: float | None = None

    cited_evidence: tuple[str, ...] = ()
    remediation: RemediationProposal | None = None

    # Cost and speed, for SPEC.md Section 9.3 metric 6. Carried on the
    # outcome because every configuration has them and a report that compares
    # quality without cost is half an answer.
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    wall_clock_seconds: float = 0.0

    # Free-form notes a report writer may show. Never graded.
    notes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")
        # An outcome that abstains and also names a culprit is incoherent, and
        # letting it through would make every downstream metric ambiguous:
        # the abstention grader and the root cause grader would disagree about
        # what the system said.
        if self.abstained and self.root_cause_service is not None:
            raise ValueError(
                f"an abstaining outcome cannot also name {self.root_cause_service!r} "
                "as the root cause"
            )

    @property
    def named_a_service(self) -> bool:
        return self.root_cause_service is not None

    def rank_of(self, service: str) -> int | None:
        """Where a service placed, one-based, or None if it is not ranked."""
        for position, candidate in enumerate(self.ranked_candidates, start=1):
            if candidate == service:
                return position
        return None
