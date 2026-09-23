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
