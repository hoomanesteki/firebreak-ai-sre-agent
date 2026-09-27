"""A proposal to change something, which is as far as the agent can go.

SPEC.md Section 6.10. `propose_remediation` writes a proposal and nothing else.
The graph then interrupts and waits, and a separate process holding the only
credentials that can change anything decides.

**Why a proposal is a data structure and not a function call.** The alternative is
a tool that performs the action and asks permission first, which puts the
credential and the decision in the same process. Then the only thing between a
language model and a production change is a conditional, and a conditional is
exactly the kind of thing an injected instruction is good at talking past. A
proposal cannot execute itself: it has no client, no credential, and no method that
does anything.

**The key is deterministic, and that is what makes double approval safe.** SPEC.md
Section 6.10 requires proposals to have deterministic keys and execution to use the
proposal id as its idempotency key. So the id is a hash of what the proposal would
do: the same action on the same target for the same incident is the same proposal,
whoever proposed it and however many times. Two approvals of one proposal therefore
execute once, because the executor records the id and refuses a second.

**Only what the allowlist contains.** `build_proposal` takes the loaded allowlist
and refuses any id not in it. The check is here rather than in the approval service
as well as here: the service checks too, because a service that trusted its input
would be a service whose safety depended on its callers.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firebreak.graph.knowledge import RemediationKind, RemediationSpec

# Proposal ids look like the evidence ids, for the same reason: short enough to
# read out over a phone, long enough that two different actions cannot collide.
PROPOSAL_ID_PREFIX = "rem"
PROPOSAL_ID_HEX = 12


class ProposalError(Exception):
    """A proposal could not be built, or is not one this system may make."""


class TargetKind(StrEnum):
    """What a proposal acts on.

    Separate from `RemediationKind` because the action and the thing it acts on are
    different vocabularies, and the pairing is checked: a `disable_feature_flag`
    with a service target and no flag is a proposal nobody can execute, and the
    place to find that out is here rather than in the approval service at three in
    the morning.
    """

    FLAG = "flag"
    SERVICE = "service"
    TEAM = "team"


# Which target each action requires. Declared once, because the eighth time this
# project had two components agree on a type and disagree on a vocabulary, it was
# in exactly this shape.
REQUIRED_TARGET: dict[RemediationKind, TargetKind] = {
    RemediationKind.DISABLE_FEATURE_FLAG: TargetKind.FLAG,
    RemediationKind.RESTART_SERVICE: TargetKind.SERVICE,
    RemediationKind.PAGE_OWNING_TEAM: TargetKind.TEAM,
}


class Proposal(BaseModel):
    """One proposed action, with everything a person needs to judge it.

    Frozen, and holds no client of any kind. A proposal is a statement about what
    should happen, and the only thing that can make it happen is the approval
    service.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=rf"^{PROPOSAL_ID_PREFIX}_[0-9a-f]{{{PROPOSAL_ID_HEX}}}$")
    incident_id: str
    remediation_id: str
    kind: RemediationKind
    target_kind: TargetKind
    target: str
    # What the flag would be set to. Only for a flag action, and required for one:
    # "turn the flag off" is ambiguous when a flag has three variants.
    variant: str | None = None

    # Why, in the proposer's words, and what it rests on. The approval service
    # shows the evidence rather than this summary, per SPEC.md Section 6.10, but a
    # reviewer still wants the one sentence.
    rationale: str = Field(max_length=600)
    evidence_ids: tuple[str, ...] = ()

    reversible: bool
    requires_approval: bool
    blast_radius: str
    proposed_at: datetime

    @model_validator(mode="after")
    def _the_target_matches_the_action(self) -> Proposal:
        expected = REQUIRED_TARGET[self.kind]
        if self.target_kind is not expected:
            raise ValueError(
                f"a {self.kind.value} acts on a {expected.value}, not a {self.target_kind.value}"
            )
        if self.kind is RemediationKind.DISABLE_FEATURE_FLAG and not self.variant:
            raise ValueError(
                f"{self.remediation_id} must name the variant to set; a flag with three "
                "variants has no unambiguous off"
            )
        if self.kind is not RemediationKind.DISABLE_FEATURE_FLAG and self.variant:
            raise ValueError(f"a {self.kind.value} has no variant to set, got {self.variant!r}")
        return self

    @model_validator(mode="after")
    def _a_proposal_cites_evidence(self) -> Proposal:
        """A change to a running system with no evidence behind it is a guess.

        The exit gate strips uncited claims from a report. A proposal is a stronger
        statement than a claim, so the same rule applies with no repair pass.
        """
        if not self.evidence_ids:
            raise ValueError(
                f"{self.remediation_id} cites no evidence; a proposed change to a "
                "running system with nothing behind it is a guess"
            )
        return self

    @property
    def changes_the_system(self) -> bool:
        """Whether executing this would alter the target system at all.

        Paging a team is not a change, and treating it as one would make the
        approval queue mostly noise, which is how a queue stops being read.
        """
        return self.kind is not RemediationKind.PAGE_OWNING_TEAM

    def describe(self) -> str:
        """One line for a log, an audit record, or a pager message."""
        if self.kind is RemediationKind.DISABLE_FEATURE_FLAG:
            return f"set flag {self.target} to {self.variant}"
        if self.kind is RemediationKind.RESTART_SERVICE:
            return f"restart service {self.target}"
        return f"page the team that owns {self.target}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "incident_id": self.incident_id,
            "remediation_id": self.remediation_id,
            "kind": self.kind.value,
            "target_kind": self.target_kind.value,
            "target": self.target,
            "variant": self.variant,
            "rationale": self.rationale,
            "evidence_ids": list(self.evidence_ids),
            "reversible": self.reversible,
            "requires_approval": self.requires_approval,
            "blast_radius": self.blast_radius,
            "proposed_at": self.proposed_at.isoformat(),
            "action": self.describe(),
        }


def proposal_id(
    incident_id: str,
    remediation_id: str,
    target: str,
    variant: str | None,
) -> str:
    """A deterministic id for one action on one target in one incident.

    Deliberately excludes the rationale, the evidence and the timestamp. Two
    proposals to turn the same flag off for the same incident are the same
    proposal, and if the wording changed the id, a model that reworded its
    rationale would produce a second proposal for the same change and both could be
    approved. Including the timestamp would do the same thing once a second.

    The incident is included, so turning the same flag off for a different incident
    is a different proposal and gets its own approval.
    """
    body = json.dumps(
        {
            "incident": incident_id,
            "remediation": remediation_id,
            "target": target,
            "variant": variant,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f"{PROPOSAL_ID_PREFIX}_{digest[:PROPOSAL_ID_HEX]}"


def build_proposal(
    incident_id: str,
    remediation_id: str,
    target: str,
    rationale: str,
    evidence_ids: tuple[str, ...],
    allowlist: dict[str, RemediationSpec],
    variant: str | None = None,
    now: datetime | None = None,
) -> Proposal:
    """Build a proposal, refusing anything the allowlist does not contain.

    The allowlist is passed in rather than loaded here, so this function touches no
    filesystem and a test does not need a knowledge directory. The caller loads it
    once per investigation.

    An unknown id is an error naming what is available. A model that invented a
    remediation id would otherwise get a proposal with a plausible name and no
    definition behind it, which is the most dangerous thing in this module: the
    approval service would show a person an action nobody had reviewed.
    """
    spec = allowlist.get(remediation_id)
    if spec is None:
        raise ProposalError(
            f"{remediation_id!r} is not in the remediation allowlist; available: "
            f"{', '.join(sorted(allowlist)) or 'none'}"
        )
    if not target:
        raise ProposalError(f"{remediation_id} needs a target to act on")

    return Proposal(
        id=proposal_id(incident_id, remediation_id, target, variant),
        incident_id=incident_id,
        remediation_id=remediation_id,
        kind=spec.kind,
        target_kind=REQUIRED_TARGET[spec.kind],
        target=target,
        variant=variant,
        rationale=rationale,
        evidence_ids=evidence_ids,
        reversible=spec.reversible,
        requires_approval=spec.requires_approval,
        blast_radius=spec.blast_radius,
        proposed_at=now or datetime.now(UTC),
    )
