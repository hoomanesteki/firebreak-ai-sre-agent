"""Change tools: what was deployed, flagged, or reconfigured near an incident.

One tool, because a change feed is only ever read one way: everything that
happened inside a window, optionally narrowed to services. The reason this
module needs more caution than its size suggests is not the query, it is
what a caller does with the answer.

A change that happened near an incident is the most tempting fact in the
whole investigation, because it comes with a timestamp and a service name
attached and asks nothing else of the reader. It is also, by a wide margin,
the most common way a human or an agent gets root cause analysis wrong:
correlation in time is not evidence of causation, and a deploy that landed
five minutes before an alert is exactly as likely to be irrelevant as it is
to be the cause. `recent_changes` returns what changed and nothing about
whether it mattered, on purpose, because nothing in a change record alone
can tell the two apart.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from firebreak.tools.base import ToolContext, ToolResult, ToolSpec, summarise_rows
from firebreak.tools.evidence import (
    EvidenceKind,
    Fact,
    TimeRange,
    build_record,
)


class ChangeWindow(BaseModel):
    """A window, as a tool takes it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: str
    end: str

    def to_range(self) -> TimeRange:
        return TimeRange.model_validate({"start": self.start, "end": self.end})


class RecentChangesInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window: ChangeWindow
    services: tuple[str, ...] = ()


def recent_changes(context: ToolContext, arguments: RecentChangesInput) -> ToolResult:
    """Deploy, config, flag, and restart events inside a window."""
    window = arguments.window.to_range()
    changes = context.backend.changes(window)
    if arguments.services:
        wanted = set(arguments.services)
        changes = [change for change in changes if str(change.get("service", "")) in wanted]

    rows = [
        {
            "at": str(change.get("at", "")),
            "kind": str(change.get("kind", "")),
            "service": str(change.get("service", "")),
            "detail": str(change["detail"]) if change.get("detail") else "",
        }
        for change in changes
    ]
    rows.sort(key=lambda row: row["at"])
    facts = [Fact(field="changes", value=float(len(rows)), unit="count")]

    record = context.record(
        build_record(
            kind=EvidenceKind.CHANGE,
            query="recent_changes",
            parameters={"services": list(arguments.services)},
            window=window,
            fingerprint=context.backend.fingerprint(),
            rows=rows,
            facts=facts,
        )
    )
    summary = (
        f"{len(rows)} changes in this window: {summarise_rows(rows)}"
        if rows
        else "no changes recorded in this window"
    )
    return ToolResult(
        tool="recent_changes",
        summary=summary[:600],
        evidence_id=record.id,
        data={"changes": rows[:20]},
        truncated=len(rows) > 20,
    )


RECENT_CHANGES = ToolSpec(
    name="recent_changes",
    description=(
        "Deploy, config, flag, and restart events recorded inside a window, "
        "optionally filtered to services. Use this to see what changed near an "
        "incident's onset, as one input among several, never as a conclusion on its "
        "own. A change near an incident is a coincidence until the same service also "
        "shows an anomaly in list_anomalies or compare_windows: chasing the most "
        "recent change is the single most common way a human or an agent gets root "
        "cause analysis wrong, and this tool cannot and does not tell you that a "
        "change it returns caused anything. There is no sibling tool for this "
        "question; the correction is always to corroborate a change with a metric "
        "or log tool before treating it as a cause."
    ),
    input_model=RecentChangesInput,
    handler=recent_changes,
)

CHANGE_TOOLS = (RECENT_CHANGES,)
