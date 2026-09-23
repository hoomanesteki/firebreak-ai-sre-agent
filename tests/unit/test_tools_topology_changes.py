"""Tests for firebreak.tools.topology (service_dependencies, blast_radius)
and firebreak.tools.changes (recent_changes)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from firebreak.backends.bundle_duckdb import BundleBackend
from firebreak.lab.bundle import derive_bundle_id
from firebreak.lab.scenario import (
    Distractor,
    DistractorKind,
    Fault,
    FaultClass,
    FaultKind,
    Load,
    ScenarioSpec,
    Split,
    Timing,
)
from firebreak.lab.synthetic import build_synthetic_bundle
from firebreak.tools.base import MAX_SUMMARY_CHARS, ToolContext, ToolError, ToolRegistry
from firebreak.tools.changes import CHANGE_TOOLS
from firebreak.tools.topology import MAX_DEPTH, TOPOLOGY_TOOLS

RUN_ID = "run-1"
SEED = 3

ALL_TOOL_NAMES = ("service_dependencies", "blast_radius", "recent_changes")


def _payment_spec() -> ScenarioSpec:
    return ScenarioSpec(
        id="payment-failure-50pct-20u",
        family="error-injection",
        fault=Fault(kind=FaultKind.FLAG, flag="paymentFailure", variant="50%"),
        target_service="payment",
        fault_class=FaultClass.ERROR_INJECTION,
        load=Load(users=20),
        timing=Timing(warmup_seconds=60, fault_seconds=60, cooldown_seconds=0),
        split=Split.TRAIN,
    )


def _distractor_spec() -> ScenarioSpec:
    return ScenarioSpec(
        id="distractor-payment-failure-20u",
        family="distractor",
        fault=Fault(kind=FaultKind.FLAG, flag="paymentFailure", variant="100%"),
        target_service="payment",
        fault_class=FaultClass.ERROR_INJECTION,
        load=Load(users=20),
        split=Split.TRAIN,
        distractors=(
            Distractor(
                kind=DistractorKind.DEPLOY_EVENT,
                service="recommendation",
                offset_seconds=-120,
            ),
        ),
    )


def _win(start: datetime, end: datetime) -> dict[str, str]:
    return {"start": start.isoformat(), "end": end.isoformat()}


@pytest.fixture(scope="module")
def payment_bundle(tmp_path_factory: pytest.TempPathFactory):
    spec = _payment_spec()
    root = tmp_path_factory.mktemp("topology-bundle")
    bundle_dir = root / derive_bundle_id(spec.id, RUN_ID)
    manifest = build_synthetic_bundle(bundle_dir, spec, RUN_ID, seed=SEED)
    return bundle_dir, manifest, spec


@pytest.fixture(scope="module")
def backend(payment_bundle: tuple[Path, object, ScenarioSpec]):
    bundle_dir, _, _ = payment_bundle
    with BundleBackend.open(bundle_dir) as opened:
        yield opened


@pytest.fixture
def context(backend: BundleBackend) -> ToolContext:
    return ToolContext(backend=backend)


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    return ToolRegistry(list(TOPOLOGY_TOOLS) + list(CHANGE_TOOLS))


@pytest.fixture(scope="module")
def distractor_bundle(tmp_path_factory: pytest.TempPathFactory):
    spec = _distractor_spec()
    root = tmp_path_factory.mktemp("distractor-bundle")
    bundle_dir = root / derive_bundle_id(spec.id, "run2")
    manifest = build_synthetic_bundle(bundle_dir, spec, "run2", seed=4)
    return bundle_dir, manifest, spec


@pytest.fixture(scope="module")
def distractor_backend(distractor_bundle: tuple[Path, object, ScenarioSpec]):
    bundle_dir, _, _ = distractor_bundle
    with BundleBackend.open(bundle_dir) as opened:
        yield opened


@pytest.fixture
def distractor_context(distractor_backend: BundleBackend) -> ToolContext:
    return ToolContext(backend=distractor_backend)


def _arguments(manifest) -> dict[str, dict]:
    full = _win(manifest.window.start, manifest.window.end)
    return {
        "service_dependencies": {"service": "payment", "direction": "both", "depth": 2},
        "blast_radius": {"service": "payment"},
        "recent_changes": {"window": full},
    }


# --- generic shape, every tool -----------------------------------------------


@pytest.mark.parametrize("name", ALL_TOOL_NAMES)
def test_tool_returns_a_well_formed_result(registry, context, payment_bundle, name):
    _, manifest, _ = payment_bundle
    arguments = _arguments(manifest)[name]

    result = registry.call(name, context, arguments)

    assert result.tool == name
    assert result.summary
    assert len(result.summary) <= MAX_SUMMARY_CHARS
    assert result.evidence_id is not None
    assert context.evidence.require(result.evidence_id) is not None


@pytest.mark.parametrize("name", ALL_TOOL_NAMES)
def test_repeated_call_reuses_the_same_evidence_record(registry, context, payment_bundle, name):
    _, manifest, _ = payment_bundle
    arguments = _arguments(manifest)[name]

    first = registry.call(name, context, arguments)
    before = len(context.evidence)
    second = registry.call(name, context, arguments)

    assert second.evidence_id == first.evidence_id
    assert len(context.evidence) == before
    assert context.evidence.repeat_count(first.evidence_id) == 1


@pytest.mark.parametrize("name", ALL_TOOL_NAMES)
def test_tool_rejects_an_undeclared_argument(registry, context, payment_bundle, name):
    _, manifest, _ = payment_bundle
    arguments = {**_arguments(manifest)[name], "not_a_real_field": True}

    with pytest.raises(ToolError, match=name):
        registry.call(name, context, arguments)


@pytest.mark.parametrize("name", ALL_TOOL_NAMES)
def test_tool_data_payload_lists_stay_small(registry, context, payment_bundle, name):
    _, manifest, _ = payment_bundle

    result = registry.call(name, context, _arguments(manifest)[name])

    for value in result.data.values():
        if isinstance(value, list):
            assert len(value) <= 20


def test_service_dependencies_refuses_a_depth_above_the_max(registry, context, payment_bundle):
    _, manifest, _ = payment_bundle
    arguments = {**_arguments(manifest)["service_dependencies"], "depth": MAX_DEPTH + 1}

    with pytest.raises(ToolError, match="service_dependencies"):
        registry.call("service_dependencies", context, arguments)


# --- specific behaviour --------------------------------------------------


def test_service_dependencies_and_blast_radius_report_checkout_and_frontend(registry, context):
    dependencies = registry.call(
        "service_dependencies",
        context,
        {"service": "payment", "direction": "upstream", "depth": MAX_DEPTH},
    )
    radius = registry.call("blast_radius", context, {"service": "payment"})

    dependents = {row["neighbour"] for row in dependencies.data["dependencies"]}
    affected = {row["service"] for row in radius.data["affected_services"]}

    assert {"checkout", "frontend"} <= dependents
    assert {"checkout", "frontend"} <= affected


def test_topology_edge_reader_raises_tool_error_on_a_legacy_shaped_edge(
    registry, context, backend, monkeypatch
):
    """Guards a real drift: an edge without requests_per_second must be refused,
    not silently read as carrying no traffic."""
    legacy_topology = {
        "services": ["a", "b"],
        "edges": [{"client": "a", "server": "b", "call_count": 10, "error_count": 1}],
    }
    monkeypatch.setattr(backend, "topology", lambda: legacy_topology)

    with pytest.raises(ToolError, match="does not match the bundle schema"):
        registry.call("service_dependencies", context, {"service": "a"})


def test_recent_changes_returns_the_distractor_deploy(
    registry, distractor_context, distractor_bundle
):
    _, manifest, spec = distractor_bundle

    result = registry.call(
        "recent_changes",
        distractor_context,
        {"window": _win(manifest.window.start, manifest.window.end)},
    )

    changes = result.data["changes"]
    deploys = [row for row in changes if row["kind"] == "deploy"]
    assert any(row["service"] == spec.distractors[0].service for row in deploys)


def test_recent_changes_change_log_contains_no_feature_flag_name(
    registry, distractor_context, distractor_bundle
):
    _, manifest, spec = distractor_bundle

    result = registry.call(
        "recent_changes",
        distractor_context,
        {"window": _win(manifest.window.start, manifest.window.end)},
    )

    assert spec.fault.flag is not None
    text = str(result.data["changes"]).lower()
    assert spec.fault.flag.lower() not in text
