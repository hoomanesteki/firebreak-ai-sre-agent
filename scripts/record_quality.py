"""Summarise the test run into `reports/quality.json`.

**Why this exists rather than typing the numbers.** SPEC.md Section 3 rule 2: every metric on
the README or the website comes from a file under `reports/` produced by a script. A test count
and a coverage percentage are metrics, and a reader assessing whether this project is carefully
built will look at them first, so they are exactly the numbers most tempting to type by hand and
leave to rot.

**Why a small summary rather than the raw reports.** A JUnit XML for two thousand tests is a
megabyte of one line per test, and a coverage JSON carries every file. Neither belongs in a
committed artefact. This writes the four numbers a page would show, plus the commit they were
measured at, so a stale figure is visible as a stale commit rather than invisible.

Run by `make test`, which is where the numbers come from, so they cannot be current for a
different tree than the one that was tested.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from firebreak.evals.report import git_commit  # noqa: E402

OUTPUT = REPO_ROOT / "reports" / "quality.json"


class QualityError(Exception):
    """A reason the summary cannot be written. Fatal, because a missing number here becomes a
    missing number on a page, and the page would not know why."""


def read_junit(path: Path) -> dict[str, int]:
    """Test counts from a JUnit XML report.

    Reads the `testsuite` element's own attributes rather than counting children, because
    pytest already aggregates them and counting cases would double anything reported twice.
    """
    if not path.is_file():
        raise QualityError(f"{path} is missing; run the tests with --junitxml")
    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError as error:
        raise QualityError(f"{path} is not valid XML: {error}") from error
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        raise QualityError(f"{path} has no testsuite element")

    def count(name: str) -> int:
        raw = suite.get(name)
        return int(raw) if raw and raw.isdigit() else 0

    total = count("tests")
    if not total:
        raise QualityError(f"{path} reports no tests, which cannot be right")
    failures = count("failures") + count("errors")
    skipped = count("skipped")
    return {
        "total": total,
        "passed": total - failures - skipped,
        "failed": failures,
        "skipped": skipped,
    }


def read_coverage(path: Path) -> float:
    """The covered percentage from a coverage JSON report."""
    if not path.is_file():
        raise QualityError(f"{path} is missing; run the tests with --cov-report=json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    percent = (payload.get("totals") or {}).get("percent_covered")
    if not isinstance(percent, int | float):
        raise QualityError(f"{path} has no totals.percent_covered")
    return round(float(percent), 2)


def build(junit: Path, coverage: Path) -> dict[str, Any]:
    counts = read_junit(junit)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "commit": git_commit(),
        "tests": counts,
        "coverage_percent": read_coverage(coverage),
        "note": (
            "Facts about this repository rather than results about the system. A passing test "
            "says the code does what a test says it should; it says nothing about whether "
            "Firebreak identifies root causes, which is unmeasured and reported as such."
        ),
    }


def write_if_changed(path: Path, payload: dict[str, Any], provenance: tuple[str, ...]) -> bool:
    """Write only when something other than provenance changed.

    Returns True if the file was written.

    **Why this exists.** `commit` and `generated_at` change on every run by design, so writing
    unconditionally left these files modified after every `make verify`. That is the defect this
    project already fixed once, for a replay's wall clock: a check that dirties the tree over a
    field nobody can compare teaches people to ignore `git status`, and the next real change hides
    in the noise.

    Provenance is still honest. When a number moves, the file is rewritten and the new commit and
    timestamp go with it. When nothing moved, the old ones stay, and they correctly describe the
    run that last produced these numbers rather than the most recent run that recomputed them.
    """
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict):
            before = {k: v for k, v in existing.items() if k not in provenance}
            after = {k: v for k, v in payload.items() if k not in provenance}
            if before == after:
                return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    arguments = parser.parse_args()
    try:
        summary = build(arguments.junit, arguments.coverage)
    except QualityError as error:
        print(f"quality: {error}", file=sys.stderr)
        return 1
    wrote = write_if_changed(arguments.output, summary, ("generated_at", "commit"))
    counts = summary["tests"]
    if not wrote:
        print("quality: unchanged, so the existing summary and its provenance stand")
    print(
        f"quality: {counts['passed']} passed, {counts['skipped']} skipped, "
        f"{summary['coverage_percent']}% covered"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
