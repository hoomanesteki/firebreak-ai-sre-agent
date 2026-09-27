"""Incident bundles: one recorded incident, frozen so it can be replayed.

A bundle is the unit the whole evaluation rests on. If a bundle can change
after it was recorded, or can be read in ways the recorder did not intend,
then every number measured against it is unverifiable. So the manifest
checksums every file, verification is a first class operation, and the
reader refuses to open anything the manifest does not list.

That last rule is leakage control 3 in SPEC.md Section 8.4. Ground truth for
a bundle lives under `labels/`, never inside the bundle directory, and a
reader that only opens manifest entries cannot wander into it even if
somebody later drops a label file in the wrong place.

A bundle is identified by an opaque id, not by its scenario. A directory
called `payment-failure-50pct-20u` states the answer in its name, and the
manifest is read by the bundle backend, which in Phase 3 mixes a backend
fingerprint into every evidence id the agent handles. Descriptive names
would carry the culprit straight through that path. Real incidents are
identified by a ticket number rather than by their cause, and so are these.
The mapping back to a scenario lives in `labels/`, with the rest of the
answer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

BUNDLE_FORMAT_VERSION = 2
MANIFEST_NAME = "manifest.json"
BUNDLE_ID_PREFIX = "inc"
BUNDLE_ID_HEX = 12
CHECKSUM_CHUNK_BYTES = 1 << 20

# The complete set of files a bundle may contain. A bundle with anything
# else in it is rejected rather than tolerated, because an unexpected file is
# how a label ends up somewhere the agent can reach it.
ALERT_FILE = "alert.json"
METRICS_FILE = "metrics.parquet"
TRACES_FILE = "traces.parquet"
LOGS_FILE = "logs.parquet"
CHANGES_FILE = "changes.json"
TOPOLOGY_FILE = "topology.json"

REQUIRED_FILES = (
    ALERT_FILE,
    METRICS_FILE,
    TRACES_FILE,
    LOGS_FILE,
    CHANGES_FILE,
    TOPOLOGY_FILE,
)

# Names that must never appear inside a bundle directory. Ground truth lives
# under labels/ and nowhere else.
FORBIDDEN_NAMES = frozenset({"label.json", "labels.json", "ground_truth.json", "spec.yaml"})


class BundleError(Exception):
    """A bundle is missing, malformed, or does not match its manifest."""


class TimeWindow(BaseModel):
    """The span a bundle covers, in UTC."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: datetime
    end: datetime

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()


class FileRecord(BaseModel):
    """One file in a bundle, with enough to prove it has not changed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0)
    rows: int | None = None


class BundleManifest(BaseModel):
    """What a bundle contains and what it was recorded from.

    Deliberately carries no ground truth: not the target service, not the
    fault class, not the flag that was flipped, and not the scenario name,
    which for a library like this one describes the fault. It carries an
    opaque bundle id, which the eval resolves through `labels/` and which
    tells anything else nothing at all.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    format_version: int = BUNDLE_FORMAT_VERSION
    bundle_id: str = Field(pattern=rf"^{BUNDLE_ID_PREFIX}_[0-9a-f]{{{BUNDLE_ID_HEX}}}$")
    run_id: str
    demo_tag: str
    recorder_version: str
    recorded_at: datetime
    window: TimeWindow
    files: tuple[FileRecord, ...]
    alert_fired: bool
    alert_fired_at: datetime | None = None
    notes: str | None = None

    @property
    def file_names(self) -> tuple[str, ...]:
        return tuple(record.name for record in self.files)

    def record_for(self, name: str) -> FileRecord:
        for record in self.files:
            if record.name == name:
                return record
        raise BundleError(f"{name!r} is not listed in the manifest")

    def row_counts(self) -> dict[str, int]:
        return {r.name: r.rows for r in self.files if r.rows is not None}


def derive_bundle_id(scenario_id: str, run_id: str) -> str:
    """An opaque, stable identifier for one recording.

    Derived rather than random so it can be recomputed from the label
    without storing a second mapping, and so re-recording the same scenario
    and run reproduces the same id.
    """
    digest = hashlib.sha256(f"{scenario_id}:{run_id}".encode()).hexdigest()
    return f"{BUNDLE_ID_PREFIX}_{digest[:BUNDLE_ID_HEX]}"


class ChangeRecord(BaseModel):
    """One deploy or configuration event the agent is allowed to see.

    Declared here, in the format module, because two different producers
    write these: the recorder for a real recording, and the synthetic
    builder for tests. They disagreed on field names for a while, and the
    backend silently returned no changes at all rather than failing, so a
    distractor scenario would have shown an agent an empty change log.

    Fault flag flips never appear here. SPEC.md Section 6.2 removes them at
    recording time, which is what makes the distractor families hard.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: datetime
    kind: str
    service: str
    detail: str

    def as_row(self) -> dict[str, Any]:
        """The JSON shape written into a bundle."""
        return {
            "at": self.at.isoformat(),
            "kind": self.kind,
            "service": self.service,
            "detail": self.detail,
        }


class EdgeRecord(BaseModel):
    """One service to service dependency edge, as a bundle records it.

    Declared here for the same reason as ChangeRecord: two producers write
    these, and they disagreed. The synthetic builder wrote call_count and
    error_count, the exporter wrote requests_per_second and
    failures_per_second, and a tool reading one shape against the other
    reported every edge as carrying no traffic.

    Rates rather than counts, because a count depends on how long the
    window was and a rate does not, which is what makes two bundles of
    different durations comparable at all.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    client: str
    server: str
    requests_per_second: float = Field(ge=0.0)
    failures_per_second: float = Field(ge=0.0)

    @property
    def error_ratio(self) -> float:
        """Share of calls on this edge that failed."""
        if self.requests_per_second <= 0.0:
            return 0.0
        return self.failures_per_second / self.requests_per_second

    def as_row(self) -> dict[str, Any]:
        """The JSON shape written into a bundle's topology."""
        return {
            "client": self.client,
            "server": self.server,
            "requests_per_second": round(self.requests_per_second, 6),
            "failures_per_second": round(self.failures_per_second, 6),
            "error_ratio": round(self.error_ratio, 6),
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> EdgeRecord:
        """Parse one recorded edge, the inverse of `as_row`.

        Strict: an unknown field is an error, because a producer that drifts
        should fail loudly rather than be accommodated. `error_ratio` is the
        one field dropped, since `as_row` derives it for a human reading the
        JSON and this model computes it rather than accepting it.

        Declared here so that both readers, the topology tools and the
        candidate ranking, share one parse. Two copies of a parsing rule is
        how the shapes drifted apart the first time.
        """
        return cls.model_validate({k: v for k, v in row.items() if k != "error_ratio"})


def sha256_of(path: Path) -> str:
    """Checksum a file without reading it all into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHECKSUM_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def describe_file(path: Path, rows: int | None = None) -> FileRecord:
    """Build the manifest entry for a file that is already written."""
    if not path.is_file():
        raise BundleError(f"cannot describe missing file {path}")
    return FileRecord(name=path.name, sha256=sha256_of(path), bytes=path.stat().st_size, rows=rows)


def write_manifest(bundle_dir: Path, manifest: BundleManifest) -> Path:
    """Write the manifest last, once every other file is final."""
    path = bundle_dir / MANIFEST_NAME
    path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def read_manifest(bundle_dir: Path) -> BundleManifest:
    """Read and validate a bundle's manifest."""
    path = bundle_dir / MANIFEST_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise BundleError(f"no manifest at {path}") from error
    except json.JSONDecodeError as error:
        raise BundleError(f"{path} is not valid JSON: {error}") from error
    try:
        return BundleManifest.model_validate(raw)
    except ValueError as error:
        raise BundleError(f"{path} is not a valid manifest: {error}") from error


# How many times the observed sampling cadence a gap may be before the
# recording is judged to have a hole in it.
#
# The cadence is measured from the bundle rather than declared, because the
# demo's metrics arrive by OTLP export rather than by Prometheus scrape, so the
# scrape_interval in the rendered config is not the cadence that reaches a
# bundle. Measuring it means this check needs no constant to drift from reality.
#
# Measured on the first thirteen real recordings: eight clean bundles had a
# median gap of 15s and a maximum gap of 15s, a ratio of 1.0. Five suspended
# bundles had ratios of 21, 27, 46, 48, 51 and 334. There is no ambiguity to
# tune against, so 4 is chosen to tolerate a few consecutive missed exports
# rather than to sit in the middle of a gap.
MAX_GAP_MULTIPLE = 4.0

# Below this a multiple means little: on a fast cadence a single stall is a
# large ratio and a small hole.
MIN_GAP_SECONDS = 120.0

# Fewer samples than this and there is no cadence to compare a gap against.
# Two timestamps have one gap and no median worth the name.
MIN_SAMPLES_TO_JUDGE = 3


class BundleContinuityError(BundleError):
    """The recording has a stretch with no telemetry at all.

    Distinct from every other bundle error because the file is intact and the
    checksums match. What is wrong is the recording: nothing in the stack
    reported for a while, so the window covers time the experiment was not
    running.

    The case this was written for: a laptop took maintenance sleep during an
    overnight recording. `datetime.now` jumped across the suspension while
    `time.monotonic` did not, so neither the subprocess timeout nor anything
    else noticed, and the bundle arrived complete, checksummed and unusable.
    """


def largest_gap(bundle_dir: Path) -> tuple[float, float, int]:
    """The largest and median gap between metric samples, and how many there are.

    Measured across every service at once. A fault that silences one service
    leaves the others reporting, so a gap in this series means nothing in the
    whole stack reported, which is a property of the recording rather than of
    the incident.
    """
    # Imported here rather than at module scope: every module that reads a
    # manifest imports this one, and only this function needs a query engine.
    import duckdb

    path = bundle_dir / METRICS_FILE
    if not path.is_file():
        raise BundleError(f"{bundle_dir.name} has no {METRICS_FILE} to check")
    with duckdb.connect() as connection:
        row = connection.execute(
            """
            with stamps as (
                select distinct epoch(timestamp::timestamp) as ts
                from read_parquet($path)
            ), gaps as (
                select ts - lag(ts) over (order by ts) as gap from stamps
            )
            select
                (select max(gap) from gaps),
                (select median(gap) from gaps),
                (select count(*) from stamps)
            """,
            {"path": str(path)},
        ).fetchone()
    if row is None:
        raise BundleError(f"{bundle_dir.name} returned no metric samples at all")
    widest, cadence, samples = row
    if widest is None or cadence is None:
        return 0.0, 0.0, int(samples or 0)
    return float(widest), float(cadence), int(samples)


def check_continuity(
    bundle_dir: Path,
    multiple: float = MAX_GAP_MULTIPLE,
    floor_seconds: float = MIN_GAP_SECONDS,
) -> float:
    """Refuse a bundle with a hole in it. Returns the largest gap in seconds.

    A bundle with too few samples to establish a cadence passes, because this
    check has no information about it, and "cannot tell" is not "has a hole".
    That leaves an almost empty bundle unrejected here; nothing else rejects one
    either, and it is worth fixing, but a continuity check is the wrong place to
    hide an emptiness check.
    """
    widest, cadence, samples = largest_gap(bundle_dir)
    if samples < MIN_SAMPLES_TO_JUDGE:
        return widest
    allowed = max(cadence * multiple, floor_seconds)
    if widest > allowed:
        raise BundleContinuityError(
            f"{bundle_dir.name} has a {widest:.0f}s gap with no telemetry, against a "
            f"{cadence:.0f}s sampling cadence over {samples} samples; the recording was "
            "suspended, so its window covers time the experiment was not running"
        )
    return widest


def verify_bundle(bundle_dir: Path) -> BundleManifest:
    """Prove a bundle is exactly what was recorded.

    Checks four things, all of which have to hold for a measurement taken
    against this bundle to mean anything:

    1. Every file the manifest lists is present.
    2. Every checksum still matches, so nothing was edited after recording.
    3. Every file required by the format is listed.
    4. Nothing else is in the directory, so a label cannot hide there.
    """
    manifest = read_manifest(bundle_dir)

    missing_required = [name for name in REQUIRED_FILES if name not in manifest.file_names]
    if missing_required:
        raise BundleError(f"manifest omits required files: {', '.join(missing_required)}")

    for record in manifest.files:
        path = bundle_dir / record.name
        if not path.is_file():
            raise BundleError(f"manifest lists {record.name} but it is missing")
        actual = sha256_of(path)
        if actual != record.sha256:
            raise BundleError(
                f"{record.name} has changed since recording: "
                f"manifest says {record.sha256[:12]}, file is {actual[:12]}"
            )

    on_disk = {p.name for p in bundle_dir.iterdir() if p.is_file()}
    unexpected = on_disk - set(manifest.file_names) - {MANIFEST_NAME}
    if unexpected:
        raise BundleError(f"unexpected files in bundle: {', '.join(sorted(unexpected))}")

    forbidden = on_disk & FORBIDDEN_NAMES
    if forbidden:
        raise BundleError(
            "ground truth must live under labels/, not in the bundle: "
            + ", ".join(sorted(forbidden))
        )
    return manifest


class BundleReader:
    """Opens only the files a bundle's manifest lists.

    Every read goes through here, so there is one place that can be pointed
    at a path outside the bundle, and it refuses. Leakage control 3 in
    SPEC.md Section 8.4.
    """

    def __init__(self, bundle_dir: Path, verify: bool = True) -> None:
        self.bundle_dir = bundle_dir
        self.manifest = verify_bundle(bundle_dir) if verify else read_manifest(bundle_dir)

    def path_for(self, name: str) -> Path:
        """Resolve a name to a path, refusing anything not in the manifest."""
        if name not in self.manifest.file_names:
            raise BundleError(
                f"{name!r} is not in this bundle's manifest; "
                f"it lists {', '.join(self.manifest.file_names)}"
            )
        resolved = (self.bundle_dir / name).resolve()
        root = self.bundle_dir.resolve()
        if root not in resolved.parents:
            raise BundleError(f"{name!r} resolves outside the bundle directory")
        return resolved

    def read_json(self, name: str) -> Any:
        """Read one of the bundle's JSON files."""
        return json.loads(self.path_for(name).read_text(encoding="utf-8"))

    def alert(self) -> dict[str, Any]:
        body = self.read_json(ALERT_FILE)
        if not isinstance(body, dict):
            raise BundleError(f"{ALERT_FILE} does not contain an object")
        return body

    def changes(self) -> list[dict[str, Any]]:
        body = self.read_json(CHANGES_FILE)
        if not isinstance(body, list):
            raise BundleError(f"{CHANGES_FILE} does not contain a list")
        return body

    def topology(self) -> dict[str, Any]:
        body = self.read_json(TOPOLOGY_FILE)
        if not isinstance(body, dict):
            raise BundleError(f"{TOPOLOGY_FILE} does not contain an object")
        return body


def iter_bundles(bundles_root: Path) -> Iterator[Path]:
    """Yield every bundle directory under a root, in a stable order.

    A bundle directory is one that contains a manifest, so partially written
    directories are skipped rather than half read.
    """
    if not bundles_root.is_dir():
        return
    for manifest_path in sorted(bundles_root.glob("*/" + MANIFEST_NAME)):
        yield manifest_path.parent


def new_run_id(now: datetime | None = None) -> str:
    """A run identifier that sorts chronologically and is filesystem safe."""
    moment = now or datetime.now(UTC)
    return moment.strftime("%Y%m%dT%H%M%SZ")
