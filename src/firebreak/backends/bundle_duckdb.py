"""Replay a frozen bundle with DuckDB.

This is the backend almost every evaluation runs against. It reads the
Parquet tables in a bundle directly, with no server and no import step, so a
hundred incidents can be replayed three times each in CI.

Two things it deliberately does not do.

It does not open files by path. Every read goes through `BundleReader`,
which resolves names against the manifest and refuses anything else. That is
leakage control 3 in SPEC.md Section 8.4, and routing DuckDB through it is
the only way the rule survives contact with a query engine that is perfectly
happy to read any path it is handed.

It does not accept SQL from a caller. Queries are built here from typed
arguments and parameter binding. SPEC.md Section 11 lists unexpected code
execution as ASI05, and a backend that takes a query string from an agent is
exactly that.
"""

from __future__ import annotations

import json
from typing import Any

import duckdb

from firebreak.backends.base import (
    MAX_ROWS,
    MAX_SERIES_POINTS,
    BackendError,
    LogRecord,
    MetricPoint,
    SpanRecord,
    check_limit,
    check_window,
)
from firebreak.lab.bundle import (
    LOGS_FILE,
    METRICS_FILE,
    TRACES_FILE,
    BundleError,
    BundleReader,
)
from firebreak.tools.evidence import BackendFingerprint, BackendMode, TimeRange

# Timestamps are cast to text in SQL rather than fetched as datetimes,
# because DuckDB needs pytz to hand back a timezone aware datetime and
# the records here carry strings anyway. The cast is done with strftime
# to a fixed ISO 8601 form rather than a bare CAST, which renders in the
# session timezone and with an offset shape that datetime.fromisoformat
# accepts but pydantic does not.
#
# A pattern is a literal substring, never a regular expression. SPEC.md
# Section 6.3 is explicit, and the reason is that log bodies are attacker
# influenced: a pattern language is a way to make the backend do work the
# caller did not have to justify.
_PATTERN_MAX_LENGTH = 200


def _iso(column: str) -> str:
    """Render a timestamp column as ISO 8601 in UTC, with a real offset.

    strftime rather than a bare CAST: a cast renders in the session
    timezone and writes the offset as -07 rather than -07:00, which
    datetime.fromisoformat accepts and pydantic rejects.
    """
    return f"strftime({column} AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%S.%gZ')"


class BundleBackend:
    """Answers queries from one frozen bundle."""

    def __init__(self, reader: BundleReader) -> None:
        self._reader = reader
        self._connection = duckdb.connect(database=":memory:")
        # Belt and braces. What actually forces UTC is the explicit
        # AT TIME ZONE conversion in _iso, and removing this line changes
        # no output today, which a mutation run confirmed. It stays
        # because the failure it guards against is severe and silent: a
        # future query that formats a timestamp without that conversion
        # would render it in the machine's local zone, and since every
        # evidence id hashes those strings, the same bundle would produce
        # different ids on different machines.
        self._connection.execute("SET TimeZone = 'UTC'")

    @classmethod
    def open(cls, bundle_dir: Any, verify: bool = True) -> BundleBackend:
        """Open a bundle, verifying it first by default.

        Verification is on by default because a measurement taken against a
        bundle that changed since recording is not a measurement.
        """
        return cls(BundleReader(bundle_dir, verify=verify))

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> BundleBackend:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def fingerprint(self) -> BackendFingerprint:
        """Identify the backend without naming the incident.

        The bundle id is opaque by construction, which is what lets it be
        mixed into every evidence id without carrying the answer.
        """
        return BackendFingerprint(
            mode=BackendMode.BUNDLE,
            identity=self._reader.manifest.bundle_id,
            format_version=self._reader.manifest.format_version,
        )

    def _table(self, name: str) -> str:
        """Resolve a bundle file to a quoted path DuckDB can read.

        Goes through the manifest, so a name the bundle does not declare is
        refused before DuckDB ever sees it.
        """
        try:
            path = self._reader.path_for(name)
        except BundleError as error:
            raise BackendError(str(error)) from error
        escaped = str(path).replace("'", "''")
        return f"read_parquet('{escaped}')"

    def _run(self, sql: str, parameters: list[Any]) -> list[dict[str, Any]]:
        try:
            cursor = self._connection.execute(sql, parameters)
        except duckdb.Error as error:
            raise BackendError(f"query failed: {error}") from error
        columns = [description[0] for description in cursor.description or []]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def query_metrics(
        self,
        metric_name: str,
        window: TimeRange,
        services: tuple[str, ...] = (),
        limit: int = MAX_ROWS,
    ) -> list[MetricPoint]:
        """Samples of one metric over a window, oldest first."""
        check_window(window)
        capped = check_limit(limit)
        sql = (
            f"SELECT {_iso('timestamp')} AS timestamp, metric_name, "
            f"service_name, value, labels_json "
            f"FROM {self._table(METRICS_FILE)} "
            "WHERE metric_name = ? AND timestamp >= ? AND timestamp <= ?"
        )
        parameters: list[Any] = [metric_name, window.start, window.end]
        if services:
            placeholders = ", ".join("?" for _ in services)
            sql += f" AND service_name IN ({placeholders})"
            parameters.extend(services)
        sql += " ORDER BY timestamp, service_name LIMIT ?"
        parameters.append(capped)

        return [
            MetricPoint(
                timestamp=str(row["timestamp"]),
                metric_name=str(row["metric_name"]),
                service_name=str(row["service_name"]),
                value=float(row["value"]),
                labels=json.loads(row["labels_json"]) if row.get("labels_json") else {},
            )
            for row in self._run(sql, parameters)
        ]

    def metric_series(
        self,
        metric_name: str,
        window: TimeRange,
        services: tuple[str, ...] = (),
        limit: int = MAX_SERIES_POINTS,
    ) -> list[MetricPoint]:
        """Every sample of one metric in a window, oldest first.

        The same query as `query_metrics` with a different bound, and the
        difference is the whole point. See `MAX_SERIES_POINTS` for why reading a
        series through the tool row cap starved the anomaly scorer of data on
        the first real recording.
        """
        check_window(window)
        capped = check_limit(limit, maximum=MAX_SERIES_POINTS)
        sql = (
            f"SELECT {_iso('timestamp')} AS timestamp, metric_name, "
            f"service_name, value, labels_json "
            f"FROM {self._table(METRICS_FILE)} "
            "WHERE metric_name = ? AND timestamp >= ? AND timestamp <= ?"
        )
        parameters: list[Any] = [metric_name, window.start, window.end]
        if services:
            placeholders = ", ".join("?" for _ in services)
            sql += f" AND service_name IN ({placeholders})"
            parameters.extend(services)
        # Ordered by service first, so that a truncation at the cap loses whole
        # services rather than the tail of every one of them. A starved series
        # that still looks present is worse than an absent one, because
        # `MIN_BASELINE_POINTS` can only reject what it can see.
        sql += " ORDER BY service_name, timestamp LIMIT ?"
        parameters.append(capped)

        return [
            MetricPoint(
                timestamp=str(row["timestamp"]),
                metric_name=str(row["metric_name"]),
                service_name=str(row["service_name"]),
                value=float(row["value"]),
                labels=json.loads(row["labels_json"]) if row.get("labels_json") else {},
            )
            for row in self._run(sql, parameters)
        ]

    def search_logs(
        self,
        window: TimeRange,
        services: tuple[str, ...] = (),
        severity: str | None = None,
        pattern: str | None = None,
        limit: int = MAX_ROWS,
    ) -> list[LogRecord]:
        """Log records in a window, newest first, matched by literal substring."""
        check_window(window)
        capped = check_limit(limit)
        if pattern is not None and len(pattern) > _PATTERN_MAX_LENGTH:
            raise BackendError(
                f"pattern of {len(pattern)} characters exceeds {_PATTERN_MAX_LENGTH}"
            )

        sql = (
            f"SELECT {_iso('timestamp')} AS timestamp, service_name, "
            "severity, body, trace_id "
            f"FROM {self._table(LOGS_FILE)} "
            "WHERE timestamp >= ? AND timestamp <= ?"
        )
        parameters: list[Any] = [window.start, window.end]
        if services:
            placeholders = ", ".join("?" for _ in services)
            sql += f" AND service_name IN ({placeholders})"
            parameters.extend(services)
        if severity:
            sql += " AND upper(severity) = upper(?)"
            parameters.append(severity)
        if pattern:
            # contains() takes the needle as a bound parameter, so a body
            # full of SQL or regex metacharacters is just text.
            sql += " AND contains(lower(body), lower(?))"
            parameters.append(pattern)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        parameters.append(capped)

        return [
            LogRecord(
                timestamp=str(row["timestamp"]),
                service_name=str(row["service_name"]),
                severity=str(row["severity"]),
                body=str(row["body"]),
                trace_id=str(row["trace_id"]) if row.get("trace_id") else None,
            )
            for row in self._run(sql, parameters)
        ]

    def find_spans(
        self,
        window: TimeRange,
        services: tuple[str, ...] = (),
        status: str | None = None,
        min_duration_ms: float | None = None,
        limit: int = MAX_ROWS,
    ) -> list[SpanRecord]:
        """Representative spans, slowest first."""
        check_window(window)
        capped = check_limit(limit)
        sql = (
            "SELECT trace_id, span_id, parent_span_id, service_name, span_name, "
            f"span_kind, {_iso('start_time')} AS start_time, "
            "duration_ms, status_code "
            f"FROM {self._table(TRACES_FILE)} "
            "WHERE start_time >= ? AND start_time <= ?"
        )
        parameters: list[Any] = [window.start, window.end]
        if services:
            placeholders = ", ".join("?" for _ in services)
            sql += f" AND service_name IN ({placeholders})"
            parameters.extend(services)
        if status:
            sql += " AND status_code = ?"
            parameters.append(status)
        if min_duration_ms is not None:
            sql += " AND duration_ms >= ?"
            parameters.append(min_duration_ms)
        sql += " ORDER BY duration_ms DESC, trace_id LIMIT ?"
        parameters.append(capped)

        return [self._to_span(row) for row in self._run(sql, parameters)]

    def trace_spans(self, trace_id: str) -> list[SpanRecord]:
        """Every span of one trace, in start order.

        Not windowed and not capped by the row limit: a trace is a unit, and
        returning half of one would produce a critical path that is wrong
        rather than incomplete.
        """
        sql = (
            "SELECT trace_id, span_id, parent_span_id, service_name, span_name, "
            f"span_kind, {_iso('start_time')} AS start_time, "
            "duration_ms, status_code "
            f"FROM {self._table(TRACES_FILE)} "
            "WHERE trace_id = ? ORDER BY start_time, span_id"
        )
        return [self._to_span(row) for row in self._run(sql, [trace_id])]

    @staticmethod
    def _to_span(row: dict[str, Any]) -> SpanRecord:
        parent = row.get("parent_span_id")
        return SpanRecord(
            trace_id=str(row["trace_id"]),
            span_id=str(row["span_id"]),
            parent_span_id=str(parent) if parent else None,
            service_name=str(row["service_name"]),
            span_name=str(row["span_name"]),
            span_kind=str(row["span_kind"]),
            start_time=str(row["start_time"]),
            duration_ms=float(row["duration_ms"]),
            status_code=str(row["status_code"]),
        )

    def topology(self) -> dict[str, Any]:
        """The service dependency graph recorded for this window."""
        return self._reader.topology()

    def changes(self, window: TimeRange) -> list[dict[str, Any]]:
        """Change events inside a window.

        Fault flag flips were removed at recording time, so what remains is
        what a real change log might have shown.
        """
        check_window(window)
        start, end = window.start.isoformat(), window.end.isoformat()
        inside = []
        for record in self._reader.changes():
            at = record.get("at")
            if not isinstance(at, str):
                continue
            if start <= at <= end:
                inside.append(record)
        return inside

    def known_services(self) -> tuple[str, ...]:
        """Every service that appears anywhere in this bundle.

        Read from the recorded topology rather than by scanning the tables,
        so an agent asking about a service that was never there gets a
        useful answer instead of an empty one.
        """
        topology = self._reader.topology()
        services = topology.get("services")
        if isinstance(services, list):
            return tuple(sorted(str(s) for s in services))
        return ()
