"""Tests for the scenario library generator.

The generator is tested through its output rather than its internals,
because its output is what 36 hours of recording will be driven by. The
invariants that matter are: every flag it names exists at the pinned tag,
the held out families are genuinely held out, and two runs agree.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
import yaml

import generate_scenarios
from firebreak.evals.splits import require_sound_splits
from firebreak.lab.scenario import (
    DistractorKind,
    FaultClass,
    FaultKind,
    Split,
    check_against_inventory,
    load_library,
)
from generate_scenarios import (
    OOD_FAMILIES,
    GeneratorError,
    assign_splits,
    generate_library,
    load_inventory,
    require_variant,
    write_specs,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_PATH = REPO_ROOT / "reports" / "lab" / "flag_inventory.json"

# Flags the library deliberately never injects. The two ai flags need a
# demo profile Firebreak does not run, and emitRawPii is not a fault: it
# puts card numbers into telemetry. See docs/target-system.md.
EXCLUDED_FLAGS = frozenset({"aiSlowResponse", "aiRunawayAgent", "emitRawPii"})

FLAGS = load_inventory(INVENTORY_PATH)


def generated_specs() -> list:
    return generate_library(FLAGS)


def write_to(directory: Path) -> dict:
    """Generate the library into a directory and load it back."""
    write_specs(generated_specs(), directory)
    return load_library(directory)


# --- inventory guards ----------------------------------------------------


def test_load_inventory_returns_the_flags_mapping():
    flags = load_inventory(INVENTORY_PATH)

    assert "paymentFailure" in flags
    assert "100%" in flags["paymentFailure"]["variants"]


def test_load_inventory_raises_when_the_document_has_no_flags(tmp_path: Path):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"demo_tag": "3.1.0"}), encoding="utf-8")

    with pytest.raises(GeneratorError, match="no 'flags' object"):
        load_inventory(path)


def test_require_variant_accepts_a_real_flag_and_variant():
    require_variant(FLAGS, "paymentFailure", "100%")


def test_require_variant_raises_for_a_flag_the_demo_does_not_have():
    """A typo costs an hour of recording before anyone notices."""
    with pytest.raises(GeneratorError, match="notAFlag"):
        require_variant(FLAGS, "notAFlag", "on")


def test_require_variant_raises_for_a_variant_the_flag_does_not_offer():
    with pytest.raises(GeneratorError, match="999%"):
        require_variant(FLAGS, "paymentFailure", "999%")


def test_require_variant_raises_against_a_trimmed_inventory():
    trimmed = {"paymentFailure": {"variants": ["off"]}}

    with pytest.raises(GeneratorError):
        require_variant(trimmed, "paymentFailure", "100%")


# --- the generated library -----------------------------------------------


def test_generate_library_produces_the_expected_size():
    assert len(generated_specs()) == 120


def test_generated_ids_are_unique():
    ids = [spec.id for spec in generated_specs()]

    assert len(ids) == len(set(ids))


def test_every_generated_spec_loads_from_disk(tmp_path: Path):
    library = write_to(tmp_path)

    assert len(library) == 120


def test_every_filename_stem_equals_its_id(tmp_path: Path):
    write_specs(generated_specs(), tmp_path)

    for path in tmp_path.glob("*.yaml"):
        declared = yaml.safe_load(path.read_text(encoding="utf-8"))["id"]
        assert declared == path.stem


def test_generated_library_names_only_flags_the_demo_has(tmp_path: Path):
    library = write_to(tmp_path)
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))

    assert check_against_inventory(library, inventory) == []


def test_generated_library_has_sound_splits(tmp_path: Path):
    library = write_to(tmp_path)

    summary = require_sound_splits(library)

    assert summary.scenarios == 120


# --- held out families ---------------------------------------------------


def test_held_out_families_appear_only_in_the_out_of_distribution_split():
    """One scenario leaking across turns the OOD number into an ID number."""
    for spec in generated_specs():
        if spec.family in OOD_FAMILIES:
            assert spec.split is Split.TEST_OOD, spec.id


def test_the_out_of_distribution_split_holds_only_held_out_families():
    for spec in generated_specs():
        if spec.split is Split.TEST_OOD:
            assert spec.family in OOD_FAMILIES, spec.id


def test_no_held_out_family_appears_in_a_tunable_split():
    tunable = {Split.TRAIN, Split.VALIDATION}
    leaked = {s.family for s in generated_specs() if s.split in tunable} & set(OOD_FAMILIES)

    assert leaked == set()


def test_every_tunable_split_has_scenarios():
    counts = Counter(spec.split for spec in generated_specs())

    for split in (Split.TRAIN, Split.VALIDATION, Split.TEST_ID, Split.TEST_OOD):
        assert counts[split] > 0


# --- determinism ---------------------------------------------------------


def test_generating_twice_produces_identical_files(tmp_path: Path):
    """Reproducible, or the library is a one off nobody can rebuild."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_specs(generated_specs(), first)
    write_specs(generated_specs(), second)

    names = sorted(p.name for p in first.glob("*.yaml"))
    assert names == sorted(p.name for p in second.glob("*.yaml"))
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_split_assignment_is_stable_across_runs():
    first = {spec.id: spec.split for spec in generated_specs()}
    second = {spec.id: spec.split for spec in generated_specs()}

    assert first == second


def test_assign_splits_is_driven_by_sorted_ids_not_input_order():
    specs = generated_specs()
    forwards = {s.id: s.split for s in assign_splits(specs)}
    backwards = {s.id: s.split for s in assign_splits(list(reversed(specs)))}

    assert forwards == backwards


# --- the no fault family -------------------------------------------------


def test_no_fault_scenarios_carry_no_culprit():
    for spec in generated_specs():
        if spec.fault_class is not FaultClass.NONE:
            continue
        assert spec.target_service is None, spec.id
        assert spec.fault.kind is FaultKind.NONE, spec.id


def test_no_fault_scenarios_actually_apply_the_flood_flag():
    """Named only in notes, the recorder would flip nothing.

    The scenario would run at ordinary load, nothing would alert, and the
    family whose entire purpose is an alert with no service at fault would
    measure nothing.
    """
    no_fault = [s for s in generated_specs() if s.fault_class is FaultClass.NONE]

    assert no_fault
    for spec in no_fault:
        flood = [d for d in spec.distractors if d.kind is DistractorKind.HARMLESS_FLAG]
        assert flood, spec.id
        assert flood[0].flag == "loadGeneratorFloodHomepage", spec.id


# --- excluded flags ------------------------------------------------------


def test_excluded_flags_are_never_used_as_a_fault():
    used = {spec.fault.flag for spec in generated_specs() if spec.fault.flag}

    assert used & set(EXCLUDED_FLAGS) == set()


def test_excluded_flags_are_never_used_as_a_second_fault():
    used = {d.flag for spec in generated_specs() for d in spec.second_faults if d.flag}

    assert used & set(EXCLUDED_FLAGS) == set()


def test_every_flag_the_generator_uses_exists_in_the_inventory():
    used = {spec.fault.flag for spec in generated_specs() if spec.fault.flag}
    used |= {d.flag for spec in generated_specs() for d in spec.second_faults if d.flag}

    assert used <= set(FLAGS)


# --- double faults -------------------------------------------------------


def test_double_fault_scenarios_name_two_distinct_services():
    doubles = [s for s in generated_specs() if s.fault_class is FaultClass.MULTIPLE]

    assert doubles
    for spec in doubles:
        second = spec.second_faults
        assert second, spec.id
        assert second[0].service != spec.target_service, spec.id


# --- entry point ---------------------------------------------------------


def test_main_writes_the_library_and_reports_a_summary(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(generate_scenarios, "SPECS_DIR", tmp_path / "specs")
    monkeypatch.setattr(generate_scenarios, "LIBRARY_CARD_PATH", tmp_path / "CARD.md")

    assert generate_scenarios.main() == 0

    output = capsys.readouterr().out
    assert "120 scenario specs" in output
    assert len(list((tmp_path / "specs").glob("*.yaml"))) == 120
    assert (tmp_path / "CARD.md").is_file()
