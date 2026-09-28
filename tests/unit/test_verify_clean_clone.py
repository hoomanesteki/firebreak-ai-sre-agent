"""Tests for the pre-push check that runs CI's steps in a fresh clone.

**Why this is tested rather than excluded from coverage.** `pyproject.toml` measures `scripts/`
with the reasoning that leaving the gatekeepers unmeasured is how an untested check quietly
starts passing everything. This script is a gatekeeper, and it had two defects on its first real
run: it leaked `VIRTUAL_ENV` into the clone so uv used the parent's environment, and it fetched
the submodule from GitHub, which took minutes and then died with a reset connection.

**What is not tested here.** Nothing runs a full clone plus install plus suite: that is four
minutes and is what the script itself is for. What is tested is the parts where being wrong is
silent, which is the step list agreeing with CI, the environment being stripped, and a failure
being reported as a failure.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "verify_clean_clone.py"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("verify_clean_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load()


class TestTheStepsMatchCi:
    """The whole value of this script is that it runs what CI runs. A step CI has and this
    does not is a step that can only fail after a push."""

    def test_every_step_has_a_name_and_a_command(self) -> None:
        assert checker.STEPS
        for name, command in checker.STEPS:
            assert name and isinstance(command, tuple)
            assert command[0] == "uv", f"{name} does not run through uv"

    def test_the_type_check_covers_all_three_platforms(self) -> None:
        """mypy narrows `sys.platform` to the one it is checking for, so a single run leaves
        half of every platform guard unchecked. A darwin-only local run once passed while CI's
        Linux run failed, which is the reason CI does three."""
        platforms = {
            command[command.index("--platform") + 1]
            for _, command in checker.STEPS
            if "--platform" in command
        }
        assert platforms == {"darwin", "linux", "win32"}

    def test_the_type_check_covers_the_same_paths_as_ci(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        in_ci = set(re.findall(r"mypy --platform \w+ ([\w /]+)", workflow))
        assert in_ci, "the workflow no longer runs mypy in a recognisable form"
        here = {
            " ".join(command[command.index("--platform") + 2 :])
            for _, command in checker.STEPS
            if "--platform" in command
        }
        assert here == {path.strip() for path in in_ci}, (
            f"this script checks {here} and CI checks {in_ci}"
        )

    def test_it_runs_the_offline_demo_last(self) -> None:
        """CLAUDE.md requires the offline demo green from Phase 11, and it is the slowest
        meaningful step, so a fast failure should come first."""
        assert checker.STEPS[-1][0] == "Offline demo"

    def test_it_checks_the_published_numbers(self) -> None:
        """The site stats check is the one that only fails in a fresh clone, because the
        machine-dependent part of that file is what caused two red builds."""
        names = [name for name, _ in checker.STEPS]
        assert "Site stats" in names


class TestLocalStateIsStrippedFromTheClone:
    """The first run's defect. uv warned that `VIRTUAL_ENV` did not match the clone's
    environment and carried on using the parent's, so the clone installed nothing and the
    check measured the wrong tree."""

    def test_the_variables_that_would_point_at_this_checkout_are_named(self) -> None:
        assert "VIRTUAL_ENV" in checker.LOCAL_ONLY_VARIABLES
        assert "UV_PROJECT_ENVIRONMENT" in checker.LOCAL_ONLY_VARIABLES

    def test_pythonpath_is_stripped_before_being_set_to_the_clone(self) -> None:
        """It is both stripped and re-set: inheriting the parent's would import the parent's
        `src`, which is the subtlest version of this whole failure."""
        assert "PYTHONPATH" in checker.LOCAL_ONLY_VARIABLES
        body = SCRIPT.read_text(encoding="utf-8")
        assert 'environment["PYTHONPATH"] = str(clone / "src")' in body

    def test_firebreak_and_llm_variables_are_stripped(self) -> None:
        """A configured model endpoint in the environment would make the clone exercise a
        different path from the one CI exercises."""
        body = SCRIPT.read_text(encoding="utf-8")
        assert 'startswith(("FIREBREAK_", "LLM_"))' in body


class TestItRefusesToMeasureTheWrongThing:
    def test_a_dirty_tree_is_refused_without_the_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A clone contains committed state only, so a pass over a dirty tree says nothing
        about what would be pushed."""
        monkeypatch.setattr(checker, "working_tree_is_clean", lambda: False)
        monkeypatch.setattr(sys, "argv", ["verify_clean_clone.py"])
        assert checker.main() == 2

    def test_the_flag_names_what_it_gives_up(self) -> None:
        body = SCRIPT.read_text(encoding="utf-8")
        assert "--allow-dirty" in body
        assert "committed state only" in body

    def test_a_clean_tree_is_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        def fake_run(*args: Any, **kwargs: Any) -> Any:
            return subprocess.CompletedProcess(args=(), returncode=0, stdout="", stderr="")

        monkeypatch.setattr(checker.subprocess, "run", fake_run)
        assert checker.working_tree_is_clean() is True

    def test_a_dirty_tree_is_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        def fake_run(*args: Any, **kwargs: Any) -> Any:
            return subprocess.CompletedProcess(
                args=(), returncode=0, stdout=" M scripts/thing.py\n", stderr=""
            )

        monkeypatch.setattr(checker.subprocess, "run", fake_run)
        assert checker.working_tree_is_clean() is False


class TestTheSubmoduleComesFromThisCheckout:
    """Fetching it from GitHub took minutes and then failed with a reset connection. A check
    meant to run before every push cannot depend on a large fetch."""

    def test_it_is_cloned_from_the_local_path(self) -> None:
        body = SCRIPT.read_text(encoding="utf-8")
        assert "which needs no network" in body
        # The command is built as a tuple, so the assertion is on the tuple's shape rather
        # than on a string that never appears in the source.
        assert '"git", "clone"' in body
        assert "str(source), str(target)" in body

    def test_the_recorded_pin_is_checked_out_explicitly(self) -> None:
        """A local checkout could be sitting on another commit, and a check whose job is
        noticing drift must not inherit it."""
        body = SCRIPT.read_text(encoding="utf-8")
        assert 'f"HEAD:{SUBMODULE}"' in body

    def test_an_absent_submodule_is_reported_rather_than_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)
        reason = checker.copy_submodule(tmp_path / "clone")
        assert reason is not None
        assert "not checked out" in reason

    def test_skipping_it_is_recorded_as_a_gap(self) -> None:
        """Fourteen tests read the pinned demo's files deliberately, so without the submodule
        they fail rather than skip. A check that is always red is a check nobody runs, which is
        why it is on by default and why turning it off says so."""
        body = SCRIPT.read_text(encoding="utf-8")
        assert "--no-submodules" in body
        assert "NOT_COVERED_EXTRA" in body


class TestItSaysWhatItDoesNotCover:
    def test_neo4j_and_the_audit_are_named(self) -> None:
        """A pass that silently covers less than CI is worse than no check, because the reader
        concludes the same thing from both."""
        covered = " ".join(checker.NOT_COVERED)
        assert "Neo4j" in covered
        assert "network" in covered

    def test_progress_is_flushed(self) -> None:
        """It takes minutes and Python buffers stdout when redirected, so without a flush a
        run into a log file shows nothing until the end and looks hung."""
        body = SCRIPT.read_text(encoding="utf-8")
        assert "flush=True" in body
