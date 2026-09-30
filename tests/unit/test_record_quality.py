"""Tests for the script that turns a test run into a publishable number.

**Why it is worth testing.** A test count and a coverage percentage are the first things a
reader checks when deciding whether a project is carefully built, so they are the numbers most
tempting to type by hand. This script exists so they are generated instead, which makes it one
more gatekeeper, and `pyproject.toml` measures `scripts/` on the argument that an unmeasured
gatekeeper quietly starts passing everything.

**The failure that matters is a plausible wrong number.** A summary that silently reported zero
tests, or read `failures` as `passed`, would put a confident figure on a page with nothing
behind it. A crash is fine; a wrong number is not, so every reader here refuses rather than
defaults.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "record_quality.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("record_quality_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


quality = _load()


def junit(tmp_path: Path, **attributes: object) -> Path:
    values = {"tests": 100, "failures": 0, "errors": 0, "skipped": 4}
    values.update(attributes)
    rendered = " ".join(f'{key}="{value}"' for key, value in values.items())
    path = tmp_path / "junit.xml"
    xml = f"<testsuites><testsuite {rendered}></testsuite></testsuites>"
    path.write_text(xml, encoding="utf-8")
    return path


def coverage(tmp_path: Path, percent: object = 85.42) -> Path:
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps({"totals": {"percent_covered": percent}}), encoding="utf-8")
    return path


class TestItReadsTheCounts:
    def test_passed_excludes_failures_and_skips(self, tmp_path: Path) -> None:
        """The arithmetic worth pinning. Reporting `tests` as `passed` would overstate on every
        run with a skip, and this suite has twenty-four of them."""
        counts = quality.read_junit(junit(tmp_path, tests=100, failures=3, errors=2, skipped=4))
        assert counts == {"total": 100, "passed": 91, "failed": 5, "skipped": 4}

    def test_errors_count_as_failures(self, tmp_path: Path) -> None:
        """A collection error is not a pass. pytest reports it separately from a failure, and a
        summary that counted only `failures` would call a broken import a success."""
        counts = quality.read_junit(junit(tmp_path, tests=10, failures=0, errors=1, skipped=0))
        assert counts["failed"] == 1
        assert counts["passed"] == 9

    def test_a_nested_testsuite_is_found(self, tmp_path: Path) -> None:
        """pytest wraps `testsuite` in `testsuites`. Both shapes exist in the wild."""
        path = tmp_path / "junit.xml"
        xml = '<testsuite tests="7" failures="0" errors="0" skipped="1"/>'
        path.write_text(xml, encoding="utf-8")
        assert quality.read_junit(path)["total"] == 7

    def test_zero_tests_is_refused(self, tmp_path: Path) -> None:
        """The silent-wrong-number case. An empty run is a broken invocation, and reporting
        "0 passed" as a fact would be worse than failing."""
        with pytest.raises(quality.QualityError, match="no tests"):
            quality.read_junit(junit(tmp_path, tests=0))

    def test_a_missing_report_names_what_to_run(self, tmp_path: Path) -> None:
        with pytest.raises(quality.QualityError, match="junitxml"):
            quality.read_junit(tmp_path / "absent.xml")

    def test_malformed_xml_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "junit.xml"
        path.write_text("<testsuite oops", encoding="utf-8")
        with pytest.raises(quality.QualityError, match="not valid XML"):
            quality.read_junit(path)

    def test_a_report_with_no_testsuite_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "junit.xml"
        path.write_text("<other/>", encoding="utf-8")
        with pytest.raises(quality.QualityError, match="no testsuite"):
            quality.read_junit(path)


class TestItReadsTheCoverage:
    def test_the_percentage_is_rounded_to_two_places(self, tmp_path: Path) -> None:
        """Two places, matching the exact floor in `[tool.coverage.report]`. Rounding further
        would let a page claim 85% for a run that measured 84.6 and failed."""
        assert quality.read_coverage(coverage(tmp_path, 85.4163)) == 85.42

    def test_a_missing_report_names_what_to_run(self, tmp_path: Path) -> None:
        with pytest.raises(quality.QualityError, match="cov-report=json"):
            quality.read_coverage(tmp_path / "absent.json")

    def test_a_report_without_totals_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "coverage.json"
        path.write_text(json.dumps({"files": {}}), encoding="utf-8")
        with pytest.raises(quality.QualityError, match="percent_covered"):
            quality.read_coverage(path)

    def test_a_non_numeric_percentage_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(quality.QualityError, match="percent_covered"):
            quality.read_coverage(coverage(tmp_path, "high"))


class TestTheSummaryIsSelfDescribing:
    def test_it_records_the_commit_it_measured(self, tmp_path: Path) -> None:
        """Without it a stale figure is invisible. With it, a stale figure is a stale commit."""
        summary = quality.build(junit(tmp_path), coverage(tmp_path))
        assert summary["commit"]
        assert summary["generated_at"]

    def test_it_says_a_passing_test_is_not_a_result(self, tmp_path: Path) -> None:
        """The distinction this whole project turns on. Coverage says the code does what a test
        says it should; it says nothing about whether Firebreak finds root causes, which is
        unmeasured."""
        note = quality.build(junit(tmp_path), coverage(tmp_path))["note"]
        assert "says nothing about whether" in note

    def test_main_writes_the_file(self, tmp_path: Path, capsys: Any) -> None:
        output = tmp_path / "quality.json"
        code = quality.main.__wrapped__ if hasattr(quality.main, "__wrapped__") else quality.main
        sys.argv = [
            "record_quality.py",
            "--junit",
            str(junit(tmp_path)),
            "--coverage",
            str(coverage(tmp_path)),
            "--output",
            str(output),
        ]
        assert code() == 0
        written = json.loads(output.read_text(encoding="utf-8"))
        assert written["tests"]["passed"] == 96
        assert written["coverage_percent"] == 85.42
        assert "passed" in capsys.readouterr().out

    def test_main_fails_rather_than_writing_a_guess(self, tmp_path: Path) -> None:
        output = tmp_path / "quality.json"
        sys.argv = [
            "record_quality.py",
            "--junit",
            str(tmp_path / "absent.xml"),
            "--coverage",
            str(coverage(tmp_path)),
            "--output",
            str(output),
        ]
        assert quality.main() == 1
        assert not output.exists()


class TestTheCommittedSummaryIsCurrent:
    def test_it_exists_and_reports_a_plausible_suite(self) -> None:
        """Written by `make test`, so it cannot describe a different tree than the tested one.

        Loose bounds, and the bounds are the interesting part. Two assertions were tried here
        and both were circular:

        `coverage >= 85` duplicated pytest's own floor, and the summary is only written by a run
        that cleared it, so the test could never go green on the run that would produce a passing
        figure.

        `failed == 0` was worse: this test's own failure put `failed: 1` into the file, which made
        it fail again. A test cannot assert that the run containing it had no failures.

        What is left is non-circular: the file exists, its counts add up, it records the commit it
        measured, and its coverage is a percentage. Whether the run passed is pytest's verdict,
        not a property to re-derive from an artefact of that same run.
        """
        path = REPO_ROOT / "reports" / "quality.json"
        if not path.is_file():
            pytest.skip("reports/quality.json not written yet; run make test")
        summary = json.loads(path.read_text(encoding="utf-8"))
        counts = summary["tests"]
        assert counts["passed"] > 1000
        assert counts["passed"] + counts["skipped"] + counts["failed"] == counts["total"]
        assert 0.0 < summary["coverage_percent"] <= 100.0
        assert summary["commit"]
