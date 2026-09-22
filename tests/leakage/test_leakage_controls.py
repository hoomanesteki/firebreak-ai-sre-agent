"""Tests for the ground truth leakage checks (SPEC.md Section 8.4).

A leakage check that passes because it looks at nothing is worse than no
check, because it produces confidence. So every rule here is tested against
a deliberate violation as well as a clean tree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import check_leakage
from check_leakage import (
    LeakageConfigError,
    check_bundle_names,
    check_bundles,
    check_flag_names_in_changes,
    check_imports,
    check_path_literals,
    collect_violations,
    imported_modules,
    known_flag_names,
    load_rules,
    matches_prefix,
    module_name_for,
)

RULES = load_rules()


def write_module(root: Path, dotted: str, body: str) -> Path:
    """Create a source file at a dotted module path."""
    path = root.joinpath(*dotted.split(".")).with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def write_bundle(root: Path, bundle_id: str, files: dict[str, str]) -> Path:
    bundle = root / bundle_id
    bundle.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (bundle / name).write_text(text, encoding="utf-8")
    return bundle


# --- configuration -------------------------------------------------------


def test_load_rules_names_the_labels_package_as_ground_truth():
    assert "firebreak_eval_labels" in RULES.ground_truth_modules


def test_load_rules_treats_scenario_specs_as_ground_truth():
    """Specs carry target_service and fault_class, so they are the answer."""
    assert "firebreak.lab.scenario" in RULES.ground_truth_modules


def test_load_rules_forbids_the_agent_packages():
    for package in ("firebreak.agent", "firebreak.tools", "firebreak.graph"):
        assert package in RULES.agent_packages


def test_load_rules_rejects_a_missing_config(tmp_path: Path):
    with pytest.raises(LeakageConfigError, match="missing leakage config"):
        load_rules(tmp_path / "absent.yaml")


def test_load_rules_rejects_a_config_that_is_not_a_mapping(tmp_path: Path):
    path = tmp_path / "leakage.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")

    with pytest.raises(LeakageConfigError, match="must be a mapping"):
        load_rules(path)


def test_load_rules_rejects_a_config_missing_keys(tmp_path: Path):
    path = tmp_path / "leakage.yaml"
    path.write_text("ground_truth_modules: []\n", encoding="utf-8")

    with pytest.raises(LeakageConfigError, match="missing keys"):
        load_rules(path)


# --- import graph --------------------------------------------------------


def test_imported_modules_finds_a_plain_import():
    tree = __import__("ast").parse("import firebreak_eval_labels\n")

    assert "firebreak_eval_labels" in imported_modules(tree)


def test_imported_modules_finds_an_aliased_import():
    """An alias is the obvious way somebody would hide this."""
    tree = __import__("ast").parse("import firebreak_eval_labels as truth\n")

    assert "firebreak_eval_labels" in imported_modules(tree)


def test_imported_modules_finds_an_import_inside_a_function():
    tree = __import__("ast").parse(
        "def helper():\n    from firebreak_eval_labels import read_label\n    return read_label\n"
    )

    found = imported_modules(tree)

    assert "firebreak_eval_labels" in found
    assert "firebreak_eval_labels.read_label" in found


def test_imported_modules_records_the_imported_name_as_well_as_the_module():
    tree = __import__("ast").parse("from firebreak.lab import scenario\n")

    assert "firebreak.lab.scenario" in imported_modules(tree)


def test_imported_modules_ignores_relative_imports():
    tree = __import__("ast").parse("from . import sibling\n")

    assert imported_modules(tree) == set()


def test_matches_prefix_matches_a_package_and_its_children():
    assert matches_prefix("firebreak.agent", ("firebreak.agent",))
    assert matches_prefix("firebreak.agent.graph", ("firebreak.agent",))


def test_matches_prefix_does_not_match_a_similar_name():
    """firebreak.agentic must not be treated as firebreak.agent."""
    assert not matches_prefix("firebreak.agentic", ("firebreak.agent",))


def test_module_name_for_drops_the_package_init():
    assert module_name_for(Path("/src/firebreak/lab/__init__.py"), Path("/src")) == "firebreak.lab"


def test_module_name_for_builds_a_dotted_path():
    assert (
        module_name_for(Path("/src/firebreak/lab/flags.py"), Path("/src")) == "firebreak.lab.flags"
    )


# --- import rule ---------------------------------------------------------


def test_check_imports_blocks_the_agent_importing_labels(tmp_path: Path):
    write_module(tmp_path, "firebreak.agent.reporter", "import firebreak_eval_labels\n")

    violations = check_imports(RULES, tmp_path)

    assert len(violations) == 1
    assert violations[0].rule == "ground-truth-import"
    assert violations[0].blocker is True
    assert "the agent under test" in violations[0].message


def test_check_imports_blocks_a_tool_importing_scenario_specs(tmp_path: Path):
    write_module(
        tmp_path, "firebreak.tools.changes", "from firebreak.lab.scenario import load_spec\n"
    )

    violations = check_imports(RULES, tmp_path)

    assert [v.rule for v in violations] == ["ground-truth-import"]


def test_check_imports_blocks_an_aliased_import_from_the_graph(tmp_path: Path):
    write_module(tmp_path, "firebreak.graph.ranking", "import firebreak_eval_labels as gt\n")

    assert check_imports(RULES, tmp_path)


def test_check_imports_allows_the_eval_package(tmp_path: Path):
    write_module(tmp_path, "firebreak.evals.graders", "import firebreak_eval_labels\n")

    assert check_imports(RULES, tmp_path) == []


def test_check_imports_allows_the_labels_package_to_import_itself(tmp_path: Path):
    write_module(tmp_path, "firebreak_eval_labels", "import firebreak_eval_labels\n")

    assert check_imports(RULES, tmp_path) == []


def test_check_imports_ignores_ordinary_imports(tmp_path: Path):
    write_module(tmp_path, "firebreak.agent.graph", "import json\nfrom pathlib import Path\n")

    assert check_imports(RULES, tmp_path) == []


def test_check_imports_reports_an_unparseable_file_without_blocking(tmp_path: Path):
    write_module(tmp_path, "firebreak.agent.broken", "def (:\n")

    violations = check_imports(RULES, tmp_path)

    assert [v.rule for v in violations] == ["unparseable"]
    assert violations[0].blocker is False


# --- path literals -------------------------------------------------------


def test_check_path_literals_blocks_opening_the_labels_directory(tmp_path: Path):
    """An import rule is useless if a module just opens the path by hand."""
    write_module(tmp_path, "firebreak.tools.sneaky", 'PATH = "labels/scenario/run.json"\n')

    violations = check_path_literals(RULES, tmp_path)

    assert [v.rule for v in violations] == ["ground-truth-path"]


def test_check_path_literals_blocks_naming_the_spec_directory(tmp_path: Path):
    write_module(tmp_path, "firebreak.agent.peek", 'D = "scenarios/specs"\n')

    assert check_path_literals(RULES, tmp_path)


def test_check_path_literals_allows_the_eval_package(tmp_path: Path):
    write_module(tmp_path, "firebreak.evals.runner", 'PATH = "labels/scenario/run.json"\n')

    assert check_path_literals(RULES, tmp_path) == []


def test_check_path_literals_ignores_a_similar_looking_string(tmp_path: Path):
    write_module(tmp_path, "firebreak.agent.notes", 'TEXT = "labelsmith is unrelated"\n')

    assert check_path_literals(RULES, tmp_path) == []


# --- bundle contents -----------------------------------------------------


def test_check_bundles_blocks_a_manifest_carrying_the_answer(tmp_path: Path):
    write_bundle(
        tmp_path,
        "inc_0123456789ab",
        {
            "manifest.json": json.dumps(
                {"bundle_id": "inc_0123456789ab", "target_service": "payment"}
            )
        },
    )

    violations = check_bundles(RULES, tmp_path)

    assert [v.rule for v in violations] == ["ground-truth-in-bundle"]
    assert "target_service" in violations[0].message


def test_check_bundles_blocks_a_canary_reaching_a_bundle(tmp_path: Path):
    write_bundle(
        tmp_path,
        "inc_0123456789ab",
        {
            "manifest.json": json.dumps({"bundle_id": "inc_0123456789ab"}),
            "alert.json": json.dumps({"canary": "fbcanary-0"}),
        },
    )

    violations = check_bundles(RULES, tmp_path)

    assert any("canary" in v.message for v in violations)


def test_check_bundles_passes_a_clean_bundle(tmp_path: Path):
    write_bundle(
        tmp_path,
        "inc_0123456789ab",
        {
            "manifest.json": json.dumps({"bundle_id": "inc_0123456789ab", "run_id": "run1"}),
            "alert.json": json.dumps({"status": "firing"}),
        },
    )

    assert check_bundles(RULES, tmp_path) == []


def test_check_bundles_returns_nothing_when_there_are_no_bundles(tmp_path: Path):
    assert check_bundles(RULES, tmp_path / "absent") == []


# --- the classic accidental leak -----------------------------------------


def test_check_flag_names_in_changes_blocks_a_flag_flip_in_the_change_log(tmp_path: Path):
    """The change log is the classic accidental leak.

    It looks like honest telemetry, and it names the flag that was flipped.
    """
    write_bundle(
        tmp_path,
        "inc_0123456789ab",
        {
            "manifest.json": "{}",
            "changes.json": json.dumps([{"kind": "flag", "detail": "paymentFailure set to 100%"}]),
        },
    )

    violations = check_flag_names_in_changes(tmp_path, {"paymentFailure", "adFailure"})

    assert [v.rule for v in violations] == ["flag-in-change-log"]
    assert "paymentFailure" in violations[0].message


def test_check_flag_names_in_changes_is_case_insensitive(tmp_path: Path):
    write_bundle(
        tmp_path,
        "inc_0123456789ab",
        {"manifest.json": "{}", "changes.json": json.dumps([{"detail": "PAYMENTFAILURE"}])},
    )

    assert check_flag_names_in_changes(tmp_path, {"paymentFailure"})


def test_check_flag_names_in_changes_passes_an_unrelated_deploy(tmp_path: Path):
    write_bundle(
        tmp_path,
        "inc_0123456789ab",
        {
            "manifest.json": "{}",
            "changes.json": json.dumps([{"kind": "deploy", "service": "recommendation"}]),
        },
    )

    assert check_flag_names_in_changes(tmp_path, {"paymentFailure"}) == []


def test_check_flag_names_in_changes_does_nothing_without_an_inventory(tmp_path: Path):
    write_bundle(tmp_path, "inc_0123456789ab", {"manifest.json": "{}", "changes.json": "[]"})

    assert check_flag_names_in_changes(tmp_path, set()) == []


def test_known_flag_names_reads_the_generated_inventory(tmp_path: Path):
    path = tmp_path / "flag_inventory.json"
    path.write_text(
        json.dumps({"flags": {"paymentFailure": {}, "adFailure": {}}}), encoding="utf-8"
    )

    assert known_flag_names(path) == {"paymentFailure", "adFailure"}


def test_known_flag_names_returns_empty_for_a_missing_inventory(tmp_path: Path):
    assert known_flag_names(tmp_path / "absent.json") == set()


def test_known_flag_names_returns_empty_for_invalid_json(tmp_path: Path):
    path = tmp_path / "flag_inventory.json"
    path.write_text("{not json", encoding="utf-8")

    assert known_flag_names(path) == set()


# --- the real repository -------------------------------------------------


def test_this_repository_has_no_route_from_ground_truth_to_the_agent():
    """The check that matters. Everything above proves this one is real."""
    assert collect_violations(RULES, check_leakage.REPO_ROOT) == []


def test_main_returns_zero_on_this_repository(capsys):
    assert check_leakage.main([]) == 0
    assert "no route" in capsys.readouterr().out


def test_main_returns_one_when_a_leak_exists(tmp_path: Path, capsys):
    write_module(tmp_path / "src", "firebreak.agent.leaky", "import firebreak_eval_labels\n")

    assert check_leakage.main(["--root", str(tmp_path)]) == 1
    assert "ground-truth-import" in capsys.readouterr().err


def test_main_returns_two_when_the_config_cannot_be_loaded(monkeypatch, capsys):
    def raise_missing(path=None):
        raise LeakageConfigError("missing leakage config: nowhere.yaml")

    monkeypatch.setattr(check_leakage, "load_rules", raise_missing)

    assert check_leakage.main([]) == 2
    assert "leakage config error" in capsys.readouterr().err


# --- descriptive bundle names --------------------------------------------


def test_check_bundle_names_blocks_a_directory_named_for_its_fault(tmp_path: Path):
    """The leak a field name check cannot see.

    A bundle at bundles/payment-failure-50pct-20u/ states the answer in its
    own path while every forbidden field is absent.
    """
    write_bundle(tmp_path, "payment-failure-50pct-20u", {"manifest.json": "{}"})

    violations = check_bundle_names(RULES, tmp_path)

    assert [v.rule for v in violations] == ["descriptive-bundle-name"]
    assert violations[0].blocker is True
    assert "states the answer in its path" in violations[0].message


def test_check_bundle_names_accepts_an_opaque_id(tmp_path: Path):
    write_bundle(tmp_path, "inc_0123456789ab", {"manifest.json": "{}"})

    assert check_bundle_names(RULES, tmp_path) == []


def test_check_bundle_names_rejects_an_id_of_the_wrong_length(tmp_path: Path):
    write_bundle(tmp_path, "inc_0123", {"manifest.json": "{}"})

    assert check_bundle_names(RULES, tmp_path)


def test_check_bundle_names_returns_nothing_without_bundles(tmp_path: Path):
    assert check_bundle_names(RULES, tmp_path / "absent") == []


def test_a_real_synthetic_bundle_is_stored_under_an_opaque_id(tmp_path: Path):
    """End to end: the builder must not reintroduce a descriptive path."""
    from firebreak.lab.bundle import derive_bundle_id
    from firebreak.lab.scenario import (
        Fault,
        FaultClass,
        FaultKind,
        Load,
        ScenarioSpec,
        Split,
    )
    from firebreak.lab.synthetic import build_synthetic_bundle

    spec = ScenarioSpec(
        id="payment-failure-100pct-20u",
        family="error-injection",
        fault=Fault(kind=FaultKind.FLAG, flag="paymentFailure", variant="100%"),
        target_service="payment",
        fault_class=FaultClass.ERROR_INJECTION,
        load=Load(users=20),
        split=Split.TRAIN,
    )
    bundles = tmp_path / "bundles"
    bundle_id = derive_bundle_id(spec.id, "run1")
    build_synthetic_bundle(bundles / bundle_id, spec, "run1", seed=3)

    assert check_bundle_names(RULES, bundles) == []
    manifest_text = (bundles / bundle_id / "manifest.json").read_text(encoding="utf-8")
    assert spec.id not in manifest_text
    assert "payment" not in manifest_text
