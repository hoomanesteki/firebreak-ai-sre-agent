"""Tests for firebreak.lab.synthetic."""

from __future__ import annotations

import ast
import json
from datetime import timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from firebreak.lab.bundle import (
    ALERT_FILE,
    LOGS_FILE,
    MANIFEST_NAME,
    METRICS_FILE,
    TRACES_FILE,
    BundleReader,
    verify_bundle,
)
from firebreak.lab.bundle_writer import LOGS_SCHEMA, METRICS_SCHEMA, TRACES_SCHEMA
from firebreak.lab.scenario import Fault, FaultClass, FaultKind, Load, ScenarioSpec, Split, Timing
from firebreak.lab.synthetic import WINDOW_ANCHOR, build_synthetic_bundle

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_SOURCE = REPO_ROOT / "src" / "firebreak" / "lab" / "synthetic.py"
FLAG_INVENTORY_PATH = REPO_ROOT / "reports" / "lab" / "flag_inventory.json"


def _fault(**overrides):
    fields = {"kind": FaultKind.FLAG, "flag": "paymentFailure", "variant": "50%"}
    fields.update(overrides)
    return Fault(**fields)


def _spec(**overrides):
    fields = {
        "id": "payment-failure-50pct-20u",
        "family": "error-injection",
        "fault": _fault(),
        "target_service": "payment",
        "fault_class": FaultClass.ERROR_INJECTION,
        "load": Load(users=20),
        "timing": Timing(warmup_seconds=60, fault_seconds=60, cooldown_seconds=0),
        "split": Split.TRAIN,
    }
    fields.update(overrides)
    return ScenarioSpec(**fields)


def _synthetic_source_ast() -> ast.Module:
    return ast.parse(SYNTHETIC_SOURCE.read_text(encoding="utf-8"))


# --- build_synthetic_bundle: passes verify_bundle --------------------------


@pytest.mark.parametrize(
    "spec",
    [
        _spec(),
        _spec(
            id="no-fault-flood-homepage-sr5-20u",
            family="no-fault",
            fault=Fault(kind=FaultKind.NONE),
            target_service=None,
            fault_class=FaultClass.NONE,
        ),
        _spec(id="frontend-failure-50pct-20u", target_service="frontend"),
        _spec(id="mystery-service-failure-50pct-20u", target_service="mystery-service"),
    ],
    ids=["error-injection-target", "no-fault", "frontend-target", "target-outside-base-cast"],
)
def test_build_synthetic_bundle_produces_a_bundle_that_passes_verify_bundle(
    tmp_path: Path, spec: ScenarioSpec
):
    bundle_dir = tmp_path / "bundle"

    manifest = build_synthetic_bundle(bundle_dir, spec, run_id="run-1", seed=0)

    assert verify_bundle(bundle_dir) == manifest


def test_build_synthetic_bundle_alert_fires_and_names_the_expected_alert_with_no_target(
    tmp_path: Path,
):
    spec = _spec(
        id="no-fault-flood-homepage-sr5-20u-alert",
        family="no-fault",
        fault=Fault(kind=FaultKind.NONE),
        target_service=None,
        fault_class=FaultClass.NONE,
        expected_alert="LoadGeneratorFloodHomepage",
    )
    bundle_dir = tmp_path / "bundle"

    manifest = build_synthetic_bundle(bundle_dir, spec, run_id="run-1", seed=9)

    assert verify_bundle(bundle_dir) == manifest
    assert manifest.alert_fired is True
    assert manifest.alert_fired_at is not None

    alert_payload = json.loads((bundle_dir / ALERT_FILE).read_text(encoding="utf-8"))
    assert alert_payload["status"] == "firing"
    assert alert_payload["alerts"][0]["labels"]["alertname"] == "LoadGeneratorFloodHomepage"


# --- determinism -------------------------------------------------------


def test_build_synthetic_bundle_same_seed_produces_byte_identical_parquet_files(tmp_path: Path):
    spec = _spec()
    dir_a, dir_b = tmp_path / "a", tmp_path / "b"

    build_synthetic_bundle(dir_a, spec, run_id="run-1", seed=7)
    build_synthetic_bundle(dir_b, spec, run_id="run-1", seed=7)

    for name in (METRICS_FILE, TRACES_FILE, LOGS_FILE):
        assert (dir_a / name).read_bytes() == (dir_b / name).read_bytes()


def test_build_synthetic_bundle_different_seeds_produce_different_bytes_for_some_file(
    tmp_path: Path,
):
    spec = _spec()
    dir_a, dir_b = tmp_path / "a", tmp_path / "b"

    build_synthetic_bundle(dir_a, spec, run_id="run-1", seed=1)
    build_synthetic_bundle(dir_b, spec, run_id="run-1", seed=2)

    differing = [
        name
        for name in (METRICS_FILE, TRACES_FILE, LOGS_FILE)
        if (dir_a / name).read_bytes() != (dir_b / name).read_bytes()
    ]
    assert differing


# --- never touches the global random module --------------------------------


def test_synthetic_module_never_calls_the_global_random_module_directly():
    tree = _synthetic_source_ast()

    bare_random_calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "random"
        and node.func.attr != "Random"
    ]

    assert bare_random_calls == []


# --- docstring -----------------------------------------------------------


def test_synthetic_module_docstring_first_line_states_data_is_synthetic_and_for_tests_only():
    tree = _synthetic_source_ast()
    docstring = ast.get_docstring(tree)
    assert docstring is not None

    first_line = docstring.splitlines()[0]

    assert first_line == (
        "Synthetic incident bundle: fabricated data for tests only, never for a reported metric."
    )


# --- the target service shows a symptom -------------------------------


def test_build_synthetic_bundle_target_service_shows_a_symptom_during_the_fault_window(
    tmp_path: Path,
):
    spec = _spec()
    bundle_dir = tmp_path / "bundle"
    build_synthetic_bundle(bundle_dir, spec, run_id="run-1", seed=11)

    rows = pq.read_table(bundle_dir / TRACES_FILE).to_pylist()
    fault_start = WINDOW_ANCHOR + timedelta(seconds=spec.timing.warmup_seconds)
    fault_end = fault_start + timedelta(seconds=spec.timing.fault_seconds)

    def error_spans(service: str) -> int:
        return sum(
            1
            for row in rows
            if row["service_name"] == service
            and fault_start <= row["start_time"] < fault_end
            and row["status_code"] == "STATUS_CODE_ERROR"
        )

    # "cart" is neither the target ("payment") nor one of its callers
    # ("frontend", "checkout") on the shortest path to it.
    target_errors = error_spans(spec.target_service)
    unrelated_errors = error_spans("cart")

    assert target_errors > 0
    assert target_errors > unrelated_errors


# --- changes.json names no feature flag --------------------------------


def test_build_synthetic_bundle_changes_json_never_names_a_flag_from_the_inventory(
    tmp_path: Path,
):
    spec = _spec()
    bundle_dir = tmp_path / "bundle"
    build_synthetic_bundle(bundle_dir, spec, run_id="run-1", seed=4)

    reader = BundleReader(bundle_dir)
    changes_text = json.dumps(reader.changes()).lower()

    inventory = json.loads(FLAG_INVENTORY_PATH.read_text(encoding="utf-8"))
    flag_names = list(inventory["flags"])
    assert flag_names

    for flag in flag_names:
        assert flag.lower() not in changes_text


# --- manifest.json carries no ground truth ------------------------------


def test_build_synthetic_bundle_manifest_json_names_no_ground_truth(tmp_path: Path):
    spec = _spec()
    bundle_dir = tmp_path / "bundle"
    build_synthetic_bundle(bundle_dir, spec, run_id="run-1", seed=5)

    manifest_text = (bundle_dir / MANIFEST_NAME).read_text(encoding="utf-8").lower()

    assert spec.target_service is not None
    assert spec.fault.flag is not None
    assert spec.target_service.lower() not in manifest_text
    assert spec.fault_class.value.lower() not in manifest_text
    assert "canary" not in manifest_text
    assert spec.fault.flag.lower() not in manifest_text


# --- manifest row counts match the parquet ------------------------------


def test_build_synthetic_bundle_manifest_row_counts_match_the_parquet_row_counts(tmp_path: Path):
    spec = _spec()
    bundle_dir = tmp_path / "bundle"
    manifest = build_synthetic_bundle(bundle_dir, spec, run_id="run-1", seed=6)

    for name in (METRICS_FILE, TRACES_FILE, LOGS_FILE):
        table = pq.read_table(bundle_dir / name)
        assert manifest.record_for(name).rows == table.num_rows


# --- parquet schemas -----------------------------------------------------


def test_build_synthetic_bundle_metrics_traces_logs_parquet_match_their_declared_schemas(
    tmp_path: Path,
):
    spec = _spec()
    bundle_dir = tmp_path / "bundle"
    build_synthetic_bundle(bundle_dir, spec, run_id="run-1", seed=8)

    for name, schema in (
        (METRICS_FILE, METRICS_SCHEMA),
        (TRACES_FILE, TRACES_SCHEMA),
        (LOGS_FILE, LOGS_SCHEMA),
    ):
        table = pq.read_table(bundle_dir / name)
        assert table.schema.names == schema.names
        assert [str(field_type) for field_type in table.schema.types] == [
            str(field_type) for field_type in schema.types
        ]
