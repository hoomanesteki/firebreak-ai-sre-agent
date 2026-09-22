"""Tests for firebreak_eval_labels."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from firebreak.lab.bundle import BundleManifest
from firebreak_eval_labels import (
    CANARY_PREFIX,
    DistractorLabel,
    IncidentLabel,
    LabelError,
    all_canaries,
    find_canaries,
    label_path,
    load_all,
    new_canary,
    read_label,
    write_label,
)

CANARY_PATTERN = re.compile(rf"^{CANARY_PREFIX}-[0-9a-f]{{32}}$")


def _label_kwargs(**overrides):
    fields = {
        "scenario_id": "cart-latency",
        "run_id": "20260101T000000Z",
        "bundle_id": "inc_0123456789ab",
        "split": "train",
        "target_service": "cart",
        "fault_class": "latency",
    }
    fields.update(overrides)
    return fields


def _full_label() -> IncidentLabel:
    return IncidentLabel(
        scenario_id="cart-latency",
        run_id="20260101T000000Z",
        bundle_id="inc_0123456789ab",
        split="train",
        target_service="cart",
        fault_class="latency",
        fault_flag="cartFailure",
        fault_variant="10%",
        fault_onset=datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC),
        fault_cleared=datetime(2026, 1, 1, 0, 15, 0, tzinfo=UTC),
        distractors=(
            DistractorLabel(
                kind="deploy_event",
                service="checkout",
                applied_at=datetime(2026, 1, 1, 0, 4, 0, tzinfo=UTC),
            ),
        ),
        alert_fired=True,
        alert_fired_at=datetime(2026, 1, 1, 0, 6, 0, tzinfo=UTC),
        notes="a test label",
    )


# --- new_canary ------------------------------------------------------------


def test_new_canary_matches_the_documented_pattern():
    canary = new_canary()

    assert CANARY_PATTERN.match(canary)


def test_new_canary_returns_a_different_value_on_each_call():
    assert new_canary() != new_canary()


# --- IncidentLabel -----------------------------------------------------


def test_incident_label_constructs_with_a_generated_canary():
    label = IncidentLabel(**_label_kwargs())

    assert CANARY_PATTERN.match(label.canary)


def test_incident_label_rejects_canary_that_does_not_match_pattern():
    with pytest.raises(ValidationError, match="pattern"):
        IncidentLabel(**_label_kwargs(canary="not-a-canary"))


def test_incident_label_is_no_fault_true_when_target_service_is_none():
    label = IncidentLabel(**_label_kwargs(target_service=None, fault_class="none"))

    assert label.is_no_fault is True


def test_incident_label_is_no_fault_false_when_target_service_is_set():
    label = IncidentLabel(**_label_kwargs(target_service="cart"))

    assert label.is_no_fault is False


# --- write_label / read_label ---------------------------------------------


def test_write_label_creates_parent_directories(tmp_path: Path):
    labels_root = tmp_path / "labels"
    label = _full_label()

    path = write_label(labels_root, label)

    assert path == label_path(labels_root, label.scenario_id, label.run_id)
    assert path.is_file()


def test_read_label_round_trips_every_field(tmp_path: Path):
    labels_root = tmp_path / "labels"
    label = _full_label()
    write_label(labels_root, label)

    loaded = read_label(labels_root, label.scenario_id, label.run_id)

    assert loaded == label


def test_read_label_raises_label_error_for_missing_file(tmp_path: Path):
    with pytest.raises(LabelError, match="no label at"):
        read_label(tmp_path, "cart-latency", "20260101T000000Z")


def test_read_label_raises_label_error_for_invalid_json(tmp_path: Path):
    path = label_path(tmp_path, "cart-latency", "20260101T000000Z")
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(LabelError, match="not valid JSON"):
        read_label(tmp_path, "cart-latency", "20260101T000000Z")


def test_read_label_raises_label_error_for_json_that_is_not_a_valid_label(tmp_path: Path):
    path = label_path(tmp_path, "cart-latency", "20260101T000000Z")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"scenario_id": "cart-latency"}), encoding="utf-8")

    with pytest.raises(LabelError, match="not a valid label"):
        read_label(tmp_path, "cart-latency", "20260101T000000Z")


# --- load_all --------------------------------------------------------------


def test_load_all_returns_empty_dict_for_root_that_does_not_exist(tmp_path: Path):
    assert load_all(tmp_path / "does-not-exist") == {}


def test_load_all_returns_every_label_keyed_by_scenario_and_run(tmp_path: Path):
    labels_root = tmp_path / "labels"
    first = IncidentLabel(**_label_kwargs(scenario_id="zeta-scenario", run_id="run-1"))
    second = IncidentLabel(**_label_kwargs(scenario_id="alpha-scenario", run_id="run-2"))
    write_label(labels_root, first)
    write_label(labels_root, second)

    labels = load_all(labels_root)

    assert labels == {
        ("zeta-scenario", "run-1"): first,
        ("alpha-scenario", "run-2"): second,
    }


def test_load_all_iterates_in_sorted_scenario_order(tmp_path: Path):
    labels_root = tmp_path / "labels"
    write_label(
        labels_root, IncidentLabel(**_label_kwargs(scenario_id="zeta-scenario", run_id="run-1"))
    )
    write_label(
        labels_root, IncidentLabel(**_label_kwargs(scenario_id="alpha-scenario", run_id="run-2"))
    )

    labels = load_all(labels_root)

    assert list(labels.keys()) == [("alpha-scenario", "run-2"), ("zeta-scenario", "run-1")]


# --- all_canaries ------------------------------------------------------


def test_all_canaries_collects_one_canary_per_label(tmp_path: Path):
    labels_root = tmp_path / "labels"
    first = IncidentLabel(**_label_kwargs(scenario_id="cart-latency", run_id="run-1"))
    second = IncidentLabel(**_label_kwargs(scenario_id="checkout-latency", run_id="run-2"))
    write_label(labels_root, first)
    write_label(labels_root, second)

    canaries = all_canaries(labels_root)

    assert canaries == {first.canary, second.canary}


# --- find_canaries -----------------------------------------------------


def test_find_canaries_finds_a_canary_embedded_in_a_larger_blob():
    canary = new_canary()
    text = f"some prompt text ... {canary} ... more text"

    assert find_canaries(text, {canary}) == {canary}


def test_find_canaries_returns_empty_set_when_none_are_present():
    assert find_canaries("nothing to see here", {new_canary()}) == set()


def test_find_canaries_finds_several_when_several_are_present():
    first, second, third = new_canary(), new_canary(), new_canary()
    text = f"{first} appears alongside {second} but not the third canary"

    assert find_canaries(text, {first, second, third}) == {first, second}


# --- structural leakage guard ----------------------------------------------


def test_incident_label_fields_do_not_leak_ground_truth_into_bundle_manifest():
    """The structural reason a manifest cannot carry the answer.

    The overlap is asserted exactly rather than loosely, so widening it
    fails here. Note scenario_id is deliberately absent from the manifest:
    for a library like this one, the scenario name describes the fault.
    """
    label_fields = set(IncidentLabel.model_fields)
    manifest_fields = set(BundleManifest.model_fields)

    assert label_fields & manifest_fields == {
        "bundle_id",
        "run_id",
        "alert_fired",
        "alert_fired_at",
        "notes",
    }
    for answer in ("target_service", "fault_class", "fault_flag", "fault_variant", "canary"):
        assert answer in label_fields
        assert answer not in manifest_fields
    assert "scenario_id" not in manifest_fields
