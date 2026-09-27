"""The durable part: propose, wait for a person, execute, check it worked.

SPEC.md Section 6.10. This is a LangGraph `StateGraph` with a checkpointer, and it
is the only part of Firebreak that is one. ADR-0008 argued the investigation loop
did not need the abstraction and said this is where that changes, so this module is
that argument being kept.

**Why here and only here.** A checkpointer earns its cost when execution stops and
resumes in a different process, which is exactly what waiting for a human approval
is. The investigation loop runs to completion in one process, so expressing it as a
graph would be the abstraction's cost with none of its benefit. Waiting for an
approval can take an hour, and the process holding that wait must be restartable
without losing the proposal.

**What `interrupt` actually does here, confirmed against langgraph rather than
assumed.** `interrupt(payload)` stops the graph, checkpoints the state, and surfaces
the payload through `get_state(config).interrupts`. `invoke(Command(resume=value))`
on the same thread id continues from that point with `interrupt` returning `value`.
The thread id is the proposal id, so resuming the wrong approval is not expressible.

**The graph holds no credential.** The executor is passed in when the workflow is
built, so a workflow constructed without one can propose and wait and never change
anything, which is what `bundle` mode is. The approval service is still what
executes and still what writes the audit chain; this module sequences the steps and
survives a restart between them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from firebreak.remediation.approval import ApprovalService, Outcome
from firebreak.remediation.proposal import Proposal
from firebreak.remediation.recovery import Recovery, RecoveryResult, verify_recovery


class ApprovalState(TypedDict, total=False):
    """What the durable workflow carries between its steps.

    A TypedDict rather than the Pydantic models used elsewhere, because this is what
    LangGraph checkpoints and serialises, and a state schema is the one place where
    matching the framework matters more than matching the rest of the codebase.

    The proposal is stored as `model_dump(mode="json")` rather than `as_dict()`. The
    two are different shapes and using the wrong one failed immediately: `as_dict`
    adds a derived `action` field for display, which the frozen model refuses on the
    way back in. A checkpoint has to round trip exactly, so it carries the
    serialisation shape and the display shape stays for display.
    """

    proposal: dict[str, Any]
    decision: str
    actor: str
    reason: str
    outcome: str
    recovery: dict[str, Any]
    notes: list[str]


# What a human sends back. Two words rather than a boolean, so a resume value that
# arrived truncated or mistyped fails rather than approving.
APPROVE = "approve"
REJECT = "reject"


@dataclass(frozen=True)
class ApprovalDecision:
    """A person's answer, as the workflow expects to receive it."""

    decision: str
    actor: str
    reason: str

    def as_resume(self) -> dict[str, str]:
        """The value to resume the graph with."""
        if self.decision not in {APPROVE, REJECT}:
            raise ValueError(
                f"decision must be {APPROVE!r} or {REJECT!r}, got {self.decision!r}; an "
                "ambiguous answer must not default to approving"
            )
        if not self.reason.strip():
            raise ValueError("SPEC.md Section 6.10 requires a reason for approval or rejection")
        return {"decision": self.decision, "actor": self.actor, "reason": self.reason}


@dataclass
class ApprovalWorkflow:
    """The compiled graph, plus the pieces its nodes need.

    `sample_signal` is what `verify_recovery` polls. Absent means no recovery check
    is possible, which the result records as unknown rather than as a pass: a
    remediation whose effect was never measured has not been shown to work.
    """

    service: ApprovalService
    sample_signal: Callable[[Proposal], float | None] | None = None
    baseline_for: Callable[[Proposal], float | None] | None = None
    checkpointer: BaseCheckpointSaver[Any] | None = None

    def __post_init__(self) -> None:
        self._graph = self._build()

    def _build(self) -> Any:
        builder: StateGraph[ApprovalState, None, ApprovalState, ApprovalState] = StateGraph(
            ApprovalState
        )
        builder.add_node("record_proposal", self._record_proposal)
        builder.add_node("await_approval", self._await_approval)
        builder.add_node("carry_out", self._carry_out)
        builder.add_node("check_recovery", self._check_recovery)
        builder.add_edge("__start__", "record_proposal")
        builder.add_edge("record_proposal", "await_approval")
        builder.add_edge("await_approval", "carry_out")
        builder.add_edge("carry_out", "check_recovery")
        builder.add_edge("check_recovery", END)
        return builder.compile(checkpointer=self.checkpointer or InMemorySaver())

    def _record_proposal(self, state: ApprovalState) -> ApprovalState:
        proposal = Proposal.model_validate(state["proposal"])
        record = self.service.record_proposal(proposal)
        return {"notes": [f"proposal recorded in the audit chain at sequence {record.sequence}"]}

    def _await_approval(self, state: ApprovalState) -> ApprovalState:
        """Stop, checkpoint, and wait for a person.

        The payload is what a reviewer is shown, so it carries the action and the
        blast radius rather than the whole proposal: an interrupt payload is a
        prompt for a decision, and burying the action in a field list is how a
        reviewer approves the wrong thing.
        """
        proposal = Proposal.model_validate(state["proposal"])
        answer = interrupt(
            {
                "proposal_id": proposal.id,
                "action": proposal.describe(),
                "blast_radius": proposal.blast_radius,
                "reversible": proposal.reversible,
                "evidence_ids": list(proposal.evidence_ids),
                "rationale": proposal.rationale,
            }
        )
        if not isinstance(answer, dict):
            raise ValueError(
                f"an approval must be resumed with a mapping, got {type(answer).__name__}; "
                "an unrecognised answer must not default to approving"
            )
        decision = str(answer.get("decision", ""))
        if decision not in {APPROVE, REJECT}:
            raise ValueError(
                f"decision must be {APPROVE!r} or {REJECT!r}, got {decision!r}; an "
                "ambiguous answer must not default to approving"
            )
        return {
            "decision": decision,
            "actor": str(answer.get("actor", "")),
            "reason": str(answer.get("reason", "")),
        }

    def _carry_out(self, state: ApprovalState) -> ApprovalState:
        proposal = Proposal.model_validate(state["proposal"])
        actor = state.get("actor") or "unknown"
        reason = state.get("reason") or ""
        if state.get("decision") == REJECT:
            outcome = self.service.reject(proposal, actor=actor, reason=reason)
        else:
            outcome = self.service.approve(proposal, actor=actor, reason=reason)
        return {"outcome": outcome.value}

    def _check_recovery(self, state: ApprovalState) -> ApprovalState:
        """Watch the alerting signal, but only if something actually changed.

        A rejected proposal changed nothing, so there is nothing to recover from, and
        running the watch would spend five minutes producing a verdict about a
        remediation that never happened.
        """
        proposal = Proposal.model_validate(state["proposal"])
        outcome = state.get("outcome")
        if outcome != Outcome.EXECUTED.value:
            return {
                "recovery": RecoveryResult(
                    outcome=Recovery.UNKNOWN,
                    baseline=None,
                    samples=(),
                    waited_seconds=0.0,
                    detail=f"nothing was executed ({outcome}), so there is nothing to verify",
                ).as_dict()
            }
        if self.sample_signal is None:
            return {
                "recovery": RecoveryResult(
                    outcome=Recovery.UNKNOWN,
                    baseline=None,
                    samples=(),
                    waited_seconds=0.0,
                    detail="no signal was configured to watch, so recovery is unknown",
                ).as_dict()
            }
        baseline = self.baseline_for(proposal) if self.baseline_for else None
        result = verify_recovery(
            baseline=baseline,
            sample=lambda: self.sample_signal(proposal) if self.sample_signal else None,
        )
        return {"recovery": result.as_dict()}

    def start(self, proposal: Proposal) -> dict[str, Any]:
        """Record the proposal and run until it needs a person.

        The thread id is the proposal id, so a resume cannot be applied to a
        different approval. That is the same deterministic key the idempotency check
        uses, which means one action on one target in one incident has exactly one
        durable conversation about it.
        """
        return dict(
            self._graph.invoke(
                {"proposal": proposal.model_dump(mode="json"), "notes": []},
                config=self._config(proposal.id),
            )
        )

    def pending(self, proposal_id: str) -> dict[str, Any] | None:
        """What the waiting approval is asking, or None if it is not waiting."""
        snapshot = self._graph.get_state(self._config(proposal_id))
        if not snapshot.interrupts:
            return None
        value = snapshot.interrupts[0].value
        return dict(value) if isinstance(value, dict) else {"payload": value}

    def resume(self, proposal_id: str, decision: ApprovalDecision) -> dict[str, Any]:
        """Continue from the interrupt with a person's answer."""
        return dict(
            self._graph.invoke(
                Command(resume=decision.as_resume()),
                config=self._config(proposal_id),
            )
        )

    @staticmethod
    def _config(proposal_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": proposal_id}}
