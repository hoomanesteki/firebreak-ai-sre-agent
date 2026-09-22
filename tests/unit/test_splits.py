"""Tests for firebreak.evals.splits."""

from pathlib import Path

import pytest

from firebreak.evals.splits import (
    SplitError,
    SplitSummary,
    check_splits,
    require_sound_splits,
    select,
    summarise,
    tunable,
)
from firebreak.lab.scenario import (
    Fault,
    FaultClass,
    FaultKind,
    Load,
    ScenarioSpec,
    Split,
    load_library,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_DIR = REPO_ROOT / "scenarios" / "specs"


def _fault(**overrides):
    fields = {"kind": FaultKind.FLAG, "flag": "cartFailure", "variant": "10%"}
    fields.update(overrides)
    return Fault(**fields)


def _spec(spec_id, family, split, **overrides):
    fields = {
        "id": spec_id,
        "family": family,
        "fault": _fault(),
        "target_service": "cart",
        "fault_class": FaultClass.LATENCY,
        "load": Load(users=10),
        "split": split,
    }
    fields.update(overrides)
    return ScenarioSpec(**fields)


def _sound_library():
    return {
        "s1": _spec("s1", "latency", Split.TRAIN),
        "s2": _spec("s2", "latency", Split.VALIDATION),
        "s3": _spec("s3", "latency", Split.TEST_ID),
        "s4": _spec("s4", "resource", Split.TEST_OOD),
    }


# --- summarise -----------------------------------------------------------


def test_summarise_counts_scenarios_by_split():
    library = {
        "s1": _spec("s1", "latency", Split.TRAIN),
        "s2": _spec("s2", "latency", Split.TRAIN),
        "s3": _spec("s3", "latency", Split.VALIDATION),
    }

    summary = summarise(library)

    assert summary.counts == {"train": 2, "validation": 1}


def test_summarise_groups_families_by_split():
    library = {
        "s1": _spec("s1", "latency", Split.TRAIN),
        "s2": _spec("s2", "cpu", Split.TRAIN),
        "s3": _spec("s3", "latency", Split.TEST_OOD),
    }

    summary = summarise(library)

    assert summary.families_by_split == {"test_ood": ("latency",), "train": ("cpu", "latency")}


def test_summarise_ood_families_lists_families_held_out_as_test_ood():
    library = {
        "s1": _spec("s1", "latency", Split.TRAIN),
        "s2": _spec("s2", "resource", Split.TEST_OOD),
        "s3": _spec("s3", "contention", Split.TEST_OOD),
    }

    summary = summarise(library)

    assert summary.ood_families == ("contention", "resource")


def test_summarise_scenarios_tunable_and_held_out_counts():
    summary = summarise(_sound_library())

    assert summary.scenarios == 4
    assert summary.tunable_scenarios == 2
    assert summary.held_out_scenarios == 2


# --- check_splits ----------------------------------------------------------


def test_check_splits_reports_empty_library():
    assert check_splits({}) == ["the library is empty"]


def test_check_splits_reports_ood_family_also_present_in_train():
    library = {
        "s1": _spec("s1", "resource", Split.TRAIN),
        "s2": _spec("s2", "resource", Split.TEST_OOD),
        "s3": _spec("s3", "latency", Split.TRAIN),
        "s4": _spec("s4", "latency", Split.VALIDATION),
        "s5": _spec("s5", "latency", Split.TEST_ID),
    }

    problems = check_splits(library)

    assert "families held out as test_ood also appear in train: resource" in problems


def test_check_splits_reports_ood_family_also_present_in_validation():
    library = {
        "s1": _spec("s1", "resource", Split.VALIDATION),
        "s2": _spec("s2", "resource", Split.TEST_OOD),
        "s3": _spec("s3", "latency", Split.TRAIN),
        "s4": _spec("s4", "latency", Split.VALIDATION),
        "s5": _spec("s5", "latency", Split.TEST_ID),
    }

    problems = check_splits(library)

    assert "families held out as test_ood also appear in validation: resource" in problems


def test_check_splits_reports_test_id_family_appearing_in_no_tunable_split():
    library = {
        "s1": _spec("s1", "latency", Split.TRAIN),
        "s2": _spec("s2", "latency", Split.VALIDATION),
        "s3": _spec("s3", "latency", Split.TEST_ID),
        "s4": _spec("s4", "unseen-family", Split.TEST_ID),
    }

    problems = check_splits(library)

    assert "families in test_id that appear in no tunable split: unseen-family" in problems


@pytest.mark.parametrize("missing_split", [Split.TRAIN, Split.VALIDATION, Split.TEST_ID])
def test_check_splits_reports_missing_required_split(missing_split):
    all_splits = {Split.TRAIN, Split.VALIDATION, Split.TEST_ID}
    library = {
        f"s-{split.value.replace('_', '-')}": _spec(
            f"s-{split.value.replace('_', '-')}", "latency", split
        )
        for split in all_splits
        if split is not missing_split
    }

    problems = check_splits(library)

    assert f"no scenarios assigned to {missing_split.value}" in problems


def test_check_splits_returns_empty_list_for_a_sound_library():
    assert check_splits(_sound_library()) == []


# --- require_sound_splits ---------------------------------------------


def test_require_sound_splits_raises_split_error_for_an_unsound_library():
    with pytest.raises(SplitError, match="the library is empty"):
        require_sound_splits({})


def test_require_sound_splits_returns_summary_for_a_sound_library():
    summary = require_sound_splits(_sound_library())

    assert isinstance(summary, SplitSummary)
    assert summary.scenarios == 4


# --- select / tunable ----------------------------------------------------


def test_select_returns_only_the_requested_split():
    library = _sound_library()

    result = select(library, Split.TRAIN)

    assert set(result) == {"s1"}
    assert result["s1"].split is Split.TRAIN


def test_tunable_returns_train_and_validation_and_excludes_both_test_splits():
    library = _sound_library()

    result = tunable(library)

    assert set(result) == {"s1", "s2"}
    assert {spec.split for spec in result.values()} == {Split.TRAIN, Split.VALIDATION}


# --- the real scenario library ----------------------------------------


def test_require_sound_splits_passes_for_the_real_scenario_library():
    library = load_library(SCENARIOS_DIR)

    summary = require_sound_splits(library)

    assert summary.scenarios == len(library)


def test_real_scenario_library_resource_and_contention_families_are_test_ood_only():
    library = load_library(SCENARIOS_DIR)

    summary = summarise(library)

    assert "resource" in summary.ood_families
    assert "contention" in summary.ood_families

    other_families: set[str] = set()
    for split_name, families in summary.families_by_split.items():
        if split_name != Split.TEST_OOD.value:
            other_families.update(families)
    assert "resource" not in other_families
    assert "contention" not in other_families
