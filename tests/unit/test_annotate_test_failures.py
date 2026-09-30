"""Tests for the thing that makes a red CI build legible.

**Why it exists.** Job logs need a token an anonymous caller does not have, so `make ci-status`
can name the failing step and never the failing test. That cost two diagnoses in this project:
both times the red step was `Test`, both times the whole suite was reproduced locally, and both
times this machine did not differ in the way that mattered because the failing tests only run
where a service container exists.

Annotations are readable without a token, so this converts failures into them.

**It must never fail the build itself.** A reporting tool that turns a red build into a
differently-red build for its own reasons removes the one signal somebody was reading.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "annotate_test_failures.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("annotate_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


annotate = _load()


def report(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>", encoding="utf-8")
    return path


PASSING = '<testcase classname="t.a" name="test_ok" file="t/a.py"/>'
FAILING = (
    '<testcase classname="t.b" name="test_bad" file="t/b.py">'
    '<failure message="assert 12 == 11">E assert 12 == 11</failure></testcase>'
)
ERRORING = (
    '<testcase classname="t.c" name="test_broken" file="t/c.py">'
    '<error message="fixture blew up">Traceback...</error></testcase>'
)


class TestItNamesTheFailures:
    def test_a_failure_is_found(self, tmp_path: Path) -> None:
        found = annotate.failures(report(tmp_path, PASSING + FAILING))
        assert [(name) for _, name, _ in found] == ["t.b::test_bad"]

    def test_an_error_counts_as_a_failure(self, tmp_path: Path) -> None:
        """A fixture that blew up is not a pass, and pytest records it separately."""
        found = annotate.failures(report(tmp_path, ERRORING))
        assert found and "test_broken" in found[0][1]

    def test_the_file_is_carried_so_the_annotation_lands_on_a_line(self, tmp_path: Path) -> None:
        file, _, _ = annotate.failures(report(tmp_path, FAILING))[0]
        assert file == "t/b.py"

    def test_the_assertion_message_is_kept(self, tmp_path: Path) -> None:
        """An annotation is read at a glance, so the assertion comes before the traceback."""
        _, _, detail = annotate.failures(report(tmp_path, FAILING))[0]
        assert detail.startswith("assert 12 == 11")

    def test_a_passing_report_yields_nothing(self, tmp_path: Path) -> None:
        assert annotate.failures(report(tmp_path, PASSING)) == []


class TestItNeverFailsTheBuildItself:
    def test_a_missing_report_is_a_warning_not_an_error(self, tmp_path: Path, capsys: Any) -> None:
        """This step runs on failure, and a failure before pytest wrote a report is exactly the
        case where there is nothing to annotate."""
        sys.argv = ["annotate.py", str(tmp_path / "absent.xml")]
        assert annotate.main() == 0
        assert "::warning::" in capsys.readouterr().out

    def test_malformed_xml_is_a_warning(self, tmp_path: Path, capsys: Any) -> None:
        path = tmp_path / "junit.xml"
        path.write_text("<testsuite oops", encoding="utf-8")
        sys.argv = ["annotate.py", str(path)]
        assert annotate.main() == 0
        assert "::warning::" in capsys.readouterr().out

    def test_a_report_with_no_failures_says_the_step_failed_for_another_reason(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """The diagnostic that was actually missing. A red Test step whose report lists no
        failures means the coverage floor, a collection error, or a crash, and knowing which of
        those it is not saves reproducing the entire suite."""
        sys.argv = ["annotate.py", str(report(tmp_path, PASSING))]
        assert annotate.main() == 0
        out = capsys.readouterr().out
        assert "no failures" in out
        assert "coverage floor" in out

    def test_it_returns_zero_even_with_failures_to_report(self, tmp_path: Path) -> None:
        sys.argv = ["annotate.py", str(report(tmp_path, FAILING))]
        assert annotate.main() == 0


class TestTheAnnotationFormatIsValid:
    def test_an_error_annotation_is_emitted(self, tmp_path: Path, capsys: Any) -> None:
        sys.argv = ["annotate.py", str(report(tmp_path, FAILING))]
        annotate.main()
        out = capsys.readouterr().out
        assert "::error file=t/b.py,title=" in out

    def test_newlines_are_escaped(self, tmp_path: Path) -> None:
        """A raw newline ends the annotation early, so the message would be truncated at the
        first line of the traceback."""
        assert annotate.escape("a\nb") == "a%0Ab"

    def test_a_double_colon_is_escaped(self, tmp_path: Path) -> None:
        """`::` inside a message would be read as the start of another command, and a test id
        contains two of them."""
        assert "%3A%3A" in annotate.escape("t.b::test_bad")

    def test_a_percent_is_escaped_first(self) -> None:
        """Escaping percent after the others would double-escape their own escapes."""
        assert annotate.escape("100%") == "100%25"

    def test_the_limit_caps_how_many_are_emitted(self, tmp_path: Path, capsys: Any) -> None:
        """A suite that failed fifty tests has one cause rather than fifty, and GitHub renders a
        few dozen annotations per run."""
        many = "".join(
            f'<testcase classname="t.x" name="test_{i}"><failure message="no">E no</failure>'
            "</testcase>"
            for i in range(30)
        )
        sys.argv = ["annotate.py", str(report(tmp_path, many)), "--limit", "5"]
        annotate.main()
        out = capsys.readouterr().out
        assert out.count("::error ") == 5
        assert "and 25 more" in out
