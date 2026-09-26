"""End to end: a scenario specification through to cited evidence.

Every phase so far has its own unit tests, and they all pass. That is not the
same as the phases working together, and the defects found while building
Phase 3 were all of that kind: two producers disagreeing on a field name, a
timestamp rendered in the wrong timezone, a bundle path carrying the answer.
None of them broke a unit test. All of them would have broken an
investigation.

So this walks the whole chain for several fault families and asserts the
properties that only exist across seams:

1. A specification from the real library loads and is internally consistent.
2. A recording produces a bundle that verifies, and a label that sits
   outside it.
3. Every metric name in that bundle is one the vocabulary declares.
4. The replay backend opens the bundle and answers every signal.
5. Every tool runs, produces a summary, and records re-runnable evidence.
6. The same question asked twice yields the same evidence id.
7. Nothing the agent can reach names the fault, the culprit, or the scenario.
8. Deterministic triage ranks the culprit from that bundle alone.
9. The B0 report names it, cites evidence that exists, and abstains when
   there is nothing to name.

Phase 4 extended this rather than adding a second end to end test, because
the seams it introduces are all inside the chain that already runs here: the
ranking reads a bundle's topology, the report reads the ranking, and the
report's citations have to resolve against the same evidence store the tools
write to.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from firebreak.backends.bundle_duckdb import BundleBackend
from firebreak.evals.splits import require_sound_splits
from firebreak.lab.bundle import (
    BundleManifest,
    BundleReader,
    ChangeRecord,
    EdgeRecord,
    derive_bundle_id,
    verify_bundle,
)
from firebreak.lab.scenario import FaultClass, ScenarioSpec, load_library
from firebreak.lab.synthetic import build_synthetic_bundle
from firebreak.signals import MetricName, is_known_metric
from firebreak.tools.base import ToolContext
from firebreak.tools.evidence import TimeRange
from firebreak.tools.registry import SPECIALIST_TOOLS, build_registry, registry_for
from firebreak.triage.pipeline import triage_bundle
from firebreak.triage.report import NO_AI_LABEL, build_b0_report

REPO_ROOT = Path(__file__).resolve().parents[2]

# The library's size, declared once so the number lives in one place.
#
# It was 120 and is 114. Twelve scenarios built on `imageSlowLoad` were removed
# and six working replacements generated, because that flag is evaluated in the
# frontend's browser code and the load generator cannot trigger it: every
# scenario built on it recorded a bundle in which nothing happened while its
# label named a culprit. See UNUSABLE_FLAGS in scripts/generate_scenarios.py.
EXPECTED_LIBRARY_SIZE = 114
SPECS_DIR = REPO_ROOT / "scenarios" / "specs"
INVENTORY = REPO_ROOT / "reports" / "lab" / "flag_inventory.json"

# One scenario per fault family that behaves differently, chosen from the
# real library rather than invented here, so this exercises what will
# actually be recorded.
FAMILIES_UNDER_TEST = (
    FaultClass.ERROR_INJECTION,
    FaultClass.UNREACHABLE_DEPENDENCY,
    FaultClass.LATENCY,
    FaultClass.MEMORY_LEAK,
    FaultClass.NONE,
)


@pytest.fixture(scope="module")
def library() -> dict[str, ScenarioSpec]:
    return load_library(SPECS_DIR)


def _one_per_family(library: dict[str, ScenarioSpec]) -> dict[FaultClass, ScenarioSpec]:
    chosen: dict[FaultClass, ScenarioSpec] = {}
    for spec in sorted(library.values(), key=lambda s: s.id):
        if spec.fault_class in FAMILIES_UNDER_TEST and spec.fault_class not in chosen:
            chosen[spec.fault_class] = spec
    return chosen


@pytest.fixture(scope="module")
def recorded(library: dict[str, ScenarioSpec]) -> Iterator[dict[FaultClass, tuple]]:
    """Build one bundle per family under test, once."""
    chosen = _one_per_family(library)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        built: dict[FaultClass, tuple[ScenarioSpec, Path, BundleManifest]] = {}
        for fault_class, spec in chosen.items():
            bundle_dir = root / derive_bundle_id(spec.id, "run1")
            manifest = build_synthetic_bundle(bundle_dir, spec, "run1", seed=17)
            built[fault_class] = (spec, bundle_dir, manifest)
        yield built


def _windows(manifest: BundleManifest) -> tuple[dict[str, str], dict[str, str]]:
    total = (manifest.window.end - manifest.window.start).total_seconds()
    baseline = {
        "start": manifest.window.start.isoformat(),
        "end": (manifest.window.start + timedelta(seconds=total * 0.33)).isoformat(),
    }
    incident = {
        "start": (manifest.window.start + timedelta(seconds=total * 0.40)).isoformat(),
        "end": manifest.window.end.isoformat(),
    }
    return baseline, incident


# --- 1. the library -------------------------------------------------------


def test_the_real_library_loads_and_its_splits_are_sound(library):
    summary = require_sound_splits(library)

    assert summary.scenarios == EXPECTED_LIBRARY_SIZE
    assert summary.tunable_scenarios > 0
    assert summary.held_out_scenarios > 0


def test_every_family_under_test_exists_in_the_real_library(library):
    chosen = _one_per_family(library)

    assert set(chosen) == set(FAMILIES_UNDER_TEST)


# --- 2 and 3. the bundle --------------------------------------------------


@pytest.mark.parametrize("fault_class", FAMILIES_UNDER_TEST)
def test_every_recorded_bundle_verifies(recorded, fault_class):
    _, bundle_dir, _ = recorded[fault_class]

    assert verify_bundle(bundle_dir).bundle_id.startswith("inc_")


@pytest.mark.parametrize("fault_class", FAMILIES_UNDER_TEST)
def test_no_bundle_carries_ground_truth_anywhere(recorded, fault_class):
    """The property the whole evaluation rests on, checked per family."""
    spec, bundle_dir, _ = recorded[fault_class]

    blob = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in sorted(bundle_dir.glob("*.json"))
    ).lower()

    assert spec.id.lower() not in blob
    assert "target_service" not in blob
    assert "fault_class" not in blob
    assert "canary" not in blob
    if spec.fault.flag:
        assert spec.fault.flag.lower() not in blob
    if spec.target_service:
        # The culprit's name may legitimately appear as a service in
        # telemetry. What must not appear is a field asserting it is the
        # answer, which the checks above cover.
        assert f'"target_service": "{spec.target_service}"' not in blob


@pytest.mark.parametrize("fault_class", FAMILIES_UNDER_TEST)
def test_every_metric_name_in_a_bundle_is_declared(recorded, fault_class):
    """Closes the gap the recorder's own test double was sitting in.

    A producer writing an undeclared name produces a bundle that every
    metric tool reads as empty, silently.
    """
    _, bundle_dir, manifest = recorded[fault_class]

    with BundleBackend.open(bundle_dir) as backend:
        window = TimeRange(start=manifest.window.start, end=manifest.window.end)
        found = {
            point.metric_name
            for name in MetricName
            for point in backend.query_metrics(name, window, limit=1)
        }

    assert found, "a bundle with no metrics cannot support any investigation"
    for name in found:
        assert is_known_metric(name), name


@pytest.mark.parametrize("fault_class", FAMILIES_UNDER_TEST)
def test_change_and_edge_records_match_their_declared_schemas(recorded, fault_class):
    _, bundle_dir, _ = recorded[fault_class]
    reader = BundleReader(bundle_dir)

    for record in reader.changes():
        ChangeRecord.model_validate(record)
    for edge in reader.topology()["edges"]:
        EdgeRecord.model_validate({k: v for k, v in edge.items() if k != "error_ratio"})


# --- 4 and 5. the tools ---------------------------------------------------


@pytest.mark.parametrize("fault_class", FAMILIES_UNDER_TEST)
def test_every_tool_runs_and_cites_evidence(recorded, fault_class):
    """The whole read only layer, against one bundle per family."""
    spec, bundle_dir, manifest = recorded[fault_class]
    baseline, incident = _windows(manifest)

    with BundleBackend.open(bundle_dir) as backend:
        context = ToolContext(backend=backend)
        registry = build_registry()
        calls: list[tuple[str, dict[str, Any]]] = [
            ("list_anomalies", {"window": incident, "baseline": baseline}),
            ("query_metric", {"metric": MetricName.SPAN_CALLS_TOTAL, "window": incident}),
            (
                "compare_windows",
                {
                    "metric": MetricName.SPAN_CALLS_TOTAL,
                    "service": spec.target_service or "frontend",
                    "baseline": baseline,
                    "incident": incident,
                },
            ),
            ("search_logs", {"window": incident, "limit": 20}),
            ("top_error_signatures", {"window": incident}),
            ("find_traces", {"window": incident, "limit": 5}),
            ("service_dependencies", {"service": "checkout", "direction": "both", "depth": 2}),
            ("blast_radius", {"service": "checkout"}),
            ("recent_changes", {"window": incident}),
        ]

        for name, arguments in calls:
            result = registry.call(name, context, arguments)
            assert result.tool == name
            assert result.summary, name
            assert len(result.summary) <= 600, name
            assert result.evidence_id, name
            assert context.evidence.get(result.evidence_id) is not None, name

        traces = registry.call("find_traces", context, {"window": incident, "limit": 3})
        trace_rows = traces.data.get("traces") or []
        if trace_rows:
            breakdown = registry.call(
                "trace_breakdown", context, {"trace_id": trace_rows[0]["trace_id"]}
            )
            assert breakdown.evidence_id
            assert breakdown.data["critical_path"]


@pytest.mark.parametrize("fault_class", FAMILIES_UNDER_TEST)
def test_the_same_question_twice_gives_the_same_evidence_id(recorded, fault_class):
    """What makes a citation checkable at all."""
    _, bundle_dir, manifest = recorded[fault_class]
    _, incident = _windows(manifest)

    with BundleBackend.open(bundle_dir) as backend:
        context = ToolContext(backend=backend)
        registry = build_registry()
        first = registry.call("search_logs", context, {"window": incident, "limit": 10})
        stored = len(context.evidence)
        second = registry.call("search_logs", context, {"window": incident, "limit": 10})

    assert first.evidence_id == second.evidence_id
    assert len(context.evidence) == stored
    assert context.evidence.repeat_count(first.evidence_id) == 1


def test_evidence_survives_reopening_the_bundle(recorded):
    """A verifier re-runs a citation in a new process, not the one that made it."""
    _, bundle_dir, manifest = recorded[FaultClass.ERROR_INJECTION]
    _, incident = _windows(manifest)
    arguments = {"window": incident, "limit": 10}

    ids = []
    for _ in range(2):
        with BundleBackend.open(bundle_dir) as backend:
            context = ToolContext(backend=backend)
            ids.append(build_registry().call("search_logs", context, arguments).evidence_id)

    assert ids[0] == ids[1]


# --- 6. the no fault family ----------------------------------------------


def test_the_no_fault_family_records_a_spike_with_no_culprit(recorded):
    """Its entire purpose: an alert with no service at fault.

    If the flood flag were named in notes rather than applied, this bundle
    would look like ordinary traffic and the family would test nothing.
    """
    spec, bundle_dir, manifest = recorded[FaultClass.NONE]

    assert spec.target_service is None
    assert spec.second_faults, "a no fault scenario must still apply the flood flag"

    with BundleBackend.open(bundle_dir) as backend:
        window = TimeRange(start=manifest.window.start, end=manifest.window.end)
        points = backend.query_metrics(MetricName.SPAN_CALLS_TOTAL, window, limit=50)

    assert points, "a demand spike scenario with no traffic records nothing"


# --- 7. the specialist boundary ------------------------------------------


def test_each_specialist_can_complete_its_own_work_and_no_more(recorded):
    """Every role must be able to gather and expand evidence unaided."""
    _, bundle_dir, manifest = recorded[FaultClass.ERROR_INJECTION]
    baseline, incident = _windows(manifest)
    first_call: dict[str, tuple[str, dict[str, Any]]] = {
        "metrics_analyst": ("list_anomalies", {"window": incident, "baseline": baseline}),
        "logs_analyst": ("search_logs", {"window": incident, "limit": 5}),
        "traces_analyst": ("find_traces", {"window": incident, "limit": 3}),
        "change_analyst": ("recent_changes", {"window": incident}),
    }

    with BundleBackend.open(bundle_dir) as backend:
        for role in SPECIALIST_TOOLS:
            context = ToolContext(backend=backend)
            scoped = registry_for(role)
            name, arguments = first_call[role]
            result = scoped.call(name, context, arguments)
            expanded = scoped.call(
                "get_evidence", context, {"evidence_id": result.evidence_id, "max_rows": 2}
            )
            assert expanded.data["row_count"] >= 0, role


# --- 8. the leakage checker over a produced bundle ------------------------


def test_the_leakage_checker_passes_on_produced_bundles(recorded, tmp_path):
    """Run the real checker against real output, not a hand made fixture."""
    import check_leakage

    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()
    for fault_class in FAMILIES_UNDER_TEST:
        _, bundle_dir, _ = recorded[fault_class]
        target = bundles_root / bundle_dir.name
        target.mkdir()
        for path in bundle_dir.iterdir():
            (target / path.name).write_bytes(path.read_bytes())

    rules = check_leakage.load_rules()

    assert check_leakage.check_bundles(rules, bundles_root) == []
    assert check_leakage.check_bundle_names(rules, bundles_root) == []
    flags = check_leakage.known_flag_names(INVENTORY)
    assert check_leakage.check_flag_names_in_changes(bundles_root, flags) == []


def test_the_window_of_every_bundle_is_utc(recorded):
    """A window in local time would make evidence ids machine dependent."""
    for fault_class in FAMILIES_UNDER_TEST:
        _, _, manifest = recorded[fault_class]
        assert manifest.window.start.tzinfo is not None
        assert manifest.window.start.utcoffset() == timedelta(0)
        assert manifest.window.start >= datetime(2000, 1, 1, tzinfo=UTC)


def test_the_flag_inventory_still_matches_the_pinned_demo():
    """A pin bump that changed a flag name would invalidate the library."""
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))

    assert inventory["flag_count"] == len(inventory["flags"])
    assert "paymentFailure" in inventory["flags"]


# --- 8. deterministic triage ---------------------------------------------


FAULTY_FAMILIES = [f for f in FAMILIES_UNDER_TEST if f is not FaultClass.NONE]


@pytest.mark.parametrize("fault_class", FAULTY_FAMILIES)
def test_triage_ranks_the_culprit_from_the_bundle_alone(recorded, fault_class):
    """The ranking gets the bundle directory and nothing else.

    Not the specification, not the label, not the fault timing. That is the
    whole point of the opaque bundle id: everything triage knows it had to
    read out of recorded telemetry.
    """
    spec, bundle_dir, _ = recorded[fault_class]
    result = triage_bundle(bundle_dir, verify=True)

    ranked = [candidate.service for candidate in result.candidates]
    assert spec.target_service in ranked, (
        f"{fault_class.value}: {spec.target_service} not in {ranked}"
    )
    assert result.top_service == spec.target_service


@pytest.mark.parametrize("fault_class", FAMILIES_UNDER_TEST)
def test_triage_is_deterministic(recorded, fault_class):
    """Two runs over one bundle must agree exactly.

    A ranking that reshuffled between identical runs would make a pass^3
    reliability measure report noise as instability in the agent.
    """
    _, bundle_dir, _ = recorded[fault_class]
    first = triage_bundle(bundle_dir, verify=False)
    second = triage_bundle(bundle_dir, verify=False)
    assert first.candidates == second.candidates
    assert first.onsets == second.onsets
    assert first.top_anomaly == second.top_anomaly


def test_triage_says_nothing_is_wrong_when_nothing_is(recorded):
    """The no fault family, which is the family it is easiest to fail.

    A ranking always returns an order, so a healthy system still produces a
    confident looking first place. Reporting it is how an operator learns to
    ignore the tool.
    """
    spec, bundle_dir, _ = recorded[FaultClass.NONE]
    assert spec.target_service is None
    result = triage_bundle(bundle_dir, verify=True)
    assert result.says_nothing_is_wrong
    assert result.top_service is None


@pytest.mark.parametrize("fault_class", FAULTY_FAMILIES)
def test_a_real_incident_clears_the_abstention_threshold(recorded, fault_class):
    """The other half of abstention, asserted so the threshold cannot drift up.

    A threshold high enough to silence every no fault case and every real
    one as well would pass the test above and be useless.
    """
    _, bundle_dir, _ = recorded[fault_class]
    result = triage_bundle(bundle_dir, verify=False)
    assert not result.says_nothing_is_wrong
    assert result.top_anomaly >= result.abstention_threshold


# --- 9. the B0 report ----------------------------------------------------


@pytest.mark.parametrize("fault_class", FAMILIES_UNDER_TEST)
def test_the_b0_report_cites_only_evidence_it_gathered(recorded, fault_class):
    """Every cited id must resolve in the store that produced it.

    A report citing an id nothing recorded is the simplest fabrication
    there is, and the exit gate in a later phase catches it exactly here.
    """
    _, bundle_dir, _ = recorded[fault_class]
    report = build_b0_report(bundle_dir, verify=True)

    assert report.cited_ids, "a report with no citations proves nothing"
    for evidence_id in report.cited_ids:
        assert evidence_id in report.evidence, f"{evidence_id} was cited but never gathered"


@pytest.mark.parametrize("fault_class", FAMILIES_UNDER_TEST)
def test_the_b0_report_is_labelled_as_having_no_analysis(recorded, fault_class):
    """SPEC.md Section 7 requires the deterministic floor to say so.

    It reads like prose either way, so without the label an operator cannot
    tell that nothing reasoned about it.
    """
    _, bundle_dir, _ = recorded[fault_class]
    assert NO_AI_LABEL in build_b0_report(bundle_dir, verify=False).to_markdown()


@pytest.mark.parametrize("fault_class", FAULTY_FAMILIES)
def test_the_b0_report_names_the_culprit(recorded, fault_class):
    spec, bundle_dir, _ = recorded[fault_class]
    report = build_b0_report(bundle_dir, verify=False)
    assert report.named_service == spec.target_service
    assert not report.abstained


def test_the_b0_report_names_nobody_when_nothing_is_wrong(recorded):
    _, bundle_dir, _ = recorded[FaultClass.NONE]
    report = build_b0_report(bundle_dir, verify=False)
    assert report.abstained
    assert report.named_service is None
    markdown = report.to_markdown()
    # The report must not name a leading suspect anywhere in its prose when
    # it has decided there is no incident.
    assert "ranks first" not in markdown


@pytest.mark.parametrize("fault_class", FAMILIES_UNDER_TEST)
def test_the_b0_report_never_names_the_fault_or_the_scenario(recorded, fault_class):
    """The report is built from the bundle, so it cannot know these.

    Asserted anyway, because the report is the artefact a person reads, and
    it is the last place a leak would be noticed.
    """
    spec, bundle_dir, _ = recorded[fault_class]
    markdown = build_b0_report(bundle_dir, verify=False).to_markdown().lower()

    forbidden = {spec.id, spec.fault_class.value, *spec.fault_flag_names}
    for term in forbidden:
        assert term.lower() not in markdown, f"the B0 report names {term!r}"


@pytest.mark.parametrize("fault_class", FAMILIES_UNDER_TEST)
def test_the_b0_report_is_byte_identical_across_runs(recorded, fault_class):
    """Same bundle, same report, including every evidence id in it."""
    _, bundle_dir, _ = recorded[fault_class]
    first = build_b0_report(bundle_dir, verify=False).to_markdown()
    second = build_b0_report(bundle_dir, verify=False).to_markdown()
    assert first == second
