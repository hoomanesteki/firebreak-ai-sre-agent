"""Tests for firebreak.lab.export_traces_logs.

Built on recorded payload shapes rather than a live stack, so the suite runs
anywhere. The shapes themselves were copied from the running demo on
2026-09-25, which is the part that could not be guessed: Jaeger puts the
service name in a `processes` map rather than on the span, and OpenSearch
mixes the collector's own logs in with the application's.

`tests/integration/` would be the place for a test against a live stack. These
cover the parsing, which is where the mistakes are.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from firebreak.lab.endpoints import DemoEndpoints
from firebreak.lab.export_traces_logs import (
    COLLECTOR_SERVICE,
    ExportError,
    JaegerExporter,
    OpenSearchExporter,
    _parse_timestamp,
)
from firebreak.signals import StatusCode

START = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)
END = datetime(2026, 9, 25, 12, 5, 0, tzinfo=UTC)
ENDPOINTS = DemoEndpoints()


def _span(
    span_id: str = "aaaa",
    trace_id: str = "tttt",
    process_id: str = "p1",
    tags: list[dict[str, Any]] | None = None,
    references: list[dict[str, Any]] | None = None,
    start_micros: int = 1774440000000000,
    duration_micros: int = 12345,
) -> dict[str, Any]:
    return {
        "spanID": span_id,
        "traceID": trace_id,
        "processID": process_id,
        "operationName": "GET /api/cart",
        "startTime": start_micros,
        "duration": duration_micros,
        "tags": tags if tags is not None else [{"key": "span.kind", "value": "client"}],
        "references": references or [],
    }


def _jaeger_client(traces: list[dict[str, Any]], services: list[str] | None = None) -> httpx.Client:
    """A client answering Jaeger's two endpoints from a fixed payload."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/services"):
            names = services if services is not None else ["frontend"]
            return httpx.Response(200, json={"data": names})
        if request.url.path.endswith("/traces"):
            return httpx.Response(200, json={"data": traces})
        return httpx.Response(404, text="not found")

    return httpx.Client(transport=httpx.MockTransport(handler))


class TestJaegerBasePath:
    def test_requests_go_under_the_confirmed_base_path(self) -> None:
        """The detail that cost the most to find.

        Port 16686 returns 404 for every path, including the root, because the
        demo configures a base path of /jaeger/ui. A request without it gets
        nothing, and an exporter that swallowed that would record no traces.
        """
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, json={"data": []})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        JaegerExporter(ENDPOINTS, client).services()
        assert seen == ["/jaeger/ui/api/services"]

    def test_an_http_error_is_reported_not_swallowed(self) -> None:
        """A recording with no traces must fail rather than look empty."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="not found")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(ExportError, match="Jaeger request"):
            JaegerExporter(ENDPOINTS, client).services()

    def test_invalid_json_is_reported(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not json")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(ExportError, match="invalid JSON"):
            JaegerExporter(ENDPOINTS, client).services()


class TestJaegerSpanParsing:
    def test_the_service_name_comes_from_the_processes_map(self) -> None:
        """A Jaeger span does not carry its own service name.

        It carries a processID indexing into the trace's processes map, and an
        exporter that looked for `serviceName` on the span would produce a
        bundle with no services in it.
        """
        traces = [
            {
                "traceID": "tttt",
                "spans": [_span()],
                "processes": {"p1": {"serviceName": "cart"}},
            }
        ]
        rows = JaegerExporter(ENDPOINTS, _jaeger_client(traces)).export_traces(START, END)
        assert [row["service_name"] for row in rows] == ["cart"]

    def test_a_span_with_no_resolvable_service_is_dropped(self) -> None:
        """Rather than filed under a placeholder.

        An `unknown` bucket looks like a real service in a ranking.
        """
        traces = [{"traceID": "tttt", "spans": [_span(process_id="missing")], "processes": {}}]
        assert JaegerExporter(ENDPOINTS, _jaeger_client(traces)).export_traces(START, END) == []

    def test_duration_is_converted_from_microseconds_to_milliseconds(self) -> None:
        traces = [
            {
                "traceID": "t",
                "spans": [_span(duration_micros=12_345)],
                "processes": {"p1": {"serviceName": "cart"}},
            }
        ]
        rows = JaegerExporter(ENDPOINTS, _jaeger_client(traces)).export_traces(START, END)
        assert rows[0]["duration_ms"] == pytest.approx(12.345)

    def test_the_start_time_is_utc(self) -> None:
        traces = [
            {"traceID": "t", "spans": [_span()], "processes": {"p1": {"serviceName": "cart"}}}
        ]
        rows = JaegerExporter(ENDPOINTS, _jaeger_client(traces)).export_traces(START, END)
        assert rows[0]["start_time"].tzinfo is not None
        assert rows[0]["start_time"].utcoffset().total_seconds() == 0  # type: ignore[union-attr]

    def test_only_a_child_of_reference_is_a_parent(self) -> None:
        """A FOLLOWS_FROM reference is not a parent.

        Treating it as one produces a critical path that does not exist.
        """
        traces = [
            {
                "traceID": "t",
                "spans": [
                    _span(
                        span_id="a",
                        references=[{"refType": "FOLLOWS_FROM", "spanID": "z"}],
                    ),
                    _span(
                        span_id="b",
                        references=[{"refType": "CHILD_OF", "spanID": "a"}],
                    ),
                ],
                "processes": {"p1": {"serviceName": "cart"}},
            }
        ]
        rows = {
            row["span_id"]: row
            for row in JaegerExporter(ENDPOINTS, _jaeger_client(traces)).export_traces(START, END)
        }
        assert rows["a"]["parent_span_id"] is None
        assert rows["b"]["parent_span_id"] == "a"

    def test_span_kind_is_uppercased_to_match_the_span_metrics(self) -> None:
        traces = [
            {"traceID": "t", "spans": [_span()], "processes": {"p1": {"serviceName": "cart"}}}
        ]
        rows = JaegerExporter(ENDPOINTS, _jaeger_client(traces)).export_traces(START, END)
        assert rows[0]["span_kind"] == "CLIENT"

    def test_a_span_with_no_kind_is_internal(self) -> None:
        traces = [
            {"traceID": "t", "spans": [_span(tags=[])], "processes": {"p1": {"serviceName": "c"}}}
        ]
        rows = JaegerExporter(ENDPOINTS, _jaeger_client(traces)).export_traces(START, END)
        assert rows[0]["span_kind"] == "INTERNAL"

    def test_a_malformed_span_is_dropped_rather_than_crashing_the_export(self) -> None:
        traces = [
            {
                "traceID": "t",
                "spans": [{"spanID": "a"}, _span(span_id="b")],
                "processes": {"p1": {"serviceName": "cart"}},
            }
        ]
        rows = JaegerExporter(ENDPOINTS, _jaeger_client(traces)).export_traces(START, END)
        assert [row["span_id"] for row in rows] == ["b"]


class TestJaegerStatusMapping:
    @pytest.mark.parametrize(
        ("tags", "expected"),
        [
            ([{"key": "otel.status_code", "value": "ERROR"}], StatusCode.ERROR.value),
            ([{"key": "otel.status_code", "value": "OK"}], StatusCode.OK.value),
            ([{"key": "error", "value": True}], StatusCode.ERROR.value),
            ([], StatusCode.UNSET.value),
            # A non-zero gRPC code is not a span error, and conflating the two
            # would invent errors on spans that reported none.
            ([{"key": "rpc.grpc.status_code", "value": 13}], StatusCode.UNSET.value),
        ],
    )
    def test_it_maps_onto_the_declared_vocabulary(
        self, tags: list[dict[str, Any]], expected: str
    ) -> None:
        traces = [
            {
                "traceID": "t",
                "spans": [_span(tags=tags)],
                "processes": {"p1": {"serviceName": "cart"}},
            }
        ]
        rows = JaegerExporter(ENDPOINTS, _jaeger_client(traces)).export_traces(START, END)
        assert rows[0]["status_code"] == expected

    def test_an_unset_status_is_not_reported_as_ok(self) -> None:
        """Most successful spans carry no status at all.

        Reporting those as OK would overstate what the telemetry says, and the
        alert rules filter on the exact string.
        """
        traces = [
            {"traceID": "t", "spans": [_span(tags=[])], "processes": {"p1": {"serviceName": "c"}}}
        ]
        rows = JaegerExporter(ENDPOINTS, _jaeger_client(traces)).export_traces(START, END)
        assert rows[0]["status_code"] == StatusCode.UNSET.value


class TestJaegerDeduplicationAndOrder:
    def test_a_span_returned_for_several_services_is_recorded_once(self) -> None:
        """Jaeger returns a whole trace per service query.

        Without deduplication a bundle holds one span several times and every
        per service count derived from it is inflated.
        """
        traces = [
            {"traceID": "t", "spans": [_span()], "processes": {"p1": {"serviceName": "cart"}}}
        ]
        client = _jaeger_client(traces, services=["frontend", "cart", "checkout"])
        rows = JaegerExporter(ENDPOINTS, client).export_traces(START, END)
        assert len(rows) == 1

    def test_rows_are_sorted_so_two_exports_are_identical(self) -> None:
        """Evidence ids hash the exported parquet, so order has to be fixed."""
        traces = [
            {
                "traceID": "t",
                "spans": [
                    _span(span_id="b", start_micros=200),
                    _span(span_id="a", start_micros=100),
                    _span(span_id="c", start_micros=100),
                ],
                "processes": {"p1": {"serviceName": "cart"}},
            }
        ]
        rows = JaegerExporter(ENDPOINTS, _jaeger_client(traces)).export_traces(START, END)
        assert [row["span_id"] for row in rows] == ["a", "c", "b"]


def _log_hit(
    service: str = "checkout",
    body: str = "[PlaceOrder]",
    severity: str | None = "INFO",
    trace_id: str | None = "71d460cc447cb115",
    timestamp: str = "2026-09-25T11:59:01.22330725Z",
    sort: list[Any] | None = None,
) -> dict[str, Any]:
    source: dict[str, Any] = {
        "@timestamp": timestamp,
        "body": body,
        "resource": {"service.name": service},
        "attributes": {"user_currency": "USD"},
    }
    if severity is not None:
        source["severity"] = {"text": severity, "number": 9}
    if trace_id is not None:
        source["traceId"] = trace_id
    return {"_source": source, "sort": sort or [timestamp, "id1"]}


def _opensearch_client(pages: list[list[dict[str, Any]]]) -> httpx.Client:
    """A client returning each page in turn, then nothing."""
    remaining = list(pages)
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        hits = remaining.pop(0) if remaining else []
        return httpx.Response(200, json={"hits": {"hits": hits}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.captured = captured  # type: ignore[attr-defined]
    return client


class TestOpenSearchQuery:
    def test_the_collector_is_excluded_by_its_keyword_subfield(self) -> None:
        """Two traps in one assertion.

        The collector's own logs arrive in the same index as the application's,
        so a bundle that kept them would record the observability stack talking
        about itself. And a term query on `resource.service.name` fails outright
        because the field is mapped as text; `.keyword` is the one that works.
        """
        client = _opensearch_client([[]])
        OpenSearchExporter(ENDPOINTS, client).export_logs(START, END)
        body = client.captured[0]  # type: ignore[attr-defined]
        must_not = body["query"]["bool"]["must_not"]
        assert must_not == [{"term": {"resource.service.name.keyword": COLLECTOR_SERVICE}}]

    def test_the_window_is_a_range_filter_on_the_timestamp(self) -> None:
        client = _opensearch_client([[]])
        OpenSearchExporter(ENDPOINTS, client).export_logs(START, END)
        body = client.captured[0]  # type: ignore[attr-defined]
        window = body["query"]["bool"]["filter"][0]["range"]["@timestamp"]
        assert window == {"gte": START.isoformat(), "lte": END.isoformat()}

    def test_the_sort_has_a_tiebreaker(self) -> None:
        """Two records in the same nanosecond would otherwise page unstably."""
        client = _opensearch_client([[]])
        OpenSearchExporter(ENDPOINTS, client).export_logs(START, END)
        body = client.captured[0]  # type: ignore[attr-defined]
        assert body["sort"] == [{"@timestamp": "asc"}, {"_id": "asc"}]

    def test_paging_uses_search_after(self) -> None:
        """Rather than from and size, which max_result_window caps."""
        first = [_log_hit(sort=["t1", "a"]) for _ in range(2)]
        client = _opensearch_client([first, []])
        OpenSearchExporter(ENDPOINTS, client, page_size=2).export_logs(START, END)
        bodies = client.captured  # type: ignore[attr-defined]
        assert "search_after" not in bodies[0]
        assert bodies[1]["search_after"] == ["t1", "a"]

    def test_a_short_page_ends_the_paging(self) -> None:
        client = _opensearch_client([[_log_hit()], [_log_hit()]])
        rows = OpenSearchExporter(ENDPOINTS, client, page_size=10).export_logs(START, END)
        assert len(rows) == 1
        assert len(client.captured) == 1  # type: ignore[attr-defined]

    def test_a_failed_search_is_reported(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(ExportError, match="OpenSearch search failed"):
            OpenSearchExporter(ENDPOINTS, client).export_logs(START, END)

    def test_a_missing_hits_envelope_is_reported(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"unexpected": True})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(ExportError, match="no hits envelope"):
            OpenSearchExporter(ENDPOINTS, client).export_logs(START, END)


class TestOpenSearchRowParsing:
    def test_a_record_becomes_the_declared_log_shape(self) -> None:
        client = _opensearch_client([[_log_hit()], []])
        rows = OpenSearchExporter(ENDPOINTS, client, page_size=1).export_logs(START, END)
        row = rows[0]
        assert sorted(row) == [
            "attributes_json",
            "body",
            "service_name",
            "severity",
            "timestamp",
            "trace_id",
        ]
        assert row["service_name"] == "checkout"
        assert row["trace_id"] == "71d460cc447cb115"
        assert json.loads(row["attributes_json"]) == {"user_currency": "USD"}

    def test_severity_is_uppercased_because_the_demo_disagrees_about_case(self) -> None:
        """The collector writes `info` and checkout writes `INFO`.

        The log tools filter on an exact severity, so one of the two would be
        invisible.
        """
        client = _opensearch_client([[_log_hit(severity="info")], []])
        rows = OpenSearchExporter(ENDPOINTS, client, page_size=1).export_logs(START, END)
        assert rows[0]["severity"] == "INFO"

    def test_a_record_with_no_severity_is_recorded_as_unspecified(self) -> None:
        client = _opensearch_client([[_log_hit(severity=None)], []])
        rows = OpenSearchExporter(ENDPOINTS, client, page_size=1).export_logs(START, END)
        assert rows[0]["severity"] == "UNSPECIFIED"

    def test_a_record_with_no_trace_id_records_none(self) -> None:
        client = _opensearch_client([[_log_hit(trace_id=None)], []])
        rows = OpenSearchExporter(ENDPOINTS, client, page_size=1).export_logs(START, END)
        assert rows[0]["trace_id"] is None

    def test_a_record_with_no_service_is_dropped(self) -> None:
        hit = _log_hit()
        hit["_source"]["resource"] = {}
        client = _opensearch_client([[hit], []])
        assert OpenSearchExporter(ENDPOINTS, client, page_size=1).export_logs(START, END) == []

    def test_a_record_with_no_body_is_dropped(self) -> None:
        hit = _log_hit()
        del hit["_source"]["body"]
        client = _opensearch_client([[hit], []])
        assert OpenSearchExporter(ENDPOINTS, client, page_size=1).export_logs(START, END) == []


class TestTimestampParsing:
    def test_nanosecond_precision_is_accepted(self) -> None:
        """The collector writes nine fractional digits.

        `datetime.fromisoformat` rejects more than six on some versions, and
        the bundle stores milliseconds anyway.
        """
        parsed = _parse_timestamp("2026-09-25T11:59:01.223307250Z")
        assert parsed.year == 2026
        assert parsed.microsecond == 223307
        assert parsed.tzinfo is not None

    def test_a_plain_timestamp_is_accepted(self) -> None:
        assert _parse_timestamp("2026-09-25T11:59:01Z").second == 1

    def test_an_explicit_offset_survives(self) -> None:
        parsed = _parse_timestamp("2026-09-25T11:59:01.223307250+02:00")
        assert parsed.utcoffset().total_seconds() == 7200  # type: ignore[union-attr]

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        """Phase 3 lost a day to timestamps read in the machine's timezone."""
        assert _parse_timestamp("2026-09-25T11:59:01.223307").tzinfo is UTC
