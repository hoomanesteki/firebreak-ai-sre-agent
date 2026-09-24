"""Both bundle producers must speak the same vocabulary.

A recording is written by `lab/recorder.py`, and the fixtures every other
test runs against are written by `lab/synthetic.py`. They disagreed once:
the recorder wrote `at`, `kind` and `detail`, the synthetic builder wrote
`timestamp`, `type` and `summary`, and the replay backend, which filters on
`at`, returned no changes at all.

Nothing failed. A distractor scenario, whose entire purpose is to put an
unrelated deploy next to the incident and see whether the agent blames it,
would have shown an empty change log and been scored as though the agent
had correctly ignored a distractor that was never there.

The metric names drifted the same way, on the signal that matters most.

So the agreement is asserted rather than assumed, in both directions:
every name a producer writes is declared, and every declared name is
actually present in a bundle.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from firebreak.backends.bundle_duckdb import BundleBackend
from firebreak.lab.bundle import BundleReader, ChangeRecord, EdgeRecord, derive_bundle_id
from firebreak.lab.scenario import (
    Distractor,
    DistractorKind,
    Fault,
    FaultClass,
    FaultKind,
    Load,
    ScenarioSpec,
    Split,
)
from firebreak.lab.synthetic import build_synthetic_bundle
from firebreak.signals import MetricName, is_known_metric
from firebreak.tools.evidence import TimeRange

CANONICAL_FIELDS = {"at", "kind", "service", "detail"}


def _spec_with_deploy_distractor() -> ScenarioSpec:
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


@pytest.fixture
def synthetic_bundle():
    spec = _spec_with_deploy_distractor()
    with tempfile.TemporaryDirectory() as tmp:
        bundle_dir = Path(tmp) / derive_bundle_id(spec.id, "run1")
        manifest = build_synthetic_bundle(bundle_dir, spec, "run1", seed=4)
        yield bundle_dir, manifest, spec


def test_change_record_declares_the_canonical_fields():
    assert set(ChangeRecord.model_fields) == CANONICAL_FIELDS


def test_synthetic_change_records_match_the_canonical_schema(synthetic_bundle):
    bundle_dir, _, _ = synthetic_bundle

    records = BundleReader(bundle_dir).changes()

    assert records
    for record in records:
        ChangeRecord.model_validate(record)


def test_recorder_change_records_match_the_canonical_schema():
    """Built through the same model, so the two producers cannot drift."""
    record = ChangeRecord(
        at=datetime(2026, 1, 1, tzinfo=UTC),
        kind="deploy",
        service="recommendation",
        detail="routine deployment of recommendation",
    )

    assert set(record.as_row()) == CANONICAL_FIELDS


def test_the_backend_actually_returns_changes(synthetic_bundle):
    """The symptom of the drift: zero changes, and no error."""
    bundle_dir, manifest, _ = synthetic_bundle

    with BundleBackend.open(bundle_dir) as backend:
        window = TimeRange(start=manifest.window.start, end=manifest.window.end)
        records = backend.changes(window)

    assert records, "a distractor scenario with no visible change log tests nothing"


def test_the_distractor_deploy_is_visible_to_the_agent(synthetic_bundle):
    bundle_dir, manifest, spec = synthetic_bundle

    with BundleBackend.open(bundle_dir) as backend:
        window = TimeRange(start=manifest.window.start, end=manifest.window.end)
        services = {record["service"] for record in backend.changes(window)}

    assert spec.distractors[0].service in services


def test_the_fault_flag_is_still_absent_from_the_change_log(synthetic_bundle):
    """Normalising the shape must not have reopened the leak."""
    bundle_dir, manifest, spec = synthetic_bundle

    with BundleBackend.open(bundle_dir) as backend:
        window = TimeRange(start=manifest.window.start, end=manifest.window.end)
        text = str(backend.changes(window)).lower()

    assert spec.fault.flag is not None
    assert spec.fault.flag.lower() not in text


def test_changes_outside_the_window_are_excluded(synthetic_bundle):
    bundle_dir, manifest, _ = synthetic_bundle
    empty = TimeRange(
        start=manifest.window.end + timedelta(hours=1),
        end=manifest.window.end + timedelta(hours=2),
    )

    with BundleBackend.open(bundle_dir) as backend:
        assert backend.changes(empty) == []


def test_change_record_rejects_an_unknown_field():
    """Strict on purpose.

    If extra fields were allowed, a producer could quietly add one naming
    the fault flag and the schema would say nothing. The sanitiser would
    still strip it, but a control that depends on a second control noticing
    is not a control.
    """
    with pytest.raises(ValueError, match="flag"):
        ChangeRecord(
            at=datetime(2026, 1, 1, tzinfo=UTC),
            kind="flag_change",
            service="payment",
            detail="something changed",
            flag="paymentFailure",
        )


def test_change_record_requires_every_canonical_field():
    for missing in sorted(CANONICAL_FIELDS):
        fields = {
            "at": datetime(2026, 1, 1, tzinfo=UTC),
            "kind": "deploy",
            "service": "cart",
            "detail": "deployed cart",
        }
        del fields[missing]
        with pytest.raises(ValueError, match=missing):
            ChangeRecord(**fields)


# --- the metric vocabulary -----------------------------------------------
#
# The same class of drift as the change record, on the signal that matters
# most. The exporter called a measurement span_calls_total and the synthetic
# builder called the same thing traces_span_metrics_calls_total, so a metric
# tool would have found nothing in half the bundles, and found it silently.


def test_every_metric_name_the_exporter_produces_is_declared():
    from firebreak.lab.export import METRIC_QUERIES

    for name in METRIC_QUERIES:
        assert is_known_metric(str(name)), name


def test_every_metric_name_in_a_synthetic_bundle_is_declared(synthetic_bundle):
    bundle_dir, manifest, _ = synthetic_bundle

    with BundleBackend.open(bundle_dir) as backend:
        window = TimeRange(start=manifest.window.start, end=manifest.window.end)
        found = {
            point.metric_name
            for name in MetricName
            for point in backend.query_metrics(name, window, limit=1)
        }

    assert found
    for name in found:
        assert is_known_metric(name), name


def test_a_synthetic_bundle_carries_every_declared_metric(synthetic_bundle):
    """A missing signal makes a whole fault family untestable.

    The resource families show no error rate at all, so a fixture without
    container metrics would give them no symptom to find.
    """
    bundle_dir, manifest, _ = synthetic_bundle

    with BundleBackend.open(bundle_dir) as backend:
        window = TimeRange(start=manifest.window.start, end=manifest.window.end)
        missing = [
            str(name) for name in MetricName if not backend.query_metrics(name, window, limit=1)
        ]

    assert missing == []


# --- the edge schema is strict -------------------------------------------


def test_edge_record_rejects_an_unknown_field():
    """Strict for the same reason the change record is.

    A producer that could add a field would eventually add one naming the
    fault, and the schema would say nothing.
    """
    with pytest.raises(ValueError, match="flag"):
        EdgeRecord(
            client="checkout",
            server="payment",
            requests_per_second=1.0,
            failures_per_second=0.0,
            flag="paymentFailure",
        )


def test_edge_record_rejects_a_negative_rate():
    """A rate below zero is a computation error, not a measurement."""
    with pytest.raises(ValueError):
        EdgeRecord(
            client="checkout",
            server="payment",
            requests_per_second=-1.0,
            failures_per_second=0.0,
        )


def test_edge_record_error_ratio_is_zero_when_no_requests_were_made():
    edge = EdgeRecord(
        client="checkout", server="payment", requests_per_second=0.0, failures_per_second=0.0
    )

    assert edge.error_ratio == 0.0


def test_edge_record_error_ratio_is_the_failing_share():
    edge = EdgeRecord(
        client="checkout", server="payment", requests_per_second=4.0, failures_per_second=1.0
    )

    assert edge.error_ratio == 0.25
