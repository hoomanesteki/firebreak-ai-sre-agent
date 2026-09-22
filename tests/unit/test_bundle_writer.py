"""Tests for firebreak.lab.bundle_writer."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from firebreak.lab.bundle import CHANGES_FILE, TOPOLOGY_FILE, TimeWindow, verify_bundle
from firebreak.lab.bundle_writer import (
    LOGS_SCHEMA,
    METRICS_SCHEMA,
    TRACES_SCHEMA,
    BundleWriteError,
    BundleWriter,
    sanitise_changes,
    write_table,
)


def _window() -> TimeWindow:
    return TimeWindow(
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 0, 10, 0, tzinfo=UTC),
    )


def _writer(bundle_dir: Path) -> BundleWriter:
    return BundleWriter(
        bundle_dir=bundle_dir,
        scenario_id="cart-latency",
        run_id="20260101T000000Z",
        demo_tag="abc123",
        recorder_version="1.0.0",
    )


def _write_all_required(writer: BundleWriter) -> None:
    writer.write_alert({"status": "none", "alerts": []})
    writer.write_metrics([])
    writer.write_traces([])
    writer.write_logs([])
    writer.write_changes([], fault_flags=set())
    writer.write_topology({"services": [], "edges": []})


# --- schemas ---------------------------------------------------------------


def test_metrics_schema_has_the_documented_field_names_and_types():
    assert METRICS_SCHEMA.names == [
        "timestamp",
        "metric_name",
        "service_name",
        "labels_json",
        "value",
    ]
    assert METRICS_SCHEMA.field("timestamp").type == pa.timestamp("ms", tz="UTC")
    assert METRICS_SCHEMA.field("metric_name").type == pa.string()
    assert METRICS_SCHEMA.field("service_name").type == pa.string()
    assert METRICS_SCHEMA.field("labels_json").type == pa.string()
    assert METRICS_SCHEMA.field("value").type == pa.float64()


def test_traces_schema_has_the_documented_field_names_and_types():
    assert TRACES_SCHEMA.names == [
        "trace_id",
        "span_id",
        "parent_span_id",
        "service_name",
        "span_name",
        "span_kind",
        "start_time",
        "duration_ms",
        "status_code",
        "attributes_json",
    ]
    assert TRACES_SCHEMA.field("trace_id").type == pa.string()
    assert TRACES_SCHEMA.field("parent_span_id").nullable is True
    assert TRACES_SCHEMA.field("start_time").type == pa.timestamp("us", tz="UTC")
    assert TRACES_SCHEMA.field("duration_ms").type == pa.float64()


def test_logs_schema_has_the_documented_field_names_and_types():
    assert LOGS_SCHEMA.names == [
        "timestamp",
        "service_name",
        "severity",
        "body",
        "trace_id",
        "attributes_json",
    ]
    assert LOGS_SCHEMA.field("timestamp").type == pa.timestamp("ms", tz="UTC")
    assert LOGS_SCHEMA.field("trace_id").nullable is True
    assert LOGS_SCHEMA.field("body").type == pa.string()


# --- write_table -----------------------------------------------------------


def test_write_table_writes_a_readable_parquet_file_and_returns_the_row_count(tmp_path: Path):
    rows = [
        {
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
            "metric_name": "m",
            "service_name": "cart",
            "labels_json": "{}",
            "value": 1.0,
        },
        {
            "timestamp": datetime(2026, 1, 1, 0, 0, 15, tzinfo=UTC),
            "metric_name": "m",
            "service_name": "cart",
            "labels_json": "{}",
            "value": 2.0,
        },
    ]
    path = tmp_path / "metrics.parquet"

    count = write_table(rows, METRICS_SCHEMA, path)

    assert count == 2
    table = pq.read_table(path)
    assert table.num_rows == 2
    assert table.column("value").to_pylist() == [1.0, 2.0]


def test_write_table_raises_bundle_write_error_naming_unexpected_column(tmp_path: Path):
    rows = [
        {
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
            "metric_name": "m",
            "service_name": "cart",
            "labels_json": "{}",
            "value": 1.0,
            "bogus": "x",
        }
    ]

    with pytest.raises(BundleWriteError, match="bogus"):
        write_table(rows, METRICS_SCHEMA, tmp_path / "metrics.parquet")


def test_write_table_raises_bundle_write_error_naming_missing_required_column(tmp_path: Path):
    rows = [
        {
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
            "metric_name": "m",
            "service_name": "cart",
            "labels_json": "{}",
        }
    ]

    with pytest.raises(BundleWriteError, match="value"):
        write_table(rows, METRICS_SCHEMA, tmp_path / "metrics.parquet")


# --- sanitise_changes --------------------------------------------------


def test_sanitise_changes_removes_record_mentioning_flag_at_top_level():
    records = [
        {"kind": "deploy", "detail": "flipped cartFailure to 10%"},
        {"kind": "deploy", "detail": "unrelated redeploy"},
    ]

    result = sanitise_changes(records, {"cartFailure"})

    assert result == [records[1]]


def test_sanitise_changes_removes_record_mentioning_flag_nested_in_dict():
    records = [
        {"kind": "deploy", "meta": {"note": "cartFailure rollout"}},
        {"kind": "deploy", "meta": {"note": "unrelated"}},
    ]

    result = sanitise_changes(records, {"cartFailure"})

    assert result == [records[1]]


def test_sanitise_changes_removes_record_mentioning_flag_nested_in_list():
    records = [
        {"kind": "deploy", "tags": ["release", "cartFailure"]},
        {"kind": "deploy", "tags": ["release", "unrelated"]},
    ]

    result = sanitise_changes(records, {"cartFailure"})

    assert result == [records[1]]


def test_sanitise_changes_removes_record_with_flag_as_a_dict_key():
    records = [
        {"kind": "deploy", "cartFailure": "10%"},
        {"kind": "deploy", "unrelated": "10%"},
    ]

    result = sanitise_changes(records, {"cartFailure"})

    assert result == [records[1]]


def test_sanitise_changes_is_case_insensitive():
    records = [{"kind": "deploy", "detail": "CARTFAILURE toggled"}]

    assert sanitise_changes(records, {"cartFailure"}) == []


def test_sanitise_changes_keeps_records_that_do_not_mention_any_fault_flag():
    records = [{"kind": "deploy", "service": "checkout"}]

    assert sanitise_changes(records, {"cartFailure"}) == records


def test_sanitise_changes_returns_all_records_unchanged_when_fault_flags_is_empty():
    records = [{"kind": "deploy", "detail": "cartFailure"}]

    result = sanitise_changes(records, set())

    assert result == records
    assert result is not records


# --- BundleWriter ------------------------------------------------------


def test_bundle_writer_finalise_raises_bundle_write_error_naming_never_written_file(
    tmp_path: Path,
):
    writer = _writer(tmp_path)
    writer.write_alert({"status": "none", "alerts": []})
    writer.write_metrics([])
    writer.write_traces([])
    writer.write_logs([])
    writer.write_changes([], fault_flags=set())
    # write_topology is never called.

    with pytest.raises(BundleWriteError) as excinfo:
        writer.finalise(window=_window(), alert_fired=False)
    assert TOPOLOGY_FILE in str(excinfo.value)


def test_bundle_writer_finalise_writes_the_manifest_last(tmp_path: Path):
    writer = _writer(tmp_path)
    _write_all_required(writer)
    manifest_path = tmp_path / "manifest.json"
    assert not manifest_path.exists()

    writer.finalise(window=_window(), alert_fired=False)

    assert manifest_path.exists()


def test_bundle_writer_produced_bundle_passes_verify_bundle(tmp_path: Path):
    writer = _writer(tmp_path)
    _write_all_required(writer)

    manifest = writer.finalise(
        window=_window(),
        alert_fired=True,
        alert_fired_at=datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC),
    )

    assert verify_bundle(tmp_path) == manifest


def test_bundle_writer_write_changes_strips_fault_flags(tmp_path: Path):
    writer = _writer(tmp_path)

    writer.write_changes(
        [
            {"kind": "deploy", "service": "cart", "detail": "flipped cartFailure"},
            {"kind": "deploy", "service": "checkout", "detail": "routine deployment"},
        ],
        fault_flags={"cartFailure"},
    )

    data = json.loads((tmp_path / CHANGES_FILE).read_text(encoding="utf-8"))

    assert data == [{"kind": "deploy", "service": "checkout", "detail": "routine deployment"}]
