"""What a query backend must do, and the limits it must respect.

Two backends answer the same questions: a live one against Prometheus,
Jaeger and OpenSearch, and a replay one against a frozen bundle. Both sit
behind this protocol so a tool is written once, and contract tests can prove
the two return the same shape for the same window (SPEC.md Section 12).

The caps are here rather than in each tool because they are a safety
property, not a tool preference. SPEC.md Section 11 lists tool misuse as
ASI02: an agent that asks for a week of data at one second resolution can
exhaust a backend without doing anything obviously wrong. A cap that lives
in one place cannot be forgotten in the fourteenth tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from firebreak.tools.evidence import BackendFingerprint, TimeRange

# SPEC.md Section 6.3. A window longer than this is almost always a mistake,
# and answering it politely would hide the mistake.
MAX_WINDOW_SECONDS = 2 * 60 * 60
MAX_ROWS = 50
MAX_LOG_BYTES = 20_000
MIN_STEP_SECONDS = 5


class BackendError(Exception):
    """A backend could not answer, or was asked something it must refuse."""


class QueryTooBroadError(BackendError):
    """The request exceeds a cap, and is refused rather than truncated.

    Truncating silently would let an agent believe it saw everything.
    """


@dataclass(frozen=True)
class MetricPoint:
    """One sample of one series."""

    timestamp: str
    metric_name: str
    service_name: str
    value: float
    labels: dict[str, str]


@dataclass(frozen=True)
class LogRecord:
    """One log line as recorded."""

    timestamp: str
    service_name: str
    severity: str
    body: str
    trace_id: str | None


@dataclass(frozen=True)
class SpanRecord:
    """One span as recorded."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    service_name: str
    span_name: str
    span_kind: str
    start_time: str
    duration_ms: float
    status_code: str


def check_window(window: TimeRange, maximum_seconds: int = MAX_WINDOW_SECONDS) -> None:
    """Refuse a window wider than the cap."""
    if window.duration_seconds > maximum_seconds:
        raise QueryTooBroadError(
            f"window of {int(window.duration_seconds)}s exceeds the "
            f"{maximum_seconds}s cap; narrow the range"
        )


def check_limit(limit: int, maximum: int = MAX_ROWS) -> int:
    """Clamp a row limit to the cap, refusing a nonsensical one."""
    if limit < 1:
        raise BackendError(f"limit must be at least 1, got {limit}")
    return min(limit, maximum)


class QueryBackend(Protocol):
    """The questions every backend can answer.

    Deliberately narrow. A backend returns rows; deciding what to ask and
    how to summarise the answer belongs to the tool layer, so that the two
    backends cannot drift in behaviour, only in plumbing.
    """

    def fingerprint(self) -> BackendFingerprint: ...

    def query_metrics(
        self,
        metric_name: str,
        window: TimeRange,
        services: tuple[str, ...] = (),
        limit: int = MAX_ROWS,
    ) -> list[MetricPoint]: ...

    def search_logs(
        self,
        window: TimeRange,
        services: tuple[str, ...] = (),
        severity: str | None = None,
        pattern: str | None = None,
        limit: int = MAX_ROWS,
    ) -> list[LogRecord]: ...

    def find_spans(
        self,
        window: TimeRange,
        services: tuple[str, ...] = (),
        status: str | None = None,
        min_duration_ms: float | None = None,
        limit: int = MAX_ROWS,
    ) -> list[SpanRecord]: ...

    def trace_spans(self, trace_id: str) -> list[SpanRecord]: ...

    def topology(self) -> dict[str, Any]: ...

    def changes(self, window: TimeRange) -> list[dict[str, Any]]: ...

    def known_services(self) -> tuple[str, ...]: ...
