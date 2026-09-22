"""Write incident bundles: the parquet and JSON files `bundle.py` reads back.

This is the only place that produces the files `verify_bundle` checks and
`BundleReader` opens, so it owns two jobs the format depends on: writing
every table against an explicit schema rather than one pyarrow infers from
whatever rows happen to arrive, and writing the manifest last, after every
other file is on disk and can be checksummed.

It also owns leakage control 2 from SPEC.md Section 8.4. `changes.json` is
the one file in a bundle that could, by construction, show the fault being
injected: it is a log of things that changed, and the fault is a change. A
change record that names the fault flag is not a subtle leak, it is the
answer, so `write_changes` cannot be called without also filtering through
`sanitise_changes`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from firebreak.lab.bundle import (
    ALERT_FILE,
    CHANGES_FILE,
    LOGS_FILE,
    METRICS_FILE,
    REQUIRED_FILES,
    TOPOLOGY_FILE,
    TRACES_FILE,
    BundleManifest,
    TimeWindow,
    describe_file,
    write_manifest,
)

METRICS_SCHEMA = pa.schema(
    [
        pa.field("timestamp", pa.timestamp("ms", tz="UTC"), nullable=False),
        pa.field("metric_name", pa.string(), nullable=False),
        pa.field("service_name", pa.string(), nullable=False),
        pa.field("labels_json", pa.string(), nullable=False),
        pa.field("value", pa.float64(), nullable=False),
    ]
)

TRACES_SCHEMA = pa.schema(
    [
        pa.field("trace_id", pa.string(), nullable=False),
        pa.field("span_id", pa.string(), nullable=False),
        pa.field("parent_span_id", pa.string(), nullable=True),
        pa.field("service_name", pa.string(), nullable=False),
        pa.field("span_name", pa.string(), nullable=False),
        pa.field("span_kind", pa.string(), nullable=False),
        pa.field("start_time", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("duration_ms", pa.float64(), nullable=False),
        pa.field("status_code", pa.string(), nullable=False),
        pa.field("attributes_json", pa.string(), nullable=False),
    ]
)

LOGS_SCHEMA = pa.schema(
    [
        pa.field("timestamp", pa.timestamp("ms", tz="UTC"), nullable=False),
        pa.field("service_name", pa.string(), nullable=False),
        pa.field("severity", pa.string(), nullable=False),
        pa.field("body", pa.string(), nullable=False),
        pa.field("trace_id", pa.string(), nullable=True),
        pa.field("attributes_json", pa.string(), nullable=False),
    ]
)


class BundleWriteError(Exception):
    """A bundle could not be written as the format requires."""


def write_table(rows: list[dict[str, Any]], schema: pa.Schema, path: Path) -> int:
    """Write rows to parquet under an explicit schema and return the row count.

    Rows are checked against the schema before anything touches pyarrow, so a
    caller with a typo in a column name gets a message naming the file and the
    row rather than an opaque error from inside pyarrow's own inference.
    """
    known_names = set(schema.names)
    required_names = {field.name for field in schema if not field.nullable}
    for index, row in enumerate(rows):
        unexpected = set(row) - known_names
        if unexpected:
            raise BundleWriteError(
                f"{path.name}: row {index} has unexpected column(s) "
                f"{sorted(unexpected)}; schema has {sorted(known_names)}"
            )
        missing = {name for name in required_names if row.get(name) is None}
        if missing:
            raise BundleWriteError(
                f"{path.name}: row {index} is missing required column(s) {sorted(missing)}"
            )

    table = pa.Table.from_pylist(rows, schema=schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    return int(table.num_rows)


def _mentions_any(value: Any, needles: set[str]) -> bool:
    """True if a fault flag name appears anywhere inside a JSON-like value.

    Recurses through dicts and lists because a change record's shape is not
    fixed. Keys are checked along with values: a flag name is exactly the
    kind of thing that ends up as a dict key in a loosely structured change
    payload, and missing that would leave a leak the depth-first search was
    supposed to close.
    """
    if isinstance(value, str):
        lowered = value.lower()
        return any(needle in lowered for needle in needles)
    if isinstance(value, dict):
        return any(_mentions_any(k, needles) for k in value) or any(
            _mentions_any(v, needles) for v in value.values()
        )
    if isinstance(value, list | tuple):
        return any(_mentions_any(item, needles) for item in value)
    return False


def sanitise_changes(records: list[dict[str, Any]], fault_flags: set[str]) -> list[dict[str, Any]]:
    """Drop every change record that names a fault flag, anywhere, case insensitive.

    Leakage control 2, SPEC.md Section 8.4. A change log is supposed to look
    like a real company's: some entries near the incident, most of them
    irrelevant. If one of those entries is the fault flag flipping to the
    variant that caused the incident, the agent under test does not need to
    reason about anything, it just needs to read the change log. So a record
    that mentions a fault flag is removed whole rather than redacted field by
    field, which would leave a gap in exactly the place the culprit was and
    would itself be a signal.
    """
    if not fault_flags:
        return list(records)
    needles = {flag.lower() for flag in fault_flags}
    return [record for record in records if not _mentions_any(record, needles)]


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot serialise {type(value).__name__} to JSON")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=_json_default) + "\n", encoding="utf-8")


class BundleWriter:
    """Builds one incident bundle directory, one file at a time.

    Each `write_*` method produces exactly one file and records that it was
    written, so `finalise` can confirm every file the format requires is
    present before it builds the manifest. Nothing here reorders that: the
    manifest is always the last file to land, which is what lets
    `verify_bundle` trust every checksum it contains.
    """

    def __init__(
        self,
        bundle_dir: Path,
        scenario_id: str,
        run_id: str,
        demo_tag: str,
        recorder_version: str,
    ) -> None:
        self.bundle_dir = bundle_dir
        self.scenario_id = scenario_id
        self.run_id = run_id
        self.demo_tag = demo_tag
        self.recorder_version = recorder_version
        self.bundle_dir.mkdir(parents=True, exist_ok=True)
        self._rows_written: dict[str, int | None] = {}

    def write_alert(self, payload: dict[str, Any]) -> None:
        _write_json(self.bundle_dir / ALERT_FILE, payload)
        self._rows_written[ALERT_FILE] = None

    def write_changes(self, records: list[dict[str, Any]], fault_flags: set[str]) -> None:
        """Write the sanitised change log.

        `fault_flags` is not part of the bundle: it is the set of flag names
        this recording touched, known only to the caller (the recorder, which
        holds the scenario spec). It exists to be filtered out, never to be
        stored, which is why it has no place in `__init__` alongside the
        identifiers that do end up in the manifest.
        """
        sanitised = sanitise_changes(records, fault_flags)
        _write_json(self.bundle_dir / CHANGES_FILE, sanitised)
        self._rows_written[CHANGES_FILE] = None

    def write_topology(self, payload: dict[str, Any]) -> None:
        _write_json(self.bundle_dir / TOPOLOGY_FILE, payload)
        self._rows_written[TOPOLOGY_FILE] = None

    def write_metrics(self, rows: list[dict[str, Any]]) -> None:
        count = write_table(rows, METRICS_SCHEMA, self.bundle_dir / METRICS_FILE)
        self._rows_written[METRICS_FILE] = count

    def write_traces(self, rows: list[dict[str, Any]]) -> None:
        count = write_table(rows, TRACES_SCHEMA, self.bundle_dir / TRACES_FILE)
        self._rows_written[TRACES_FILE] = count

    def write_logs(self, rows: list[dict[str, Any]]) -> None:
        count = write_table(rows, LOGS_SCHEMA, self.bundle_dir / LOGS_FILE)
        self._rows_written[LOGS_FILE] = count

    def finalise(
        self,
        window: TimeWindow,
        alert_fired: bool,
        alert_fired_at: datetime | None = None,
        notes: str | None = None,
    ) -> BundleManifest:
        """Build and write the manifest, once every required file exists.

        Raises `BundleWriteError` rather than building a partial manifest,
        because a manifest that lists a file nobody wrote would fail
        `verify_bundle` anyway, just later and with a less useful message.
        """
        missing = [name for name in REQUIRED_FILES if name not in self._rows_written]
        if missing:
            raise BundleWriteError(
                f"cannot finalise {self.scenario_id}/{self.run_id}: "
                f"never wrote required file(s) {', '.join(missing)}"
            )

        files = tuple(
            describe_file(self.bundle_dir / name, rows=self._rows_written[name])
            for name in REQUIRED_FILES
        )
        manifest = BundleManifest(
            scenario_id=self.scenario_id,
            run_id=self.run_id,
            demo_tag=self.demo_tag,
            recorder_version=self.recorder_version,
            recorded_at=datetime.now(UTC),
            window=window,
            files=files,
            alert_fired=alert_fired,
            alert_fired_at=alert_fired_at,
            notes=notes,
        )
        write_manifest(self.bundle_dir, manifest)
        return manifest
