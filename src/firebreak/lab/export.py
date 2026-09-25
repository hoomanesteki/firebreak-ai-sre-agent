"""Pull a window of telemetry out of the running stack.

Recording an incident means freezing what the observability stack saw while
it happened. This is the seam that does the pulling, kept behind a protocol
so the recorder can be exercised without a stack and so the three backends
can be written and verified one at a time.

`PrometheusExporter` handles metrics and the dependency graph. Spans and logs
come from `lab/export_traces_logs.py`, whose query APIs were confirmed against
the running stack rather than assumed, and `LiveExporter` composes all three
into the one protocol the recorder takes.

The composition is deliberate rather than one class doing everything. Each of
the three backends was confirmed separately and each fails separately, so when
a recording cannot get logs it says so about logs.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

import httpx

from firebreak.lab.bundle import EdgeRecord
from firebreak.lab.endpoints import DEFAULT_ENDPOINTS, DemoEndpoints
from firebreak.signals import MetricName

# Resolution of an exported metrics series. SPEC.md Section 6.2 records
# bundles at a 15 second step, which is also the export interval the live
# overlay sets on the services so that the samples actually exist.
EXPORT_STEP_SECONDS = 15
MAX_POINTS_PER_QUERY = 11_000

# What a recording captures, per service, for every scenario family. Named
# templates rather than free-form PromQL, for the same reason the tool layer
# uses them: a query that can be anything cannot be reviewed.
#
# Keyed by the canonical metric names in firebreak.signals, because this
# and the synthetic builder once wrote different names for the same
# measurement and every metric tool would have found nothing in half the
# bundles, silently.
METRIC_QUERIES: dict[str, str] = {
    MetricName.SPAN_CALLS_TOTAL: (
        "sum by (service_name, status_code) (rate(traces_span_metrics_calls_total[2m]))"
    ),
    MetricName.SPAN_DURATION_P95_MS: (
        "histogram_quantile(0.95, sum by (service_name, le) "
        "(rate(traces_span_metrics_duration_milliseconds_bucket[2m])))"
    ),
    MetricName.SPAN_DURATION_P50_MS: (
        "histogram_quantile(0.50, sum by (service_name, le) "
        "(rate(traces_span_metrics_duration_milliseconds_bucket[2m])))"
    ),
    MetricName.SERVICE_GRAPH_REQUESTS: (
        "sum by (client, server) (rate(traces_service_graph_request_total[2m]))"
    ),
    MetricName.SERVICE_GRAPH_FAILED: (
        "sum by (client, server) (rate(traces_service_graph_request_failed_total[2m]))"
    ),
    # Container metrics are labelled `container_name`, not `service_name`, and
    # their names are not what a reader would guess. All three facts were
    # confirmed against the running stack on 2026-09-25; the previous values
    # here were `container_memory_usage` and `container_cpu_utilization`
    # grouped by `service_name`, and all three were wrong, so both queries
    # returned nothing. SPEC.md rule 3 exists for exactly this, and the cost
    # of skipping it here would have been every resource family recording
    # arriving with no resource signal in it.
    #
    # In this demo a container name equals its service name for all sixteen
    # application services, which `knowledge/services.yaml` records in its
    # `container` field and a contract test asserts.
    MetricName.CONTAINER_MEMORY_BYTES: (
        "sum by (container_name) (container_memory_usage_total_bytes)"
    ),
    MetricName.CONTAINER_CPU_UTILISATION: (
        "sum by (container_name) (container_cpu_utilization_ratio)"
    ),
}

# What the service_graph connector calls a peer it could not identify. It
# emits this when a client span has no matching server span in the window,
# which happens at a window boundary and for calls out of the instrumented
# system.
#
# Edges touching it are dropped from the topology. The traffic is real, and
# keeping it would put a service named `unknown` into the dependency graph,
# where the candidate ranking would treat it as a service that could be a root
# cause and could name it in a report. An unidentifiable peer cannot inform a
# dependency ranking, so the honest thing is to leave it out rather than to
# rank a placeholder. On a four minute window this dropped 9 of 37 edges
# carrying about 15 percent of the observed traffic.
UNKNOWN_PEER = "unknown"

# Labels that name the subject of a sample, in the order they are trusted.
# Declared as a list rather than written into a chain of `or` calls, because
# adding a metric with a new subject label should be one edit in one place.
SUBJECT_LABELS: tuple[str, ...] = ("service_name", "container_name", "client")


class ExportError(Exception):
    """Telemetry could not be pulled for the requested window."""


class TelemetryExporter(Protocol):
    """Pulls one signal for one window.

    A protocol rather than a base class so the recorder can be tested with a
    plain object and so a bundle can be rebuilt from a different backend
    later without touching the recorder.
    """

    def export_metrics(self, start: datetime, end: datetime) -> list[dict[str, Any]]: ...

    def export_traces(self, start: datetime, end: datetime) -> list[dict[str, Any]]: ...

    def export_logs(self, start: datetime, end: datetime) -> list[dict[str, Any]]: ...

    def export_topology(self, start: datetime, end: datetime) -> dict[str, Any]: ...


class PrometheusExporter:
    """Exports metrics and the service dependency graph from Prometheus."""

    def __init__(
        self,
        client: httpx.Client,
        endpoints: DemoEndpoints = DEFAULT_ENDPOINTS,
        queries: dict[str, str] | None = None,
        step_seconds: int = EXPORT_STEP_SECONDS,
    ) -> None:
        self._client = client
        self._endpoints = endpoints
        self._queries = dict(queries) if queries is not None else dict(METRIC_QUERIES)
        self._step = step_seconds

    def query_range(self, query: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Run one range query and return its raw series."""
        points = (end - start).total_seconds() / self._step
        if points > MAX_POINTS_PER_QUERY:
            raise ExportError(
                f"window of {int((end - start).total_seconds())}s at {self._step}s step "
                f"is {int(points)} points, above the {MAX_POINTS_PER_QUERY} Prometheus allows"
            )
        response = self._client.get(
            f"{self._endpoints.prometheus}/api/v1/query_range",
            params={
                "query": query,
                "start": str(int(start.timestamp())),
                "end": str(int(end.timestamp())),
                "step": str(self._step),
            },
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("status") != "success":
            raise ExportError(f"Prometheus rejected the query: {query}")
        result = body.get("data", {}).get("result", [])
        if not isinstance(result, list):
            raise ExportError(f"unexpected result shape for query: {query}")
        return result

    def export_metrics(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Flatten every named query into one row per sample.

        One long table rather than one file per metric, because the replay
        backend queries it with SQL and a single table is far easier to
        reason about than seven.
        """
        rows: list[dict[str, Any]] = []
        for metric_name, query in sorted(self._queries.items()):
            for series in self.query_range(query, start, end):
                labels = series.get("metric", {})
                if not isinstance(labels, dict):
                    continue
                service = _subject(labels)
                labels_json = json.dumps(labels, sort_keys=True)
                for sample in series.get("values", []):
                    if not isinstance(sample, list) or len(sample) != 2:
                        continue
                    value = _as_float(sample[1])
                    if value is None:
                        continue
                    rows.append(
                        {
                            "timestamp": _as_datetime(sample[0]),
                            "metric_name": metric_name,
                            "service_name": service,
                            "labels_json": labels_json,
                            "value": value,
                        }
                    )
        return rows

    def export_topology(self, start: datetime, end: datetime) -> dict[str, Any]:
        """Snapshot the service dependency graph over the window.

        Edges come from the service_graph connector, which derives them by
        pairing client and server spans, so this is what actually called what
        during the incident rather than a diagram somebody drew.
        """
        # Looked up by the declared MetricName, not by a second spelling of
        # it. This previously asked for "service_graph_requests_total" while
        # the declared key is MetricName.SERVICE_GRAPH_REQUESTS, whose value is
        # "traces_service_graph_request_total", so the lookup missed and every
        # recording exported a topology with zero edges. Nothing failed: the
        # dependency ranking would simply have had no graph to walk.
        #
        # That is the fifth instance in this codebase of two components
        # agreeing on a type and disagreeing on a name, and the fifth time it
        # returned empty rather than erroring.
        totals = self._edge_totals(MetricName.SERVICE_GRAPH_REQUESTS, start, end)
        failures = self._edge_totals(MetricName.SERVICE_GRAPH_FAILED, start, end)

        edges: list[dict[str, Any]] = []
        for (client, server), requests in sorted(totals.items()):
            if client == UNKNOWN_PEER or server == UNKNOWN_PEER:
                continue
            failed = failures.get((client, server), 0.0)
            edges.append(
                EdgeRecord(
                    client=client,
                    server=server,
                    requests_per_second=requests,
                    failures_per_second=failed,
                ).as_row()
            )
        services = sorted({str(e["client"]) for e in edges} | {str(e["server"]) for e in edges})
        return {
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "services": services,
            "edges": edges,
        }

    def _edge_totals(
        self, metric_name: MetricName, start: datetime, end: datetime
    ) -> dict[tuple[str, str], float]:
        query = self._queries.get(metric_name)
        if query is None:
            raise ExportError(
                f"no query is declared for {metric_name}, so the topology would be "
                "exported empty; add it to METRIC_QUERIES rather than returning nothing"
            )
        totals: dict[tuple[str, str], float] = {}
        for series in self.query_range(query, start, end):
            labels = series.get("metric", {})
            client = str(labels.get("client", ""))
            server = str(labels.get("server", ""))
            if not client or not server:
                continue
            values = [
                v
                for v in (_as_float(s[1]) for s in series.get("values", []) if len(s) == 2)
                if v is not None
            ]
            if values:
                totals[(client, server)] = sum(values) / len(values)
        return totals

    def export_traces(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Prometheus has no spans. Use `LiveExporter`.

        Raising rather than returning an empty list. An empty list would let a
        recording succeed with no traces in it, and a bundle missing a whole
        signal is worse than a recording that refused to start.
        """
        raise ExportError(
            "Prometheus does not hold spans; compose with JaegerExporter via LiveExporter"
        )

    def export_logs(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Prometheus has no logs. Use `LiveExporter`."""
        raise ExportError(
            "Prometheus does not hold logs; compose with OpenSearchExporter via LiveExporter"
        )


def _subject(labels: dict[str, Any]) -> str:
    """Which service a sample belongs to, from whichever label carries it.

    The span metrics use `service_name`, the container metrics use
    `container_name`, and the service graph metrics use `client`. Recording an
    empty subject would produce a bundle whose per service queries silently
    return nothing for a whole metric.
    """
    for label in SUBJECT_LABELS:
        value = labels.get(label)
        if value:
            return str(value)
    return ""


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # Prometheus reports gaps as NaN. A gap is absence, not a measurement.
    return None if number != number else number


def _as_datetime(value: Any) -> datetime:
    from datetime import UTC

    return datetime.fromtimestamp(float(value), tz=UTC)


def collect_signals(
    exporter: TelemetryExporter, start: datetime, end: datetime
) -> dict[str, Sequence[dict[str, Any]] | dict[str, Any]]:
    """Pull every signal for a window, in one place so the recorder stays flat."""
    return {
        "metrics": exporter.export_metrics(start, end),
        "traces": exporter.export_traces(start, end),
        "logs": exporter.export_logs(start, end),
        "topology": exporter.export_topology(start, end),
    }


class LiveExporter:
    """Every signal from the running stack, composed from three backends.

    Satisfies `TelemetryExporter` by delegation. Composition rather than
    inheritance or one large class, because the three systems underneath are
    genuinely independent: Prometheus can be healthy while OpenSearch is
    behind, and a recording that fails should say which one failed.
    """

    def __init__(
        self,
        client: httpx.Client,
        endpoints: DemoEndpoints = DEFAULT_ENDPOINTS,
        step_seconds: int = EXPORT_STEP_SECONDS,
    ) -> None:
        from firebreak.lab.export_traces_logs import JaegerExporter, OpenSearchExporter

        self._metrics = PrometheusExporter(client, endpoints, step_seconds=step_seconds)
        self._traces = JaegerExporter(endpoints, client)
        self._logs = OpenSearchExporter(endpoints, client)

    def export_metrics(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return self._metrics.export_metrics(start, end)

    def export_topology(self, start: datetime, end: datetime) -> dict[str, Any]:
        return self._metrics.export_topology(start, end)

    def export_traces(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return self._traces.export_traces(start, end)

    def export_logs(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return self._logs.export_logs(start, end)
