"""The vocabulary of the telemetry inside a bundle.

Metric names, span status codes and log severities, declared once.

This exists because they were declared twice and disagreed. The exporter
wrote a metric called `span_calls_total`; the synthetic builder wrote the
same measurement as `traces_span_metrics_calls_total`. A tool asking for
either would have found nothing in half the bundles, and found it silently,
because an empty result set is what a query for a name nobody uses returns.

The names are the ones the pinned demo actually emits, confirmed against its
own Grafana dashboards rather than invented. Two reasons: the live backend
has to use them verbatim when it queries Prometheus, and a reader who knows
OpenTelemetry recognises them.
"""

from __future__ import annotations

from enum import StrEnum


class MetricName(StrEnum):
    """Every metric a bundle may contain.

    A recording that writes a name not listed here, or a tool that asks for
    one, is a mistake caught by a contract test rather than by a silently
    empty result.
    """

    # Per service RED signals, from the demo's span_metrics connector.
    SPAN_CALLS_TOTAL = "traces_span_metrics_calls_total"

    # Latency is stored as derived percentiles rather than as raw histogram
    # buckets. A tool wants "p95 for checkout", not a bucket set it has to
    # do histogram maths on, and the live backend can compute the same
    # quantile in PromQL. The name says derived so nobody mistakes it for
    # something the demo emits directly.
    SPAN_DURATION_P50_MS = "traces_span_metrics_duration_p50_milliseconds"
    SPAN_DURATION_P95_MS = "traces_span_metrics_duration_p95_milliseconds"

    # Service to service edges, from Firebreak's service_graph connector.
    # These are what the knowledge graph's CALLS relationships come from.
    SERVICE_GRAPH_REQUESTS = "traces_service_graph_request_total"
    SERVICE_GRAPH_FAILED = "traces_service_graph_request_failed_total"

    # Container resources, for the fault families whose symptom is not an
    # error rate: memory leaks and cpu saturation show up here first.
    CONTAINER_MEMORY_BYTES = "container_memory_usage"
    CONTAINER_CPU_UTILISATION = "container_cpu_utilization"


class StatusCode(StrEnum):
    """Span status, as the demo reports it.

    The literal strings matter: the alert rules filter on
    status_code="STATUS_CODE_ERROR", and that value was read from the demo's
    own dashboards.
    """

    OK = "STATUS_CODE_OK"
    ERROR = "STATUS_CODE_ERROR"
    UNSET = "STATUS_CODE_UNSET"


class Severity(StrEnum):
    """Log severity, upper case as OpenTelemetry writes it."""

    TRACE = "TRACE"
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    FATAL = "FATAL"


# Severities worth treating as a symptom. WARN is included because several
# fault families degrade rather than fail: a latency injection produces slow
# warnings long before it produces errors.
PROBLEM_SEVERITIES: frozenset[str] = frozenset({Severity.WARN, Severity.ERROR, Severity.FATAL})

ERROR_SEVERITIES: frozenset[str] = frozenset({Severity.ERROR, Severity.FATAL})


def is_known_metric(name: str) -> bool:
    """True when a name is part of the declared vocabulary."""
    return name in set(MetricName)


def metric_names() -> tuple[str, ...]:
    """Every declared metric name, sorted."""
    return tuple(sorted(str(name) for name in MetricName))
