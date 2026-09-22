"""Tests for firebreak.lab.scenario."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from firebreak.lab.scenario import (
    MAX_LOAD_USERS,
    MIN_LOAD_USERS,
    Distractor,
    DistractorKind,
    Fault,
    FaultClass,
    FaultKind,
    Load,
    ScenarioError,
    ScenarioSpec,
    Split,
    Timing,
    check_against_inventory,
    load_library,
    load_spec,
)


def _fault(**overrides):
    fields = {"kind": FaultKind.FLAG, "flag": "cartFailure", "variant": "10%"}
    fields.update(overrides)
    return Fault(**fields)


def _spec_kwargs(**overrides):
    fields = {
        "id": "cart-latency-fault",
        "family": "cart-latency",
        "fault": _fault(),
        "target_service": "cart",
        "fault_class": FaultClass.LATENCY,
        "load": Load(users=10),
        "split": Split.TRAIN,
    }
    fields.update(overrides)
    return fields


def _spec(spec_id, **overrides):
    return ScenarioSpec(**_spec_kwargs(id=spec_id, **overrides))


# --- Fault -------------------------------------------------------------


def test_fault_flag_kind_without_flag_raises():
    with pytest.raises(ValidationError, match="a flag fault needs both flag and variant"):
        Fault(kind=FaultKind.FLAG, variant="10%")


def test_fault_flag_kind_without_variant_raises():
    with pytest.raises(ValidationError, match="a flag fault needs both flag and variant"):
        Fault(kind=FaultKind.FLAG, flag="cartFailure")


def test_fault_none_kind_with_flag_set_raises():
    with pytest.raises(ValidationError, match="must not name a flag"):
        Fault(kind=FaultKind.NONE, flag="cartFailure", variant="10%")


def test_fault_flag_kind_with_flag_and_variant_constructs():
    fault = Fault(kind=FaultKind.FLAG, flag="cartFailure", variant="10%")

    assert fault.flag == "cartFailure"
    assert fault.variant == "10%"


# --- Load ----------------------------------------------------------------


def test_load_rejects_users_below_minimum():
    with pytest.raises(ValidationError, match="users"):
        Load(users=MIN_LOAD_USERS - 1)


def test_load_rejects_users_above_maximum():
    with pytest.raises(ValidationError, match="users"):
        Load(users=MAX_LOAD_USERS + 1)


def test_load_rejects_non_positive_spawn_rate():
    with pytest.raises(ValidationError, match="spawn_rate"):
        Load(users=10, spawn_rate=0)


# --- Timing --------------------------------------------------------------


def test_timing_total_seconds_sums_the_three_stages():
    timing = Timing(warmup_seconds=100, fault_seconds=200, cooldown_seconds=50)

    assert timing.total_seconds == 350


def test_timing_rejects_warmup_below_sixty_seconds():
    with pytest.raises(ValidationError, match="warmup_seconds"):
        Timing(warmup_seconds=59)


# --- Distractor ------------------------------------------------------------


def test_distractor_harmless_flag_without_flag_raises():
    with pytest.raises(
        ValidationError, match="a harmless_flag distractor needs both flag and variant"
    ):
        Distractor(
            kind=DistractorKind.HARMLESS_FLAG, service="cart", offset_seconds=-30, variant="on"
        )


def test_distractor_harmless_flag_without_variant_raises():
    with pytest.raises(
        ValidationError, match="a harmless_flag distractor needs both flag and variant"
    ):
        Distractor(
            kind=DistractorKind.HARMLESS_FLAG, service="cart", offset_seconds=-30, flag="adFailure"
        )


def test_distractor_deploy_event_without_flag_or_variant_constructs():
    distractor = Distractor(
        kind=DistractorKind.DEPLOY_EVENT, service="checkout", offset_seconds=-60
    )

    assert distractor.flag is None
    assert distractor.variant is None


# --- ScenarioSpec ground truth consistency --------------------------------


def test_scenario_spec_no_fault_class_with_target_service_raises():
    no_fault = _fault(kind=FaultKind.NONE, flag=None, variant=None)
    with pytest.raises(ValidationError, match="must not name a target service"):
        ScenarioSpec(**_spec_kwargs(fault_class=FaultClass.NONE, fault=no_fault))


def test_scenario_spec_no_fault_class_with_flag_fault_raises():
    with pytest.raises(ValidationError, match="must not inject a flag fault"):
        ScenarioSpec(**_spec_kwargs(fault_class=FaultClass.NONE, target_service=None))


def test_scenario_spec_non_none_fault_class_without_target_service_raises():
    with pytest.raises(ValidationError, match="needs a target service"):
        ScenarioSpec(**_spec_kwargs(target_service=None))


def test_scenario_spec_valid_no_fault_spec_constructs():
    spec = ScenarioSpec(
        **_spec_kwargs(
            fault_class=FaultClass.NONE,
            target_service=None,
            fault=_fault(kind=FaultKind.NONE, flag=None, variant=None),
        )
    )

    assert spec.fault_class is FaultClass.NONE
    assert spec.target_service is None
    assert spec.fault.kind is FaultKind.NONE


# --- id / family patterns --------------------------------------------------


@pytest.mark.parametrize("bad_id", ["Cart-Fault", "cart_fault", "-cart-fault", "cart-fault-"])
def test_scenario_spec_id_rejects_invalid_characters(bad_id):
    with pytest.raises(ValidationError, match="pattern"):
        ScenarioSpec(**_spec_kwargs(id=bad_id))


@pytest.mark.parametrize(
    "bad_family", ["Cart-Family", "cart_family", "-cart-family", "cart-family-"]
)
def test_scenario_spec_family_rejects_invalid_characters(bad_family):
    with pytest.raises(ValidationError, match="pattern"):
        ScenarioSpec(**_spec_kwargs(family=bad_family))


# --- second_faults -----------------------------------------------------


def test_scenario_spec_second_faults_returns_only_harmless_flag_distractors():
    distractors = (
        Distractor(kind=DistractorKind.DEPLOY_EVENT, service="checkout", offset_seconds=-60),
        Distractor(
            kind=DistractorKind.HARMLESS_FLAG,
            service="cart",
            offset_seconds=-30,
            flag="adFailure",
            variant="on",
        ),
    )
    spec = ScenarioSpec(**_spec_kwargs(distractors=distractors))

    assert spec.second_faults == (distractors[1],)


# --- load_spec -----------------------------------------------------------


def _minimal_spec_yaml(spec_id: str) -> str:
    return (
        f"id: {spec_id}\n"
        "family: cart-family\n"
        "target_service: cart\n"
        "fault_class: latency\n"
        "split: train\n"
        "fault:\n"
        "  kind: flag\n"
        "  flag: cartFailure\n"
        '  variant: "10%"\n'
        "load:\n"
        "  users: 10\n"
    )


def test_load_spec_rejects_invalid_yaml(tmp_path: Path):
    path = tmp_path / "broken.yaml"
    path.write_text("key: [1, 2\n", encoding="utf-8")

    with pytest.raises(ScenarioError, match="not valid YAML"):
        load_spec(path)


def test_load_spec_rejects_non_mapping_document(tmp_path: Path):
    path = tmp_path / "list-doc.yaml"
    path.write_text("- one\n- two\n", encoding="utf-8")

    with pytest.raises(ScenarioError, match="does not contain a mapping"):
        load_spec(path)


def test_load_spec_rejects_missing_file(tmp_path: Path):
    with pytest.raises(ScenarioError, match="cannot read"):
        load_spec(tmp_path / "absent.yaml")


def test_load_spec_rejects_id_that_does_not_match_filename(tmp_path: Path):
    path = tmp_path / "other-name.yaml"
    path.write_text(_minimal_spec_yaml("cart-fault"), encoding="utf-8")

    with pytest.raises(ScenarioError, match="does not match its filename"):
        load_spec(path)


def test_load_spec_rejects_a_mapping_that_is_not_a_valid_scenario(tmp_path: Path):
    path = tmp_path / "no-fault-with-target.yaml"
    path.write_text(
        "id: no-fault-with-target\n"
        "family: cart-family\n"
        "target_service: cart\n"
        "fault_class: none\n"
        "split: train\n"
        "fault:\n"
        "  kind: none\n"
        "load:\n"
        "  users: 10\n",
        encoding="utf-8",
    )

    with pytest.raises(ScenarioError, match="is not a valid scenario"):
        load_spec(path)


def test_load_spec_round_trips_every_field(tmp_path: Path):
    path = tmp_path / "cart-checkout-latency.yaml"
    path.write_text(
        "id: cart-checkout-latency\n"
        "family: latency-family\n"
        "target_service: cart\n"
        "fault_class: latency\n"
        "split: train\n"
        "expected_alert: CartLatencyHigh\n"
        "notes: a test scenario\n"
        "fault:\n"
        "  kind: flag\n"
        "  flag: cartFailure\n"
        '  variant: "10%"\n'
        "load:\n"
        "  users: 25\n"
        "  spawn_rate: 3.5\n"
        "timing:\n"
        "  warmup_seconds: 120\n"
        "  fault_seconds: 600\n"
        "  cooldown_seconds: 90\n"
        "distractors:\n"
        "  - kind: deploy_event\n"
        "    service: checkout\n"
        "    offset_seconds: -60\n"
        "  - kind: harmless_flag\n"
        "    service: cart\n"
        "    offset_seconds: -30\n"
        "    flag: adFailure\n"
        '    variant: "on"\n',
        encoding="utf-8",
    )

    spec = load_spec(path)

    assert spec.id == "cart-checkout-latency"
    assert spec.family == "latency-family"
    assert spec.target_service == "cart"
    assert spec.fault_class is FaultClass.LATENCY
    assert spec.split is Split.TRAIN
    assert spec.expected_alert == "CartLatencyHigh"
    assert spec.notes == "a test scenario"
    assert spec.fault == Fault(kind=FaultKind.FLAG, flag="cartFailure", variant="10%")
    assert spec.load == Load(users=25, spawn_rate=3.5)
    assert spec.timing == Timing(warmup_seconds=120, fault_seconds=600, cooldown_seconds=90)
    assert spec.distractors == (
        Distractor(kind=DistractorKind.DEPLOY_EVENT, service="checkout", offset_seconds=-60),
        Distractor(
            kind=DistractorKind.HARMLESS_FLAG,
            service="cart",
            offset_seconds=-30,
            flag="adFailure",
            variant="on",
        ),
    )


# --- load_library ----------------------------------------------------------


def test_load_library_rejects_empty_directory(tmp_path: Path):
    with pytest.raises(ScenarioError, match="no scenario specifications found"):
        load_library(tmp_path)


def test_load_library_rejects_directory_that_does_not_exist(tmp_path: Path):
    with pytest.raises(ScenarioError, match="no scenario directory at"):
        load_library(tmp_path / "does-not-exist")


def test_load_library_returns_both_specs_keyed_by_id(tmp_path: Path):
    (tmp_path / "cart-fault.yaml").write_text(_minimal_spec_yaml("cart-fault"), encoding="utf-8")
    (tmp_path / "checkout-fault.yaml").write_text(
        _minimal_spec_yaml("checkout-fault"), encoding="utf-8"
    )

    library = load_library(tmp_path)

    assert set(library) == {"cart-fault", "checkout-fault"}
    assert library["cart-fault"].id == "cart-fault"
    assert library["checkout-fault"].id == "checkout-fault"


# --- check_against_inventory -------------------------------------------


def test_check_against_inventory_reports_unknown_flag():
    library = {
        "s1": _spec("s1", fault=Fault(kind=FaultKind.FLAG, flag="doesNotExist", variant="on"))
    }
    inventory = {"flags": {"cartFailure": {"variants": ["off", "10%"]}}}

    problems = check_against_inventory(library, inventory)

    assert problems == ["s1: flag 'doesNotExist' does not exist in the demo"]


def test_check_against_inventory_reports_unknown_variant_listing_offered_variants():
    library = {
        "s1": _spec("s1", fault=Fault(kind=FaultKind.FLAG, flag="cartFailure", variant="999%"))
    }
    inventory = {"flags": {"cartFailure": {"variants": ["off", "10%", "100%"]}}}

    problems = check_against_inventory(library, inventory)

    assert problems == ["s1: flag 'cartFailure' has no variant '999%'; it offers off, 10%, 100%"]


def test_check_against_inventory_skips_spec_whose_fault_flag_is_none():
    library = {
        "s1": _spec(
            "s1",
            fault=_fault(kind=FaultKind.NONE, flag=None, variant=None),
            target_service=None,
            fault_class=FaultClass.NONE,
        )
    }
    inventory = {"flags": {}}

    assert check_against_inventory(library, inventory) == []


def test_check_against_inventory_also_checks_harmless_flag_distractors():
    library = {
        "s1": _spec(
            "s1",
            distractors=(
                Distractor(
                    kind=DistractorKind.HARMLESS_FLAG,
                    service="cart",
                    offset_seconds=-30,
                    flag="doesNotExist",
                    variant="on",
                ),
            ),
        )
    }
    inventory = {"flags": {"cartFailure": {"variants": ["off", "10%"]}}}

    problems = check_against_inventory(library, inventory)

    assert problems == ["s1: flag 'doesNotExist' does not exist in the demo"]


def test_check_against_inventory_returns_empty_list_for_fully_valid_library():
    library = {"s1": _spec("s1")}
    inventory = {"flags": {"cartFailure": {"variants": ["off", "10%"]}}}

    assert check_against_inventory(library, inventory) == []


def test_check_against_inventory_raises_scenario_error_when_inventory_has_no_flags_key():
    with pytest.raises(ScenarioError, match="flags"):
        check_against_inventory({}, {"notFlags": {}})
