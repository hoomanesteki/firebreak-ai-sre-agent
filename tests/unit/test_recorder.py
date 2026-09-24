"""Tests for firebreak.lab.recorder."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from firebreak.lab.bundle import verify_bundle
from firebreak.lab.flags import OFF_VARIANT
from firebreak.lab.recorder import (
    AlertWatcher,
    Recorder,
    RecordingClock,
    RecordingError,
)
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
from firebreak.signals import MetricName


class Events:
    """Records every fault, load and sleep call, in the order it happened."""

    def __init__(self) -> None:
        self.log: list[tuple] = []


class FakeFlags:
    """A FlagController stand-in that records every reset and apply call."""

    def __init__(self, events: Events) -> None:
        self._events = events

    def reset_to(self, defaults):
        self._events.log.append(("reset", dict(defaults)))
        return []

    def apply_fault(self, flag, variant):
        self._events.log.append(("apply_fault", flag, variant))
        return None


class FakeLoad:
    """A LoadController stand-in that records every ramp request."""

    def __init__(self, events: Events) -> None:
        self._events = events

    def set_users(self, users, spawn_rate, wait=False):
        self._events.log.append(("set_users", users, spawn_rate, wait))
        return None


class FakeClock:
    """Advances a counter on sleep and returns increasing datetimes from now."""

    def __init__(self, events: Events, start: datetime) -> None:
        self._events = events
        self._current = start

    def sleep(self, seconds: float) -> None:
        self._events.log.append(("sleep", seconds))
        self._current += timedelta(seconds=seconds)

    def now(self) -> datetime:
        value = self._current
        self._current += timedelta(microseconds=1)
        return value


class StubExporter:
    """A TelemetryExporter stand-in returning fixed, schema-valid rows."""

    def export_metrics(self, start, end):
        return [
            {
                "timestamp": start,
                "metric_name": MetricName.SPAN_CALLS_TOTAL,
                "service_name": "cart",
                "labels_json": "{}",
                "value": 1.0,
            }
        ]

    def export_traces(self, start, end):
        return [
            {
                "trace_id": "trace-1",
                "span_id": "span-1",
                "parent_span_id": None,
                "service_name": "cart",
                "span_name": "GET /cart",
                "span_kind": "SERVER",
                "start_time": start,
                "duration_ms": 12.5,
                "status_code": "OK",
                "attributes_json": "{}",
            }
        ]

    def export_logs(self, start, end):
        return [
            {
                "timestamp": start,
                "service_name": "cart",
                "severity": "INFO",
                "body": "hello",
                "trace_id": None,
                "attributes_json": "{}",
            }
        ]

    def export_topology(self, start, end):
        return {"services": ["cart"], "edges": []}


DEFAULT_RESTING = {"cartFailure": "off", "adFailure": "off"}


def _build_recorder(
    tmp_path: Path,
    events: Events | None = None,
    *,
    alerts: AlertWatcher | None = None,
    resting: dict[str, str] | None = None,
) -> tuple[Recorder, Events]:
    events = events if events is not None else Events()
    flags = FakeFlags(events)
    load = FakeLoad(events)
    exporter = StubExporter()
    clock_source = FakeClock(events, datetime(2026, 1, 1, tzinfo=UTC))
    clock = RecordingClock(sleep=clock_source.sleep, now=clock_source.now)
    recorder = Recorder(
        flags=flags,
        load=load,
        exporter=exporter,
        bundles_root=tmp_path / "bundles",
        labels_root=tmp_path / "labels",
        demo_tag="abc123",
        resting_variants=resting if resting is not None else DEFAULT_RESTING,
        clock=clock,
        alerts=alerts,
    )
    return recorder, events


def _fault(**overrides):
    fields = {"kind": FaultKind.FLAG, "flag": "cartFailure", "variant": "10%"}
    fields.update(overrides)
    return Fault(**fields)


def _spec(**overrides):
    fields = {
        "id": "cart-latency-fault",
        "family": "cart-latency",
        "fault": _fault(),
        "target_service": "cart",
        "fault_class": FaultClass.LATENCY,
        "load": Load(users=10, spawn_rate=5.0),
        "timing": Timing(warmup_seconds=120, fault_seconds=240, cooldown_seconds=60),
        "split": Split.TRAIN,
    }
    fields.update(overrides)
    return ScenarioSpec(**fields)


# --- ordering: reset, load, fault around sleeps ---------------------------


def test_recorder_record_resets_flags_to_defaults_before_any_other_action(tmp_path: Path):
    recorder, events = _build_recorder(tmp_path)
    spec = _spec()

    recorder.record(spec, run_id="run-1")

    assert events.log[0] == ("reset", DEFAULT_RESTING)
    kinds = [entry[0] for entry in events.log]
    assert kinds.index("reset") < kinds.index("set_users")
    assert kinds.index("reset") < kinds.index("apply_fault")


def test_recorder_record_ramps_load_with_wait_true_and_spec_users_and_spawn_rate(tmp_path: Path):
    recorder, events = _build_recorder(tmp_path)
    spec = _spec(load=Load(users=42, spawn_rate=7.5))

    recorder.record(spec, run_id="run-1")

    assert ("set_users", 42, 7.5, True) in events.log


def test_recorder_record_applies_fault_after_warmup_sleep_and_clears_before_cooldown_sleep(
    tmp_path: Path,
):
    recorder, events = _build_recorder(tmp_path)
    spec = _spec()

    recorder.record(spec, run_id="run-1")

    sleep_indices = [i for i, entry in enumerate(events.log) if entry[0] == "sleep"]
    apply_indices = [i for i, entry in enumerate(events.log) if entry[0] == "apply_fault"]
    warmup_sleep_index, fault_sleep_index, cooldown_sleep_index = sleep_indices
    onset_index, clear_index = apply_indices

    assert warmup_sleep_index < onset_index < fault_sleep_index
    assert fault_sleep_index < clear_index < cooldown_sleep_index


def test_recorder_record_sleep_durations_equal_warmup_fault_and_cooldown_in_order(tmp_path: Path):
    recorder, events = _build_recorder(tmp_path)
    spec = _spec(timing=Timing(warmup_seconds=90, fault_seconds=150, cooldown_seconds=30))

    recorder.record(spec, run_id="run-1")

    sleeps = [entry[1] for entry in events.log if entry[0] == "sleep"]
    assert sleeps == [90, 150, 30]


# --- window ----------------------------------------------------------------


def test_recorder_record_window_spans_from_before_fault_onset_to_after_fault_cleared(
    tmp_path: Path,
):
    recorder, _ = _build_recorder(tmp_path)
    spec = _spec()

    outcome = recorder.record(spec, run_id="run-1")

    assert outcome.manifest.window.start < outcome.label.fault_onset
    assert outcome.manifest.window.end > outcome.label.fault_cleared


# --- label location and content -------------------------------------------


def test_recorder_record_writes_label_outside_the_bundle_directory(tmp_path: Path):
    recorder, _ = _build_recorder(tmp_path)
    spec = _spec()

    outcome = recorder.record(spec, run_id="run-1")

    assert outcome.label_path.is_relative_to(tmp_path / "labels")
    assert not outcome.label_path.is_relative_to(outcome.bundle_dir)
    assert not (outcome.bundle_dir / "label.json").exists()
    assert outcome.label_path.is_file()


def test_recorder_record_label_matches_spec_ground_truth_for_a_flag_fault(tmp_path: Path):
    recorder, _ = _build_recorder(tmp_path)
    spec = _spec()

    outcome = recorder.record(spec, run_id="run-1")

    assert outcome.label.target_service == spec.target_service
    assert outcome.label.fault_class == spec.fault_class.value
    assert outcome.label.fault_flag == spec.fault.flag
    assert outcome.label.fault_variant == spec.fault.variant
    assert outcome.label.split == spec.split.value
    assert outcome.label.fault_onset is not None


def test_recorder_record_no_fault_spec_skips_fault_application_and_label_is_no_fault(
    tmp_path: Path,
):
    recorder, events = _build_recorder(tmp_path)
    spec = _spec(
        fault_class=FaultClass.NONE,
        target_service=None,
        fault=_fault(kind=FaultKind.NONE, flag=None, variant=None),
    )

    outcome = recorder.record(spec, run_id="run-1")

    assert [entry for entry in events.log if entry[0] == "apply_fault"] == []
    assert outcome.label.fault_onset is None
    assert outcome.label.fault_cleared is None
    assert outcome.label.is_no_fault is True


# --- distractors -------------------------------------------------------


def test_recorder_record_harmless_flag_distractor_is_applied_recorded_and_cleared(
    tmp_path: Path,
):
    recorder, events = _build_recorder(tmp_path)
    spec = _spec(
        distractors=(
            Distractor(
                kind=DistractorKind.HARMLESS_FLAG,
                service="ad",
                offset_seconds=-30,
                flag="adFailure",
                variant="on",
            ),
        )
    )

    outcome = recorder.record(spec, run_id="run-1")

    applies = [entry for entry in events.log if entry[0] == "apply_fault"]
    assert ("apply_fault", "adFailure", "on") in applies
    assert ("apply_fault", "adFailure", OFF_VARIANT) in applies
    on_index = applies.index(("apply_fault", "adFailure", "on"))
    off_index = applies.index(("apply_fault", "adFailure", OFF_VARIANT))
    assert on_index < off_index

    assert len(outcome.label.distractors) == 1
    distractor_label = outcome.label.distractors[0]
    assert distractor_label.kind == "harmless_flag"
    assert distractor_label.service == "ad"
    assert distractor_label.flag == "adFailure"
    assert distractor_label.variant == "on"


def test_recorder_record_deploy_event_distractor_appears_in_changes_without_fault_flag_name(
    tmp_path: Path,
):
    recorder, _ = _build_recorder(tmp_path)
    spec = _spec(
        distractors=(
            Distractor(kind=DistractorKind.DEPLOY_EVENT, service="checkout", offset_seconds=-60),
        )
    )

    outcome = recorder.record(spec, run_id="run-1")

    changes = json.loads((outcome.bundle_dir / "changes.json").read_text())

    assert len(changes) == 1
    assert changes[0]["kind"] == "deploy"
    assert changes[0]["service"] == "checkout"
    assert "cartfailure" not in json.dumps(changes).lower()


# --- the flag set a change log is cleaned against ------------------------
#
# Lives on the specification, not on the recorder. Two producers computed it
# separately and one forgot the distractor flags, which let the flood flag
# survive into a no fault bundle's change log and hand the agent the answer
# in the one family whose answer is that nothing broke.


def test_fault_flag_names_includes_second_fault_flags():
    spec = _spec(
        distractors=(
            Distractor(
                kind=DistractorKind.HARMLESS_FLAG,
                service="ad",
                offset_seconds=-30,
                flag="adFailure",
                variant="on",
            ),
        )
    )

    assert spec.fault_flag_names == {"cartFailure", "adFailure"}


def test_fault_flag_names_is_just_the_primary_flag_without_second_faults():
    assert _spec().fault_flag_names == {"cartFailure"}


def test_fault_flag_names_includes_a_distractor_flag_when_there_is_no_primary_fault():
    """The no fault case: the only flag flipped is the distractor."""
    spec = _spec(
        fault_class=FaultClass.NONE,
        target_service=None,
        fault=Fault(kind=FaultKind.NONE),
        distractors=(
            Distractor(
                kind=DistractorKind.HARMLESS_FLAG,
                service="frontend",
                offset_seconds=0,
                flag="loadGeneratorFloodHomepage",
                variant="on",
            ),
        ),
    )

    assert spec.fault_flag_names == {"loadGeneratorFloodHomepage"}


# --- alert payload -------------------------------------------------------


def test_recorder_record_alert_payload_is_status_none_with_expected_alert_when_never_fires(
    tmp_path: Path,
):
    recorder, _ = _build_recorder(tmp_path, alerts=AlertWatcher(poll=lambda: None))
    spec = _spec(expected_alert="CartLatencyHigh")

    outcome = recorder.record(spec, run_id="run-1")

    payload = json.loads((outcome.bundle_dir / "alert.json").read_text())

    assert payload == {"status": "none", "alerts": [], "expected": "CartLatencyHigh"}
    assert outcome.label.alert_fired is False


def test_recorder_record_alert_payload_is_firing_with_starts_at_when_alert_fires(tmp_path: Path):
    fired_at = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
    recorder, _ = _build_recorder(tmp_path, alerts=AlertWatcher(poll=lambda: fired_at))
    spec = _spec(expected_alert="CartLatencyHigh")

    outcome = recorder.record(spec, run_id="run-1")

    payload = json.loads((outcome.bundle_dir / "alert.json").read_text())

    assert payload["status"] == "firing"
    assert payload["alerts"][0]["labels"]["alertname"] == "CartLatencyHigh"
    assert payload["alerts"][0]["startsAt"] == fired_at.isoformat()
    assert outcome.label.alert_fired is True


# --- AlertWatcher.wait_for_alert -------------------------------------------


def test_alert_watcher_wait_for_alert_returns_datetime_and_does_not_sleep_on_first_success():
    fired_at = datetime(2026, 1, 1, tzinfo=UTC)
    sleeps: list[float] = []
    clock = RecordingClock(sleep=sleeps.append)
    watcher = AlertWatcher(poll=lambda: fired_at)

    result = watcher.wait_for_alert(clock)

    assert result == fired_at
    assert sleeps == []


def test_alert_watcher_wait_for_alert_returns_none_after_timeout_when_poll_always_none():
    sleeps: list[float] = []
    clock = RecordingClock(sleep=sleeps.append)
    watcher = AlertWatcher(poll=lambda: None)

    result = watcher.wait_for_alert(clock, timeout_seconds=1.0, poll_seconds=1.0)

    assert result is None
    assert sleeps == [1.0, 1.0]


# --- RecordingError ------------------------------------------------------


def test_recorder_record_raises_recording_error_when_flag_fault_has_no_flag_or_variant(
    tmp_path: Path,
):
    recorder, _ = _build_recorder(tmp_path)
    bad_fault = Fault.model_construct(kind=FaultKind.FLAG, flag=None, variant=None)
    # ScenarioSpec(**fields) revalidates a nested Fault instance, so the
    # invalid fault has to be smuggled in through model_construct too.
    spec = ScenarioSpec.model_construct(
        id="cart-latency-fault",
        family="cart-latency",
        fault=bad_fault,
        target_service="cart",
        fault_class=FaultClass.LATENCY,
        load=Load(users=10, spawn_rate=5.0),
        timing=Timing(warmup_seconds=120, fault_seconds=240, cooldown_seconds=60),
        distractors=(),
        expected_alert=None,
        split=Split.TRAIN,
        notes=None,
    )

    with pytest.raises(RecordingError, match="flag fault with no flag or variant"):
        recorder.record(spec, run_id="run-1")


# --- bundle verification ---------------------------------------------------


def test_recorder_record_produces_a_bundle_that_passes_verify_bundle(tmp_path: Path):
    recorder, _ = _build_recorder(tmp_path)
    spec = _spec()

    outcome = recorder.record(spec, run_id="run-1")

    assert verify_bundle(outcome.bundle_dir) == outcome.manifest
