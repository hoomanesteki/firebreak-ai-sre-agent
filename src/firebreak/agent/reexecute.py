"""Re-running cited evidence, which is what makes the exit gate more than a lint.

SPEC.md Section 6.9 check 2. Checks 1, 3 and 5 are about internal consistency: a
claim citing something, a number matching a fact, a confidence backed by enough
signals. All three are satisfied by a report that cites evidence which no longer
says what it said, or never said it. Re-running each cited record and comparing
the hash is the only check that catches that.

**Why this is not in `gates.py`.** The gate calls nothing: no model, no backend,
no clock. That is what makes it testable in a few microseconds and impossible to
mislead. So the re-run happens here, and the gate is handed the results.

**A fresh context, not the investigation's.** The re-run uses its own evidence
store and its own call counts. Sharing them would spend the investigation's
per-tool cap on verification, so a long investigation would fail its own gate
for having looked too hard, and the fresh records would collide with the
originals in the store they are being compared against.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from firebreak.tools.base import ToolContext, ToolError, ToolRegistry
from firebreak.tools.evidence import EvidenceRecord


@dataclass(frozen=True)
class ReExecution:
    """What came back when the cited evidence was asked for again.

    `records` is keyed by the original id, which is what the gate looks up.
    `notes` says why anything is absent, because "did not re-run" and "could not
    be re-run" are different failures and a reader needs to know which.
    """

    records: dict[str, EvidenceRecord] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def verified(self) -> int:
        return len(self.records)


def re_execute(
    registry: ToolRegistry,
    context: ToolContext,
    evidence: dict[str, EvidenceRecord],
    refs: tuple[str, ...],
) -> ReExecution:
    """Ask each cited question again, through the tool that answered it.

    `context` must be a fresh one over the same backend. The records come back
    keyed by the id they are being compared against, so a re-run that produced a
    different id is absent rather than mismatched: a different id means the
    arguments did not reproduce the question, which is a worse failure than a
    changed answer and would be hidden by comparing hashes.
    """
    records: dict[str, EvidenceRecord] = {}
    notes: list[str] = []
    for ref in refs:
        original = evidence.get(ref)
        if original is None:
            notes.append(f"{ref} is not in the evidence store, so there is nothing to re-run")
            continue
        if original.tool is None:
            # Built outside the registry, so nothing recorded which tool to ask.
            # Not silently passed: the gate reports it as unverified.
            notes.append(f"{ref} records no tool, so it cannot be re-run")
            continue
        try:
            result = registry.call(original.tool, context, original.arguments)
        except ToolError as error:
            notes.append(f"re-running {ref} through {original.tool} failed: {error}")
            continue
        if result.evidence_id is None:
            notes.append(f"re-running {ref} through {original.tool} recorded no evidence")
            continue
        if result.evidence_id != ref:
            notes.append(
                f"re-running {ref} through {original.tool} asked a different question "
                f"and produced {result.evidence_id}"
            )
            continue
        fresh = context.evidence.get(result.evidence_id)
        if fresh is None:
            notes.append(f"re-running {ref} recorded nothing under {result.evidence_id}")
            continue
        records[ref] = fresh
    return ReExecution(records=records, notes=tuple(notes))
