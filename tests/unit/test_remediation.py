"""Tests for proposals, the audit chain, and the approval service.

CLAUDE.md requires tests first for approvals. SPEC.md Section 17 Phase 9 names two
acceptance criteria that live here: double approval executes once, and the audit is
complete. The third, that agent processes hold no write credentials, is in
`tests/security/`.

The failure this whole area guards against is a language model causing a production
change. Every test below is about one of the three things standing in the way: the
allowlist, the separation of proposing from executing, and the record of what
happened.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from firebreak.graph.knowledge import RemediationKind, RemediationSpec
from firebreak.remediation.approval import (
    ApprovalError,
    NotAllowedError,
    Outcome,
    open_service,
)
from firebreak.remediation.audit import (
    GENESIS,
    AuditError,
    AuditLog,
    AuditRecord,
    Decision,
    verify_chain,
)
from firebreak.remediation.proposal import (
    ProposalError,
    TargetKind,
    build_proposal,
    proposal_id,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
EVIDENCE = ("ev_metric_000000000001",)


def test_crash_after_side_effect_cannot_execute_again(tmp_path, monkeypatch):
    built = proposal()
    executed = []
    executors = {built.kind: lambda p: executed.append(p.id) or "done"}
    service = open_service(tmp_path / "audit.jsonl", allowlist(), executors)
    append = service.audit.append

    def crash(decision, *args, **kwargs):
        if decision is Decision.EXECUTED:
            raise OSError("interrupted before audit persistence")
        return append(decision, *args, **kwargs)

    monkeypatch.setattr(service.audit, "append", crash)
    with pytest.raises(OSError):
        service.approve(built, "owner", "fix incident")
    restarted = open_service(tmp_path / "audit.jsonl", allowlist(), executors)
    outcome = restarted.approve(built, "owner", "retry")
    assert len(executed) == 1
    assert outcome.value == "execution_uncertain"


def test_concurrent_approvals_reserve_one_execution(tmp_path):
    import time
    from concurrent.futures import ThreadPoolExecutor

    built = proposal()
    executed = []

    def execute(p):
        executed.append(p.id)
        time.sleep(0.05)
        return "done"

    def approve(_):
        service = open_service(tmp_path / "audit.jsonl", allowlist(), {built.kind: execute})
        return service.approve(built, "owner", "fix incident")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(approve, range(2)))
    assert len(executed) == 1
    assert verify_chain(AuditLog(tmp_path / "audit.jsonl").records()) is None


def spec(
    remediation_id: str = "disable-flag",
    kind: RemediationKind = RemediationKind.DISABLE_FEATURE_FLAG,
    reversible: bool = True,
    requires_approval: bool = True,
) -> RemediationSpec:
    return RemediationSpec(
        id=remediation_id,
        kind=kind,
        title="a title",
        description="a description",
        blast_radius="one flag",
        reversible=reversible,
        requires_approval=requires_approval,
    )


def allowlist(*specs: RemediationSpec) -> dict[str, RemediationSpec]:
    return {item.id: item for item in (specs or (spec(),))}


def proposal(**overrides):  # type: ignore[no-untyped-def]
    fields = {
        "incident_id": "inc_000000000000",
        "remediation_id": "disable-flag",
        "target": "paymentFailure",
        "variant": "off",
        "rationale": "payment is failing and this flag is why",
        "evidence_ids": EVIDENCE,
        "allowlist": allowlist(),
        "now": NOW,
    }
    fields.update(overrides)
    return build_proposal(**fields)  # type: ignore[arg-type]


class TestAProposalCannotExecuteItself:
    """The structural half of the privilege boundary.

    A tool that performed the action and asked permission first would put the
    credential and the decision in one process, and then the only thing between a
    model and a production change is a conditional. A conditional is exactly what an
    injected instruction is good at talking past.
    """

    def test_the_class_defines_only_read_only_methods(self) -> None:
        """Inspected on the class rather than the instance, so this asserts what
        `Proposal` adds rather than what Pydantic provides."""
        from firebreak.remediation.proposal import Proposal

        own = {
            name
            for name, value in vars(Proposal).items()
            if callable(value) and not name.startswith("_")
        }
        assert own == {"describe", "as_dict"}

    def test_it_holds_no_client_credential_or_connection(self) -> None:
        """A proposal is a statement about what should happen. Anything in it that
        could reach the target system would make it a statement that could act."""
        from firebreak.remediation.proposal import Proposal

        forbidden = ("client", "credential", "token", "session", "connection", "executor")
        for field in Proposal.model_fields:
            assert not any(word in field.lower() for word in forbidden), (
                f"Proposal.{field} looks like a way to reach the target system"
            )

    def test_the_proposal_module_imports_nothing_that_can_write(self) -> None:
        """flagd and docker are the two things that can change the target system."""
        source = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "firebreak"
            / "remediation"
            / "proposal.py"
        ).read_text(encoding="utf-8")
        for module in (
            "firebreak.lab.flags",
            "docker",
            "subprocess",
            "firebreak.remediation.approval",
        ):
            assert f"import {module}" not in source, (
                f"proposal.py imports {module}, which can change the target system"
            )


class TestTheAllowlistIsTheOnlyMenu:
    def test_an_unknown_remediation_is_refused(self) -> None:
        """A model that invented an id would otherwise produce a proposal with a
        plausible name and no definition behind it, and the approval service would
        show a person an action nobody had reviewed."""
        with pytest.raises(ProposalError, match="not in the remediation allowlist"):
            proposal(remediation_id="delete-the-database")

    def test_the_error_names_what_is_available(self) -> None:
        with pytest.raises(ProposalError, match="disable-flag"):
            proposal(remediation_id="nope")

    def test_a_proposal_with_no_target_is_refused(self) -> None:
        with pytest.raises(ProposalError, match="needs a target"):
            proposal(target="")

    def test_the_service_checks_the_allowlist_again(self) -> None:
        """A service that trusted its input would have its safety depend on every
        caller, and it is the last thing before a running system."""
        built = proposal()
        service = open_service(Path("unused"), allowlist=allowlist(spec("something-else")))
        with pytest.raises(NotAllowedError, match="not in the remediation allowlist"):
            service.record_proposal(built)

    def test_a_proposal_whose_kind_contradicts_the_allowlist_is_refused(
        self, tmp_path: Path
    ) -> None:
        """The forged-proposal case: the id is real and the action is not."""
        built = proposal()
        service = open_service(
            tmp_path / "audit.jsonl",
            allowlist=allowlist(spec("disable-flag", kind=RemediationKind.RESTART_SERVICE)),
        )
        with pytest.raises(NotAllowedError, match="is a restart_service in the allowlist"):
            service.approve(built, actor="someone", reason="looks right")


class TestTheActionAndItsTargetMustAgree:
    def test_a_flag_action_needs_a_variant(self) -> None:
        """A flag with three variants has no unambiguous off."""
        with pytest.raises(ValueError, match="must name the variant"):
            proposal(variant=None)

    def test_a_restart_has_no_variant(self) -> None:
        with pytest.raises(ValueError, match="has no variant to set"):
            proposal(
                remediation_id="restart",
                allowlist=allowlist(spec("restart", kind=RemediationKind.RESTART_SERVICE)),
                variant="off",
            )

    def test_each_kind_gets_the_target_it_acts_on(self) -> None:
        flag = proposal()
        assert flag.target_kind is TargetKind.FLAG
        restart = proposal(
            remediation_id="restart",
            allowlist=allowlist(spec("restart", kind=RemediationKind.RESTART_SERVICE)),
            variant=None,
        )
        assert restart.target_kind is TargetKind.SERVICE

    def test_paging_a_team_is_not_a_change_to_the_system(self) -> None:
        """Treating it as one would make the approval queue mostly noise, which is
        how a queue stops being read."""
        page = proposal(
            remediation_id="page",
            allowlist=allowlist(spec("page", kind=RemediationKind.PAGE_OWNING_TEAM)),
            variant=None,
            target="payments-team",
        )
        assert not page.changes_the_system
        assert proposal().changes_the_system


class TestAProposalCitesEvidence:
    def test_an_uncited_proposal_is_refused(self) -> None:
        """The exit gate strips uncited claims from a report. A proposal is a
        stronger statement than a claim, so the same rule applies with no repair."""
        with pytest.raises(ValueError, match="cites no evidence"):
            proposal(evidence_ids=())


class TestTheIdIsDeterministic:
    """SPEC.md Section 6.10: deterministic keys, and execution uses the id as the
    idempotency key. Everything about double approval rests on this."""

    def test_the_same_action_on_the_same_target_is_the_same_proposal(self) -> None:
        assert proposal().id == proposal().id

    def test_rewording_the_rationale_does_not_change_the_id(self) -> None:
        """Otherwise a model that reworded would produce a second proposal for the
        same change, and both could be approved."""
        first = proposal(rationale="one wording")
        second = proposal(rationale="a completely different wording")
        assert first.id == second.id

    def test_the_time_does_not_change_the_id(self) -> None:
        """Including it would produce a new proposal once a second."""
        later = datetime(2026, 6, 1, tzinfo=UTC)
        assert proposal(now=NOW).id == proposal(now=later).id

    def test_a_different_target_is_a_different_proposal(self) -> None:
        assert proposal(target="paymentFailure").id != proposal(target="cartFailure").id

    def test_a_different_variant_is_a_different_proposal(self) -> None:
        assert proposal(variant="off").id != proposal(variant="on").id

    def test_a_different_incident_is_a_different_proposal(self) -> None:
        """Turning the same flag off for a different incident gets its own
        approval."""
        assert proposal().id != proposal(incident_id="inc_111111111111").id

    def test_the_helper_and_the_builder_agree(self) -> None:
        built = proposal()
        assert built.id == proposal_id(
            built.incident_id, built.remediation_id, built.target, built.variant
        )


class TestTheAuditChain:
    def test_the_first_record_chains_onto_genesis(self, tmp_path: Path) -> None:
        log = AuditLog(tmp_path / "audit.jsonl")
        record = log.append(Decision.PROPOSED, "rem_000000000000", actor="agent")
        assert record.previous_hash == GENESIS
        assert record.sequence == 0

    def test_each_record_chains_onto_the_one_before(self, tmp_path: Path) -> None:
        log = AuditLog(tmp_path / "audit.jsonl")
        first = log.append(Decision.PROPOSED, "rem_000000000000", actor="agent")
        second = log.append(Decision.APPROVED, "rem_000000000000", actor="a person", reason="yes")
        assert second.previous_hash == first.record_hash
        assert verify_chain(log.records()) is None

    def test_editing_a_record_breaks_the_chain_at_that_record(self, tmp_path: Path) -> None:
        """The property a hash chain buys: an after-the-fact edit is detectable by
        anyone who reads the file, without needing a copy of the original."""
        path = tmp_path / "audit.jsonl"
        log = AuditLog(path)
        log.append(Decision.PROPOSED, "rem_000000000000", actor="agent")
        log.append(Decision.APPROVED, "rem_000000000000", actor="a person", reason="yes")
        log.append(Decision.EXECUTED, "rem_000000000000", actor="service")

        lines = path.read_text().splitlines()
        tampered = AuditRecord.model_validate_json(lines[1]).model_copy(
            update={"reason": "something else entirely"}
        )
        lines[1] = tampered.model_dump_json()
        path.write_text("\n".join(lines) + "\n")

        break_found = verify_chain(log.records())
        assert break_found is not None
        assert break_found.sequence == 1
        assert "edited after it was written" in break_found.problem

    def test_removing_a_record_breaks_the_chain(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        log = AuditLog(path)
        for index in range(3):
            log.append(Decision.PROPOSED, f"rem_00000000000{index}", actor="agent")
        lines = path.read_text().splitlines()
        path.write_text("\n".join([lines[0], lines[2]]) + "\n")
        break_found = verify_chain(log.records())
        assert break_found is not None

    def test_a_reordered_chain_is_detected(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        log = AuditLog(path)
        for index in range(3):
            log.append(Decision.PROPOSED, f"rem_00000000000{index}", actor="agent")
        lines = path.read_text().splitlines()
        path.write_text("\n".join([lines[0], lines[2], lines[1]]) + "\n")
        break_found = verify_chain(log.records())
        assert break_found is not None
        assert break_found.sequence == 2

    def test_a_malformed_line_is_an_error_not_a_skip(self, tmp_path: Path) -> None:
        """Skipping it would let somebody corrupt a record to remove it."""
        path = tmp_path / "audit.jsonl"
        path.write_text('{"not": "a record"}\n')
        with pytest.raises(AuditError, match="is not a record"):
            AuditLog(path).records()

    def test_an_absent_log_has_no_records_rather_than_failing(self, tmp_path: Path) -> None:
        assert AuditLog(tmp_path / "nothing.jsonl").records() == []

    def test_an_empty_chain_verifies(self) -> None:
        assert verify_chain([]) is None

    def test_a_record_cannot_be_built_with_a_wrong_hash_by_accident(self) -> None:
        """`sealed` is the only constructor that computes the hash, so a record whose
        hash does not match its content has to be made deliberately."""
        record = AuditRecord.sealed(
            sequence=0,
            decision=Decision.PROPOSED,
            proposal_id="rem_000000000000",
            actor="agent",
            previous_hash=GENESIS,
        )
        assert record.intact()
        assert not record.model_copy(update={"actor": "somebody else"}).intact()


class TestDoubleApprovalExecutesOnce:
    """SPEC.md Section 17 Phase 9's acceptance criterion, by name."""

    def service(self, tmp_path: Path, executed: list[str]):  # type: ignore[no-untyped-def]
        return open_service(
            tmp_path / "audit.jsonl",
            allowlist=allowlist(),
            executors={
                RemediationKind.DISABLE_FEATURE_FLAG: lambda p: (
                    executed.append(p.id) or "flag set"  # type: ignore[func-returns-value]
                )
            },
        )

    def test_the_second_approval_executes_nothing(self, tmp_path: Path) -> None:
        executed: list[str] = []
        service = self.service(tmp_path, executed)
        built = proposal()

        first = service.approve(built, actor="alice", reason="payment is down")
        second = service.approve(built, actor="bob", reason="agreed, do it")

        assert first is Outcome.EXECUTED
        assert second is Outcome.ALREADY_EXECUTED
        assert executed == [built.id]

    def test_both_approvals_are_recorded(self, tmp_path: Path) -> None:
        """Two people approving is information worth keeping, so the second
        approval is recorded even though it changed nothing."""
        executed: list[str] = []
        service = self.service(tmp_path, executed)
        built = proposal()
        service.approve(built, actor="alice", reason="payment is down")
        service.approve(built, actor="bob", reason="agreed")

        approvals = [
            record
            for record in service.audit.decisions_for(built.id)
            if record.decision is Decision.APPROVED
        ]
        assert [record.actor for record in approvals] == ["alice", "bob"]
        executions = [
            record
            for record in service.audit.decisions_for(built.id)
            if record.decision is Decision.EXECUTED
        ]
        assert len(executions) == 1

    def test_a_restarted_service_still_refuses_to_execute_twice(self, tmp_path: Path) -> None:
        """The state is the audit log, not a process-local set. A restart is exactly
        when a human approves again."""
        executed: list[str] = []
        built = proposal()
        self.service(tmp_path, executed).approve(built, actor="alice", reason="down")
        # A completely new service object over the same log, which is what a restart
        # produces.
        outcome = self.service(tmp_path, executed).approve(built, actor="bob", reason="again")
        assert outcome is Outcome.ALREADY_EXECUTED
        assert len(executed) == 1

    def test_a_rewording_of_the_same_action_does_not_execute_twice(self, tmp_path: Path) -> None:
        """The reason the id excludes the rationale."""
        executed: list[str] = []
        service = self.service(tmp_path, executed)
        service.approve(proposal(rationale="one wording"), actor="alice", reason="down")
        outcome = service.approve(proposal(rationale="another wording"), actor="bob", reason="down")
        assert outcome is Outcome.ALREADY_EXECUTED
        assert len(executed) == 1


class TestApprovalNeedsAReason:
    def test_an_approval_with_no_reason_is_refused(self, tmp_path: Path) -> None:
        service = open_service(tmp_path / "audit.jsonl", allowlist=allowlist())
        with pytest.raises(ApprovalError, match="needs a reason"):
            service.approve(proposal(), actor="alice", reason="")

    def test_whitespace_is_not_a_reason(self, tmp_path: Path) -> None:
        service = open_service(tmp_path / "audit.jsonl", allowlist=allowlist())
        with pytest.raises(ApprovalError, match="needs a reason"):
            service.approve(proposal(), actor="alice", reason="   ")

    def test_a_rejection_with_no_reason_is_refused(self, tmp_path: Path) -> None:
        """The more costly omission: the next person to see the proposal has to work
        out afresh why it was refused."""
        service = open_service(tmp_path / "audit.jsonl", allowlist=allowlist())
        with pytest.raises(ApprovalError, match="needs a reason"):
            service.reject(proposal(), actor="alice", reason="")

    def test_a_rejection_is_recorded_with_its_reason(self, tmp_path: Path) -> None:
        service = open_service(tmp_path / "audit.jsonl", allowlist=allowlist())
        built = proposal()
        assert service.reject(built, actor="alice", reason="wrong service") is Outcome.REJECTED
        records = service.audit.decisions_for(built.id)
        assert records[0].decision is Decision.REJECTED
        assert records[0].reason == "wrong service"


class TestExecutionInBundleMode:
    def test_with_no_executor_nothing_is_changed_and_it_is_recorded(self, tmp_path: Path) -> None:
        """SPEC.md Section 6.10: in bundle mode proposals are graded, not executed.
        Recorded rather than silently skipped, so a report cannot claim a change was
        made."""
        service = open_service(tmp_path / "audit.jsonl", allowlist=allowlist())
        built = proposal()
        assert service.approve(built, actor="alice", reason="down") is Outcome.RECORDED_ONLY
        decisions = [record.decision for record in service.audit.decisions_for(built.id)]
        assert Decision.EXECUTED not in decisions
        assert Decision.FAILED in decisions

    def test_a_recorded_only_approval_does_not_block_a_later_execution(
        self, tmp_path: Path
    ) -> None:
        """Nothing happened, so a service that later gains an executor should still
        be able to act."""
        built = proposal()
        open_service(tmp_path / "audit.jsonl", allowlist=allowlist()).approve(
            built, actor="alice", reason="down"
        )
        executed: list[str] = []
        live = open_service(
            tmp_path / "audit.jsonl",
            allowlist=allowlist(),
            executors={
                RemediationKind.DISABLE_FEATURE_FLAG: lambda p: (
                    executed.append(p.id) or "flag set"  # type: ignore[func-returns-value]
                )
            },
        )
        assert live.approve(built, actor="bob", reason="now for real") is Outcome.EXECUTED
        assert executed == [built.id]


class TestAFailedExecutionIsRecorded:
    def test_the_failure_is_written_before_the_outcome_is_returned(self, tmp_path: Path) -> None:
        """An execution that failed halfway is the case where the audit log matters
        most."""

        def explode(_: object) -> str:
            raise RuntimeError("flagd refused the write")

        service = open_service(
            tmp_path / "audit.jsonl",
            allowlist=allowlist(),
            executors={RemediationKind.DISABLE_FEATURE_FLAG: explode},
        )
        built = proposal()
        assert service.approve(built, actor="alice", reason="down") is Outcome.FAILED
        records = service.audit.decisions_for(built.id)
        assert records[-1].decision is Decision.FAILED
        assert "flagd refused the write" in records[-1].reason

    def test_a_failed_execution_requires_reconciliation(self, tmp_path: Path) -> None:
        """An exception does not prove that the target was unchanged."""
        attempts: list[str] = []

        def sometimes(proposal_object) -> str:  # type: ignore[no-untyped-def]
            attempts.append(proposal_object.id)
            if len(attempts) == 1:
                raise RuntimeError("transient")
            return "flag set"

        service = open_service(
            tmp_path / "audit.jsonl",
            allowlist=allowlist(),
            executors={RemediationKind.DISABLE_FEATURE_FLAG: sometimes},
        )
        built = proposal()
        assert service.approve(built, actor="alice", reason="down") is Outcome.FAILED
        assert service.approve(built, actor="alice", reason="retry") is Outcome.EXECUTION_UNCERTAIN
        assert len(attempts) == 1


class TestTheReviewerSeesRawEvidence:
    def test_the_agent_rationale_and_the_re_read_evidence_are_separate(
        self, tmp_path: Path
    ) -> None:
        """A user interface cannot then present one as the other."""
        service = open_service(tmp_path / "audit.jsonl", allowlist=allowlist())
        built = proposal()
        item = service.review(built, lambda ref: {"id": ref, "rows": [{"score": 9.0}]})
        assert item.proposal.rationale == built.rationale
        assert item.evidence == ({"id": EVIDENCE[0], "rows": [{"score": 9.0}]},)
        assert item.fully_verified

    def test_evidence_that_cannot_be_re_read_is_named(self, tmp_path: Path) -> None:
        """A reviewer approving a change whose evidence cannot be reproduced is
        approving the agent's word for it."""
        service = open_service(tmp_path / "audit.jsonl", allowlist=allowlist())
        item = service.review(proposal(), lambda ref: None)
        assert not item.fully_verified
        assert item.unavailable == EVIDENCE


class TestTheRealAllowlistWorks:
    def test_a_proposal_can_be_built_from_the_shipped_knowledge_files(self) -> None:
        """The allowlist is data, so the one that ships has to be usable."""
        from firebreak.graph.knowledge import load_knowledge

        knowledge = load_knowledge()
        specs = {item.id: item for item in knowledge.remediations}
        page = next(
            item for item in knowledge.remediations if item.kind is RemediationKind.PAGE_OWNING_TEAM
        )
        built = build_proposal(
            incident_id="inc_000000000000",
            remediation_id=page.id,
            target="payments-team",
            rationale="the evidence names a service but not a mechanism",
            evidence_ids=EVIDENCE,
            allowlist=specs,
            now=NOW,
        )
        assert built.kind is RemediationKind.PAGE_OWNING_TEAM
        assert not built.changes_the_system
