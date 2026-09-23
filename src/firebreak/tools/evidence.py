"""Evidence: a stored, re-runnable query with the numbers it produced.

This is the mechanism behind Firebreak's one hard rule, that no sentence in a
report stands without evidence a verifier can re-run and match (SPEC.md
Sections 6.3 and 6.9).

Three properties make that work, and all three are load bearing.

**Deterministic ids.** An evidence id is a hash of the query, the window and
the backend, so the same question asked twice produces the same id. That is
what lets the exit gate look up what a claim cited, and what stops an agent
inventing an id that happens to look plausible.

**Extracted facts.** A record stores the numbers pulled out of the result,
not just the rows. The gate checks that every number in a claim appears in
the facts of the evidence it cites, which is how a report that rounds 4.2 to
"about 40 percent" gets caught.

**Nothing identifying.** The fingerprint names the backend mode and an
opaque identity, never a scenario name or a file path that describes a
fault. This module sits inside the agent's blast radius: `firebreak.tools`
may not import ground truth, and `config/leakage.yaml` enforces that.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

EVIDENCE_ID_PREFIX = "ev"
EVIDENCE_ID_HEX = 12

# A result set large enough to be worth storing by reference rather than
# inline. Keeps a notebook small without losing the ability to re-run.
INLINE_ROW_LIMIT = 50


class EvidenceKind(StrEnum):
    """What kind of question produced this evidence.

    Part of the id, so a metric query and a log query over the same window
    can never collide, and a reader can tell what a citation is without
    opening it.
    """

    METRIC = "metric"
    LOG = "log"
    TRACE = "trace"
    TOPOLOGY = "topology"
    CHANGE = "change"
    RUNBOOK = "runbook"
    MEMORY = "memory"


class BackendMode(StrEnum):
    """Where an answer came from."""

    BUNDLE = "bundle"
    LIVE = "live"


class TimeRange(BaseModel):
    """The window a query covered, in UTC."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end < self.start:
            raise ValueError("a time range must end at or after it starts")
        return self

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    def canonical(self) -> str:
        """A stable string form, so the same window always hashes the same."""
        return f"{self.start.isoformat()}/{self.end.isoformat()}"


class BackendFingerprint(BaseModel):
    """Which backend answered, without saying anything about the incident.

    The identity is opaque on purpose. Bundles are stored under an opaque id
    precisely so that nothing downstream, including this, carries the name
    of the fault into something the agent handles.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: BackendMode
    identity: str
    format_version: int = 1

    def canonical(self) -> str:
        return f"{self.mode.value}:{self.identity}:v{self.format_version}"


class Fact(BaseModel):
    """One number extracted from a result, with enough to check a claim.

    A claim citing "error rate of 0.42" is verified by finding a fact with
    that value and that unit in the evidence it cited. Without the unit the
    check is meaningless, since 0.42 and 42 percent are the same measurement
    and a report may state either.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    value: float
    unit: str
    subject: str | None = None

    def matches(self, value: float, tolerance: float) -> bool:
        """True when a stated number is this fact, within tolerance.

        Counts are compared exactly and rates within a relative tolerance,
        which is the distinction SPEC.md Section 6.9 draws: a count that
        drifts is a different measurement, a rate that drifts by a fraction
        of a percent is the same one read a moment later.
        """
        if self.unit == "count":
            return math.isclose(self.value, value, rel_tol=0.0, abs_tol=0.0)
        if self.value == 0.0:
            return abs(value) <= tolerance
        return abs(value - self.value) / abs(self.value) <= tolerance


# Some questions have no time range at all. A trace is a unit and a
# dependency graph is a shape, so neither is asked "between when and when".
# An evidence record still needs a window to hash, so those use this
# sentinel rather than each inventing one: two modules picking different
# placeholders would give the same question two different ids, and the exit
# gate looks a citation up by id.
NO_WINDOW = TimeRange(
    start=datetime(1970, 1, 1, tzinfo=UTC),
    end=datetime(1970, 1, 1, tzinfo=UTC),
)


def canonical_query(query: str, parameters: dict[str, Any]) -> str:
    """A stable text form of a query and its parameters.

    Sorted keys and compact separators, so two callers that build the same
    query with keys in a different order get the same evidence id. Without
    this, an agent asking the same question twice would produce two records
    and the gate could not tell they were the same.
    """
    encoded = json.dumps(parameters, sort_keys=True, separators=(",", ":"), default=str)
    return f"{query.strip()}|{encoded}"


def evidence_id(
    kind: EvidenceKind,
    query: str,
    parameters: dict[str, Any],
    window: TimeRange,
    fingerprint: BackendFingerprint,
) -> str:
    """Derive the deterministic id for one question.

    SPEC.md Section 6.3: ev_<kind>_<sha256(query + range + backend)[:12]>.
    """
    material = "\n".join(
        (canonical_query(query, parameters), window.canonical(), fingerprint.canonical())
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{EVIDENCE_ID_PREFIX}_{kind.value}_{digest[:EVIDENCE_ID_HEX]}"


class EvidenceRecord(BaseModel):
    """Everything needed to re-run one question and check what it answered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=rf"^{EVIDENCE_ID_PREFIX}_[a-z]+_[0-9a-f]{{{EVIDENCE_ID_HEX}}}$")
    kind: EvidenceKind
    query: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    window: TimeRange
    fingerprint: BackendFingerprint
    row_count: int = Field(ge=0)
    rows: tuple[dict[str, Any], ...] = ()
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    facts: tuple[Fact, ...] = ()
    truncated: bool = False

    @model_validator(mode="after")
    def _id_matches_its_content(self) -> Self:
        """An id that does not describe its own record is unusable.

        The gate looks a claim's citation up by id and re-runs what it
        finds. If the two could disagree, a record could be swapped for
        another under the same id and the check would pass on the wrong
        question.
        """
        expected = evidence_id(
            self.kind, self.query, self.parameters, self.window, self.fingerprint
        )
        if self.id != expected:
            raise ValueError(
                f"evidence id {self.id} does not match its content, expected {expected}"
            )
        return self

    def fact_for(self, field: str, subject: str | None = None) -> Fact | None:
        for fact in self.facts:
            if fact.field == field and (subject is None or fact.subject == subject):
                return fact
        return None

    def supports(self, value: float, tolerance: float) -> bool:
        """True when any extracted fact matches a stated number."""
        return any(fact.matches(value, tolerance) for fact in self.facts)


def hash_rows(rows: list[dict[str, Any]]) -> str:
    """Hash a result set so a re-run can be compared without storing it all.

    Rows are serialised with sorted keys but in their original order,
    because order is part of the answer for anything ranked.
    """
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_record(
    kind: EvidenceKind,
    query: str,
    parameters: dict[str, Any],
    window: TimeRange,
    fingerprint: BackendFingerprint,
    rows: list[dict[str, Any]],
    facts: list[Fact] | None = None,
    inline_limit: int = INLINE_ROW_LIMIT,
) -> EvidenceRecord:
    """Build a record from a result, storing large results by hash alone.

    The full result is hashed either way, so a re-run is still verifiable
    when the rows were too many to keep in context. SPEC.md principle H4:
    context is finite, and a tool that returns everything it found spends it
    all on one call.
    """
    return EvidenceRecord(
        id=evidence_id(kind, query, parameters, window, fingerprint),
        kind=kind,
        query=query,
        parameters=parameters,
        window=window,
        fingerprint=fingerprint,
        row_count=len(rows),
        rows=tuple(rows[:inline_limit]),
        result_sha256=hash_rows(rows),
        facts=tuple(facts or ()),
        truncated=len(rows) > inline_limit,
    )


class EvidenceStore:
    """The evidence gathered during one investigation.

    Deduplicates by id, which is what makes a repeated tool call cheap: the
    same question asked twice returns the record already held rather than
    querying again. SPEC.md Section 6.6 counts those repeats against a
    limit, and this is where they are noticed.
    """

    def __init__(self) -> None:
        self._records: dict[str, EvidenceRecord] = {}
        self._hits: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, evidence_ref: str) -> bool:
        return evidence_ref in self._records

    def add(self, record: EvidenceRecord) -> EvidenceRecord:
        """Store a record, or return the one already held under its id."""
        existing = self._records.get(record.id)
        if existing is not None:
            self._hits[record.id] = self._hits.get(record.id, 0) + 1
            return existing
        self._records[record.id] = record
        return record

    def get(self, evidence_ref: str) -> EvidenceRecord | None:
        return self._records.get(evidence_ref)

    def require(self, evidence_ref: str) -> EvidenceRecord:
        """Look a record up, refusing an id that was never gathered.

        A report citing an id the store has never seen is the simplest
        fabrication there is, and the exit gate catches it here.
        """
        record = self._records.get(evidence_ref)
        if record is None:
            raise KeyError(f"no evidence recorded under {evidence_ref!r}")
        return record

    def repeat_count(self, evidence_ref: str) -> int:
        """How many times this exact question was asked again."""
        return self._hits.get(evidence_ref, 0)

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._records))

    def total_rows(self) -> int:
        return sum(record.row_count for record in self._records.values())
