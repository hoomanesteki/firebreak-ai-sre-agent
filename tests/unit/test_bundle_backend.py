"""Tests for firebreak.backends.bundle_duckdb, and the caps in backends.base it relies on."""

from __future__ import annotations

import inspect
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from firebreak.backends.base import (
    MAX_ROWS,
    MAX_WINDOW_SECONDS,
    BackendError,
    QueryTooBroadError,
)
from firebreak.backends.bundle_duckdb import BundleBackend
from firebreak.lab.bundle import (
    METRICS_FILE,
    TRACES_FILE,
    BundleError,
    BundleReader,
    derive_bundle_id,
)
from firebreak.lab.scenario import Fault, FaultClass, FaultKind, Load, ScenarioSpec, Split, Timing
from firebreak.lab.synthetic import build_synthetic_bundle
from firebreak.signals import MetricName
from firebreak.tools.evidence import BackendMode, TimeRange

RUN_ID = "run-1"
SEED = 3


def _spec(**overrides) -> ScenarioSpec:
    fields = {
        "id": "payment-failure-50pct-20u",
        "family": "error-injection",
        "fault": Fault(kind=FaultKind.FLAG, flag="paymentFailure", variant="50%"),
        "target_service": "payment",
        "fault_class": FaultClass.ERROR_INJECTION,
        "load": Load(users=20),
        "timing": Timing(warmup_seconds=60, fault_seconds=60, cooldown_seconds=0),
        "split": Split.TRAIN,
    }
    fields.update(overrides)
    return ScenarioSpec(**fields)


def _full_window(manifest) -> TimeRange:
    return TimeRange(start=manifest.window.start, end=manifest.window.end)


def _too_wide_window() -> TimeRange:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return TimeRange(start=start, end=start + timedelta(seconds=MAX_WINDOW_SECONDS + 1))


@pytest.fixture
def bundle(tmp_path: Path):
    """A fresh synthetic bundle on disk, not yet opened by any backend."""
    spec = _spec()
    bundle_dir = tmp_path / derive_bundle_id(spec.id, RUN_ID)
    manifest = build_synthetic_bundle(bundle_dir, spec, RUN_ID, seed=SEED)
    return bundle_dir, manifest, spec


@pytest.fixture
def backend(bundle):
    bundle_dir, _, _ = bundle
    with BundleBackend.open(bundle_dir) as opened:
        yield opened


# --- fingerprint ---------------------------------------------------------


def test_fingerprint_mode_is_bundle_and_identity_matches_the_opaque_shape(backend):
    fingerprint = backend.fingerprint()

    assert fingerprint.mode is BackendMode.BUNDLE
    assert re.fullmatch(r"inc_[0-9a-f]{12}", fingerprint.identity)


def test_fingerprint_identity_contains_no_fragment_of_the_scenario_id(bundle):
    """The leak this closed: a fingerprint must never carry the answer into evidence ids."""
    bundle_dir, _, spec = bundle
    assert "payment" in spec.id

    with BundleBackend.open(bundle_dir) as opened:
        identity = opened.fingerprint().identity

    assert "payment" not in identity


# --- query_metrics -----------------------------------------------------


def test_query_metrics_returns_points_ordered_by_timestamp(backend, bundle):
    _, manifest, _ = bundle

    points = backend.query_metrics(
        "traces_span_metrics_calls_total", _full_window(manifest), limit=MAX_ROWS
    )

    timestamps = [point.timestamp for point in points]
    assert points
    assert timestamps == sorted(timestamps)


def test_query_metrics_filters_by_metric_name(backend, bundle):
    _, manifest, _ = bundle
    window = _full_window(manifest)

    points = backend.query_metrics("traces_span_metrics_calls_total", window, limit=MAX_ROWS)
    missing = backend.query_metrics("does_not_exist_total", window, limit=MAX_ROWS)

    assert points
    assert all(point.metric_name == "traces_span_metrics_calls_total" for point in points)
    assert missing == []


def test_query_metrics_filters_by_services(backend, bundle):
    _, manifest, _ = bundle

    points = backend.query_metrics(
        "traces_span_metrics_calls_total",
        _full_window(manifest),
        services=("payment",),
        limit=MAX_ROWS,
    )

    assert points
    assert all(point.service_name == "payment" for point in points)


def test_query_metrics_respects_limit(backend, bundle):
    _, manifest, _ = bundle

    points = backend.query_metrics(
        "traces_span_metrics_calls_total", _full_window(manifest), limit=3
    )

    assert len(points) == 3


def test_query_metrics_labels_are_parsed_from_labels_json_into_a_dict(backend, bundle):
    _, manifest, _ = bundle

    points = backend.query_metrics(
        "traces_span_metrics_calls_total", _full_window(manifest), limit=1
    )

    assert points[0].labels.keys() == {"status_code"}
    assert points[0].labels["status_code"] in {"STATUS_CODE_OK", "STATUS_CODE_ERROR"}


# --- search_logs -----------------------------------------------------------


def test_search_logs_returns_records_newest_first(backend, bundle):
    _, manifest, _ = bundle

    records = backend.search_logs(_full_window(manifest), limit=MAX_ROWS)

    timestamps = [record.timestamp for record in records]
    assert records
    assert timestamps == sorted(timestamps, reverse=True)


def test_search_logs_filters_by_severity_case_insensitively(backend, bundle):
    _, manifest, _ = bundle

    records = backend.search_logs(_full_window(manifest), severity="error", limit=MAX_ROWS)

    assert records
    assert all(record.severity == "ERROR" for record in records)


def test_search_logs_filters_by_services(backend, bundle):
    _, manifest, _ = bundle

    records = backend.search_logs(_full_window(manifest), services=("payment",), limit=MAX_ROWS)

    assert records
    assert all(record.service_name == "payment" for record in records)


def test_search_logs_literal_pattern_matches_a_substring_of_the_body(backend, bundle):
    """A substring that spans the fixed part of a log line and none of its variables.

    The pattern is deliberately a fragment rather than a whole body. Log
    bodies carry request ids and durations that differ per line, so a test
    asserting on a whole body would be asserting on the random number
    generator.
    """
    _, manifest, _ = bundle

    records = backend.search_logs(_full_window(manifest), pattern="handled", limit=MAX_ROWS)

    assert records
    assert all("handled" in record.body for record in records)


def test_search_logs_pattern_with_sql_metacharacters_matches_nothing_rather_than_everything(
    backend, bundle
):
    """Proves the pattern is bound as a parameter, never interpolated into the SQL text."""
    _, manifest, _ = bundle

    records = backend.search_logs(_full_window(manifest), pattern="' OR 1=1 -- ", limit=MAX_ROWS)

    assert records == []


def test_search_logs_pattern_longer_than_200_characters_raises_backend_error(backend, bundle):
    _, manifest, _ = bundle

    with pytest.raises(BackendError, match="exceeds"):
        backend.search_logs(_full_window(manifest), pattern="x" * 201, limit=MAX_ROWS)


# --- find_spans ------------------------------------------------------------


def test_find_spans_orders_by_duration_descending(backend, bundle):
    _, manifest, _ = bundle

    spans = backend.find_spans(_full_window(manifest), limit=MAX_ROWS)

    durations = [span.duration_ms for span in spans]
    assert spans
    assert durations == sorted(durations, reverse=True)


def test_find_spans_filters_by_services(backend, bundle):
    _, manifest, _ = bundle

    spans = backend.find_spans(_full_window(manifest), services=("payment",), limit=MAX_ROWS)

    assert spans
    assert all(span.service_name == "payment" for span in spans)


def test_find_spans_filters_by_status(backend, bundle):
    _, manifest, _ = bundle

    spans = backend.find_spans(_full_window(manifest), status="STATUS_CODE_ERROR", limit=MAX_ROWS)

    assert spans
    assert all(span.status_code == "STATUS_CODE_ERROR" for span in spans)


def test_find_spans_filters_by_min_duration_ms(backend, bundle):
    _, manifest, _ = bundle
    window = _full_window(manifest)
    unfiltered = backend.find_spans(window, limit=MAX_ROWS)
    threshold = unfiltered[len(unfiltered) // 2].duration_ms

    filtered = backend.find_spans(window, min_duration_ms=threshold, limit=MAX_ROWS)

    assert filtered
    assert all(span.duration_ms >= threshold for span in filtered)


def test_find_spans_respects_limit(backend, bundle):
    _, manifest, _ = bundle

    spans = backend.find_spans(_full_window(manifest), limit=5)

    assert len(spans) == 5


# --- trace_spans -----------------------------------------------------------


def test_trace_spans_returns_every_span_of_one_trace_in_start_order(backend, bundle):
    bundle_dir, manifest, _ = bundle
    seed_spans = backend.find_spans(_full_window(manifest), limit=MAX_ROWS)
    trace_id = seed_spans[0].trace_id

    spans = backend.trace_spans(trace_id)

    start_times = [span.start_time for span in spans]
    assert all(span.trace_id == trace_id for span in spans)
    assert start_times == sorted(start_times)

    table = pq.read_table(bundle_dir / TRACES_FILE)
    on_disk = sum(
        1 for row_trace_id in table.column("trace_id").to_pylist() if row_trace_id == trace_id
    )
    assert len(spans) == on_disk


def test_trace_spans_accepts_no_row_limit_argument():
    """Not windowed and not capped: a trace is a unit, so nothing here can cap it."""
    assert "limit" not in inspect.signature(BundleBackend.trace_spans).parameters


# --- topology and known_services ------------------------------------------


def test_topology_returns_the_recorded_graph(backend, bundle):
    bundle_dir, _, _ = bundle
    expected = BundleReader(bundle_dir).topology()

    assert backend.topology() == expected


def test_known_services_returns_a_sorted_tuple_of_services(backend, bundle):
    bundle_dir, _, _ = bundle
    expected = tuple(sorted(BundleReader(bundle_dir).topology()["services"]))

    services = backend.known_services()

    assert services == expected
    assert services == tuple(sorted(services))


# --- changes -------------------------------------------------------------


def test_changes_returns_records_inside_the_window(backend, bundle):
    _, manifest, _ = bundle
    window = _full_window(manifest)

    records = backend.changes(window)

    assert records
    for record in records:
        assert window.start.isoformat() <= record["at"] <= window.end.isoformat()


def test_changes_excludes_records_outside_the_window(backend, bundle):
    _, manifest, _ = bundle
    empty_window = TimeRange(
        start=manifest.window.end + timedelta(hours=1),
        end=manifest.window.end + timedelta(hours=2),
    )

    assert backend.changes(empty_window) == []


# --- caps ------------------------------------------------------------------


def test_window_wider_than_max_raises_query_too_broad_on_query_metrics(backend):
    with pytest.raises(QueryTooBroadError):
        backend.query_metrics("traces_span_metrics_calls_total", _too_wide_window())


def test_window_wider_than_max_raises_query_too_broad_on_search_logs(backend):
    with pytest.raises(QueryTooBroadError):
        backend.search_logs(_too_wide_window())


def test_window_wider_than_max_raises_query_too_broad_on_find_spans(backend):
    with pytest.raises(QueryTooBroadError):
        backend.find_spans(_too_wide_window())


@pytest.mark.parametrize("limit", [0, -1])
def test_limit_zero_or_negative_raises_backend_error(backend, bundle, limit):
    _, manifest, _ = bundle

    with pytest.raises(BackendError, match="limit must be at least 1"):
        backend.query_metrics(
            "traces_span_metrics_calls_total", _full_window(manifest), limit=limit
        )


def test_limit_above_max_rows_is_clamped_rather_than_refused(backend, bundle):
    _, manifest, _ = bundle

    points = backend.query_metrics(
        "traces_span_metrics_calls_total", _full_window(manifest), limit=MAX_ROWS + 1000
    )

    assert len(points) == MAX_ROWS


# --- manifest-only rule ----------------------------------------------------


def test_table_raises_backend_error_for_a_name_the_manifest_does_not_list(backend):
    with pytest.raises(BackendError, match="not in this bundle's manifest"):
        backend._table("does-not-exist.parquet")


def test_open_raises_when_a_file_was_modified_after_recording(bundle):
    bundle_dir, _, _ = bundle
    (bundle_dir / METRICS_FILE).write_bytes(b"tampered after recording")

    with pytest.raises(BundleError, match="has changed since recording"):
        BundleBackend.open(bundle_dir)


def test_open_with_verify_false_skips_verification(bundle):
    bundle_dir, _, _ = bundle
    (bundle_dir / METRICS_FILE).write_bytes(b"tampered after recording")

    with BundleBackend.open(bundle_dir, verify=False) as opened:
        assert opened.fingerprint().mode is BackendMode.BUNDLE


# --- timestamp determinism -----------------------------------------------
#
# Without the session timezone pinned, casting a timestamp renders it in the
# machine's local zone: a bundle anchored at 2025-01-01 UTC came back as
# 2024-12-31 17:13-07. Every evidence id is a hash over those strings, so the
# same bundle produced different ids on different machines and a verifier
# re-running a citation recorded elsewhere could never match it.


def test_timestamps_are_rendered_in_utc_not_the_local_zone(backend, bundle):
    _, manifest, _ = bundle
    window = _full_window(manifest)

    points = backend.query_metrics(MetricName.SPAN_CALLS_TOTAL, window, limit=1)

    assert points
    assert points[0].timestamp.endswith("Z"), points[0].timestamp
    assert points[0].timestamp.startswith(manifest.window.start.strftime("%Y-%m-%d"))


def test_every_timestamp_the_backend_returns_parses_as_a_datetime(backend, bundle):
    """pydantic is stricter than fromisoformat, and the gate uses pydantic."""
    _, manifest, _ = bundle
    window = _full_window(manifest)

    values = [
        backend.query_metrics(MetricName.SPAN_CALLS_TOTAL, window, limit=1)[0].timestamp,
        backend.search_logs(window, limit=1)[0].timestamp,
        backend.find_spans(window, limit=1)[0].start_time,
    ]

    for value in values:
        TimeRange(start=value, end=value)


def test_timestamps_do_not_depend_on_the_process_timezone(bundle):
    """The regression guard for the determinism defect.

    Renders the same bundle under two different process timezones and
    requires byte identical output.
    """
    import os
    import time

    bundle_dir, manifest, _ = bundle
    window = _full_window(manifest)
    rendered = []
    original = os.environ.get("TZ")
    try:
        for zone in ("America/Edmonton", "Asia/Tokyo"):
            os.environ["TZ"] = zone
            time.tzset()
            with BundleBackend.open(bundle_dir) as opened:
                rendered.append(
                    opened.query_metrics(MetricName.SPAN_CALLS_TOTAL, window, limit=3)[0].timestamp
                )
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()

    assert rendered[0] == rendered[1], rendered
