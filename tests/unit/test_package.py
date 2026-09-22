"""Tests for packaging the incident library (SPEC.md Section 7.1).

The archive comes off the internet, so both ends are checked here: that a
tampered archive is refused, and that a crafted archive cannot write outside
the directory it is unpacked into.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from firebreak.lab.bundle import derive_bundle_id
from firebreak.lab.package import (
    CHECKSUM_SUFFIX,
    INDEX_NAME,
    PackageError,
    build_index,
    package_library,
    read_checksum_file,
    unpack_library,
    verify_archive,
)
from firebreak.lab.scenario import (
    Fault,
    FaultClass,
    FaultKind,
    Load,
    ScenarioSpec,
    Split,
)
from firebreak.lab.synthetic import build_synthetic_bundle


def _spec(scenario_id: str = "payment-failure-100pct-20u") -> ScenarioSpec:
    return ScenarioSpec(
        id=scenario_id,
        family="error-injection",
        fault=Fault(kind=FaultKind.FLAG, flag="paymentFailure", variant="100%"),
        target_service="payment",
        fault_class=FaultClass.ERROR_INJECTION,
        load=Load(users=20),
        split=Split.TRAIN,
    )


def _library(root: Path, runs: tuple[str, ...] = ("run1", "run2")) -> ScenarioSpec:
    """Bundles live under their opaque id, never under their scenario name."""
    spec = _spec()
    for run in runs:
        build_synthetic_bundle(root / derive_bundle_id(spec.id, run), spec, run, seed=1)
    return spec


# --- index ---------------------------------------------------------------


def test_build_index_describes_every_bundle(tmp_path: Path):
    root = tmp_path / "bundles"
    _library(root)

    index = build_index(root)

    assert index["bundles"] == 2
    entries = index["entries"]
    assert [e["run_id"] for e in entries] == ["run1", "run2"]
    assert all(e["bundle_id"].startswith("inc_") for e in entries)


def test_build_index_carries_no_ground_truth(tmp_path: Path):
    root = tmp_path / "bundles"
    _library(root, runs=("run1",))

    index = build_index(root)
    entry = index["entries"][0]

    for field in ("target_service", "fault_class", "fault_flag", "canary", "scenario_id"):
        assert field not in entry


def test_build_index_is_empty_for_a_root_with_no_bundles(tmp_path: Path):
    assert build_index(tmp_path)["bundles"] == 0


# --- packaging -----------------------------------------------------------


def test_package_library_writes_an_archive_and_a_checksum(tmp_path: Path):
    root = tmp_path / "bundles"
    _library(root)

    result = package_library(root, tmp_path / "dist" / "lib.tar.gz")

    assert result.archive.is_file()
    assert result.checksum_file.name.endswith(CHECKSUM_SUFFIX)
    assert result.bundles == 2
    assert result.bytes > 0
    assert result.checksum_file.read_text(encoding="utf-8").split()[0] == result.sha256


def test_package_library_refuses_an_empty_library(tmp_path: Path):
    with pytest.raises(PackageError, match="no bundles to package"):
        package_library(tmp_path / "bundles", tmp_path / "lib.tar.gz")


def test_package_library_refuses_a_bundle_that_fails_verification(tmp_path: Path):
    """Publishing a broken library makes everyone who downloads it find out."""
    root = tmp_path / "bundles"
    spec = _library(root, runs=("run1",))
    bundle = root / derive_bundle_id(spec.id, "run1")
    (bundle / "alert.json").write_text("tampered", encoding="utf-8")

    with pytest.raises(PackageError, match="failed verification"):
        package_library(root, tmp_path / "lib.tar.gz")


def test_package_library_includes_the_index_in_the_archive(tmp_path: Path):
    root = tmp_path / "bundles"
    _library(root, runs=("run1",))

    result = package_library(root, tmp_path / "lib.tar.gz")

    with tarfile.open(result.archive, "r:gz") as tar:
        assert INDEX_NAME in tar.getnames()


def test_package_library_leaves_no_index_behind_in_the_library(tmp_path: Path):
    """A stray file in the bundles root would fail the next verification."""
    root = tmp_path / "bundles"
    _library(root, runs=("run1",))

    package_library(root, tmp_path / "lib.tar.gz")

    assert not (root / INDEX_NAME).exists()


# --- checksums -----------------------------------------------------------


def test_read_checksum_file_returns_the_digest(tmp_path: Path):
    path = tmp_path / "lib.tar.gz.sha256"
    digest = "a" * 64
    path.write_text(f"{digest}  lib.tar.gz\n", encoding="utf-8")

    assert read_checksum_file(path) == digest


def test_read_checksum_file_rejects_a_missing_file(tmp_path: Path):
    with pytest.raises(PackageError, match="cannot read"):
        read_checksum_file(tmp_path / "absent.sha256")


def test_read_checksum_file_rejects_text_that_is_not_a_digest(tmp_path: Path):
    path = tmp_path / "lib.tar.gz.sha256"
    path.write_text("not-a-digest  lib.tar.gz\n", encoding="utf-8")

    with pytest.raises(PackageError, match="does not contain a SHA-256"):
        read_checksum_file(path)


def test_read_checksum_file_rejects_an_empty_file(tmp_path: Path):
    path = tmp_path / "lib.tar.gz.sha256"
    path.write_text("", encoding="utf-8")

    with pytest.raises(PackageError, match="does not contain a SHA-256"):
        read_checksum_file(path)


def test_verify_archive_accepts_an_untouched_archive(tmp_path: Path):
    root = tmp_path / "bundles"
    _library(root, runs=("run1",))
    result = package_library(root, tmp_path / "lib.tar.gz")

    assert verify_archive(result.archive) == result.sha256


def test_verify_archive_rejects_a_tampered_archive(tmp_path: Path):
    root = tmp_path / "bundles"
    _library(root, runs=("run1",))
    result = package_library(root, tmp_path / "lib.tar.gz")

    data = bytearray(result.archive.read_bytes())
    data[len(data) // 2] ^= 0xFF
    result.archive.write_bytes(bytes(data))

    with pytest.raises(PackageError, match="does not match its checksum"):
        verify_archive(result.archive)


def test_verify_archive_rejects_a_missing_archive(tmp_path: Path):
    with pytest.raises(PackageError, match="no archive at"):
        verify_archive(tmp_path / "absent.tar.gz")


# --- unpacking -----------------------------------------------------------


def test_unpack_library_restores_and_verifies_every_bundle(tmp_path: Path):
    root = tmp_path / "bundles"
    _library(root)
    result = package_library(root, tmp_path / "lib.tar.gz")

    restored = tmp_path / "restored"

    assert unpack_library(result.archive, restored) == 2


def test_unpack_library_refuses_an_archive_that_fails_its_checksum(tmp_path: Path):
    root = tmp_path / "bundles"
    _library(root, runs=("run1",))
    result = package_library(root, tmp_path / "lib.tar.gz")
    result.checksum_file.write_text(f"{'b' * 64}  lib.tar.gz\n", encoding="utf-8")

    with pytest.raises(PackageError, match="does not match its checksum"):
        unpack_library(result.archive, tmp_path / "restored")


def test_unpack_library_refuses_a_member_escaping_the_destination(tmp_path: Path):
    """The classic archive attack, and this archive comes off the internet."""
    archive = tmp_path / "evil.tar.gz"
    payload = tmp_path / "payload.txt"
    payload.write_text("owned", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload, arcname="../escaped.txt")

    from firebreak.lab.bundle import sha256_of

    archive.with_name(archive.name + CHECKSUM_SUFFIX).write_text(
        f"{sha256_of(archive)}  {archive.name}\n", encoding="utf-8"
    )

    with pytest.raises(PackageError, match="escapes the destination"):
        unpack_library(archive, tmp_path / "restored")

    assert not (tmp_path / "escaped.txt").exists()
