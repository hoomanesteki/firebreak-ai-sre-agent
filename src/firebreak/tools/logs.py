"""Log tools: the lines themselves, and what keeps repeating inside them.

Two tools with different jobs, the same split metrics.py makes between
reading samples and finding what stands out.

- `search_logs` answers "show me the lines", for a window, optionally
  narrowed to services, one severity, and a literal substring. It is for
  reading, not for counting.
- `top_error_signatures` answers "what keeps failing", by masking the
  parts of an error line that vary and counting what is left. It is for a
  noisy service where the useful signal is buried inside hundreds of
  superficially different lines.

Neither takes a pattern language beyond a literal substring. SPEC.md
Section 6.3 is explicit about this, and the reason mirrors metrics.py's
refusal of free-form queries: log bodies are attacker influenced text, and
a pattern that can be anything cannot be reviewed or bounded in advance.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from firebreak.backends.base import MAX_ROWS, LogRecord
from firebreak.signals import ERROR_SEVERITIES, Severity
from firebreak.tools.base import ToolContext, ToolResult, ToolSpec
from firebreak.tools.evidence import (
    EvidenceKind,
    Fact,
    TimeRange,
    build_record,
)
from firebreak.triage.log_templates import cluster_templates, mask_log_line

# How many templates a summary names. Wide enough to show the shape of a
# noisy service, small enough that the summary stays a summary.
TOP_SIGNATURES = 10

# How many services a search_logs summary names before falling back to
# "and N more"; a summary that lists every service defeats the point of one.
TOP_SERVICES_IN_SUMMARY = 3


class LogWindow(BaseModel):
    """A window, as a tool takes it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: str
    end: str

    def to_range(self) -> TimeRange:
        return TimeRange.model_validate({"start": self.start, "end": self.end})


class SearchLogsInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window: LogWindow
    services: tuple[str, ...] = ()
    severity: Severity | None = None
    pattern: str | None = None
    limit: int = Field(default=MAX_ROWS, ge=1, le=MAX_ROWS)


class TopErrorSignaturesInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window: LogWindow
    service: str | None = None


def _top_services(records: list[LogRecord], limit: int) -> list[tuple[str, int]]:
    """The services with the most matching lines, most first."""
    counts: dict[str, int] = {}
    for record in records:
        counts[record.service_name] = counts.get(record.service_name, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ranked[:limit]


def search_logs(context: ToolContext, arguments: SearchLogsInput) -> ToolResult:
    """Return log lines matching a window, services, one severity, and a pattern."""
    window = arguments.window.to_range()
    records = context.backend.search_logs(
        window,
        services=arguments.services,
        severity=str(arguments.severity) if arguments.severity is not None else None,
        pattern=arguments.pattern,
        limit=arguments.limit,
    )
    rows = [
        {
            "timestamp": record.timestamp,
            "service": record.service_name,
            "severity": record.severity,
            "body": record.body,
            **({"trace_id": record.trace_id} if record.trace_id else {}),
        }
        for record in records
    ]
    facts = [Fact(field="matches", value=float(len(rows)), unit="count")]

    record_ = context.record(
        build_record(
            kind=EvidenceKind.LOG,
            query="search_logs",
            parameters={
                "services": list(arguments.services),
                "severity": str(arguments.severity) if arguments.severity is not None else None,
                "pattern": arguments.pattern,
                "limit": arguments.limit,
            },
            window=window,
            fingerprint=context.backend.fingerprint(),
            rows=rows,
            facts=facts,
        )
    )
    top_services = _top_services(records, TOP_SERVICES_IN_SUMMARY)
    summary = (
        f"{len(rows)} matching log lines; top services: "
        + ", ".join(f"{service}={count}" for service, count in top_services)
        if rows
        else "no matching log lines in this window"
    )
    return ToolResult(
        tool="search_logs",
        summary=summary[:600],
        evidence_id=record_.id,
        data={"logs": rows[:20], "match_count": len(rows)},
        truncated=record_.truncated,
    )


def top_error_signatures(context: ToolContext, arguments: TopErrorSignaturesInput) -> ToolResult:
    """Cluster error and fatal log lines into templates, ranked by frequency."""
    window = arguments.window.to_range()
    services = (arguments.service,) if arguments.service else ()
    records = context.backend.search_logs(window, services=services, limit=MAX_ROWS)
    error_records = [record for record in records if record.severity in ERROR_SEVERITIES]

    first_seen: dict[str, str] = {}
    for record in error_records:
        template = mask_log_line(record.body)
        if template not in first_seen or record.timestamp < first_seen[template]:
            first_seen[template] = record.timestamp

    ranked = cluster_templates(record.body for record in error_records)[:TOP_SIGNATURES]
    rows = [
        {"template": template, "count": count, "first_seen": first_seen.get(template, "")}
        for template, count in ranked
    ]
    facts = [Fact(field="distinct_templates", value=float(len(ranked)), unit="count")]
    if ranked:
        facts.append(Fact(field="top_template_count", value=float(ranked[0][1]), unit="count"))

    record_ = context.record(
        build_record(
            kind=EvidenceKind.LOG,
            query="top_error_signatures",
            parameters={"service": arguments.service},
            window=window,
            fingerprint=context.backend.fingerprint(),
            rows=rows,
            facts=facts,
        )
    )
    summary = (
        f"{len(ranked)} distinct templates from {len(error_records)} error or fatal lines; "
        f"top: '{ranked[0][0]}' x{ranked[0][1]}"
        if ranked
        else "no error or fatal log lines in this window"
    )
    return ToolResult(
        tool="top_error_signatures",
        summary=summary[:600],
        evidence_id=record_.id,
        data={"templates": rows},
    )


SEARCH_LOGS = ToolSpec(
    name="search_logs",
    description=(
        "Search raw log lines in a window, optionally filtered to services, one "
        "severity, and a literal substring (never a regular expression, since a "
        "caller-supplied pattern language cannot be reviewed or bounded in advance). "
        "Use this to read the actual lines around a suspected fault, or to confirm "
        "what an anomaly looks like at the log level once a service and window are "
        "already suspected. Do not use it to find what is repeating across many "
        "lines or many services, since a raw match list drowns a shared failure mode "
        "in one-off phrasing; use top_error_signatures instead when the question is "
        "what keeps failing rather than what a specific line said."
    ),
    input_model=SearchLogsInput,
    handler=search_logs,
)

TOP_ERROR_SIGNATURES = ToolSpec(
    name="top_error_signatures",
    description=(
        "Cluster error and fatal log lines into templates by masking the parts that "
        "vary (ids, timestamps, numbers, quoted values) and counting what is left, "
        "with when each template first appeared. Use this first when a service is "
        "noisy, to find the one or two failure modes hiding inside hundreds of "
        "superficially different lines, and to compare which service's errors "
        "started earliest. It ignores warning and informational lines by design, "
        "since those are not the failure signal it clusters. Use search_logs "
        "instead to read an individual line verbatim or to look at severities "
        "other than error and fatal."
    ),
    input_model=TopErrorSignaturesInput,
    handler=top_error_signatures,
)

LOG_TOOLS = (SEARCH_LOGS, TOP_ERROR_SIGNATURES)
