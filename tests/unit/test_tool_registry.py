"""Tests for the assembled tool layer and the specialist allowlists.

The allowlist is a structural control, not a suggestion. A logs analyst that
can reach the trace tools will use them, report a finding nobody asked it
for, and make its own output impossible to attribute to a brief. So the
boundary is asserted rather than trusted to a prompt.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from firebreak.backends.bundle_duckdb import BundleBackend
from firebreak.lab.bundle import BundleManifest, derive_bundle_id
from firebreak.lab.scenario import (
    Fault,
    FaultClass,
    FaultKind,
    Load,
    ScenarioSpec,
    Split,
)
from firebreak.lab.synthetic import build_synthetic_bundle
from firebreak.tools.base import ToolContext, ToolError, ToolRegistry, UnknownToolError
from firebreak.tools.registry import (
    ALL_TOOLS,
    MAX_EXPANDED_ROWS,
    SPECIALIST_TOOLS,
    build_registry,
    registry_for,
)

EXPECTED_TOOL_COUNT = 11


def _spec() -> ScenarioSpec:
    return ScenarioSpec(
        id="payment-failure-100pct-20u",
        family="error-injection",
        fault=Fault(kind=FaultKind.FLAG, flag="paymentFailure", variant="100%"),
        target_service="payment",
        fault_class=FaultClass.ERROR_INJECTION,
        load=Load(users=20),
        split=Split.TRAIN,
    )


@pytest.fixture(scope="module")
def bundle() -> Iterator[tuple[Path, BundleManifest]]:
    spec = _spec()
    with tempfile.TemporaryDirectory() as tmp:
        bundle_dir = Path(tmp) / derive_bundle_id(spec.id, "run1")
        manifest = build_synthetic_bundle(bundle_dir, spec, "run1", seed=5)
        yield bundle_dir, manifest


@pytest.fixture(scope="module")
def backend(bundle: tuple[Path, BundleManifest]) -> Iterator[BundleBackend]:
    bundle_dir, _ = bundle
    with BundleBackend.open(bundle_dir) as opened:
        yield opened


@pytest.fixture
def context(backend: BundleBackend) -> ToolContext:
    return ToolContext(backend=backend)


def _incident(manifest: BundleManifest) -> dict[str, str]:
    total = (manifest.window.end - manifest.window.start).total_seconds()
    return {
        "start": (manifest.window.start + timedelta(seconds=total * 0.40)).isoformat(),
        "end": manifest.window.end.isoformat(),
    }


# --- the assembled registry ----------------------------------------------


def test_build_registry_holds_every_declared_tool():
    assert len(build_registry()) == len(ALL_TOOLS) == EXPECTED_TOOL_COUNT


# Phrasings that steer a caller away from a tool as well as toward it.
# This is a smell check, not a proof: it catches a description that only
# advertises, which is the failure mode SPEC.md principle H5 warns about.
# The phase report grades the descriptions properly, by reading them.
STEERING_PHRASES = (
    "do not use",
    "instead",
    "rather than",
    "prefer",
    "not a way",
    "never as",
    "cannot and does not",
)


def test_every_registered_tool_has_a_description_saying_when_not_to_use_it():
    """SPEC.md principle H5.

    Fourteen tools with fuzzy boundaries is how an agent calls four of them
    to answer one question, so each description has to steer away as well as
    toward.
    """
    for entry in build_registry().describe():
        description = entry["description"].lower()
        assert len(description) > 200, entry["name"]
        assert any(phrase in description for phrase in STEERING_PHRASES), entry["name"]


def test_tool_names_are_unique_and_sorted():
    names = build_registry().names()

    assert len(set(names)) == len(names)
    assert list(names) == sorted(names)


# --- specialist allowlists -----------------------------------------------


@pytest.mark.parametrize("role", sorted(SPECIALIST_TOOLS))
def test_every_specialist_can_expand_its_own_evidence(role: str):
    """A specialist that cannot expand a summary has to guess from it."""
    assert "get_evidence" in registry_for(role).names()


def test_a_logs_analyst_cannot_reach_the_trace_tools(context: ToolContext, bundle):
    _, manifest = bundle
    logs_only = registry_for("logs_analyst")

    with pytest.raises(UnknownToolError):
        logs_only.call("find_traces", context, {"window": _incident(manifest)})


def test_a_metrics_analyst_cannot_reach_the_log_tools(context: ToolContext, bundle):
    _, manifest = bundle

    with pytest.raises(UnknownToolError):
        registry_for("metrics_analyst").call(
            "search_logs", context, {"window": _incident(manifest)}
        )


def test_the_change_analyst_can_reach_the_topology_tools():
    """Its job is deciding whether a change is cause or coincidence.

    That question cannot be answered without knowing what the changed
    service is connected to.
    """
    names = registry_for("change_analyst").names()

    assert "recent_changes" in names
    assert "service_dependencies" in names
    assert "blast_radius" in names


def test_no_specialist_is_given_the_whole_registry():
    for role in SPECIALIST_TOOLS:
        assert len(registry_for(role)) < EXPECTED_TOOL_COUNT, role


def test_every_allowlisted_name_exists_in_the_full_registry():
    """An allowlist naming a tool that does not exist fails at run time."""
    available = set(build_registry().names())

    for role, names in SPECIALIST_TOOLS.items():
        assert set(names) <= available, role


def test_registry_for_rejects_an_unknown_role():
    with pytest.raises(ToolError, match="no tool allowlist"):
        registry_for("database_analyst")


# --- get_evidence --------------------------------------------------------


def test_get_evidence_returns_the_rows_behind_an_id(context: ToolContext, bundle):
    _, manifest = bundle
    registry = build_registry()
    found = registry.call(
        "search_logs", context, {"window": _incident(manifest), "severity": "ERROR", "limit": 20}
    )

    expanded = registry.call(
        "get_evidence", context, {"evidence_id": found.evidence_id, "max_rows": 3}
    )

    assert len(expanded.data["rows"]) == 3
    assert expanded.data["row_count"] >= 3
    assert expanded.evidence_id == found.evidence_id


def test_get_evidence_refuses_an_id_that_was_never_gathered(context: ToolContext):
    """The simplest fabrication there is, caught at the first opportunity."""
    with pytest.raises(ToolError, match="no evidence recorded"):
        build_registry().call("get_evidence", context, {"evidence_id": "ev_log_000000000000"})


def test_get_evidence_caps_how_many_rows_it_will_return(context: ToolContext):
    with pytest.raises(ToolError):
        build_registry().call(
            "get_evidence",
            context,
            {"evidence_id": "ev_log_000000000000", "max_rows": MAX_EXPANDED_ROWS + 1},
        )


def test_get_evidence_rejects_an_undeclared_argument(context: ToolContext):
    with pytest.raises(ToolError):
        build_registry().call(
            "get_evidence",
            context,
            {"evidence_id": "ev_log_000000000000", "unexpected": True},
        )


def test_get_evidence_reports_truncation_when_it_returns_fewer_rows(context: ToolContext, bundle):
    _, manifest = bundle
    registry = build_registry()
    found = registry.call("search_logs", context, {"window": _incident(manifest), "limit": 20})

    expanded = registry.call(
        "get_evidence", context, {"evidence_id": found.evidence_id, "max_rows": 1}
    )

    assert expanded.truncated is True


def test_get_evidence_does_not_query_the_backend_again(context: ToolContext, bundle):
    """It expands what was stored, so it cannot be used to explore."""
    _, manifest = bundle
    registry = build_registry()
    found = registry.call("search_logs", context, {"window": _incident(manifest), "limit": 5})
    before = len(context.evidence)

    registry.call("get_evidence", context, {"evidence_id": found.evidence_id})

    assert len(context.evidence) == before


def test_subset_of_a_subset_stays_within_the_allowlist():
    logs_only = registry_for("logs_analyst")

    assert logs_only.subset(("search_logs",)).names() == ("search_logs",)
    with pytest.raises(UnknownToolError):
        logs_only.subset(("find_traces",))


def test_a_subset_shares_the_same_specs_as_the_parent():
    """Subsetting must not rebuild a tool differently from the full registry."""
    full: ToolRegistry = build_registry()
    subset = full.subset(("search_logs",))

    assert subset.spec("search_logs") is full.spec("search_logs")
