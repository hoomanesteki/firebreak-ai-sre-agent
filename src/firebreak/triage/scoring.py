"""Score every service on every metric, once, in one place.

This exists because two callers need the same answer: the `list_anomalies`
tool, which reports it to the agent, and the deterministic triage pipeline,
which feeds it to the candidate ranking. Phase 3 shipped four defects that
were all the same defect, two components agreeing on a type and disagreeing
on a vocabulary, so the rule now is that a vocabulary gets declared once and
a contract test asserts every producer speaks it.

Here the vocabulary is the pair of "which metrics count as a signal" and
"which direction of change counts as bad". A tool that scored six metrics
while the ranking scored seven would not fail any type check, and the
ranking would simply be blind to one signal for the rest of the project.
"""

from __future__ import annotations

from datetime import UTC, datetime

from firebreak.backends.base import MAX_SERIES_POINTS, MetricPoint, QueryBackend
from firebreak.signals import MetricName, StatusCode
from firebreak.tools.evidence import TimeRange
from firebreak.triage.anomaly import (
    MIN_BASELINE_POINTS,
    AnomalyScore,
    Direction,
    onset_index,
    score_series,
)

# Which way a change has to go for each metric to be worth reporting. An
# error rate falling is a recovery, not an incident.
ANOMALY_DIRECTIONS: dict[MetricName, Direction] = {
    MetricName.SPAN_CALLS_TOTAL: Direction.UP,
    MetricName.SPAN_DURATION_P95_MS: Direction.UP,
    MetricName.SPAN_DURATION_P50_MS: Direction.UP,
    MetricName.SERVICE_GRAPH_FAILED: Direction.UP,
    MetricName.CONTAINER_MEMORY_BYTES: Direction.UP,
    MetricName.CONTAINER_CPU_UTILISATION: Direction.UP,
    # A request rate that collapses is as much a symptom as one that spikes.
    MetricName.SERVICE_GRAPH_REQUESTS: Direction.EITHER,
}

# Read through `metric_series`, not `query_metrics`. The tool row cap of fifty
# exists to protect a model's context and this code has none to protect; going
# through it gave each of eighteen services three samples on the first real
# recording, which `MIN_BASELINE_POINTS` then discarded, and a 25x latency
# regression on the true culprit scored zero.
MAX_SAMPLES_PER_SERIES = MAX_SERIES_POINTS

# How far a series has to move before its crossing counts as the onset. The
# same number the anomaly tools use as their default floor, so "anomalous"
# means one thing across the system.
ONSET_THRESHOLD = 3.0


def _parse(timestamp: str) -> datetime:
    """Read a recorded timestamp, which the backends render as UTC.

    A naive value is read as UTC rather than as local time. Phase 3 lost a
    day to timestamps being rendered in the machine's timezone, and the fix
    is only a fix while every reader agrees about what a missing offset
    means.
    """
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def error_only(points: list[MetricPoint]) -> list[MetricPoint]:
    """Keep only the error status series of the call counter.

    The span call counter carries both successful and failed calls. Scoring
    the total would score traffic, not failure, and a service whose requests
    merely rose would look as bad as one that started erroring.
    """
    return [p for p in points if p.labels.get("status_code") == StatusCode.ERROR.value]


def values_by_service(points: list[MetricPoint]) -> dict[str, list[float]]:
    """Group samples by service, preserving the order they arrived in."""
    grouped: dict[str, list[float]] = {}
    for point in points:
        grouped.setdefault(point.service_name, []).append(point.value)
    return grouped


def fetch_metric(
    backend: QueryBackend,
    metric: MetricName,
    window: TimeRange,
    services: tuple[str, ...] = (),
    limit: int = MAX_SAMPLES_PER_SERIES,
) -> list[MetricPoint]:
    """One metric over one window, with the call counter reduced to errors."""
    points = backend.metric_series(metric, window, services=services, limit=limit)
    return error_only(points) if metric is MetricName.SPAN_CALLS_TOTAL else points


def score_services(
    backend: QueryBackend,
    baseline: TimeRange,
    incident: TimeRange,
    services: tuple[str, ...] = (),
    limit: int = MAX_SAMPLES_PER_SERIES,
) -> list[AnomalyScore]:
    """Score every service against its baseline, on every declared metric.

    Returns one score per service and metric pair, unranked and unfiltered.
    A metric that returned no samples still produces a score, which will
    report itself as untrustworthy rather than as healthy: "no data" and
    "nothing wrong" are different claims and the difference matters when the
    reason for no data is that the service stopped responding.
    """
    scores = []
    for metric, direction in ANOMALY_DIRECTIONS.items():
        base_points = fetch_metric(backend, metric, baseline, services, limit)
        inc_points = fetch_metric(backend, metric, incident, services, limit)
        base_by_service = values_by_service(base_points)
        inc_by_service = values_by_service(inc_points)
        for service in sorted(set(base_by_service) | set(inc_by_service)):
            scores.append(
                score_series(
                    subject=service,
                    metric=str(metric),
                    baseline=base_by_service.get(service, []),
                    incident=inc_by_service.get(service, []),
                    direction=direction,
                )
            )
    return scores


def samples_by_service(points: list[MetricPoint]) -> dict[str, list[tuple[str, float]]]:
    """Group samples by service as (timestamp, value), in time order.

    Sorted here rather than trusted from the backend. Onset is the first
    crossing, and "first" is meaningless if the rows arrive in whatever
    order a query planner produced.
    """
    grouped: dict[str, list[tuple[str, float]]] = {}
    for point in points:
        grouped.setdefault(point.service_name, []).append((point.timestamp, point.value))
    for series in grouped.values():
        series.sort(key=lambda pair: pair[0])
    return grouped


def onset_seconds(
    backend: QueryBackend,
    baseline: TimeRange,
    incident: TimeRange,
    services: tuple[str, ...] = (),
    limit: int = MAX_SAMPLES_PER_SERIES,
    threshold: float = ONSET_THRESHOLD,
) -> dict[str, float]:
    """When each service first went abnormal, in seconds into the incident.

    Causes precede symptoms, which is the whole reason this is computed
    (SPEC.md Section 6.5). A service is timed at its earliest crossing on
    any metric, because the question is when it first showed distress, not
    when its worst signal did.

    The time of a crossing is read from the sample's own timestamp rather
    than reconstructed from an assumed sampling interval. A hard coded step
    would be an assumption about the recorder that this code cannot check,
    and would silently mis-time every onset the day a collector's export
    interval changed.

    A service with no crossing is absent from the result rather than present
    with a large number, so a caller cannot mistake "never went abnormal"
    for "went abnormal late".
    """
    earliest: dict[str, float] = {}
    start = incident.start
    for metric in ANOMALY_DIRECTIONS:
        base_by_service = values_by_service(
            fetch_metric(backend, metric, baseline, services, limit)
        )
        inc_by_service = samples_by_service(
            fetch_metric(backend, metric, incident, services, limit)
        )
        for service, series in inc_by_service.items():
            baseline_values = base_by_service.get(service, [])
            if len(baseline_values) < MIN_BASELINE_POINTS:
                continue
            index = onset_index([value for _, value in series], baseline_values, threshold)
            if index is None:
                continue
            at = (_parse(series[index][0]) - start).total_seconds()
            if service not in earliest or at < earliest[service]:
                earliest[service] = at
    return earliest
