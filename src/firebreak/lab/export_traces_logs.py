"""Trace and log exporters, confirmed against the pinned release.

These are the two gaps `lab/export.py` has carried since Phase 1, and the
reason they stayed gaps is SPEC.md rule 3: an API is confirmed against the
running system rather than assumed. Both were confirmed on 2026-09-25 against
the pinned demo, and what was found is worth writing down because neither was
where the documentation would suggest.

**Jaeger 2.19.0 serves its query API under a base path.** `/api/services` on
port 16686 returns 404 for everything, including `/`, because the demo
configures a base path of `/jaeger/ui`. The working base is
`http://localhost:16686/jaeger/ui/api`, and the same paths answer through the
Envoy proxy on 8080. The payload is the version 1 JSON shape, which Jaeger 2
keeps: `{"data": [{"traceID", "spans": [...], "processes": {...}}]}`. A span
does not carry its own service name; it carries a `processID` that indexes
into the trace's `processes` map.

**OpenSearch 3.7.0 holds application logs, and the Phase 1 note saying the
exporter still fails is stale.** The `otel-logs-*` indices held 1,156
application log documents from 17 services when this was written, alongside
the collector's own self-logs. Application logs also carry `traceId` and
`spanId`, so log to trace correlation works.

**Two OpenSearch details that would otherwise cost an afternoon.** A term
query or aggregation on `resource.service.name` fails, because the field is
mapped as text; the `.keyword` subfield is the one to use. And the collector's
own logs arrive under `service.name: otelcol-contrib` in the same index as the
application's, so they have to be excluded or a bundle records the observability
stack talking about itself.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from firebreak.lab.endpoints import DEFAULT_ENDPOINTS, DemoEndpoints
from firebreak.signals import StatusCode

# Jaeger's query API sits under this base path in the pinned demo. Confirmed,
# not assumed: without it every request returns 404.
JAEGER_API_BASE = "/jaeger/ui/api"

# OpenSearch index pattern the collector writes logs to, and the service whose
# logs are the observability stack rather than the system under test.
LOGS_INDEX_PATTERN = "otel-logs-*"
COLLECTOR_SERVICE = "otelcol-contrib"

# Per request caps. A recording window is twenty minutes and a page of a
# thousand covers it several times over, but an unbounded query against a
# stack that has been running for hours would pull the lot.
TRACES_PER_SERVICE = 400
LOGS_PAGE_SIZE = 1000
MAX_LOG_PAGES = 50

HTTP_TIMEOUT_SECONDS = 60.0


class ExportError(Exception):
    """Telemetry could not be pulled out of the running stack."""


def _micros(moment: datetime) -> int:
    """Jaeger takes microseconds since the epoch, as integers."""
    return int(moment.timestamp() * 1_000_000)


def _span_kind(tags: dict[str, Any]) -> str:
    """The span kind, as the declared vocabulary spells it.

    Jaeger reports `span.kind` as a lowercase word such as `client`. The
    bundle records it uppercased, matching how the collector's span metrics
    label it, so the two sources agree.
    """
    kind = tags.get("span.kind")
    return str(kind).upper() if kind else "INTERNAL"


def _status_code(tags: dict[str, Any]) -> str:
    """Map a Jaeger span's tags onto the declared status vocabulary.

    Three sources, checked in order of how directly they say it. `otel.status_code`
    is the OpenTelemetry status when the exporter set one. A bare `error` tag is
    Jaeger's own convention and appears when it did not. An unset status is
    `STATUS_CODE_UNSET`, which is what most successful spans carry: reporting
    those as OK would overstate what the telemetry says.

    `rpc.grpc.status_code` is deliberately not consulted. A non-zero gRPC code
    is not necessarily a span error, and conflating the two would invent errors
    on spans that reported none.
    """
    declared = tags.get("otel.status_code")
    if isinstance(declared, str):
        upper = declared.upper()
        if upper in {"ERROR", "STATUS_CODE_ERROR"}:
            return StatusCode.ERROR.value
        if upper in {"OK", "STATUS_CODE_OK"}:
            return StatusCode.OK.value
    if tags.get("error") is True:
        return StatusCode.ERROR.value
    return StatusCode.UNSET.value


def _parent_span_id(references: list[dict[str, Any]]) -> str | None:
    """The CHILD_OF parent within the same trace, if there is one.

    A FOLLOWS_FROM reference is not a parent, and a reference into another
    trace is not a parent either. Treating either as one would produce a
    critical path that does not exist.
    """
    for reference in references:
        if reference.get("refType") == "CHILD_OF":
            parent = reference.get("spanID")
            if isinstance(parent, str) and parent:
                return parent
    return None


def _tag_map(tags: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        str(tag["key"]): tag.get("value") for tag in tags if isinstance(tag, dict) and "key" in tag
    }


class JaegerExporter:
    """Pull spans for a window out of Jaeger.

    Queries per service, because Jaeger's trace search requires a service
    name: there is no "everything" query. The service list comes from Jaeger
    itself rather than from a hard coded inventory, so a recording captures
    whatever was actually running.
    """

    def __init__(
        self,
        endpoints: DemoEndpoints = DEFAULT_ENDPOINTS,
        client: httpx.Client | None = None,
        traces_per_service: int = TRACES_PER_SERVICE,
    ) -> None:
        self._base = endpoints.jaeger.rstrip("/") + JAEGER_API_BASE
        self._client = client or httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
        self._traces_per_service = traces_per_service

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._client.get(f"{self._base}{path}", params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as error:
            raise ExportError(f"Jaeger request to {path} failed: {error}") from error
        except json.JSONDecodeError as error:
            raise ExportError(f"Jaeger returned invalid JSON from {path}: {error}") from error
        if not isinstance(payload, dict):
            raise ExportError(f"Jaeger returned {type(payload).__name__} from {path}")
        return payload

    def services(self) -> list[str]:
        """Every service Jaeger has seen a span from."""
        data = self._get("/services").get("data")
        if not isinstance(data, list):
            raise ExportError("Jaeger /services did not return a list")
        return sorted(str(name) for name in data)

    def export_traces(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Every span in the window, in the bundle's declared span shape.

        Deduplicated by span id across services. The same span is returned by
        every service whose trace it appears in, so without this a bundle
        would hold one span several times and every per service count derived
        from it would be inflated.

        Sorted by start time then span id, so two exports of the same window
        produce byte identical parquet and therefore identical evidence ids.
        """
        seen: dict[str, dict[str, Any]] = {}
        for service in self.services():
            payload = self._get(
                "/traces",
                {
                    "service": service,
                    "start": _micros(start),
                    "end": _micros(end),
                    "limit": self._traces_per_service,
                },
            )
            for trace in payload.get("data") or []:
                if not isinstance(trace, dict):
                    continue
                processes = trace.get("processes") or {}
                for span in trace.get("spans") or []:
                    row = self._span_row(span, processes)
                    if row is not None:
                        seen[row["span_id"]] = row

        return sorted(seen.values(), key=lambda row: (row["start_time"], row["span_id"]))

    def _span_row(self, span: Any, processes: dict[str, Any]) -> dict[str, Any] | None:
        """One Jaeger span as a bundle row, or None when it cannot be read.

        A span with no resolvable service name is dropped rather than filed
        under a placeholder. Every per service metric downstream would
        otherwise carry a bucket named `unknown` that looks like a real
        service in a ranking.
        """
        if not isinstance(span, dict):
            return None
        span_id = span.get("spanID")
        trace_id = span.get("traceID")
        if not isinstance(span_id, str) or not isinstance(trace_id, str):
            return None

        process_id = span.get("processID")
        process = processes.get(process_id) if isinstance(process_id, str) else None
        service = process.get("serviceName") if isinstance(process, dict) else None
        if not isinstance(service, str) or not service:
            return None

        tags = _tag_map(span.get("tags") or [])
        try:
            start_micros = int(span["startTime"])
            duration_micros = int(span["duration"])
        except (KeyError, TypeError, ValueError):
            return None

        return {
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": _parent_span_id(span.get("references") or []),
            "service_name": service,
            "span_name": str(span.get("operationName") or ""),
            "span_kind": _span_kind(tags),
            "start_time": datetime.fromtimestamp(start_micros / 1_000_000, tz=UTC),
            "duration_ms": duration_micros / 1000.0,
            "status_code": _status_code(tags),
            "attributes_json": json.dumps(
                {key: tags[key] for key in sorted(tags)}, sort_keys=True, default=str
            ),
        }


class OpenSearchExporter:
    """Pull application log records for a window out of OpenSearch."""

    def __init__(
        self,
        endpoints: DemoEndpoints = DEFAULT_ENDPOINTS,
        client: httpx.Client | None = None,
        page_size: int = LOGS_PAGE_SIZE,
        max_pages: int = MAX_LOG_PAGES,
    ) -> None:
        self._base = endpoints.opensearch.rstrip("/")
        self._client = client or httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
        self._page_size = page_size
        self._max_pages = max_pages

    def export_logs(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Every application log record in the window, oldest first.

        Paged with `search_after` rather than `from` and `size`. Deep paging
        with an offset is capped by `max_result_window` at ten thousand
        documents and gets slower the further it goes; `search_after` has
        neither problem and gives a stable order across pages.

        The collector's own logs are excluded. They arrive in the same index
        as the application's, and a bundle recording them would be recording
        the observability stack talking about itself.
        """
        body: dict[str, Any] = {
            "size": self._page_size,
            "query": {
                "bool": {
                    "filter": [
                        {
                            "range": {
                                "@timestamp": {
                                    "gte": start.isoformat(),
                                    "lte": end.isoformat(),
                                }
                            }
                        }
                    ],
                    "must_not": [{"term": {"resource.service.name.keyword": COLLECTOR_SERVICE}}],
                }
            },
            # Sorted on a tiebreaker as well as the timestamp, because two
            # records written in the same nanosecond would otherwise page
            # nondeterministically and a bundle would differ between exports.
            "sort": [{"@timestamp": "asc"}, {"_id": "asc"}],
        }

        rows: list[dict[str, Any]] = []
        search_after: list[Any] | None = None
        for _ in range(self._max_pages):
            page = dict(body)
            if search_after is not None:
                page["search_after"] = search_after
            hits = self._search(page)
            if not hits:
                break
            for hit in hits:
                row = self._log_row(hit)
                if row is not None:
                    rows.append(row)
            search_after = hits[-1].get("sort")
            if search_after is None or len(hits) < self._page_size:
                break
        return rows

    def _search(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            response = self._client.post(f"{self._base}/{LOGS_INDEX_PATTERN}/_search", json=body)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as error:
            raise ExportError(f"OpenSearch search failed: {error}") from error
        except json.JSONDecodeError as error:
            raise ExportError(f"OpenSearch returned invalid JSON: {error}") from error
        if not isinstance(payload, dict) or "hits" not in payload:
            raise ExportError(f"OpenSearch returned no hits envelope: {payload!r:.200}")
        hits = payload["hits"].get("hits")
        return [hit for hit in hits if isinstance(hit, dict)] if isinstance(hits, list) else []

    def _log_row(self, hit: dict[str, Any]) -> dict[str, Any] | None:
        """One OpenSearch hit as a bundle row, or None when unusable.

        A record with no service name or no body is dropped. Both are required
        by the bundle schema, and a placeholder would appear downstream as a
        service that does not exist or an empty log template.
        """
        source = hit.get("_source")
        if not isinstance(source, dict):
            return None

        resource = source.get("resource")
        service = resource.get("service.name") if isinstance(resource, dict) else None
        body = source.get("body")
        timestamp = source.get("@timestamp")
        if not isinstance(service, str) or not service:
            return None
        if body is None or not isinstance(timestamp, str):
            return None

        severity = source.get("severity")
        severity_text = (
            severity.get("text") if isinstance(severity, dict) else None
        ) or "UNSPECIFIED"

        trace_id = source.get("traceId")
        attributes = source.get("attributes")
        return {
            "timestamp": _parse_timestamp(timestamp),
            "service_name": service,
            # Uppercased, because the demo's services disagree about case:
            # the collector writes `info` and checkout writes `INFO`, and the
            # log tools filter on an exact severity.
            "severity": str(severity_text).upper(),
            "body": str(body),
            "trace_id": trace_id if isinstance(trace_id, str) and trace_id else None,
            "attributes_json": json.dumps(
                attributes if isinstance(attributes, dict) else {},
                sort_keys=True,
                default=str,
            ),
        }


def _parse_timestamp(value: str) -> datetime:
    """Read an OpenSearch timestamp, which carries nanosecond precision.

    `datetime.fromisoformat` rejects more than six fractional digits on some
    versions, and the collector writes nine. Truncating to microseconds is
    lossless for anything this project measures, since the bundle schema
    stores log timestamps at millisecond resolution anyway.
    """
    text = value.replace("Z", "+00:00")
    if "." in text:
        head, _, tail = text.partition(".")
        fraction = tail
        offset = ""
        for marker in ("+", "-"):
            if marker in tail:
                fraction, _, rest = tail.partition(marker)
                offset = marker + rest
                break
        text = f"{head}.{fraction[:6]}{offset}"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
