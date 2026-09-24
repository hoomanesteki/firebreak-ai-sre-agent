"""Tests for firebreak.tools.metrics: list_anomalies, query_metric, compare_windows."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from firebreak.backends.base import MAX_ROWS
from firebreak.backends.bundle_duckdb import BundleBackend
from firebreak.lab.bundle import derive_bundle_id
from firebreak.lab.scenario import Fault, FaultClass, FaultKind, Load, ScenarioSpec, Split, Timing
from firebreak.lab.synthetic import build_synthetic_bundle
from firebreak.signals import MetricName
from firebreak.tools.base import MAX_SUMMARY_CHARS, ToolContext, ToolError, ToolRegistry
from firebreak.tools.metrics import METRIC_TOOLS

RUN_ID = "run-1"
SEED = 3

ALL_TOOL_NAMES = ("list_anomalies", "query_metric", "compare_windows")


def _spec() -> ScenarioSpec:
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


def _win(start: datetime, end: datetime) -> dict[str, str]:
    return {"start": start.isoformat(), "end": end.isoformat()}


@pytest.fixture(scope="module")
def payment_bundle(tmp_path_factory: pytest.TempPathFactory):
    spec = _spec()
    root = tmp_path_factory.mktemp("metrics-bundle")
    bundle_dir = root / derive_bundle_id(spec.id, RUN_ID)
    manifest = build_synthetic_bundle(bundle_dir, spec, RUN_ID, seed=SEED)
    return bundle_dir, manifest


@pytest.fixture(scope="module")
def backend(payment_bundle: tuple[Path, object]):
    bundle_dir, _ = payment_bundle
    with BundleBackend.open(bundle_dir) as opened:
        yield opened


@pytest.fixture
def context(backend: BundleBackend) -> ToolContext:
    return ToolContext(backend=backend)


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    return ToolRegistry(list(METRIC_TOOLS))


def _arguments(manifest) -> dict[str, dict]:
    fault_start = manifest.window.start + timedelta(seconds=60)
    full = _win(manifest.window.start, manifest.window.end)
    baseline = _win(manifest.window.start, fault_start)
    incident = _win(fault_start, manifest.window.end)
    metric = str(MetricName.SPAN_DURATION_P95_MS)
    return {
        "list_anomalies": {"window": incident, "baseline": baseline},
        "query_metric": {"metric": metric, "window": full},
        "compare_windows": {
            "metric": metric,
            "service": "payment",
            "baseline": baseline,
            "incident": incident,
        },
    }


# --- generic shape, every tool -----------------------------------------------


@pytest.mark.parametrize("name", ALL_TOOL_NAMES)
def test_tool_returns_a_well_formed_result(registry, context, payment_bundle, name):
    _, manifest = payment_bundle
    arguments = _arguments(manifest)[name]

    result = registry.call(name, context, arguments)

    assert result.tool == name
    assert result.summary
    assert len(result.summary) <= MAX_SUMMARY_CHARS
    assert result.evidence_id is not None
    assert context.evidence.require(result.evidence_id) is not None


@pytest.mark.parametrize("name", ALL_TOOL_NAMES)
def test_repeated_call_reuses_the_same_evidence_record(registry, context, payment_bundle, name):
    _, manifest = payment_bundle
    arguments = _arguments(manifest)[name]

    first = registry.call(name, context, arguments)
    before = len(context.evidence)
    second = registry.call(name, context, arguments)

    assert second.evidence_id == first.evidence_id
    assert len(context.evidence) == before
    assert context.evidence.repeat_count(first.evidence_id) == 1


@pytest.mark.parametrize("name", ALL_TOOL_NAMES)
def test_tool_rejects_an_undeclared_argument(registry, context, payment_bundle, name):
    _, manifest = payment_bundle
    arguments = {**_arguments(manifest)[name], "not_a_real_field": True}

    with pytest.raises(ToolError, match=name):
        registry.call(name, context, arguments)


@pytest.mark.parametrize("name", ALL_TOOL_NAMES)
def test_tool_data_payload_lists_stay_small(registry, context, payment_bundle, name):
    _, manifest = payment_bundle

    result = registry.call(name, context, _arguments(manifest)[name])

    for value in result.data.values():
        if isinstance(value, list):
            assert len(value) <= 20


def test_list_anomalies_rejects_a_negative_minimum_score(registry, context, payment_bundle):
    _, manifest = payment_bundle
    arguments = {**_arguments(manifest)["list_anomalies"], "minimum_score": -1.0}

    with pytest.raises(ToolError, match="list_anomalies"):
        registry.call("list_anomalies", context, arguments)


def test_query_metric_rejects_a_limit_above_max_rows(registry, context, payment_bundle):
    _, manifest = payment_bundle
    arguments = {**_arguments(manifest)["query_metric"], "limit": MAX_ROWS + 1}

    with pytest.raises(ToolError, match="query_metric"):
        registry.call("query_metric", context, arguments)


def test_query_metric_rejects_a_limit_below_one(registry, context, payment_bundle):
    _, manifest = payment_bundle
    arguments = {**_arguments(manifest)["query_metric"], "limit": 0}

    with pytest.raises(ToolError, match="query_metric"):
        registry.call("query_metric", context, arguments)


# --- specific behaviour --------------------------------------------------


def test_query_metric_rejects_a_metric_name_outside_the_declared_vocabulary(
    registry, context, payment_bundle
):
    _, manifest = payment_bundle
    arguments = {**_arguments(manifest)["query_metric"], "metric": "not_a_declared_metric"}

    with pytest.raises(ToolError, match="query_metric"):
        registry.call("query_metric", context, arguments)


def test_list_anomalies_on_a_payment_bundle_ranks_payment_in_the_top_three(
    registry, context, payment_bundle
):
    _, manifest = payment_bundle

    result = registry.call("list_anomalies", context, _arguments(manifest)["list_anomalies"])

    services_in_order: list[str] = []
    for row in result.data["anomalies"]:
        if row["service"] not in services_in_order:
            services_in_order.append(row["service"])

    assert "payment" in services_in_order[:3]


def test_compare_windows_reports_not_trustworthy_with_fewer_than_four_baseline_points(
    registry, context, payment_bundle
):
    _, manifest = payment_bundle
    narrow_baseline = _win(manifest.window.start, manifest.window.start + timedelta(seconds=30))
    incident = _win(
        manifest.window.start + timedelta(seconds=60),
        manifest.window.end,
    )

    result = registry.call(
        "compare_windows",
        context,
        {
            "metric": str(MetricName.SPAN_DURATION_P95_MS),
            "service": "payment",
            "baseline": narrow_baseline,
            "incident": incident,
        },
    )

    assert result.data["comparison"]["trustworthy"] is False
