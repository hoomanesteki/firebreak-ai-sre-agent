"""Freeze live telemetry before investigating, without reading fault configuration."""

from __future__ import annotations

import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from firebreak.agent.graph import InvestigationResult, investigate, stub_handlers
from firebreak.agent.llm import LlmClient, configured_client
from firebreak.lab.bundle import TimeWindow, verify_bundle
from firebreak.lab.bundle_writer import BundleWriteError, BundleWriter
from firebreak.lab.endpoints import DEMO_TAG
from firebreak.lab.export import TelemetryExporter
from firebreak.settings import LlmMode
from firebreak.web.store import write_console_report


def snapshot_and_investigate(
    exporter: TelemetryExporter,
    bundles_dir: Path,
    reports_dir: Path,
    *,
    now: datetime | None = None,
    llm: LlmClient | None = None,
) -> tuple[InvestigationResult, Path]:
    """Capture the last half hour and publish a report with re-runnable evidence.

    No injected fault or labelled onset is available. Triage uses its documented
    unanchored window policy, and a healthy snapshot may correctly abstain.
    """
    end = now or datetime.now(UTC)
    window = TimeWindow(start=end - timedelta(minutes=30), end=end)
    bundle_id = "inc_" + uuid.uuid4().hex[:12]
    bundles_dir.mkdir(parents=True, exist_ok=True)
    destination = bundles_dir / bundle_id
    with tempfile.TemporaryDirectory(prefix=".live-", dir=bundles_dir) as scratch:
        stage = Path(scratch) / bundle_id
        writer = BundleWriter(stage, bundle_id, "live", DEMO_TAG, "live-snapshot-v1")
        metrics = exporter.export_metrics(window.start, window.end)
        if not metrics:
            raise BundleWriteError("live snapshot contains no metrics; check the telemetry stack")
        writer.write_metrics(metrics)
        writer.write_traces(exporter.export_traces(window.start, window.end))
        writer.write_logs(exporter.export_logs(window.start, window.end))
        writer.write_topology(exporter.export_topology(window.start, window.end))
        writer.write_alert({"alerts": []})
        writer.write_changes([], fault_flags=set())
        writer.finalise(
            window,
            alert_fired=False,
            notes="Captured from live telemetry; no alert anchor or change feed supplied.",
        )
        verify_bundle(stage)
        stage.rename(destination)
    client = llm or configured_client(stub_handlers())
    if client.mode in {LlmMode.STUB, LlmMode.REPLAY}:
        # A live snapshot must not masquerade as model analysis through a synthetic answer.
        client = LlmClient(mode=LlmMode.STUB)
    result = investigate(destination, llm=client, verify=True)
    return result, write_console_report(result, directory=reports_dir)
