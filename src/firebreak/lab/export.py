"""Pull a window of telemetry out of the running stack.

Recording an incident means freezing what the observability stack saw while
it happened. This is the seam that does the pulling, kept behind a protocol
so the recorder can be exercised without a stack and so the three backends
can be written and verified one at a time.

Only the Prometheus exporter is implemented here. The Jaeger and OpenSearch
query APIs have not been checked against the pinned release yet, and SPEC.md
rule 3 is explicit that APIs are confirmed rather than assumed. They arrive
with the rest of the backends in Phase 3, before any real recording happens.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

import httpx

from firebreak.lab.endpoints import DEFAULT_ENDPOINTS, DemoEndpoints

# Resolution of an exported metrics series. SPEC.md Section 6.2 records
# bundles at a 15 second step, which is also the export interval the live
# overlay sets on the services so that the samples actually exist.
EXPORT_STEP_SECONDS = 15
MAX_POINTS_PER_QUERY = 11_000

# What a recording captures, per service, for every scenario family. Named
# templates rather than free-form PromQL, for the same reason the tool layer
# uses them: a query that can be anything cannot be reviewed.
METRIC_QUERIES: dict[str, str] = {
    "span_calls_total": (
        "sum by (service_name, status_code) (rate(traces_span_metrics_calls_total[2m]))"
    ),
    "span_duration_p95_ms": (
        "histogram_quantile(0.95, sum by (service_name, le) "
        "(rate(traces_span_metrics_duration_milliseconds_bucket[2m])))"
    ),
    "span_duration_p50_ms": (
        "histogram_quantile(0.50, sum by (service_name, le) "
        "(rate(traces_span_metrics_duration_milliseconds_bucket[2m])))"
    ),
    "service_graph_requests_total": (
        "sum by (client, server) (rate(traces_service_graph_request_total[2m]))"
    ),
    "service_graph_failed_total": (
        "sum by (client, server) (rate(traces_service_graph_request_failed_total[2m]))"
    ),
    "container_memory_usage_bytes": "sum by (service_name) (container_memory_usage)",
    "container_cpu_utilisation": "sum by (service_name) (container_cpu_utilization)",
}


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
                service = str(labels.get("service_name") or labels.get("client") or "")
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
        totals = self._edge_totals("service_graph_requests_total", start, end)
        failures = self._edge_totals("service_graph_failed_total", start, end)

        edges: list[dict[str, Any]] = []
        for (client, server), requests in sorted(totals.items()):
            failed = failures.get((client, server), 0.0)
            edges.append(
                {
                    "client": client,
                    "server": server,
                    "requests_per_second": round(requests, 6),
                    "failures_per_second": round(failed, 6),
                    "error_ratio": round(failed / requests, 6) if requests else 0.0,
                }
            )
        services = sorted({str(e["client"]) for e in edges} | {str(e["server"]) for e in edges})
        return {
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "services": services,
            "edges": edges,
        }

    def _edge_totals(
        self, metric_name: str, start: datetime, end: datetime
    ) -> dict[tuple[str, str], float]:
        query = self._queries.get(metric_name)
        if query is None:
            return {}
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
        """Not implemented here. See the module docstring."""
        raise ExportError(
            "trace export needs the Jaeger query API, which is confirmed and "
            "implemented in Phase 3 before any real recording"
        )

    def export_logs(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Not implemented here. See the module docstring."""
        raise ExportError(
            "log export needs the OpenSearch query API, which is confirmed and "
            "implemented in Phase 3 before any real recording"
        )


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
