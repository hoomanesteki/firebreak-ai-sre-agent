"""Tests for firebreak.lab.bundle."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from firebreak.lab.bundle import (
    ALERT_FILE,
    CHANGES_FILE,
    FORBIDDEN_NAMES,
    LOGS_FILE,
    MANIFEST_NAME,
    METRICS_FILE,
    REQUIRED_FILES,
    TOPOLOGY_FILE,
    TRACES_FILE,
    BundleError,
    BundleManifest,
    BundleReader,
    FileRecord,
    TimeWindow,
    describe_file,
    iter_bundles,
    new_run_id,
    read_manifest,
    sha256_of,
    verify_bundle,
    write_manifest,
)


def _window() -> TimeWindow:
    return TimeWindow(
        start=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 1, 1, 0, 10, 0, tzinfo=UTC),
    )


def _manifest(files: tuple[FileRecord, ...]) -> BundleManifest:
    return BundleManifest(
        scenario_id="cart-latency",
        run_id="20260101T000000Z",
        demo_tag="abc123",
        recorder_version="0.0.1",
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        window=_window(),
        files=files,
        alert_fired=True,
    )


def _write_required_files(
    bundle_dir: Path, *, alert: object = None, changes: object = None, topology: object = None
) -> None:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / ALERT_FILE).write_text(
        json.dumps({"alert": "CartLatencyHigh"} if alert is None else alert), encoding="utf-8"
    )
    (bundle_dir / METRICS_FILE).write_bytes(b"metrics-bytes")
    (bundle_dir / TRACES_FILE).write_bytes(b"traces-bytes")
    (bundle_dir / LOGS_FILE).write_bytes(b"logs-bytes")
    (bundle_dir / CHANGES_FILE).write_text(
        json.dumps([{"service": "cart"}] if changes is None else changes), encoding="utf-8"
    )
    (bundle_dir / TOPOLOGY_FILE).write_text(
        json.dumps({"nodes": ["cart"]} if topology is None else topology), encoding="utf-8"
    )


def _build_bundle(
    bundle_dir: Path, *, alert: object = None, changes: object = None, topology: object = None
) -> BundleManifest:
    """Write a minimal, valid bundle to disk and return its manifest."""
    _write_required_files(bundle_dir, alert=alert, changes=changes, topology=topology)
    files = tuple(
        describe_file(bundle_dir / name, rows=10 if name.endswith(".parquet") else None)
        for name in REQUIRED_FILES
    )
    manifest = _manifest(files)
    write_manifest(bundle_dir, manifest)
    return manifest


# --- sha256_of -----------------------------------------------------------


def test_sha256_of_matches_hashlib_for_the_same_bytes(tmp_path: Path):
    data = b"some incident bytes"
    path = tmp_path / "blob.bin"
    path.write_bytes(data)

    assert sha256_of(path) == hashlib.sha256(data).hexdigest()


# --- describe_file ---------------------------------------------------------


def test_describe_file_raises_bundle_error_for_missing_file(tmp_path: Path):
    with pytest.raises(BundleError, match="cannot describe missing file"):
        describe_file(tmp_path / "absent.parquet")


def test_describe_file_records_name_size_and_row_count(tmp_path: Path):
    data = b"row1\nrow2\nrow3\n"
    path = tmp_path / METRICS_FILE
    path.write_bytes(data)

    record = describe_file(path, rows=3)

    assert record.name == METRICS_FILE
    assert record.bytes == len(data)
    assert record.rows == 3
    assert record.sha256 == hashlib.sha256(data).hexdigest()


# --- write_manifest / read_manifest --------------------------------------


def test_write_manifest_then_read_manifest_round_trips(tmp_path: Path):
    manifest = _build_bundle(tmp_path)

    assert read_manifest(tmp_path) == manifest


def test_read_manifest_raises_bundle_error_for_missing_file(tmp_path: Path):
    with pytest.raises(BundleError, match="no manifest at"):
        read_manifest(tmp_path)


def test_read_manifest_raises_bundle_error_for_invalid_json(tmp_path: Path):
    (tmp_path / MANIFEST_NAME).write_text("{not json", encoding="utf-8")

    with pytest.raises(BundleError, match="not valid JSON"):
        read_manifest(tmp_path)


def test_read_manifest_raises_bundle_error_for_json_that_is_not_a_valid_manifest(tmp_path: Path):
    (tmp_path / MANIFEST_NAME).write_text(json.dumps({"format_version": 1}), encoding="utf-8")

    with pytest.raises(BundleError, match="not a valid manifest"):
        read_manifest(tmp_path)


# --- verify_bundle -----------------------------------------------------


def test_verify_bundle_passes_and_returns_manifest_for_well_formed_bundle(tmp_path: Path):
    manifest = _build_bundle(tmp_path)

    assert verify_bundle(tmp_path) == manifest


def test_verify_bundle_raises_when_a_listed_file_is_deleted(tmp_path: Path):
    _build_bundle(tmp_path)
    (tmp_path / METRICS_FILE).unlink()

    with pytest.raises(BundleError, match="but it is missing") as excinfo:
        verify_bundle(tmp_path)
    assert METRICS_FILE in str(excinfo.value)


def test_verify_bundle_names_the_file_when_bytes_change_after_recording(tmp_path: Path):
    _build_bundle(tmp_path)
    (tmp_path / METRICS_FILE).write_bytes(b"tampered bytes")

    with pytest.raises(BundleError, match="has changed since recording") as excinfo:
        verify_bundle(tmp_path)
    assert METRICS_FILE in str(excinfo.value)


def test_verify_bundle_raises_when_a_required_file_is_absent_from_manifest(tmp_path: Path):
    _write_required_files(tmp_path)
    incomplete = tuple(
        describe_file(tmp_path / name, rows=10 if name.endswith(".parquet") else None)
        for name in REQUIRED_FILES
        if name != TOPOLOGY_FILE
    )
    write_manifest(tmp_path, _manifest(incomplete))

    with pytest.raises(BundleError, match="manifest omits required files") as excinfo:
        verify_bundle(tmp_path)
    assert TOPOLOGY_FILE in str(excinfo.value)


def test_verify_bundle_raises_when_an_extra_unlisted_file_is_present(tmp_path: Path):
    _build_bundle(tmp_path)
    (tmp_path / "extra.bin").write_bytes(b"not part of the bundle")

    with pytest.raises(BundleError, match="unexpected files in bundle") as excinfo:
        verify_bundle(tmp_path)
    assert "extra.bin" in str(excinfo.value)


def test_verify_bundle_mentions_labels_dir_when_label_json_is_present(tmp_path: Path):
    assert "label.json" in FORBIDDEN_NAMES
    _write_required_files(tmp_path)
    (tmp_path / "label.json").write_text(json.dumps({"target_service": "cart"}), encoding="utf-8")
    files = tuple(
        describe_file(tmp_path / name, rows=10 if name.endswith(".parquet") else None)
        for name in (*REQUIRED_FILES, "label.json")
    )
    write_manifest(tmp_path, _manifest(files))

    with pytest.raises(BundleError, match="labels/"):
        verify_bundle(tmp_path)


# --- BundleReader ----------------------------------------------------------


def test_bundle_reader_path_for_returns_path_for_listed_name(tmp_path: Path):
    _build_bundle(tmp_path)
    reader = BundleReader(tmp_path)

    assert reader.path_for(ALERT_FILE) == (tmp_path / ALERT_FILE).resolve()


def test_bundle_reader_path_for_raises_when_a_manifest_entry_escapes_the_bundle_directory(
    tmp_path: Path,
):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    escaping_record = FileRecord(name="../escape.txt", sha256="0" * 64, bytes=0, rows=None)
    write_manifest(bundle_dir, _manifest((escaping_record,)))
    reader = BundleReader(bundle_dir, verify=False)

    with pytest.raises(BundleError, match="resolves outside the bundle directory"):
        reader.path_for("../escape.txt")


def test_bundle_reader_path_for_raises_and_lists_bundle_contents_for_unlisted_name(
    tmp_path: Path,
):
    _build_bundle(tmp_path)
    reader = BundleReader(tmp_path)

    with pytest.raises(BundleError) as excinfo:
        reader.path_for("does-not-exist.json")

    message = str(excinfo.value)
    assert "does-not-exist.json" in message
    for name in REQUIRED_FILES:
        assert name in message


def test_bundle_reader_read_json_alert_changes_topology_read_their_files(tmp_path: Path):
    _build_bundle(tmp_path)
    reader = BundleReader(tmp_path)

    assert reader.alert() == {"alert": "CartLatencyHigh"}
    assert reader.changes() == [{"service": "cart"}]
    assert reader.topology() == {"nodes": ["cart"]}
    assert reader.read_json(ALERT_FILE) == {"alert": "CartLatencyHigh"}


def test_bundle_reader_alert_raises_bundle_error_when_not_an_object(tmp_path: Path):
    _build_bundle(tmp_path, alert=["not", "an", "object"])
    reader = BundleReader(tmp_path)

    with pytest.raises(BundleError, match=f"{ALERT_FILE} does not contain an object"):
        reader.alert()


def test_bundle_reader_changes_raises_bundle_error_when_not_a_list(tmp_path: Path):
    _build_bundle(tmp_path, changes={"not": "a list"})
    reader = BundleReader(tmp_path)

    with pytest.raises(BundleError, match=f"{CHANGES_FILE} does not contain a list"):
        reader.changes()


def test_bundle_reader_topology_raises_bundle_error_when_not_an_object(tmp_path: Path):
    _build_bundle(tmp_path, topology=["not", "an", "object"])
    reader = BundleReader(tmp_path)

    with pytest.raises(BundleError, match=f"{TOPOLOGY_FILE} does not contain an object"):
        reader.topology()


def test_bundle_reader_with_verify_false_does_not_raise_on_corrupted_checksum(tmp_path: Path):
    _build_bundle(tmp_path)
    (tmp_path / METRICS_FILE).write_bytes(b"tampered bytes")

    reader = BundleReader(tmp_path, verify=False)

    assert reader.manifest.file_names == REQUIRED_FILES


def test_bundle_reader_with_verify_true_raises_on_corrupted_checksum(tmp_path: Path):
    _build_bundle(tmp_path)
    (tmp_path / METRICS_FILE).write_bytes(b"tampered bytes")

    with pytest.raises(BundleError, match="has changed since recording"):
        BundleReader(tmp_path, verify=True)


# --- BundleManifest ------------------------------------------------------


def test_bundle_manifest_record_for_finds_listed_file(tmp_path: Path):
    manifest = _build_bundle(tmp_path)

    record = manifest.record_for(METRICS_FILE)

    assert record.name == METRICS_FILE


def test_bundle_manifest_record_for_raises_for_unlisted_file(tmp_path: Path):
    manifest = _build_bundle(tmp_path)

    with pytest.raises(BundleError, match="not listed in the manifest"):
        manifest.record_for("does-not-exist.json")


def test_bundle_manifest_row_counts_omits_files_with_no_row_count(tmp_path: Path):
    manifest = _build_bundle(tmp_path)

    counts = manifest.row_counts()

    assert counts == {METRICS_FILE: 10, TRACES_FILE: 10, LOGS_FILE: 10}
    assert ALERT_FILE not in counts
    assert CHANGES_FILE not in counts
    assert TOPOLOGY_FILE not in counts


# --- iter_bundles ----------------------------------------------------------


def test_iter_bundles_yields_nothing_for_root_that_does_not_exist(tmp_path: Path):
    assert list(iter_bundles(tmp_path / "does-not-exist")) == []


def test_iter_bundles_yields_only_manifest_directories_in_sorted_order(tmp_path: Path):
    _build_bundle(tmp_path / "zeta-scenario" / "run-1")
    _build_bundle(tmp_path / "alpha-scenario" / "run-2")
    (tmp_path / "alpha-scenario" / "incomplete-run").mkdir(parents=True)

    bundles = list(iter_bundles(tmp_path))

    assert bundles == [
        tmp_path / "alpha-scenario" / "run-2",
        tmp_path / "zeta-scenario" / "run-1",
    ]


# --- new_run_id ----------------------------------------------------------


def test_new_run_id_matches_the_documented_format():
    run_id = new_run_id(datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC))

    assert run_id == "20260304T050607Z"


def test_new_run_id_is_filesystem_safe():
    run_id = new_run_id(datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC))

    assert not any(char in run_id for char in "/\\: ")


def test_new_run_id_sorts_chronologically_for_increasing_datetimes():
    earlier = new_run_id(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
    later = new_run_id(datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC))

    assert earlier < later
