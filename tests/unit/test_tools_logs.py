"""Tests for firebreak.tools.logs: search_logs and top_error_signatures."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from firebreak.backends.base import MAX_ROWS, LogRecord
from firebreak.backends.bundle_duckdb import BundleBackend
from firebreak.lab.bundle import derive_bundle_id
from firebreak.lab.scenario import Fault, FaultClass, FaultKind, Load, ScenarioSpec, Split, Timing
from firebreak.lab.synthetic import build_synthetic_bundle
from firebreak.tools.base import MAX_SUMMARY_CHARS, ToolContext, ToolError, ToolRegistry
from firebreak.tools.logs import LOG_TOOLS

RUN_ID = "run-1"
SEED = 3

ALL_TOOL_NAMES = ("search_logs", "top_error_signatures")


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
    root = tmp_path_factory.mktemp("logs-bundle")
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
    return ToolRegistry(list(LOG_TOOLS))


def _arguments(manifest) -> dict[str, dict]:
    full = _win(manifest.window.start, manifest.window.end)
    return {
        "search_logs": {"window": full, "services": ("payment",)},
        "top_error_signatures": {"window": full, "service": "payment"},
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


def test_search_logs_rejects_a_limit_above_max_rows(registry, context, payment_bundle):
    _, manifest = payment_bundle
    arguments = {**_arguments(manifest)["search_logs"], "limit": MAX_ROWS + 1}

    with pytest.raises(ToolError, match="search_logs"):
        registry.call("search_logs", context, arguments)


def test_search_logs_rejects_a_limit_below_one(registry, context, payment_bundle):
    _, manifest = payment_bundle
    arguments = {**_arguments(manifest)["search_logs"], "limit": 0}

    with pytest.raises(ToolError, match="search_logs"):
        registry.call("search_logs", context, arguments)


# --- specific behaviour --------------------------------------------------


def test_top_error_signatures_groups_lines_differing_only_by_identifiers(
    registry, context, payment_bundle, monkeypatch
):
    """The masker collapses ids and numbers, so two otherwise identical error
    lines with different order and charge ids count as one failure mode."""
    _, manifest = payment_bundle
    records = [
        LogRecord(
            timestamp="2025-01-01T00:00:00.000Z",
            service_name="payment",
            severity="ERROR",
            body="payment charge 111 failed for order 42",
            trace_id=None,
        ),
        LogRecord(
            timestamp="2025-01-01T00:00:05.000Z",
            service_name="payment",
            severity="ERROR",
            body="payment charge 222 failed for order 99",
            trace_id=None,
        ),
    ]
    monkeypatch.setattr(context.backend, "search_logs", lambda *args, **kwargs: records)

    result = registry.call(
        "top_error_signatures",
        context,
        {"window": _win(manifest.window.start, manifest.window.end), "service": "payment"},
    )

    assert len(result.data["templates"]) == 1
    assert result.data["templates"][0]["count"] == 2
