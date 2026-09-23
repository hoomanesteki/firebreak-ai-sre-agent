"""Record one scenario into a bundle plus a label.

The order of operations here is the whole point, because most of the ways a
recording goes wrong are silent:

- The flags are reset and verified **before** anything else, so a recording
  cannot inherit a fault the previous run left on and then be labelled with
  the wrong cause.
- Load is ramped and waited for, because the fault has to land under the
  traffic the scenario says it lands under.
- The fault's onset is the moment flagd actually served it, not the moment
  it was written. Those differ by about a second, and every onset error in
  the eval would otherwise carry that offset.
- The window spans warmup through cooldown, so a bundle contains a baseline,
  the incident, and the recovery. A bundle with no baseline cannot support a
  before and after comparison, which is most of triage.
- The label is written outside the bundle, always.

Time and sleeping are injected so the whole sequence can be tested without a
stack and without waiting twenty minutes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from firebreak.lab.bundle import BundleManifest, ChangeRecord, TimeWindow, derive_bundle_id
from firebreak.lab.bundle_writer import BundleWriter
from firebreak.lab.export import TelemetryExporter
from firebreak.lab.flags import OFF_VARIANT, FlagController
from firebreak.lab.load import LoadController
from firebreak.lab.scenario import DistractorKind, FaultKind, ScenarioSpec
from firebreak_eval_labels import DistractorLabel, IncidentLabel, write_label

RECORDER_VERSION = "1.0.0"

# How long to wait for the expected alert before giving up and recording
# that it never fired. A scenario that does not alert is a real result, not
# an error: SPEC.md Section 6.2 says to record whether and when it fired.
ALERT_WAIT_SECONDS = 180.0
ALERT_POLL_SECONDS = 5.0


class RecordingError(Exception):
    """A recording could not be made, or could not be trusted."""


@dataclass
class RecordingClock:
    """Injected time, so a twenty minute recording tests in milliseconds."""

    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass
class RecordingOutcome:
    """What one recording produced."""

    manifest: BundleManifest
    label: IncidentLabel
    bundle_dir: Path
    label_path: Path
    stages: dict[str, datetime] = field(default_factory=dict)


class AlertWatcher:
    """Tells the recorder whether the expected alert fired, and when.

    A protocol would be overkill for one method, but this is deliberately a
    seam: in Phase 6 the real Firebreak API receives alerts, and until then a
    recording watches the webhook sink.
    """

    def __init__(self, poll: Callable[[], datetime | None]) -> None:
        self._poll = poll

    def wait_for_alert(
        self,
        clock: RecordingClock,
        timeout_seconds: float = ALERT_WAIT_SECONDS,
        poll_seconds: float = ALERT_POLL_SECONDS,
    ) -> datetime | None:
        """Return when the alert fired, or None if it never did."""
        waited = 0.0
        while waited <= timeout_seconds:
            fired_at = self._poll()
            if fired_at is not None:
                return fired_at
            clock.sleep(poll_seconds)
            waited += poll_seconds
        return None


class Recorder:
    """Drives one scenario from a clean stack to a verified bundle."""

    def __init__(
        self,
        flags: FlagController,
        load: LoadController,
        exporter: TelemetryExporter,
        bundles_root: Path,
        labels_root: Path,
        demo_tag: str,
        resting_variants: dict[str, str],
        clock: RecordingClock | None = None,
        alerts: AlertWatcher | None = None,
    ) -> None:
        self._flags = flags
        self._load = load
        self._exporter = exporter
        self._bundles_root = bundles_root
        self._labels_root = labels_root
        self._demo_tag = demo_tag
        self._resting = dict(resting_variants)
        self._clock = clock or RecordingClock()
        self._alerts = alerts

    def record(self, spec: ScenarioSpec, run_id: str) -> RecordingOutcome:
        """Record one scenario. Raises rather than producing a doubtful bundle."""
        stages: dict[str, datetime] = {}

        self._start_clean()
        stages["reset"] = self._clock.now()

        self._load.set_users(spec.load.users, spec.load.spawn_rate, wait=True)
        stages["load_ready"] = self._clock.now()

        window_start = self._clock.now()
        self._clock.sleep(spec.timing.warmup_seconds)
        stages["warmup_done"] = self._clock.now()

        applied = self._apply_distractors_before_onset(spec)
        onset = self._apply_fault(spec)
        stages["fault_onset"] = onset or self._clock.now()

        alert_fired_at = self._watch_for_alert()
        self._clock.sleep(spec.timing.fault_seconds)
        stages["fault_held"] = self._clock.now()

        cleared = self._clear_fault(spec)
        if cleared is not None:
            stages["fault_cleared"] = cleared

        self._clock.sleep(spec.timing.cooldown_seconds)
        window_end = self._clock.now()
        stages["cooldown_done"] = window_end

        manifest = self._export_bundle(
            spec=spec,
            run_id=run_id,
            window=TimeWindow(start=window_start, end=window_end),
            alert_fired_at=alert_fired_at,
        )
        label = self._write_label(
            spec=spec,
            run_id=run_id,
            onset=onset,
            cleared=cleared,
            alert_fired_at=alert_fired_at,
            distractors=applied,
        )
        return RecordingOutcome(
            manifest=manifest,
            label=label,
            bundle_dir=self._bundle_dir(spec.id, run_id),
            label_path=self._labels_root / spec.id / f"{run_id}.json",
            stages=stages,
        )

    def _start_clean(self) -> None:
        """Reset every flag and prove it took.

        reset_to raises if a write did not land, which is the behaviour that
        matters here: a recording that quietly starts with the last run's
        fault still on gets a label naming a service that was fine.
        """
        self._flags.reset_to(self._resting)

    def _apply_distractors_before_onset(self, spec: ScenarioSpec) -> list[DistractorLabel]:
        """Apply the distractors that are meant to land before the fault.

        Only flag distractors are applied here. A deploy_event distractor is
        a change log entry, written into the bundle at export time, because
        the demo has no deploys to make.
        """
        applied: list[DistractorLabel] = []
        for distractor in spec.distractors:
            if distractor.kind is not DistractorKind.HARMLESS_FLAG:
                continue
            if distractor.flag is None or distractor.variant is None:
                continue
            self._flags.apply_fault(distractor.flag, distractor.variant)
            applied.append(
                DistractorLabel(
                    kind=distractor.kind.value,
                    service=distractor.service,
                    applied_at=self._clock.now(),
                    flag=distractor.flag,
                    variant=distractor.variant,
                )
            )
        return applied

    def _apply_fault(self, spec: ScenarioSpec) -> datetime | None:
        """Inject the fault and return the moment flagd served it."""
        if spec.fault.kind is FaultKind.NONE:
            return None
        if spec.fault.flag is None or spec.fault.variant is None:
            raise RecordingError(f"{spec.id} has a flag fault with no flag or variant")
        self._flags.apply_fault(spec.fault.flag, spec.fault.variant)
        return self._clock.now()

    def _clear_fault(self, spec: ScenarioSpec) -> datetime | None:
        """Turn everything off again and record when."""
        if spec.fault.kind is FaultKind.NONE and not spec.second_faults:
            return None
        if spec.fault.flag is not None:
            self._flags.apply_fault(spec.fault.flag, OFF_VARIANT)
        for distractor in spec.second_faults:
            if distractor.flag is not None:
                self._flags.apply_fault(distractor.flag, OFF_VARIANT)
        return self._clock.now()

    def _watch_for_alert(self) -> datetime | None:
        if self._alerts is None:
            return None
        return self._alerts.wait_for_alert(self._clock)

    def _bundle_dir(self, scenario_id: str, run_id: str) -> Path:
        """Bundles are stored under an opaque id, never under their scenario.

        A directory named for the fault states the answer in its own path.
        """
        return self._bundles_root / derive_bundle_id(scenario_id, run_id)

    def _fault_flag_names(self, spec: ScenarioSpec) -> set[str]:
        """Every flag this scenario touched, so the change log can be cleaned."""
        names = {spec.fault.flag} if spec.fault.flag else set()
        names |= {d.flag for d in spec.second_faults if d.flag}
        return {name for name in names if name}

    def _export_bundle(
        self,
        spec: ScenarioSpec,
        run_id: str,
        window: TimeWindow,
        alert_fired_at: datetime | None,
    ) -> BundleManifest:
        bundle_dir = self._bundle_dir(spec.id, run_id)
        bundle_dir.mkdir(parents=True, exist_ok=True)

        writer = BundleWriter(
            bundle_dir=bundle_dir,
            bundle_id=derive_bundle_id(spec.id, run_id),
            run_id=run_id,
            demo_tag=self._demo_tag,
            recorder_version=RECORDER_VERSION,
        )
        writer.write_metrics(self._exporter.export_metrics(window.start, window.end))
        writer.write_traces(self._exporter.export_traces(window.start, window.end))
        writer.write_logs(self._exporter.export_logs(window.start, window.end))
        writer.write_topology(self._exporter.export_topology(window.start, window.end))
        writer.write_alert(self._alert_payload(spec, alert_fired_at))
        writer.write_changes(
            self._change_records(spec, window), fault_flags=self._fault_flag_names(spec)
        )
        return writer.finalise(
            window=window,
            alert_fired=alert_fired_at is not None,
            alert_fired_at=alert_fired_at,
        )

    def _alert_payload(self, spec: ScenarioSpec, fired_at: datetime | None) -> dict[str, Any]:
        """The alert as Firebreak received it, or a record that none came."""
        if fired_at is None:
            return {"status": "none", "alerts": [], "expected": spec.expected_alert}
        return {
            "status": "firing",
            "receiver": "firebreak",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": spec.expected_alert or "unknown"},
                    "startsAt": fired_at.isoformat(),
                }
            ],
        }

    def _change_records(self, spec: ScenarioSpec, window: TimeWindow) -> list[dict[str, Any]]:
        """Deploy style events the agent is allowed to see.

        Fault flag flips are deliberately absent. SPEC.md Section 6.2 says a
        real change log may or may not show a flag flip, and v1 tests the
        harder case. The writer strips them again as a second line of
        defence.

        Built through ChangeRecord so this producer and the synthetic
        builder cannot drift apart. They did once, and the replay backend
        silently returned no changes rather than failing.
        """
        records: list[dict[str, Any]] = []
        for distractor in spec.distractors:
            if distractor.kind is not DistractorKind.DEPLOY_EVENT:
                continue
            at = window.start + timedelta(
                seconds=spec.timing.warmup_seconds + distractor.offset_seconds
            )
            records.append(
                ChangeRecord(
                    at=at,
                    kind="deploy",
                    service=distractor.service,
                    detail=f"routine deployment of {distractor.service}",
                ).as_row()
            )
        return records

    def _write_label(
        self,
        spec: ScenarioSpec,
        run_id: str,
        onset: datetime | None,
        cleared: datetime | None,
        alert_fired_at: datetime | None,
        distractors: list[DistractorLabel],
    ) -> IncidentLabel:
        label = IncidentLabel(
            scenario_id=spec.id,
            run_id=run_id,
            bundle_id=derive_bundle_id(spec.id, run_id),
            split=spec.split.value,
            target_service=spec.target_service,
            fault_class=spec.fault_class.value,
            fault_flag=spec.fault.flag,
            fault_variant=spec.fault.variant,
            fault_onset=onset,
            fault_cleared=cleared,
            distractors=tuple(distractors),
            alert_fired=alert_fired_at is not None,
            alert_fired_at=alert_fired_at,
            notes=spec.notes,
        )
        write_label(self._labels_root, label)
        return label
