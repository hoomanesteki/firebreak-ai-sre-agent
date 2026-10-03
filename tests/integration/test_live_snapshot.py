"""Run captured telemetry through the public investigation path without a model."""

from datetime import UTC, datetime, timedelta

import pytest

from firebreak.lab.bundle import BundleReader
from firebreak.lab.live import snapshot_and_investigate


class Exporter:
    def export_metrics(self, start, end):
        return [
            {
                "timestamp": start + timedelta(seconds=i * 15),
                "metric_name": "traces_span_metrics_calls_total",
                "service_name": "payment",
                "labels_json": "{}",
                "value": 1.0,
            }
            for i in range(80)
        ]

    def export_traces(self, start, end):
        return []

    def export_logs(self, start, end):
        return []

    def export_topology(self, start, end):
        return {"services": ["payment"], "edges": []}


def test_snapshot_is_investigated_and_published(tmp_path):
    result, path = snapshot_and_investigate(
        Exporter(),
        tmp_path / "bundles",
        tmp_path / "reports",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert path.is_file()
    assert result.report.incident_id.startswith("inc_")
    reader = BundleReader(tmp_path / "bundles" / result.report.incident_id, verify=True)
    assert reader.manifest.notes and "live" in reader.manifest.notes
    assert result.gate.passed


def test_export_failure_publishes_no_partial_bundle(tmp_path):
    class Broken(Exporter):
        def export_logs(self, start, end):
            raise RuntimeError("backend unavailable")

    with pytest.raises(RuntimeError):
        snapshot_and_investigate(Broken(), tmp_path / "bundles", tmp_path / "reports")
    assert not list((tmp_path / "bundles").glob("inc_*"))
    assert not list((tmp_path / "reports").glob("*.json"))
