"""`propose_remediation`: the only tool that asks for anything to change.

SPEC.md Section 6.10. It writes a proposal and returns its id. It does not execute,
cannot execute, and holds nothing that could.

**Why this is a tool at all rather than a field on the report.** A proposal has to
be an evidence-recorded event with a deterministic id, because the approval service
looks it up, the audit chain references it, and the eval grades it. A field on a
report would be a sentence the reporter model wrote, with no record of when it was
proposed or what it rested on.

**What makes it safe is not in this file.** The allowlist is three entries, the
proposal object holds no credential, and the only process that can execute holds the
only credentials. This tool's own contribution is narrow: it refuses an id the
allowlist does not contain, and it refuses to propose without evidence. Both checks
are repeated in the approval service, because a service whose safety depended on its
callers would not be a boundary.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from firebreak.graph.knowledge import KnowledgeError, load_knowledge
from firebreak.remediation.proposal import ProposalError, build_proposal
from firebreak.tools.base import ToolContext, ToolError, ToolResult, ToolSpec
from firebreak.tools.evidence import (
    NO_WINDOW,
    EvidenceKind,
    Fact,
    build_record,
)

MAX_RATIONALE_CHARS = 600


class ProposeRemediationInput(BaseModel):
    """What the agent must supply to propose anything.

    Every field is required except the variant, and the requirements are the
    argument: a proposal needs an action from the allowlist, a target, a reason, and
    the evidence the reason rests on. An optional evidence list would make an
    uncited proposal a typo away.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    remediation_id: str = Field(min_length=1)
    target: str = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=MAX_RATIONALE_CHARS)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    # Only for a flag action. A flag with three variants has no unambiguous off, so
    # the proposal names the variant it wants rather than leaving the approval
    # service to guess.
    variant: str | None = None


def propose_remediation(context: ToolContext, arguments: ProposeRemediationInput) -> ToolResult:
    """Write a proposal and record it as evidence.

    Recorded as evidence for the same reason every other tool call is: so the exit
    gate can cite it, the approval service can look it up, and the eval can grade
    what was proposed rather than what the report says was proposed.

    The evidence the proposal cites must already be in the store. An agent proposing
    a change on the strength of an evidence id it invented is the exact failure the
    citation rules exist for, and catching it here means the approval service never
    shows a person a proposal resting on nothing.
    """
    try:
        allowlist = {item.id: item for item in load_knowledge().remediations}
    except KnowledgeError as error:
        raise ToolError(f"the remediation allowlist could not be read: {error}") from error

    unknown = [ref for ref in arguments.evidence_ids if ref not in context.evidence]
    if unknown:
        raise ToolError(
            f"cannot propose on evidence this investigation never gathered: {', '.join(unknown)}"
        )

    try:
        proposal = build_proposal(
            incident_id=context.backend.fingerprint().identity,
            remediation_id=arguments.remediation_id,
            target=arguments.target,
            rationale=arguments.rationale,
            evidence_ids=arguments.evidence_ids,
            allowlist=allowlist,
            variant=arguments.variant,
        )
    except (ProposalError, ValueError) as error:
        raise ToolError(f"that proposal was refused: {error}") from error

    record = context.record(
        build_record(
            kind=EvidenceKind.CHANGE,
            query="propose_remediation",
            parameters={
                "remediation_id": proposal.remediation_id,
                "target": proposal.target,
                "variant": proposal.variant,
            },
            # A proposal is about now, not about a window of telemetry. NO_WINDOW is
            # the declared way to say that, rather than inventing a range that would
            # then be re-run as if it were a query.
            window=NO_WINDOW,
            fingerprint=context.backend.fingerprint(),
            rows=[proposal.as_dict()],
            facts=[
                Fact(
                    field="requires_approval",
                    value=1.0 if proposal.requires_approval else 0.0,
                    unit="count",
                ),
                Fact(
                    field="changes_the_system",
                    value=1.0 if proposal.changes_the_system else 0.0,
                    unit="count",
                ),
            ],
        )
    )
    return ToolResult(
        tool="propose_remediation",
        summary=(
            f"proposed {proposal.id}: {proposal.describe()}. "
            + (
                "Needs approval before anything changes."
                if proposal.requires_approval
                else "No approval required; nothing about the system changes."
            )
        )[:600],
        evidence_id=record.id,
        data={"proposal": proposal.as_dict()},
    )


PROPOSE_REMEDIATION = ToolSpec(
    name="propose_remediation",
    description=(
        "Propose one action from the remediation allowlist, citing the evidence that "
        "justifies it. This writes a proposal and changes nothing: a separate "
        "approval service holds the only credentials that can turn a flag off or "
        "restart a service, and a person decides there. Use it once the "
        "investigation has named a service and has evidence for why. Do not use it "
        "to suggest an action that is not in the allowlist, and do not use it as a "
        "way to describe a conclusion: a report's claims are for that. The available "
        "actions are in knowledge/remediations.yaml, and paging the owning team is "
        "always one of them, which is the right proposal whenever the evidence names "
        "a service but not a mechanism."
    ),
    input_model=ProposeRemediationInput,
    handler=propose_remediation,
)

REMEDIATION_TOOLS: tuple[ToolSpec, ...] = (PROPOSE_REMEDIATION,)
