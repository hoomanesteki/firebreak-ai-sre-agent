"""The audit chain: every decision, hashed onto the one before it.

SPEC.md Section 6.10 requires the approval service to write a hash-chained audit
record. The property that buys is narrow and worth being precise about.

**What a hash chain gives you.** Each record contains the hash of the previous
record, so changing or removing any record breaks every hash after it. An
after-the-fact edit is therefore detectable by anyone who reads the file, without
needing a copy of the original.

**What it does not give you.** It does not stop somebody who can write the file
from rewriting the whole chain from the point they changed, which is why
`verify_chain` reports the first broken link rather than claiming the file is
authentic. Tamper evidence, not tamper proofing. Signing would give more and needs
a key nobody has yet; the honest thing is to be clear about which one this is.

**Why the executed record is separate from the approved record.** An approval that
was never executed and an approval that was executed are different facts, and the
gap between them is where an executor crash lives. Two records mean the file
answers "was this approved" and "did it actually happen" separately, which is what
somebody reading it after an incident needs.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

# The hash a first record chains onto. A literal rather than an empty string, so a
# file whose first record was deleted does not verify: the new first record would
# still carry the real previous hash and fail against this.
GENESIS = "0" * 64


class AuditError(Exception):
    """The audit log is missing, malformed, or does not verify."""


class Decision(StrEnum):
    """What happened to a proposal.

    `EXECUTED` and `FAILED` are recorded separately from `APPROVED` because an
    approval that was never carried out is a different fact from one that was, and
    the difference is exactly what a reader after an incident is looking for.
    """

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


class AuditRecord(BaseModel):
    """One entry in the chain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(ge=0)
    at: datetime
    decision: Decision
    proposal_id: str
    # Who decided. Free text on purpose: this runs behind whatever authentication
    # the deployment has, and inventing an identity model here would be a fiction
    # the audit log then records as fact.
    actor: str = Field(min_length=1)
    # SPEC.md Section 6.10 requires a reason for approval or rejection. Enforced by
    # the service rather than here, because a `proposed` record has no reason to
    # give and a required-everywhere field would be filled with "n/a".
    reason: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    previous_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    def body(self) -> dict[str, Any]:
        """Everything the hash covers.

        `record_hash` is excluded, because a hash cannot cover itself. Everything
        else is included, so no field can be edited without breaking the chain.
        """
        return {
            "sequence": self.sequence,
            "at": self.at.isoformat(),
            "decision": self.decision.value,
            "proposal_id": self.proposal_id,
            "actor": self.actor,
            "reason": self.reason,
            "detail": self.detail,
            "previous_hash": self.previous_hash,
        }

    def expected_hash(self) -> str:
        return hash_body(self.body())

    def intact(self) -> bool:
        return self.record_hash == self.expected_hash()

    @classmethod
    def sealed(
        cls,
        sequence: int,
        decision: Decision,
        proposal_id: str,
        actor: str,
        previous_hash: str,
        reason: str = "",
        detail: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> Self:
        """Build a record and compute its own hash.

        The only way to make one, so a record whose hash does not match its content
        cannot be constructed by accident.
        """
        draft = {
            "sequence": sequence,
            "at": (at or datetime.now(UTC)).isoformat(),
            "decision": decision.value,
            "proposal_id": proposal_id,
            "actor": actor,
            "reason": reason,
            "detail": detail or {},
            "previous_hash": previous_hash,
        }
        return cls(**draft, record_hash=hash_body(draft))  # type: ignore[arg-type]


def hash_body(body: dict[str, Any]) -> str:
    """Hash one record's content.

    Sorted keys and a fixed separator, so a record re-serialised by a different
    library version hashes the same. Without that, verification would fail for a
    reason that looks exactly like tampering.
    """
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ChainBreak(BaseModel):
    """Where the chain stops verifying, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int
    problem: str


class AuditLog:
    """An append-only hash chain on disk, one JSON record per line.

    JSON Lines rather than one JSON array, for one reason that matters: appending a
    line cannot corrupt the lines before it. Rewriting an array means reading it
    all, and a crash during that write loses the history the file exists to keep.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def records(self) -> list[AuditRecord]:
        """Every record, in order. An empty or absent file has none."""
        if not self._path.is_file():
            return []
        records = []
        for number, line in enumerate(self._path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                records.append(AuditRecord.model_validate_json(line))
            except ValueError as error:
                raise AuditError(
                    f"{self._path.name} line {number} is not a record: {error}"
                ) from error
        return records

    def head_hash(self) -> str:
        """The hash the next record chains onto."""
        records = self.records()
        return records[-1].record_hash if records else GENESIS

    def append(
        self,
        decision: Decision,
        proposal_id: str,
        actor: str,
        reason: str = "",
        detail: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> AuditRecord:
        """Seal a record onto the chain and write it.

        Reads the head before writing, so two processes appending at once produce a
        detectable break rather than a silently forked chain. A single writer is the
        intended deployment; this makes the unintended one visible.
        """
        existing = self.records()
        record = AuditRecord.sealed(
            sequence=len(existing),
            decision=decision,
            proposal_id=proposal_id,
            actor=actor,
            previous_hash=existing[-1].record_hash if existing else GENESIS,
            reason=reason,
            detail=detail,
            at=at,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")
        return record

    def decisions_for(self, proposal_id: str) -> list[AuditRecord]:
        return [record for record in self.records() if record.proposal_id == proposal_id]

    def was_executed(self, proposal_id: str) -> bool:
        """Whether this proposal has already been carried out.

        The idempotency check SPEC.md Section 6.10 requires. Reads the log rather
        than an in-memory set, so a restarted service does not execute an approved
        proposal a second time.
        """
        return any(
            record.decision is Decision.EXECUTED for record in self.decisions_for(proposal_id)
        )


def verify_chain(records: list[AuditRecord]) -> ChainBreak | None:
    """The first broken link, or None when the chain verifies.

    Returns the break rather than a boolean, because "the log was edited" and
    "record 41 was edited" are different amounts of information, and the second is
    what somebody investigating needs.

    This is tamper evidence, not tamper proofing: somebody who can write the file
    can rewrite the chain from the point they changed. What they cannot do is change
    one record and leave the rest verifying.
    """
    previous = GENESIS
    for index, record in enumerate(records):
        if record.sequence != index:
            return ChainBreak(
                sequence=record.sequence,
                problem=f"record at position {index} claims sequence {record.sequence}",
            )
        if record.previous_hash != previous:
            return ChainBreak(
                sequence=record.sequence,
                problem="previous_hash does not match the record before it, so a record "
                "was changed, removed, or inserted",
            )
        if not record.intact():
            return ChainBreak(
                sequence=record.sequence,
                problem="the record's own hash does not match its content, so a field "
                "was edited after it was written",
            )
        previous = record.record_hash
    return None
