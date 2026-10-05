"""The approval service: the only process that may change the target system.

SPEC.md Section 6.10. A separate process holding the only credentials that can
change flagd config or restart containers. It shows a proposal with the raw
evidence rather than the agent's summary, requires a reason, writes a hash-chained
audit record, and executes at most once per proposal.

**The privilege separation is the point, and it is structural.** The agent imports
`firebreak.remediation.proposal` and never this module. `tests/security/` asserts
that, by walking the agent package's imports. A rule that lives only in a review
comment is a rule that holds until somebody is in a hurry.

**Why the evidence is re-run for the reviewer.** SPEC.md Section 6.10 says the
service shows the raw evidence, not the agent's summary. A summary is the agent's
account of what it found, and the whole premise of this project is that such an
account has to be checkable. So the service re-runs each cited record through the
same backend and shows the reviewer what comes back now, with the proposal's own
rationale alongside rather than instead.

**Executing at most once, across restarts.** The idempotency key is the proposal
id, which is a hash of the action and its target. A durable reservation beside the audit
log is committed before execution. A crash with no completion record leaves an
uncertain outcome that cannot execute again without operator reconciliation. A
process-local set would forget across a restart, which is exactly when a human
approves again.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from firebreak.graph.knowledge import RemediationKind, RemediationSpec
from firebreak.remediation.audit import AuditLog, AuditRecord, Decision
from firebreak.remediation.proposal import Proposal

# What an executor is: something that carries out one proposal and either returns
# or raises. Deliberately narrow. Anything wider would let the service pass a
# proposal somewhere that could reinterpret it.
Executor = Callable[[Proposal], str]


class ApprovalError(Exception):
    """A proposal could not be approved or executed."""


class NotAllowedError(ApprovalError):
    """The proposal is not one this system may execute.

    Separate from the other failures because the response is different: a rejected
    proposal is a decision, and this is a proposal that should never have reached
    the queue.
    """


class Outcome(StrEnum):
    """What a call to the service did."""

    EXECUTED = "executed"
    ALREADY_EXECUTED = "already_executed"
    REJECTED = "rejected"
    RECORDED_ONLY = "recorded_only"
    FAILED = "failed"
    EXECUTION_UNCERTAIN = "execution_uncertain"


@dataclass(frozen=True)
class ReviewItem:
    """What a reviewer is shown for one proposal.

    The freshly re-run evidence is a separate field from the agent's rationale, so a
    user interface cannot present one as the other. `rationale` is what the agent
    said; `evidence` is what the backend says now.
    """

    proposal: Proposal
    evidence: tuple[dict[str, object], ...]
    unavailable: tuple[str, ...] = ()

    @property
    def fully_verified(self) -> bool:
        """Whether every cited record could be re-run.

        A reviewer approving a change whose evidence cannot be reproduced is
        approving the agent's word for it, so this is surfaced rather than
        smoothed over.
        """
        return not self.unavailable


@dataclass
class ApprovalService:
    """Approves, rejects and executes proposals, and records all three.

    `executors` maps a remediation kind to the thing that performs it. A kind with
    no executor cannot be executed, which is how `bundle` mode works: SPEC.md
    Section 6.10 grades proposals there and does not execute them, so the service
    runs with no executors and records the decision without acting.
    """

    audit: AuditLog
    allowlist: dict[str, RemediationSpec]
    executors: dict[RemediationKind, Executor] = field(default_factory=dict)
    # Named in every audit record, so a reader can tell a live service from a test
    # or a dry run without inferring it from the absence of executors.
    service_name: str = "approval-service"

    def review(
        self,
        proposal: Proposal,
        fetch_evidence: Callable[[str], dict[str, object] | None],
    ) -> ReviewItem:
        """Everything a reviewer needs, with the evidence re-read now.

        `fetch_evidence` is injected rather than the service holding a backend,
        because the service's one job is holding a credential and the fewer other
        things it holds the smaller the blast radius of a bug in it.
        """
        found = []
        missing = []
        for evidence_id in proposal.evidence_ids:
            record = fetch_evidence(evidence_id)
            if record is None:
                missing.append(evidence_id)
                continue
            found.append(record)
        return ReviewItem(
            proposal=proposal,
            evidence=tuple(found),
            unavailable=tuple(missing),
        )

    def record_proposal(self, proposal: Proposal) -> AuditRecord:
        """Note that a proposal was made, before anybody has decided.

        So the audit log shows proposals nobody acted on. A queue with a proposal
        sitting in it for an hour is a fact about the operation, and a log that only
        recorded decisions would lose it.
        """
        self._check_allowed(proposal)
        return self.audit.append(
            Decision.PROPOSED,
            proposal.id,
            actor=proposal.incident_id,
            detail={"action": proposal.describe(), "remediation": proposal.remediation_id},
        )

    def reject(self, proposal: Proposal, actor: str, reason: str) -> Outcome:
        """Record a rejection. A reason is required.

        SPEC.md Section 6.10 requires one for approval or rejection, and a rejection
        with no reason is the more costly omission: the next person to see the same
        proposal has to work out afresh why it was refused.
        """
        self._require_reason(reason, "rejection")
        self.audit.append(Decision.REJECTED, proposal.id, actor=actor, reason=reason)
        return Outcome.REJECTED

    def approve(
        self,
        proposal: Proposal,
        actor: str,
        reason: str,
        at: datetime | None = None,
    ) -> Outcome:
        """Approve and execute, at most once per proposal.

        Returns `ALREADY_EXECUTED` rather than raising for a second approval, because
        a second approval is a normal thing for two on-call engineers to do and is
        not an error. The approval is still recorded: two people approving is
        information worth keeping.
        """
        self._check_allowed(proposal)
        self._require_reason(reason, "approval")

        already = self.audit.was_executed(proposal.id)
        self.audit.append(
            Decision.APPROVED,
            proposal.id,
            actor=actor,
            reason=reason,
            detail={"action": proposal.describe(), "already_executed": already},
            at=at,
        )
        if already:
            return Outcome.ALREADY_EXECUTED

        executor = self.executors.get(proposal.kind)
        if executor is None:
            # SPEC.md Section 6.10: in bundle mode proposals are graded, not
            # executed. Recorded rather than silently skipped, so a report cannot
            # claim a change was made.
            self.audit.append(
                Decision.FAILED,
                proposal.id,
                actor=self.service_name,
                reason="no executor is configured for this kind, so nothing was changed",
                detail={"kind": proposal.kind.value},
                at=at,
            )
            return Outcome.RECORDED_ONLY

        if not self.audit.reserve_execution(proposal.id):
            return (
                Outcome.ALREADY_EXECUTED
                if self.audit.was_executed(proposal.id)
                else Outcome.EXECUTION_UNCERTAIN
            )

        try:
            detail = executor(proposal)
        except Exception as error:
            # Recorded before re-raising, because an execution that failed halfway
            # is the case where the audit log matters most.
            self.audit.append(
                Decision.FAILED,
                proposal.id,
                actor=self.service_name,
                reason=str(error)[:500],
                detail={"action": proposal.describe()},
                at=at,
            )
            return Outcome.FAILED

        self.audit.append(
            Decision.EXECUTED,
            proposal.id,
            actor=self.service_name,
            reason=f"approved by {actor}",
            detail={"action": proposal.describe(), "result": detail},
            at=at,
        )
        return Outcome.EXECUTED

    def _check_allowed(self, proposal: Proposal) -> None:
        """Refuse anything the allowlist does not contain, or that it contradicts.

        Checked here as well as when the proposal was built. A service that trusted
        its input would be a service whose safety depended on every caller, and this
        one is the last thing between a proposal and a running system.
        """
        spec = self.allowlist.get(proposal.remediation_id)
        if spec is None:
            raise NotAllowedError(
                f"{proposal.remediation_id!r} is not in the remediation allowlist; "
                "the proposal was built against a different allowlist or was forged"
            )
        if spec.kind is not proposal.kind:
            raise NotAllowedError(
                f"{proposal.remediation_id} is a {spec.kind.value} in the allowlist and "
                f"the proposal says {proposal.kind.value}"
            )
        if not spec.reversible and not spec.requires_approval:
            # Unreachable through `RemediationSpec`, which refuses to validate. Kept
            # because this is the last check before a change nobody can undo, and a
            # defence that depends on one validator elsewhere is one edit from gone.
            raise NotAllowedError(
                f"{proposal.remediation_id} is irreversible and does not require "
                "approval, which is a combination this system refuses"
            )

    @staticmethod
    def _require_reason(reason: str, what: str) -> None:
        if not reason.strip():
            raise ApprovalError(f"a {what} needs a reason; SPEC.md Section 6.10 requires one")


def open_service(
    audit_path: Path,
    allowlist: dict[str, RemediationSpec],
    executors: dict[RemediationKind, Executor] | None = None,
) -> ApprovalService:
    """An approval service writing to one audit log.

    A function rather than a constructor call at the call site, so the audit path is
    the first thing anybody reading the wiring sees. A service with no audit log is
    not a thing this module can produce.
    """
    return ApprovalService(
        audit=AuditLog(audit_path),
        allowlist=allowlist,
        executors=executors or {},
    )
