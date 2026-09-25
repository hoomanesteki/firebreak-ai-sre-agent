"""Tests for firebreak.lab.export."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from firebreak.lab.export import (
    EXPORT_STEP_SECONDS,
    MAX_POINTS_PER_QUERY,
    ExportError,
    PrometheusExporter,
    collect_signals,
)
from firebreak.signals import MetricName


def _client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


def _window():
    return datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)


# --- query_range -----------------------------------------------------------


def test_query_range_builds_start_end_step_params_from_datetimes():
    captured = {}

    def handler(request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    exporter = PrometheusExporter(client=_client(handler), queries={"q": "sum(x)"})
    start, end = _window()

    result = exporter.query_range("sum(x)", start, end)

    assert result == []
    assert captured["params"] == {
        "query": "sum(x)",
        "start": str(int(start.timestamp())),
        "end": str(int(end.timestamp())),
        "step": str(EXPORT_STEP_SECONDS),
    }


def test_query_range_raises_export_error_naming_point_count_above_max():
    def unreachable(request):
        raise AssertionError("query_range must reject before making a request")

    exporter = PrometheusExporter(client=_client(unreachable), queries={"q": "sum(x)"})
    over_max = MAX_POINTS_PER_QUERY + 1
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(seconds=over_max * EXPORT_STEP_SECONDS)

    with pytest.raises(ExportError, match=str(over_max)):
        exporter.query_range("sum(x)", start, end)


def test_query_range_raises_export_error_when_status_is_not_success():
    def handler(request):
        return httpx.Response(200, json={"status": "error", "errorType": "bad_data"})

    exporter = PrometheusExporter(client=_client(handler), queries={"q": "sum(x)"})
    start, end = _window()

    with pytest.raises(ExportError, match="rejected the query"):
        exporter.query_range("sum(x)", start, end)


def test_query_range_raises_export_error_when_result_is_not_a_list():
    def handler(request):
        return httpx.Response(
            200, json={"status": "success", "data": {"result": {"not": "a list"}}}
        )

    exporter = PrometheusExporter(client=_client(handler), queries={"q": "sum(x)"})
    start, end = _window()

    with pytest.raises(ExportError, match="unexpected result shape"):
        exporter.query_range("sum(x)", start, end)


def test_query_range_raises_via_raise_for_status_for_non_2xx_response():
    def handler(request):
        return httpx.Response(500, json={"error": "boom"})

    exporter = PrometheusExporter(client=_client(handler), queries={"q": "sum(x)"})
    start, end = _window()

    with pytest.raises(httpx.HTTPStatusError):
        exporter.query_range("sum(x)", start, end)


# --- export_metrics ----------------------------------------------------


def test_export_metrics_flattens_series_into_one_row_per_sample_with_expected_keys():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": {"service_name": "cart"},
                            "values": [[1735689600, "12.5"], [1735689615, "13.0"]],
                        }
                    ]
                },
            },
        )

    exporter = PrometheusExporter(client=_client(handler), queries={"latency_ms": "sum(latency)"})
    start, end = _window()

    rows = exporter.export_metrics(start, end)

    assert len(rows) == 2
    assert set(rows[0]) == {"timestamp", "metric_name", "service_name", "labels_json", "value"}
    assert rows[0]["metric_name"] == "latency_ms"
    assert rows[0]["service_name"] == "cart"
    assert rows[0]["value"] == 12.5
    assert rows[1]["value"] == 13.0


def test_export_metrics_uses_client_as_service_name_when_service_name_absent():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": {"client": "frontend", "server": "cart"},
                            "values": [[1735689600, "3.2"]],
                        }
                    ]
                },
            },
        )

    exporter = PrometheusExporter(client=_client(handler), queries={"requests": "sum(requests)"})
    start, end = _window()

    rows = exporter.export_metrics(start, end)

    assert rows[0]["service_name"] == "frontend"


def test_export_metrics_skips_samples_with_nan_value():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": {"service_name": "cart"},
                            "values": [[1735689600, "12.5"], [1735689615, "NaN"]],
                        }
                    ]
                },
            },
        )

    exporter = PrometheusExporter(client=_client(handler), queries={"latency_ms": "sum(latency)"})
    start, end = _window()

    rows = exporter.export_metrics(start, end)

    assert len(rows) == 1
    assert rows[0]["value"] == 12.5


def test_export_metrics_skips_malformed_sample_pairs():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": {"service_name": "cart"},
                            "values": [[1735689600, "12.5"], [1735689615], "not-a-pair"],
                        }
                    ]
                },
            },
        )

    exporter = PrometheusExporter(client=_client(handler), queries={"latency_ms": "sum(latency)"})
    start, end = _window()

    rows = exporter.export_metrics(start, end)

    assert len(rows) == 1
    assert rows[0]["value"] == 12.5


def test_export_metrics_labels_json_is_sorted_key_json():
    labels = {"service_name": "cart", "app": "otel-demo"}

    def handler(request):
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"result": [{"metric": labels, "values": [[1735689600, "1.0"]]}]},
            },
        )

    exporter = PrometheusExporter(client=_client(handler), queries={"latency_ms": "sum(latency)"})
    start, end = _window()

    rows = exporter.export_metrics(start, end)

    assert rows[0]["labels_json"] == json.dumps(labels, sort_keys=True)
    assert rows[0]["labels_json"] == '{"app": "otel-demo", "service_name": "cart"}'


# --- export_topology -----------------------------------------------------


def test_export_topology_builds_edges_with_rates_and_error_ratio():
    def handler(request):
        query = request.url.params["query"]
        if query == "requests_query":
            result = [
                {
                    "metric": {"client": "frontend", "server": "cart"},
                    "values": [[1735689600, "10"], [1735689615, "20"]],
                }
            ]
        elif query == "failed_query":
            result = [
                {
                    "metric": {"client": "frontend", "server": "cart"},
                    "values": [[1735689600, "1"], [1735689615, "3"]],
                }
            ]
        else:
            raise AssertionError(f"unexpected query: {query}")
        return httpx.Response(200, json={"status": "success", "data": {"result": result}})

    queries = {
        # The declared names, deliberately. An earlier version of this stub
        # used "service_graph_requests_total" and "service_graph_failed_total",
        # which are not what `METRIC_QUERIES` is keyed by, and because the
        # production lookup used the same two wrong names the test passed while
        # every real export produced a topology with no edges. A stub written
        # from the implementation cannot catch a vocabulary bug; it has to be
        # written from the declared vocabulary.
        MetricName.SERVICE_GRAPH_REQUESTS: "requests_query",
        MetricName.SERVICE_GRAPH_FAILED: "failed_query",
    }
    exporter = PrometheusExporter(client=_client(handler), queries=queries)
    start, end = _window()

    topology = exporter.export_topology(start, end)

    assert topology["edges"] == [
        {
            "client": "frontend",
            "server": "cart",
            "requests_per_second": 15.0,
            "failures_per_second": 2.0,
            "error_ratio": round(2 / 15, 6),
        }
    ]
    assert topology["services"] == ["cart", "frontend"]


def test_export_topology_error_ratio_is_zero_when_requests_are_zero():
    def handler(request):
        query = request.url.params["query"]
        if query == "requests_query":
            result = [{"metric": {"client": "a", "server": "b"}, "values": [[1735689600, "0"]]}]
        elif query == "failed_query":
            result = [{"metric": {"client": "a", "server": "b"}, "values": [[1735689600, "5"]]}]
        else:
            raise AssertionError(f"unexpected query: {query}")
        return httpx.Response(200, json={"status": "success", "data": {"result": result}})

    queries = {
        # The declared names, deliberately. An earlier version of this stub
        # used "service_graph_requests_total" and "service_graph_failed_total",
        # which are not what `METRIC_QUERIES` is keyed by, and because the
        # production lookup used the same two wrong names the test passed while
        # every real export produced a topology with no edges. A stub written
        # from the implementation cannot catch a vocabulary bug; it has to be
        # written from the declared vocabulary.
        MetricName.SERVICE_GRAPH_REQUESTS: "requests_query",
        MetricName.SERVICE_GRAPH_FAILED: "failed_query",
    }
    exporter = PrometheusExporter(client=_client(handler), queries=queries)
    start, end = _window()

    topology = exporter.export_topology(start, end)

    assert topology["edges"][0]["requests_per_second"] == 0.0
    assert topology["edges"][0]["error_ratio"] == 0.0


def test_export_topology_skips_series_missing_client_or_server():
    def handler(request):
        query = request.url.params["query"]
        if query == "requests_query":
            result = [
                {
                    "metric": {"client": "frontend", "server": "cart"},
                    "values": [[1735689600, "1"]],
                },
                {"metric": {"server": "only-server"}, "values": [[1735689600, "1"]]},
                {"metric": {"client": "only-client"}, "values": [[1735689600, "1"]]},
            ]
        elif query == "failed_query":
            result = []
        else:
            raise AssertionError(f"unexpected query: {query}")
        return httpx.Response(200, json={"status": "success", "data": {"result": result}})

    queries = {
        # The declared names, deliberately. An earlier version of this stub
        # used "service_graph_requests_total" and "service_graph_failed_total",
        # which are not what `METRIC_QUERIES` is keyed by, and because the
        # production lookup used the same two wrong names the test passed while
        # every real export produced a topology with no edges. A stub written
        # from the implementation cannot catch a vocabulary bug; it has to be
        # written from the declared vocabulary.
        MetricName.SERVICE_GRAPH_REQUESTS: "requests_query",
        MetricName.SERVICE_GRAPH_FAILED: "failed_query",
    }
    exporter = PrometheusExporter(client=_client(handler), queries=queries)
    start, end = _window()

    topology = exporter.export_topology(start, end)

    assert len(topology["edges"]) == 1
    assert topology["edges"][0]["client"] == "frontend"
    assert topology["services"] == ["cart", "frontend"]


# --- export_traces / export_logs ----------------------------------------


def test_export_traces_from_prometheus_points_at_the_right_backend():
    exporter = PrometheusExporter(client=_client(lambda request: httpx.Response(200, json={})))
    start, end = _window()

    with pytest.raises(ExportError, match="does not hold"):
        exporter.export_traces(start, end)


def test_export_logs_from_prometheus_points_at_the_right_backend():
    exporter = PrometheusExporter(client=_client(lambda request: httpx.Response(200, json={})))
    start, end = _window()

    with pytest.raises(ExportError, match="does not hold"):
        exporter.export_logs(start, end)


# --- collect_signals -----------------------------------------------------


class _StubExporter:
    def __init__(self):
        self.calls: list[str] = []

    def export_metrics(self, start, end):
        self.calls.append("metrics")
        return [{"value": 1.0}]

    def export_traces(self, start, end):
        self.calls.append("traces")
        return [{"trace_id": "t"}]

    def export_logs(self, start, end):
        self.calls.append("logs")
        return [{"body": "hi"}]

    def export_topology(self, start, end):
        self.calls.append("topology")
        return {"services": []}


def test_collect_signals_calls_all_four_exporter_methods_and_returns_all_four_keys():
    exporter = _StubExporter()
    start, end = _window()

    signals = collect_signals(exporter, start, end)

    assert exporter.calls == ["metrics", "traces", "logs", "topology"]
    assert set(signals) == {"metrics", "traces", "logs", "topology"}
    assert signals["metrics"] == [{"value": 1.0}]
    assert signals["topology"] == {"services": []}
