"""What an investigation knows, as it learns it.

SPEC.md Section 6.6 defines the state the LangGraph nodes pass between them.
Two properties of the design matter more than the field list.

**A specialist never sees the whole state.** SPEC.md Section 6.6 and [R9] are
explicit: each specialist gets its own tools, a focused brief, and the evidence
ids it needs, never the full transcript. `Brief` is that slice, and it is a
separate type rather than a subset of this one so that passing the wrong thing
is a type error rather than a review comment.

**Nothing here is a claim until it cites evidence.** A `Finding` carries the
evidence ids it rests on, a `Claim` in a report carries them too, and the exit
gate in SPEC.md Section 6.9 refuses a report whose claims cite nothing. The
types make an uncited claim awkward to construct, which is the cheapest place
to stop one.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firebreak.agent.budget import BudgetState, StopReason
from firebreak.tools.evidence import EvidenceRecord
from firebreak.triage.pipeline import TriageResult


class Status(StrEnum):
    """Where an investigation has got to."""

    STARTED = "started"
    TRIAGED = "triaged"
    INVESTIGATING = "investigating"
    CRITIQUING = "critiquing"
    REPORTING = "reporting"
    COMPLETE = "complete"
    ABSTAINED = "abstained"
    FAILED = "failed"


class Confidence(StrEnum):
    """How strongly a finding is held.

    A closed vocabulary rather than a number, because a small model asked for a
    probability produces a number with no calibration behind it. Three levels a
    model can actually distinguish, mapped to numbers once, in one place, where
    the mapping can be calibrated against outcomes.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def as_probability(self) -> float:
        """The number the calibration metrics score.

        Deliberately not 0.0 or 1.0 at the ends. A specialist that has looked
        at one signal is never certain, and a report claiming certainty is the
        claim calibration punishes hardest.
        """
        return {"low": 0.3, "medium": 0.6, "high": 0.85}[self.value]


class Alert(BaseModel):
    """What woke the system up.

    Optional in practice: an investigation can start from a recording with no
    alert at all, which is why `TriageResult` reports whether its window was
    anchored on one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = "unknown"
    service: str | None = None
    fired_at: datetime | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class Hypothesis(BaseModel):
    """A candidate explanation, and what has been found for and against it.

    Support is counted rather than scored by a model. A model asked to weigh
    its own evidence tends to agree with itself, and the point of the critic is
    to make disagreement structural.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    service: str
    statement: str
    supporting_evidence: tuple[str, ...] = ()
    contradicting_evidence: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()

    @property
    def support(self) -> int:
        """Evidence for, minus evidence against.

        A plain difference rather than a ratio, so that a hypothesis with one
        piece of evidence each way scores zero rather than one half, which
        would read as moderate support for something entirely unresolved.
        """
        return len(self.supporting_evidence) - len(self.contradicting_evidence)

    @property
    def is_contested(self) -> bool:
        """Whether anything argues against this.

        SPEC.md Section 6.6 will not let an investigation finish on a
        hypothesis with an unresolved objection, so this is the flag that keeps
        the loop running.
        """
        return bool(self.contradicting_evidence)


class Finding(BaseModel):
    """One specialist's answer about one hypothesis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    specialist: str
    hypothesis_id: str
    summary: str = Field(max_length=600)
    supports: bool
    confidence: Confidence
    evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _a_finding_cites_something(self) -> Finding:
        """A specialist that looked and found nothing still cites the looking.

        The evidence record for a query that returned no rows is exactly how a
        report says "there were no errors on payment" and can be checked. An
        uncited finding is an opinion, and the exit gate would strip it later
        anyway.
        """
        if not self.evidence_ids:
            raise ValueError(
                f"{self.specialist} returned a finding on {self.hypothesis_id} citing "
                "no evidence; a tool call that found nothing still produces a record"
            )
        return self


class Critique(BaseModel):
    """The critic's attempt to refute a hypothesis.

    SPEC.md Section 6.6 gives the critic a different prompt and, where
    configured, a different model family, because a model reviewing its own
    reasoning agrees with it [R8].
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    hypothesis_id: str
    objection: str = Field(max_length=600)
    # An objection the evidence already answers is not an objection. The critic
    # may also ask for a specific check, which is what keeps it from being a
    # veto with no route forward.
    requested_checks: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    resolved: bool = False


class Claim(BaseModel):
    """One sentence of a report, with what it rests on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    evidence_ids: tuple[str, ...] = ()

    @property
    def is_supported(self) -> bool:
        return bool(self.evidence_ids)


class Report(BaseModel):
    """What the investigation concluded, as checkable claims."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str
    root_cause_service: str | None
    fault_class: str | None = None
    fault_onset: datetime | None = None
    confidence: Confidence | None = None
    claims: tuple[Claim, ...] = ()
    # Set when the investigation stopped early. SPEC.md Section 6.6 requires a
    # report produced under a budget stop or a stall to say so and lower its
    # confidence, and a reader has to be able to see which.
    stopped_because: StopReason | None = None

    @property
    def abstained(self) -> bool:
        return self.root_cause_service is None

    @property
    def unsupported_claims(self) -> tuple[Claim, ...]:
        """Claims the exit gate will strip.

        Exposed on the report rather than computed in the gate, so that a node
        writing a report can see what it is about to lose.
        """
        return tuple(claim for claim in self.claims if not claim.is_supported)

    @property
    def cited_evidence(self) -> tuple[str, ...]:
        seen: list[str] = []
        for claim in self.claims:
            for evidence_id in claim.evidence_ids:
                if evidence_id not in seen:
                    seen.append(evidence_id)
        return tuple(seen)


class Brief(BaseModel):
    """What one specialist is told, and nothing more.

    The slice of state a specialist receives. A separate type rather than a
    subset of `InvestigationState` so that handing a specialist the whole
    investigation is a type error rather than something a reviewer has to
    notice.

    SPEC.md Section 6.6 and [R9]: each specialist gets only its own tools, a
    focused brief, and the evidence ids it needs, never the full transcript.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    specialist: str
    hypothesis: Hypothesis
    question: str
    window_start: datetime
    window_end: datetime
    baseline_start: datetime
    baseline_end: datetime
    # Evidence already gathered that this specialist may build on, by id. The
    # records themselves are fetched through `get_evidence`, so a brief stays
    # small however much has been gathered.
    known_evidence_ids: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()


class Notebook(BaseModel):
    """The shared working memory: hypotheses, and what is still open.

    Separate from the evidence store because the two have different lifetimes.
    Evidence is immutable once recorded; a hypothesis changes as the
    investigation learns.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    hypotheses: tuple[Hypothesis, ...] = ()
    open_questions: tuple[str, ...] = ()

    def by_id(self, hypothesis_id: str) -> Hypothesis | None:
        return next((h for h in self.hypotheses if h.id == hypothesis_id), None)

    def ranked(self) -> tuple[Hypothesis, ...]:
        """Hypotheses by support, best first, ties broken by id.

        Deterministic ordering, for the same reason the candidate ranking is:
        a board that reshuffled between identical runs would make pass^3
        measure the shuffling rather than the agent.
        """
        return tuple(sorted(self.hypotheses, key=lambda h: (-h.support, h.id)))

    @property
    def leader(self) -> Hypothesis | None:
        ranked = self.ranked()
        return ranked[0] if ranked else None


class InvestigationState(BaseModel):
    """Everything one investigation knows.

    Mutated by returning a new state from each node, which is how LangGraph
    expects a node to work, with the single exception of `budget`: that is a
    mutable dataclass because every node spends from it, and a node that forgot
    to return it would silently reset the spend.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    incident_id: str
    alert: Alert = Field(default_factory=Alert)
    window_start: datetime | None = None
    window_end: datetime | None = None
    baseline_start: datetime | None = None
    baseline_end: datetime | None = None

    triage: TriageResult | None = None
    notebook: Notebook = Field(default_factory=Notebook)
    evidence: dict[str, EvidenceRecord] = Field(default_factory=dict)
    findings: tuple[Finding, ...] = ()
    critiques: tuple[Critique, ...] = ()
    report: Report | None = None
    budget: BudgetState = Field(default_factory=BudgetState)
    status: Status = Status.STARTED
    stopped_because: StopReason | None = None

    @property
    def unresolved_critiques(self) -> tuple[Critique, ...]:
        """Objections the investigation has not answered.

        SPEC.md Section 6.6 will not let a run finish while one stands, which
        is what stops the loop from agreeing with itself after one round.
        """
        return tuple(c for c in self.critiques if not c.resolved)

    def brief_for(
        self, specialist: str, hypothesis: Hypothesis, question: str, tools: tuple[str, ...]
    ) -> Brief:
        """The slice of state one specialist is allowed to see.

        Raises rather than guessing when the windows are unset, because a
        specialist given no window would query the widest one the backend
        allows and spend the budget on it.
        """
        if (
            self.window_start is None
            or self.window_end is None
            or self.baseline_start is None
            or self.baseline_end is None
        ):
            raise ValueError("cannot brief a specialist before triage has chosen the windows")
        return Brief(
            specialist=specialist,
            hypothesis=hypothesis,
            question=question,
            window_start=self.window_start,
            window_end=self.window_end,
            baseline_start=self.baseline_start,
            baseline_end=self.baseline_end,
            known_evidence_ids=tuple(sorted(self.evidence)),
            allowed_tools=tools,
        )
