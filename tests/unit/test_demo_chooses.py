"""Tests for `make demo`, which is a Phase 12 acceptance criterion by name.

SPEC.md Section 17 asks that a fresh clone on another machine runs `make demo`. It could not:
the target went straight at the live stack, so on a clone with no Docker it failed with a
Compose error about a missing submodule. A first-time reader learned nothing about Firebreak
from that, which makes it the worst possible place for the project to fail.

**The property worth guarding is that it says which demo it ran.** The offline path replays a
deterministic stub and names 8 of 8 faults; the same triage scores 1 of 7 on real recordings. A
command that quietly ran the lesser path would leave a reader believing they had seen something
they had not.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "demo.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("demo_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


demo = _load()


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Capture what would have been run instead of running it."""
    calls: list[tuple[str, ...]] = []

    def fake_run(command: tuple[str, ...]) -> int:
        calls.append(command)
        return 0

    monkeypatch.setattr(demo, "run", fake_run)
    return calls


class TestItChoosesTheRightDemo:
    def test_with_no_live_stack_it_replays_offline(
        self, ran: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The case that matters: a fresh clone with no Docker."""
        monkeypatch.setattr(demo, "stack_is_up", lambda: False)
        monkeypatch.setattr(demo, "has_credentials", lambda: False)
        assert demo.main() == 0
        assert ran and ran[0][-1].endswith("demo_offline.py")

    def test_with_a_live_stack_it_investigates_live(
        self, ran: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(demo, "stack_is_up", lambda: True)
        monkeypatch.setattr(demo, "has_credentials", lambda: True)
        assert demo.main() == 0
        assert ran and "firebreak.cli.app" in ran[0]
        assert ran[0][-1] == "investigate-live"

    def test_it_says_which_one_it_chose(
        self, ran: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        """The offline path replays a deterministic stub and names 8 of 8 faults, where the
        same triage scores 1 of 7 on real recordings. A reader who does not know which path
        ran takes away the wrong number."""
        monkeypatch.setattr(demo, "stack_is_up", lambda: False)
        monkeypatch.setattr(demo, "has_credentials", lambda: False)
        demo.main()
        out = capsys.readouterr().out
        assert "No live stack, so running the offline showcase instead" in out
        assert "no model, no network and no Docker" in out

    def test_it_says_what_would_change_the_choice(
        self, ran: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        monkeypatch.setattr(demo, "stack_is_up", lambda: False)
        monkeypatch.setattr(demo, "has_credentials", lambda: False)
        demo.main()
        assert "make live" in capsys.readouterr().out

    def test_a_live_stack_without_credentials_says_the_floor_will_answer(
        self, ran: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        """Otherwise a reader sees triage's answer and believes a model produced it."""
        monkeypatch.setattr(demo, "stack_is_up", lambda: True)
        monkeypatch.setattr(demo, "has_credentials", lambda: False)
        demo.main()
        out = capsys.readouterr().out
        assert "deterministic floor" in out
        assert "no AI analysis" in out

    def test_it_reports_both_conditions_before_choosing(
        self, ran: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        monkeypatch.setattr(demo, "stack_is_up", lambda: False)
        monkeypatch.setattr(demo, "has_credentials", lambda: False)
        demo.main()
        out = capsys.readouterr().out
        assert "live stack answering" in out
        assert "model credentials configured" in out

    def test_with_neither_a_stack_nor_cassettes_it_fails_with_the_command_to_run(
        self,
        ran: list[tuple[str, ...]],
        monkeypatch: pytest.MonkeyPatch,
        capsys: Any,
        tmp_path: Path,
    ) -> None:
        """Rather than replaying nothing and reporting success."""
        monkeypatch.setattr(demo, "stack_is_up", lambda: False)
        monkeypatch.setattr(demo, "has_credentials", lambda: False)
        monkeypatch.setattr(demo, "CASSETTE_MANIFEST", tmp_path / "absent.json")
        assert demo.main() == 1
        assert "make cassettes" in capsys.readouterr().err
        assert not ran


class TestItDoesNotStartAnything:
    def test_the_only_docker_command_is_a_read(self) -> None:
        """Bringing up a six gigabyte Compose project is not something a command called demo
        should do to somebody who typed it to see what this is.

        Asserted on the commands rather than on the file text, because the first version of
        this test matched the word Compose in a docstring explaining why none is run.
        """
        import ast

        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        docker_commands = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Tuple):
                continue
            values = [
                element.value
                for element in node.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
            if values and values[0] == "docker":
                docker_commands.append(values)
        assert docker_commands == [["docker", "info"]], docker_commands

    def test_it_offers_the_live_path_by_name(self) -> None:
        assert "make live" in SCRIPT.read_text(encoding="utf-8")

    def test_it_asks_the_application_rather_than_counting_containers(self) -> None:
        """A Compose project can be up with the frontend still starting, and an investigation
        against a half-started stack looks like an investigation of a healthy system."""
        body = SCRIPT.read_text(encoding="utf-8")
        assert "urlopen" in body
        assert "localhost:8080" in body

    def test_an_unreachable_stack_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The check runs on every invocation, including on machines with no Docker, so it has
        to answer rather than raise."""
        monkeypatch.setattr(demo, "docker_is_running", lambda: True)
        assert demo.stack_is_up() in (True, False)

    def test_no_docker_means_no_live_stack(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(demo, "docker_is_running", lambda: False)
        assert demo.stack_is_up() is False


class TestTheMakefileAndTheSpecAgree:
    def test_the_makefile_runs_this_script(self) -> None:
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        assert "scripts/demo.py" in makefile

    def test_demo_is_still_a_documented_target(self) -> None:
        """SPEC.md names `make demo` as a Phase 12 acceptance criterion, and the README and
        the site quote it."""
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        assert "\ndemo:" in makefile
        assert "## Run the best demo" in makefile
