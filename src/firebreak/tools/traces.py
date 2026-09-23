"""Trace tools: which traces are worth looking at, and where one spent its time.

Two tools, one that finds candidates and one that explains a single one in
depth, the same split metrics.py makes between ranking and detail.

- `find_traces` answers "which traces", returning representative failing or
  slow traces for a window with a one line critical path summary each. It
  is a ranking, not a dump: SPEC.md principle H4 says a tool that returns
  everything it found spends a whole call's worth of context on one
  question.
- `trace_breakdown` answers "where did this one trace's time go", for a
  single trace id already in hand. It is pure computation over spans
  already fetched: no window, no filter, just the shape of one trace.

Both derive from the same two facts about a trace: its critical path, the
chain of spans whose own duration actually explains how long the whole
thing took, and each span's self time, the part of its duration that is not
accounted for by anything it called. A span that is slow because it called
something slow is not the same problem as a span that is slow on its own.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from firebreak.backends.base import MAX_ROWS, SpanRecord
from firebreak.signals import StatusCode
from firebreak.tools.base import ToolContext, ToolResult, ToolSpec, summarise_rows
from firebreak.tools.evidence import (
    NO_WINDOW,
    EvidenceKind,
    Fact,
    TimeRange,
    build_record,
)

# trace_breakdown takes a trace id, not a window: a trace is a unit rather
# than a span of time, so there is no caller-chosen range to record. The
# shared NO_WINDOW sentinel in firebreak.tools.evidence keeps the evidence
# id for a given trace deterministic, and keeps it the same id that any
# other module asking the same question would derive.

# How many distinct traces find_traces summarises with a full critical path.
# Each one costs an extra trace_spans call, so this is kept well under the
# row cap rather than matching it.
TOP_TRACES = 10

# A safety bound on how many hops critical_path will walk. Real traces in
# this system are a handful of services deep; this exists only so a
# malformed bundle with a parent pointing to itself cannot loop.
MAX_PATH_SPANS = 40


class TraceWindow(BaseModel):
    """A window, as a tool takes it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: str
    end: str

    def to_range(self) -> TimeRange:
        return TimeRange.model_validate({"start": self.start, "end": self.end})


class FindTracesInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window: TraceWindow
    services: tuple[str, ...] = ()
    status: StatusCode | None = None
    min_duration_ms: float | None = Field(default=None, ge=0.0)
    limit: int = Field(default=MAX_ROWS, ge=1, le=MAX_ROWS)


class TraceBreakdownInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str


def _children_by_parent(spans: list[SpanRecord]) -> dict[str | None, list[SpanRecord]]:
    children: dict[str | None, list[SpanRecord]] = {}
    for span in spans:
        children.setdefault(span.parent_span_id, []).append(span)
    return children


def _roots(spans: list[SpanRecord]) -> list[SpanRecord]:
    """Spans with no parent inside this trace.

    A parent id pointing outside the set, for instance because an exporter
    dropped the parent span, is treated the same as no parent: a span this
    code cannot find the parent of cannot be attached anywhere either.
    """
    span_ids = {span.span_id for span in spans}
    return [
        span for span in spans if span.parent_span_id is None or span.parent_span_id not in span_ids
    ]


def critical_path(spans: list[SpanRecord]) -> list[SpanRecord]:
    """The chain of longest duration spans from the root.

    At each level, the child with the largest own duration is followed.
    That is what actually explains where the trace's time went: a span
    that itself ran long, not merely one that happened to start first or
    have the most children.
    """
    if not spans:
        return []
    roots = _roots(spans)
    if not roots:
        return []
    children = _children_by_parent(spans)
    current = max(roots, key=lambda span: (span.duration_ms, span.span_id))
    path = [current]
    seen = {current.span_id}
    for _ in range(MAX_PATH_SPANS):
        options = [
            child for child in children.get(current.span_id, []) if child.span_id not in seen
        ]
        if not options:
            break
        current = max(options, key=lambda span: (span.duration_ms, span.span_id))
        path.append(current)
        seen.add(current.span_id)
    return path


def self_times_ms(spans: list[SpanRecord]) -> dict[str, float]:
    """Each span's own time: duration minus its direct children's durations.

    Floored at zero. A child that outlasted its parent, which clock skew
    between processes can produce, should read as "no self time left" and
    not as a negative number nobody can act on.
    """
    children = _children_by_parent(spans)
    return {
        span.span_id: max(
            span.duration_ms - sum(child.duration_ms for child in children.get(span.span_id, [])),
            0.0,
        )
        for span in spans
    }


def _path_summary(path: list[SpanRecord]) -> str:
    if not path:
        return "no spans recorded"
    hops = " -> ".join(f"{span.service_name}:{span.span_name}" for span in path)
    slowest = max(path, key=lambda span: span.duration_ms)
    return f"{hops} ({slowest.duration_ms:.0f}ms slowest: {slowest.service_name})"


def find_traces(context: ToolContext, arguments: FindTracesInput) -> ToolResult:
    """Representative failing or slow traces, with a critical path summary each."""
    window = arguments.window.to_range()
    spans = context.backend.find_spans(
        window,
        services=arguments.services,
        status=str(arguments.status) if arguments.status is not None else None,
        min_duration_ms=arguments.min_duration_ms,
        limit=arguments.limit,
    )

    # find_spans is ordered slowest first, so taking unique trace ids in
    # that order keeps the traces most relevant to the caller's filter,
    # rather than an arbitrary sample of the traces that happened to match.
    trace_ids: list[str] = []
    for span in spans:
        if span.trace_id not in trace_ids:
            trace_ids.append(span.trace_id)
    trace_ids = trace_ids[:TOP_TRACES]

    rows: list[dict[str, Any]] = []
    for trace_id in trace_ids:
        trace_spans = context.backend.trace_spans(trace_id)
        path = critical_path(trace_spans)
        has_error = any(span.status_code == StatusCode.ERROR.value for span in trace_spans)
        rows.append(
            {
                "trace_id": trace_id,
                "critical_path": _path_summary(path),
                "duration_ms": round(path[0].duration_ms, 2) if path else 0.0,
                "error": has_error,
            }
        )

    facts = [Fact(field="matches", value=float(len(rows)), unit="count")]

    record = context.record(
        build_record(
            kind=EvidenceKind.TRACE,
            query="find_traces",
            parameters={
                "services": list(arguments.services),
                "status": str(arguments.status) if arguments.status is not None else None,
                "min_duration_ms": arguments.min_duration_ms,
                "limit": arguments.limit,
            },
            window=window,
            fingerprint=context.backend.fingerprint(),
            rows=rows,
            facts=facts,
        )
    )
    summary = (
        f"{len(rows)} representative traces: {summarise_rows(rows)}"
        if rows
        else "no matching traces in this window"
    )
    return ToolResult(
        tool="find_traces",
        summary=summary[:600],
        evidence_id=record.id,
        data={"traces": rows},
        truncated=record.truncated,
    )


def trace_breakdown(context: ToolContext, arguments: TraceBreakdownInput) -> ToolResult:
    """Compute the critical path, per-span self time, and error spans of one trace."""
    spans = context.backend.trace_spans(arguments.trace_id)
    path = critical_path(spans)
    self_times = self_times_ms(spans)

    path_rows = [
        {
            "span_id": span.span_id,
            "service": span.service_name,
            "span_name": span.span_name,
            "duration_ms": round(span.duration_ms, 2),
            "self_ms": round(self_times.get(span.span_id, 0.0), 2),
        }
        for span in path
    ]
    error_rows = [
        {
            "span_id": span.span_id,
            "service": span.service_name,
            "span_name": span.span_name,
            "status_code": span.status_code,
        }
        for span in spans
        if span.status_code == StatusCode.ERROR.value
    ][:20]

    total_duration = path[0].duration_ms if path else 0.0
    slowest_self = max(self_times.values()) if self_times else 0.0
    facts = [
        Fact(field="total_duration", value=round(total_duration, 2), unit="ms"),
        Fact(field="slowest_self_time", value=round(slowest_self, 2), unit="ms"),
    ]

    record = context.record(
        build_record(
            kind=EvidenceKind.TRACE,
            query="trace_breakdown",
            parameters={"trace_id": arguments.trace_id},
            window=NO_WINDOW,
            fingerprint=context.backend.fingerprint(),
            rows=path_rows,
            facts=facts,
        )
    )
    summary = (
        f"trace {arguments.trace_id}: {total_duration:.0f}ms total over "
        f"{len(path_rows)} span critical path, {len(error_rows)} error spans"
        if spans
        else f"trace {arguments.trace_id}: no spans found"
    )
    return ToolResult(
        tool="trace_breakdown",
        summary=summary[:600],
        evidence_id=record.id,
        data={"critical_path": path_rows, "error_spans": error_rows},
    )


FIND_TRACES = ToolSpec(
    name="find_traces",
    description=(
        "Return representative failing or slow traces for a window, optionally "
        "filtered to services, a status, and a minimum duration, each with a one "
        "line critical path summary. Use this to go from a suspected service or "
        "window to concrete trace ids worth a closer look, before asking for any "
        "one trace in detail. Do not use it to analyse a single trace's timing in "
        "depth, which is what trace_breakdown is for once an id is in hand; this "
        "tool's summaries are for choosing, not for explaining."
    ),
    input_model=FindTracesInput,
    handler=find_traces,
)

TRACE_BREAKDOWN = ToolSpec(
    name="trace_breakdown",
    description=(
        "Compute one trace's critical path, the self time of every span on it, and "
        "every span in the trace with an error status. Use this once a specific "
        "trace id is worth explaining, to see which service actually spent the "
        "time rather than which service merely called something slow. Self time is "
        "duration minus the sum of a span's direct children, floored at zero, so a "
        "span that is only slow because it waited on a dependency reads as fast on "
        "its own. Do not use it to find which traces to look at in the first place; "
        "that is find_traces."
    ),
    input_model=TraceBreakdownInput,
    handler=trace_breakdown,
)

TRACE_TOOLS = (FIND_TRACES, TRACE_BREAKDOWN)
