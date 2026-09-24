"""Tests for firebreak.tools.traces: find_traces and trace_breakdown."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from firebreak.backends.base import MAX_ROWS
from firebreak.backends.bundle_duckdb import BundleBackend
from firebreak.lab.bundle import derive_bundle_id
from firebreak.lab.scenario import Fault, FaultClass, FaultKind, Load, ScenarioSpec, Split, Timing
from firebreak.lab.synthetic import build_synthetic_bundle
from firebreak.tools.base import MAX_SUMMARY_CHARS, ToolContext, ToolError, ToolRegistry
from firebreak.tools.evidence import TimeRange
from firebreak.tools.traces import TRACE_TOOLS

RUN_ID = "run-1"
SEED = 3

ALL_TOOL_NAMES = ("find_traces", "trace_breakdown")


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
    root = tmp_path_factory.mktemp("traces-bundle")
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
    return ToolRegistry(list(TRACE_TOOLS))


@pytest.fixture(scope="module")
def sample_trace_id(backend: BundleBackend, payment_bundle: tuple[Path, object]) -> str:
    _, manifest = payment_bundle
    window = _win(manifest.window.start, manifest.window.end)

    spans = backend.find_spans(TimeRange.model_validate(window), services=("payment",), limit=1)
    return spans[0].trace_id


def _arguments(manifest, trace_id: str) -> dict[str, dict]:
    full = _win(manifest.window.start, manifest.window.end)
    return {
        "find_traces": {"window": full, "services": ("payment",)},
        "trace_breakdown": {"trace_id": trace_id},
    }


# --- generic shape, every tool -----------------------------------------------


@pytest.mark.parametrize("name", ALL_TOOL_NAMES)
def test_tool_returns_a_well_formed_result(
    registry, context, payment_bundle, sample_trace_id, name
):
    _, manifest = payment_bundle
    arguments = _arguments(manifest, sample_trace_id)[name]

    result = registry.call(name, context, arguments)

    assert result.tool == name
    assert result.summary
    assert len(result.summary) <= MAX_SUMMARY_CHARS
    assert result.evidence_id is not None
    assert context.evidence.require(result.evidence_id) is not None


@pytest.mark.parametrize("name", ALL_TOOL_NAMES)
def test_repeated_call_reuses_the_same_evidence_record(
    registry, context, payment_bundle, sample_trace_id, name
):
    _, manifest = payment_bundle
    arguments = _arguments(manifest, sample_trace_id)[name]

    first = registry.call(name, context, arguments)
    before = len(context.evidence)
    second = registry.call(name, context, arguments)

    assert second.evidence_id == first.evidence_id
    assert len(context.evidence) == before
    assert context.evidence.repeat_count(first.evidence_id) == 1


@pytest.mark.parametrize("name", ALL_TOOL_NAMES)
def test_tool_rejects_an_undeclared_argument(
    registry, context, payment_bundle, sample_trace_id, name
):
    _, manifest = payment_bundle
    arguments = {**_arguments(manifest, sample_trace_id)[name], "not_a_real_field": True}

    with pytest.raises(ToolError, match=name):
        registry.call(name, context, arguments)


@pytest.mark.parametrize("name", ALL_TOOL_NAMES)
def test_tool_data_payload_lists_stay_small(
    registry, context, payment_bundle, sample_trace_id, name
):
    _, manifest = payment_bundle

    result = registry.call(name, context, _arguments(manifest, sample_trace_id)[name])

    for value in result.data.values():
        if isinstance(value, list):
            assert len(value) <= 20


def test_find_traces_rejects_a_limit_above_max_rows(
    registry, context, payment_bundle, sample_trace_id
):
    _, manifest = payment_bundle
    arguments = {**_arguments(manifest, sample_trace_id)["find_traces"], "limit": MAX_ROWS + 1}

    with pytest.raises(ToolError, match="find_traces"):
        registry.call("find_traces", context, arguments)


def test_find_traces_rejects_a_limit_below_one(registry, context, payment_bundle, sample_trace_id):
    _, manifest = payment_bundle
    arguments = {**_arguments(manifest, sample_trace_id)["find_traces"], "limit": 0}

    with pytest.raises(ToolError, match="find_traces"):
        registry.call("find_traces", context, arguments)


# --- specific behaviour --------------------------------------------------


def test_find_traces_returns_trace_ids_that_trace_breakdown_can_then_resolve(
    registry, context, payment_bundle
):
    _, manifest = payment_bundle
    full = _win(manifest.window.start, manifest.window.end)

    found = registry.call("find_traces", context, {"window": full, "services": ("payment",)})
    assert found.data["traces"]
    trace_id = found.data["traces"][0]["trace_id"]

    breakdown = registry.call("trace_breakdown", context, {"trace_id": trace_id})

    assert breakdown.data["critical_path"]


def test_trace_breakdowns_critical_path_starts_at_the_root_span(
    registry, context, backend, sample_trace_id
):
    all_spans = backend.trace_spans(sample_trace_id)
    span_ids = {span.span_id for span in all_spans}
    roots = {
        span.span_id
        for span in all_spans
        if span.parent_span_id is None or span.parent_span_id not in span_ids
    }

    result = registry.call("trace_breakdown", context, {"trace_id": sample_trace_id})

    first_hop = result.data["critical_path"][0]
    assert first_hop["span_id"] in roots


def test_trace_breakdowns_self_time_is_never_negative(registry, context, sample_trace_id):
    result = registry.call("trace_breakdown", context, {"trace_id": sample_trace_id})

    for hop in result.data["critical_path"]:
        assert hop["self_ms"] >= 0.0
