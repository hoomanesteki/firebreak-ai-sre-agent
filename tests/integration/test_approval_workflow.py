"""The durable approval workflow: propose, wait for a person, execute, verify.

SPEC.md Section 6.10, and the interrupt that ADR-0008 deferred from Phase 6 to this
phase. The property worth most of these tests is that nothing changes while the
workflow is waiting, and that an answer which is not clearly an approval does not
become one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from firebreak.graph.knowledge import RemediationKind, RemediationSpec
from firebreak.remediation.approval import Outcome, open_service
from firebreak.remediation.audit import Decision, verify_chain
from firebreak.remediation.proposal import Proposal, build_proposal
from firebreak.remediation.workflow import (
    APPROVE,
    REJECT,
    ApprovalDecision,
    ApprovalWorkflow,
)

FLAG = RemediationSpec(
    id="disable-flag",
    kind=RemediationKind.DISABLE_FEATURE_FLAG,
    title="Turn the flag off",
    description="Return the flag to its resting variant",
    blast_radius="one feature flag, reversible in one call",
    reversible=True,
    requires_approval=True,
)
ALLOWLIST = {FLAG.id: FLAG}


def a_proposal() -> Proposal:
    return build_proposal(
        incident_id="inc_000000000000",
        remediation_id=FLAG.id,
        target="paymentFailure",
        rationale="payment errors are 40x baseline and this flag is on",
        evidence_ids=("ev_metric_000000000001",),
        allowlist=ALLOWLIST,
        variant="off",
    )


@pytest.fixture
def executed() -> list[str]:
    return []


@pytest.fixture
def workflow(tmp_path: Path, executed: list[str]) -> ApprovalWorkflow:
    service = open_service(
        tmp_path / "audit.jsonl",
        ALLOWLIST,
        executors={
            RemediationKind.DISABLE_FEATURE_FLAG: lambda proposal: (
                executed.append(proposal.id) or "flag set"  # type: ignore[func-returns-value]
            )
        },
    )
    samples = iter([0.4, 0.01, 0.01, 0.01, 0.01])
    return ApprovalWorkflow(
        service=service,
        sample_signal=lambda _: next(samples, 0.01),
        baseline_for=lambda _: 0.01,
    )


class TestNothingHappensWhileItWaits:
    def test_starting_records_the_proposal_and_stops(
        self, workflow: ApprovalWorkflow, executed: list[str]
    ) -> None:
        proposal = a_proposal()
        workflow.start(proposal)
        assert workflow.pending(proposal.id) is not None
        assert executed == [], "nothing may change before a person has answered"

    def test_the_proposal_is_in_the_audit_chain_before_any_decision(
        self, workflow: ApprovalWorkflow
    ) -> None:
        """So the log shows proposals nobody acted on. A queue with a proposal
        sitting in it for an hour is a fact about the operation."""
        proposal = a_proposal()
        workflow.start(proposal)
        decisions = [r.decision for r in workflow.service.audit.decisions_for(proposal.id)]
        assert decisions == [Decision.PROPOSED]

    def test_the_reviewer_is_shown_the_action_and_the_blast_radius(
        self, workflow: ApprovalWorkflow
    ) -> None:
        """An interrupt payload is a prompt for a decision. Burying the action in a
        field list is how a reviewer approves the wrong thing."""
        proposal = a_proposal()
        workflow.start(proposal)
        asked = workflow.pending(proposal.id)
        assert asked is not None
        assert asked["action"] == "set flag paymentFailure to off"
        assert asked["blast_radius"] == FLAG.blast_radius
        assert asked["reversible"] is True
        assert asked["evidence_ids"] == ["ev_metric_000000000001"]

    def test_a_workflow_that_was_never_started_has_nothing_pending(
        self, workflow: ApprovalWorkflow
    ) -> None:
        assert workflow.pending("rem_000000000000") is None


class TestApproving:
    def test_it_executes_once_and_verifies_recovery(
        self, workflow: ApprovalWorkflow, executed: list[str]
    ) -> None:
        proposal = a_proposal()
        workflow.start(proposal)
        final = workflow.resume(
            proposal.id, ApprovalDecision(APPROVE, "alice", "confirmed from the metrics")
        )
        assert final["outcome"] == Outcome.EXECUTED.value
        assert executed == [proposal.id]
        assert final["recovery"]["outcome"] == "recovered"

    def test_the_audit_chain_records_the_whole_sequence(self, workflow: ApprovalWorkflow) -> None:
        proposal = a_proposal()
        workflow.start(proposal)
        workflow.resume(proposal.id, ApprovalDecision(APPROVE, "alice", "confirmed"))
        decisions = [r.decision for r in workflow.service.audit.records()]
        assert decisions == [Decision.PROPOSED, Decision.APPROVED, Decision.EXECUTED]
        assert verify_chain(workflow.service.audit.records()) is None

    def test_the_approver_is_named_in_the_record(self, workflow: ApprovalWorkflow) -> None:
        proposal = a_proposal()
        workflow.start(proposal)
        workflow.resume(proposal.id, ApprovalDecision(APPROVE, "alice", "confirmed"))
        approved = next(
            r for r in workflow.service.audit.records() if r.decision is Decision.APPROVED
        )
        assert approved.actor == "alice"
        assert approved.reason == "confirmed"


class TestRejecting:
    def test_it_changes_nothing(self, workflow: ApprovalWorkflow, executed: list[str]) -> None:
        proposal = a_proposal()
        workflow.start(proposal)
        final = workflow.resume(
            proposal.id, ApprovalDecision(REJECT, "bob", "wrong service, cart is the cause")
        )
        assert final["outcome"] == Outcome.REJECTED.value
        assert executed == []

    def test_no_recovery_is_claimed_for_something_that_did_not_happen(
        self, workflow: ApprovalWorkflow
    ) -> None:
        """Running the watch would spend five minutes producing a verdict about a
        remediation that never happened."""
        proposal = a_proposal()
        workflow.start(proposal)
        final = workflow.resume(proposal.id, ApprovalDecision(REJECT, "bob", "wrong service"))
        assert final["recovery"]["outcome"] == "unknown"
        assert "nothing was executed" in final["recovery"]["detail"]

    def test_the_rejection_and_its_reason_are_recorded(self, workflow: ApprovalWorkflow) -> None:
        proposal = a_proposal()
        workflow.start(proposal)
        workflow.resume(proposal.id, ApprovalDecision(REJECT, "bob", "cart is the cause"))
        rejected = next(
            r for r in workflow.service.audit.records() if r.decision is Decision.REJECTED
        )
        assert rejected.reason == "cart is the cause"


class TestAnUnclearAnswerDoesNotApprove:
    """The failure mode with the worst consequence: a malformed resume that defaults
    to yes."""

    def test_an_unrecognised_decision_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must not default to approving"):
            ApprovalDecision("maybe", "alice", "unsure").as_resume()

    def test_an_empty_decision_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must not default to approving"):
            ApprovalDecision("", "alice", "a reason").as_resume()

    def test_a_truncated_decision_is_refused(self) -> None:
        """ "appro" is not "approve", and a prefix match would be a way in."""
        with pytest.raises(ValueError, match="must not default to approving"):
            ApprovalDecision("appro", "alice", "a reason").as_resume()

    def test_an_approval_with_no_reason_is_refused(self) -> None:
        with pytest.raises(ValueError, match="requires a reason"):
            ApprovalDecision(APPROVE, "alice", "   ").as_resume()

    def test_the_node_refuses_a_non_mapping_resume(
        self, workflow: ApprovalWorkflow, executed: list[str]
    ) -> None:
        """The graph is resumed by whatever calls it, so the check has to be in the
        node and not only in the helper that builds the value."""
        from langgraph.types import Command

        proposal = a_proposal()
        workflow.start(proposal)
        with pytest.raises(ValueError, match="must be resumed with a mapping"):
            workflow._graph.invoke(
                Command(resume="yes please"),
                config={"configurable": {"thread_id": proposal.id}},
            )
        assert executed == []


class TestItSurvivesARestart:
    def test_a_second_approval_after_a_restart_executes_nothing(
        self, tmp_path: Path, executed: list[str]
    ) -> None:
        """The idempotency state is the audit log, not the process. A restart is
        exactly when a human approves again."""
        audit = tmp_path / "audit.jsonl"

        def build() -> ApprovalWorkflow:
            service = open_service(
                audit,
                ALLOWLIST,
                executors={
                    RemediationKind.DISABLE_FEATURE_FLAG: lambda p: (
                        executed.append(p.id) or "flag set"  # type: ignore[func-returns-value]
                    )
                },
            )
            return ApprovalWorkflow(service=service)

        proposal = a_proposal()
        first = build()
        first.start(proposal)
        first.resume(proposal.id, ApprovalDecision(APPROVE, "alice", "confirmed"))
        assert executed == [proposal.id]

        # A new process: a new workflow, a new service, a new in-memory checkpointer,
        # over the same audit log.
        second = build()
        second.start(proposal)
        outcome = second.resume(proposal.id, ApprovalDecision(APPROVE, "bob", "again"))
        assert outcome["outcome"] == Outcome.ALREADY_EXECUTED.value
        assert executed == [proposal.id]


class TestWithoutAnExecutor:
    def test_it_records_the_decision_and_changes_nothing(self, tmp_path: Path) -> None:
        """Bundle mode. SPEC.md Section 6.10 grades proposals there rather than
        executing them."""
        service = open_service(tmp_path / "audit.jsonl", ALLOWLIST)
        workflow = ApprovalWorkflow(service=service)
        proposal = a_proposal()
        workflow.start(proposal)
        final = workflow.resume(proposal.id, ApprovalDecision(APPROVE, "alice", "confirmed"))
        assert final["outcome"] == Outcome.RECORDED_ONLY.value
        decisions = [r.decision for r in service.audit.records()]
        assert Decision.EXECUTED not in decisions

    def test_recovery_is_unknown_rather_than_a_pass(self, tmp_path: Path) -> None:
        service = open_service(tmp_path / "audit.jsonl", ALLOWLIST)
        workflow = ApprovalWorkflow(service=service)
        proposal = a_proposal()
        workflow.start(proposal)
        final = workflow.resume(proposal.id, ApprovalDecision(APPROVE, "alice", "confirmed"))
        assert final["recovery"]["outcome"] == "unknown"


class TestTheCheckpointRoundTrips:
    def test_the_proposal_can_be_read_back_out_of_the_state(
        self, workflow: ApprovalWorkflow
    ) -> None:
        """This failed on the first attempt: the state carried `as_dict()`, which adds
        a derived `action` field the frozen model refuses on the way back in. A
        checkpoint that cannot round trip cannot survive the restart it exists for.
        """
        proposal = a_proposal()
        state = workflow.start(proposal)
        assert Proposal.model_validate(state["proposal"]) == proposal

    def test_the_thread_is_the_proposal_id_so_a_resume_cannot_be_misapplied(
        self, workflow: ApprovalWorkflow, executed: list[str]
    ) -> None:
        """One action on one target in one incident has exactly one durable
        conversation about it."""
        first = a_proposal()
        other = build_proposal(
            incident_id="inc_000000000000",
            remediation_id=FLAG.id,
            target="cartFailure",
            rationale="a different flag entirely",
            evidence_ids=("ev_metric_000000000002",),
            allowlist=ALLOWLIST,
            variant="off",
        )
        assert first.id != other.id
        workflow.start(first)
        workflow.start(other)
        workflow.resume(other.id, ApprovalDecision(APPROVE, "alice", "cart it is"))
        assert executed == [other.id]
        assert workflow.pending(first.id) is not None, "the other approval still waits"
