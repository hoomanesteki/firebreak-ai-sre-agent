"""`similar_incidents`: what happened last time, offered as a lead to test.

SPEC.md Section 6.3 and Section 10.3. Retrieves the closest few past incidents by
symptom overlap and shared services.

**Every word of the output is shaped by one risk.** A memory entry is a confident
prior arriving before any evidence has been gathered, and a wrong one points a whole
investigation at the wrong service. So the tool returns entries phrased as leads to
test, the summary says how many and how close rather than asserting anything, and the
evidence record holds the retrieval rather than the conclusion.

**What it deliberately does not do.** It does not rank the current incident's
candidates, it does not filter them, and nothing downstream treats its output as a
finding. SPEC.md Section 11's ASI06 is memory poisoning, and the control is that
memory is advisory and re-tested, which only means anything if the advisory path and
the evidence path stay separate.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from firebreak.memory.store import (
    TOP_MATCHES,
    IncidentMemoryError,
    MemoryStore,
)
from firebreak.tools.base import ToolContext, ToolError, ToolResult, ToolSpec
from firebreak.tools.evidence import NO_WINDOW, EvidenceKind, Fact, build_record

MAX_SUMMARY_CHARS = 600


class SimilarIncidentsInput(BaseModel):
    """What to look for. A description of symptoms, not of a conclusion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The symptoms as this investigation sees them. Asking for the conclusion instead
    # would retrieve incidents by their answers, which is how memory turns from a
    # lead into a way of agreeing with itself.
    symptoms: str = Field(min_length=10, max_length=MAX_SUMMARY_CHARS)
    services: tuple[str, ...] = ()
    limit: int = Field(default=TOP_MATCHES, ge=1, le=TOP_MATCHES)


def similar_incidents(context: ToolContext, arguments: SimilarIncidentsInput) -> ToolResult:
    """Retrieve past incidents with similar symptoms, as hypotheses to test."""
    try:
        matches = MemoryStore().similar(
            arguments.symptoms, services=arguments.services, limit=arguments.limit
        )
    except IncidentMemoryError as error:
        raise ToolError(f"incident memory could not be read: {error}") from error

    rows = [match.as_dict() for match in matches]
    record = context.record(
        build_record(
            kind=EvidenceKind.MEMORY,
            query="similar_incidents",
            parameters={
                "symptoms": arguments.symptoms,
                "services": list(arguments.services),
                "limit": arguments.limit,
            },
            # Memory is about the past, not about this incident's window. NO_WINDOW is
            # the declared way to say that rather than inventing a range that would
            # then be re-run as if it were a query over telemetry.
            window=NO_WINDOW,
            fingerprint=context.backend.fingerprint(),
            rows=rows,
            facts=[
                Fact(field="matches", value=float(len(rows)), unit="count"),
                Fact(
                    field="closest_similarity",
                    value=matches[0].similarity if matches else 0.0,
                    unit="ratio",
                ),
            ],
        )
    )

    if not matches:
        summary = (
            "no past incident in memory has similar symptoms, so there is no prior "
            "here and this incident should be investigated on its own evidence"
        )
    else:
        described = "; ".join(
            f"{match.entry.root_cause_service} ({match.entry.fault_class}), "
            f"similarity {match.similarity:.2f}"
            for match in matches
        )
        summary = (
            f"{len(matches)} past incident(s) with similar symptoms, as leads to test "
            f"rather than evidence: {described}"
        )

    return ToolResult(
        tool="similar_incidents",
        summary=summary[:MAX_SUMMARY_CHARS],
        evidence_id=record.id,
        data={
            "matches": rows,
            "hypotheses": [
                match.entry.as_hypothesis(index) for index, match in enumerate(matches, start=1)
            ],
        },
    )


SIMILAR_INCIDENTS = ToolSpec(
    name="similar_incidents",
    description=(
        "Find past incidents whose symptoms resemble this one, and what they turned "
        "out to be caused by. Use it early, to decide where to spend the first tool "
        "calls: an entry says which evidence types mattered last time. Treat every "
        "result as a lead to test against this incident's own data, never as evidence "
        "and never as a reason to stop looking: a past incident with similar symptoms "
        "and a different cause is exactly the case this tool will get wrong. It "
        "searches on symptoms, so describe what you are seeing rather than what you "
        "suspect. Memory holds only incidents that were confirmed and reviewed, and "
        "never an incident from a held-out evaluation split."
    ),
    input_model=SimilarIncidentsInput,
    handler=similar_incidents,
)

MEMORY_TOOLS: tuple[ToolSpec, ...] = (SIMILAR_INCIDENTS,)
