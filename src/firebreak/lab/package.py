"""Package the incident library for distribution, and verify it on arrival.

A recorded library is tens of gigabytes, which is too much for a Git
repository. SPEC.md Section 7.1 keeps a showcase subset in the repository
and publishes the whole library as a release asset.

That splits the trust problem in two. Inside the archive, each bundle
already carries a manifest with a SHA-256 per file, so tampering with a
bundle is detectable. The archive itself needs the same guarantee, so
packaging writes a checksum file alongside it and unpacking refuses an
archive whose checksum does not match.

The point is that a number measured on a downloaded library means the same
thing as one measured on the library that was recorded. Without this, "we
evaluated on the published bundles" is an unverifiable claim.
"""

from __future__ import annotations

import json
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from firebreak.lab.bundle import (
    BundleError,
    iter_bundles,
    read_manifest,
    sha256_of,
    verify_bundle,
)

CHECKSUM_SUFFIX = ".sha256"
INDEX_NAME = "library_index.json"
ARCHIVE_SUFFIX = ".tar.gz"


class PackageError(Exception):
    """The library could not be packaged, or an archive is not trustworthy."""


@dataclass(frozen=True)
class PackagedLibrary:
    """What packaging produced, and enough to check it later."""

    archive: Path
    checksum_file: Path
    sha256: str
    bundles: int
    bytes: int


def build_index(bundles_root: Path) -> dict[str, object]:
    """Describe every bundle in the library without opening its data.

    Lets a reader see what a release contains, and which scenario and run
    each bundle belongs to, before downloading tens of gigabytes.
    """
    entries: list[dict[str, object]] = []
    for bundle_dir in iter_bundles(bundles_root):
        manifest = read_manifest(bundle_dir)
        entries.append(
            {
                "scenario_id": manifest.scenario_id,
                "run_id": manifest.run_id,
                "demo_tag": manifest.demo_tag,
                "recorded_at": manifest.recorded_at.isoformat(),
                "alert_fired": manifest.alert_fired,
                "row_counts": manifest.row_counts(),
                "path": str(bundle_dir.relative_to(bundles_root)),
            }
        )
    return {
        "built_at": datetime.now(UTC).isoformat(),
        "bundles": len(entries),
        "entries": sorted(entries, key=lambda e: (str(e["scenario_id"]), str(e["run_id"]))),
    }


def package_library(
    bundles_root: Path, destination: Path, verify_each: bool = True
) -> PackagedLibrary:
    """Archive every bundle, with a checksum for the archive itself.

    Every bundle is verified before it goes in by default. Publishing a
    library that already fails its own checksums would hand everyone who
    downloads it a broken artefact, and they would find out one bundle at a
    time.
    """
    bundles = list(iter_bundles(bundles_root))
    if not bundles:
        raise PackageError(f"no bundles to package under {bundles_root}")

    if verify_each:
        broken: list[str] = []
        for bundle_dir in bundles:
            try:
                verify_bundle(bundle_dir)
            except BundleError as error:
                broken.append(f"{bundle_dir.name}: {error}")
        if broken:
            raise PackageError(
                f"{len(broken)} bundle(s) failed verification and were not packaged: "
                + "; ".join(broken)
            )

    destination.parent.mkdir(parents=True, exist_ok=True)
    index = build_index(bundles_root)
    index_path = bundles_root / INDEX_NAME
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")

    try:
        with tarfile.open(destination, "w:gz") as archive:
            archive.add(index_path, arcname=INDEX_NAME)
            for bundle_dir in bundles:
                archive.add(bundle_dir, arcname=str(bundle_dir.relative_to(bundles_root)))
    finally:
        index_path.unlink(missing_ok=True)

    digest = sha256_of(destination)
    checksum_file = destination.with_name(destination.name + CHECKSUM_SUFFIX)
    checksum_file.write_text(f"{digest}  {destination.name}\n", encoding="utf-8")

    return PackagedLibrary(
        archive=destination,
        checksum_file=checksum_file,
        sha256=digest,
        bundles=len(bundles),
        bytes=destination.stat().st_size,
    )


def read_checksum_file(checksum_file: Path) -> str:
    """Read the expected digest from a two field checksum file."""
    try:
        text = checksum_file.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise PackageError(f"cannot read {checksum_file}: {error}") from error
    digest = text.split()[0] if text else ""
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise PackageError(f"{checksum_file} does not contain a SHA-256 digest")
    return digest


def verify_archive(archive: Path, checksum_file: Path | None = None) -> str:
    """Check a downloaded archive against its published checksum."""
    if not archive.is_file():
        raise PackageError(f"no archive at {archive}")
    expected_file = checksum_file or archive.with_name(archive.name + CHECKSUM_SUFFIX)
    expected = read_checksum_file(expected_file)
    actual = sha256_of(archive)
    if actual != expected:
        raise PackageError(
            f"{archive.name} does not match its checksum: "
            f"expected {expected[:12]}, got {actual[:12]}"
        )
    return actual


def unpack_library(archive: Path, bundles_root: Path, checksum_file: Path | None = None) -> int:
    """Verify an archive, unpack it, and verify every bundle inside.

    Both ends are checked. The archive checksum proves the download is the
    file that was published; each bundle's own manifest proves the contents
    are what was recorded.
    """
    verify_archive(archive, checksum_file)
    bundles_root.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            # A path that escapes the destination is the classic archive
            # attack, and this archive comes off the internet.
            target = (bundles_root / member.name).resolve()
            if not target.is_relative_to(bundles_root.resolve()):
                raise PackageError(f"archive member escapes the destination: {member.name}")
        tar.extractall(bundles_root, filter="data")

    unpacked = list(iter_bundles(bundles_root))
    broken = []
    for bundle_dir in unpacked:
        try:
            verify_bundle(bundle_dir)
        except BundleError as error:
            broken.append(f"{bundle_dir.name}: {error}")
    if broken:
        raise PackageError(
            f"{len(broken)} unpacked bundle(s) failed verification: " + "; ".join(broken)
        )
    return len(unpacked)
