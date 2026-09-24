"""The assembled tool layer, and who is allowed to use which part of it.

Two things live here.

`get_evidence` is the other half of the bounded output rule. Every tool
returns a summary and an id rather than the rows it found, which keeps a
notebook small, and this is how the rows are fetched back when they are
actually needed. SPEC.md principle H4: context is finite, and the way to
spend it well is to load detail just in time rather than up front.

The allowlists are how each specialist gets its own tools and no others.
A logs analyst handed the trace tools will use them, report a finding nobody
asked it for, and make its own output harder to attribute. SPEC.md Section
6.6 gives each specialist a focused brief and its own tools for exactly that
reason, and `ToolRegistry.subset` is what enforces it rather than trusting a
prompt to say please.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from firebreak.tools.base import (
    MAX_SUMMARY_CHARS,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from firebreak.tools.changes import CHANGE_TOOLS
from firebreak.tools.logs import LOG_TOOLS
from firebreak.tools.metrics import METRIC_TOOLS
from firebreak.tools.topology import TOPOLOGY_TOOLS
from firebreak.tools.traces import TRACE_TOOLS

# How many stored rows one expansion may return. Higher than a tool's own
# summary because the caller has asked for detail by id, but still bounded:
# the point of storing rows by reference was never to hand them all back.
MAX_EXPANDED_ROWS = 50


class GetEvidenceInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    max_rows: int = Field(default=10, ge=1, le=MAX_EXPANDED_ROWS)


def get_evidence(context: ToolContext, arguments: GetEvidenceInput) -> ToolResult:
    """Return the stored rows behind an evidence id."""
    try:
        record = context.evidence.require(arguments.evidence_id)
    except KeyError as error:
        # A citation to an id that was never gathered is the simplest
        # fabrication there is, and this is the first place it shows up.
        raise ToolError(str(error)) from error

    rows = list(record.rows[: arguments.max_rows])
    summary = (
        f"{record.kind} evidence from {record.query}: "
        f"{record.row_count} rows recorded, {len(rows)} returned"
        + (", truncated at record time" if record.truncated else "")
    )
    return ToolResult(
        tool="get_evidence",
        summary=summary[:MAX_SUMMARY_CHARS],
        evidence_id=record.id,
        data={
            "rows": rows,
            "row_count": record.row_count,
            "facts": [fact.model_dump() for fact in record.facts],
            "window": record.window.canonical(),
        },
        truncated=record.truncated or len(rows) < record.row_count,
    )


GET_EVIDENCE = ToolSpec(
    name="get_evidence",
    description=(
        "Expand a stored evidence id into the rows behind it. Every other tool "
        "returns a short summary and an id rather than its raw results, so use this "
        "when a summary is not enough to decide, for example to read the individual "
        "log lines behind a template count. Ask for the smallest number of rows that "
        "answers the question: pulling fifty rows into context to check one number "
        "spends the budget that later steps of the investigation need. It cannot "
        "fetch an id that was never gathered, so it is not a way to explore."
    ),
    input_model=GetEvidenceInput,
    handler=get_evidence,
)

EVIDENCE_TOOLS = (GET_EVIDENCE,)

ALL_TOOLS: tuple[ToolSpec, ...] = (
    *METRIC_TOOLS,
    *LOG_TOOLS,
    *TRACE_TOOLS,
    *TOPOLOGY_TOOLS,
    *CHANGE_TOOLS,
    *EVIDENCE_TOOLS,
)

# What each specialist may call. get_evidence is in every list because a
# specialist that cannot expand its own findings has to guess from a
# summary, which is how a confident wrong number gets into a report.
SPECIALIST_TOOLS: dict[str, tuple[str, ...]] = {
    "metrics_analyst": (
        "list_anomalies",
        "query_metric",
        "compare_windows",
        "get_evidence",
    ),
    "logs_analyst": ("search_logs", "top_error_signatures", "get_evidence"),
    "traces_analyst": ("find_traces", "trace_breakdown", "get_evidence"),
    # The change analyst gets the topology tools as well as the change log,
    # because its whole job is deciding whether a change near the incident
    # is the cause or a coincidence, and that question cannot be answered
    # without knowing what the changed service is connected to.
    "change_analyst": (
        "recent_changes",
        "service_dependencies",
        "blast_radius",
        "get_evidence",
    ),
}


def build_registry(specs: tuple[ToolSpec, ...] = ALL_TOOLS) -> ToolRegistry:
    """The full tool layer."""
    return ToolRegistry(list(specs))


def registry_for(role: str, registry: ToolRegistry | None = None) -> ToolRegistry:
    """The tools one specialist may call, and no others."""
    names = SPECIALIST_TOOLS.get(role)
    if names is None:
        raise ToolError(
            f"no tool allowlist for role {role!r}; known roles: "
            f"{', '.join(sorted(SPECIALIST_TOOLS))}"
        )
    return (registry or build_registry()).subset(names)
