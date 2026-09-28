"""Which eval report the Console shows when a split has several.

**The defect.** The Console picked the report with the newest modification time. Git sets
mtime when it writes a file, so a clone, a checkout, or a branch switch restamps every report
in the tree, usually with one identical timestamp. The ordering then reflects what the working
tree last did rather than when any report was produced.

**Why it matters more here than it sounds.** `reports/eval/b0/validation/` holds a fixture run
from 2026-09-25 reporting 1.000 root-cause accuracy alongside the recorded run reporting
0.143. Under mtime ordering, switching branches can promote the fixture run to B0's validation
result, and the Evaluation page would then show this system at perfect accuracy on recorded
incidents. That is the single most misleading number this project could display, and it would
appear without anything changing in the repository.

The fix is the report's own `generated_at`, which is the only one of the three candidate keys
that means what the ordering needs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from firebreak.web import app as web_app


def write_report(
    directory: Path, name: str, generated: str, accuracy: float, mtime: float | None = None
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    payload: dict[str, Any] = {
        "generated_at": generated,
        "configuration": "b0",
        "split": "validation",
        "data_source": "recorded bundles",
        "overall": {"graders": {"root_cause": {"accuracy": accuracy}}},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def reports_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(web_app, "REPORTS_DIR", tmp_path)
    return tmp_path / "eval" / "b0" / "validation"


class TestTheNewestReportWins:
    def test_the_report_with_the_later_generated_at_is_chosen(self, reports_root: Path) -> None:
        write_report(reports_root, "2026-09-25_aaa.json", "2026-09-25T02:27:22+00:00", 1.0)
        write_report(reports_root, "2026-09-26_bbb.json", "2026-09-26T23:31:48+00:00", 0.143)
        chosen = web_app.latest_eval_reports()["b0/validation"]
        assert chosen["overall"]["graders"]["root_cause"]["accuracy"] == 0.143

    def test_modification_time_does_not_decide(self, reports_root: Path) -> None:
        """The defect, reproduced. The older report is given the newer mtime, which is
        exactly what a git checkout does, and it must still lose."""
        write_report(
            reports_root, "2026-09-25_aaa.json", "2026-09-25T02:27:22+00:00", 1.0, mtime=2_000_000
        )
        write_report(
            reports_root, "2026-09-26_bbb.json", "2026-09-26T23:31:48+00:00", 0.143, mtime=1_000_000
        )
        chosen = web_app.latest_eval_reports()["b0/validation"]
        assert chosen["overall"]["graders"]["root_cause"]["accuracy"] == 0.143, (
            "the fixture run with 1.000 accuracy was promoted by its modification time"
        )

    def test_identical_mtimes_do_not_make_the_choice_arbitrary(self, reports_root: Path) -> None:
        """A checkout gives every file the same mtime, which leaves the old code choosing
        by whatever order the filesystem listed them in."""
        stamp = 1_500_000.0
        write_report(
            reports_root, "2026-09-25_aaa.json", "2026-09-25T02:27:22+00:00", 1.0, mtime=stamp
        )
        write_report(
            reports_root, "2026-09-26_bbb.json", "2026-09-26T23:31:48+00:00", 0.143, mtime=stamp
        )
        chosen = web_app.latest_eval_reports()["b0/validation"]
        assert chosen["overall"]["graders"]["root_cause"]["accuracy"] == 0.143

    def test_the_filename_date_does_not_decide_either(self, reports_root: Path) -> None:
        """A filename can disagree with the content it names. The content wins, because the
        content is what gets published."""
        write_report(reports_root, "2026-12-31_aaa.json", "2026-09-25T02:27:22+00:00", 1.0)
        write_report(reports_root, "2026-01-01_bbb.json", "2026-09-26T23:31:48+00:00", 0.143)
        chosen = web_app.latest_eval_reports()["b0/validation"]
        assert chosen["overall"]["graders"]["root_cause"]["accuracy"] == 0.143


class TestItStaysDeterministic:
    def test_two_reports_generated_at_the_same_instant_break_the_tie_by_name(
        self, reports_root: Path
    ) -> None:
        """Deterministic rather than correct, since nothing can tell these apart. The point
        is that the Evaluation page shows the same number on every reader's machine."""
        same = "2026-09-26T23:31:48+00:00"
        write_report(reports_root, "2026-09-26_aaa.json", same, 0.1)
        write_report(reports_root, "2026-09-26_zzz.json", same, 0.9)
        first = web_app.latest_eval_reports()["b0/validation"]
        second = web_app.latest_eval_reports()["b0/validation"]
        assert first == second
        assert first["overall"]["graders"]["root_cause"]["accuracy"] == 0.9

    def test_a_report_without_a_timestamp_loses_to_one_with_it(self, reports_root: Path) -> None:
        """Rather than raising. A report predating the convention is older than the
        convention, and refusing to render anything would take the page down over it."""
        path = reports_root / "2026-09-30_old.json"
        reports_root.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"overall": {"graders": {"root_cause": {"accuracy": 1.0}}}}),
            encoding="utf-8",
        )
        write_report(reports_root, "2026-09-26_bbb.json", "2026-09-26T23:31:48+00:00", 0.143)
        chosen = web_app.latest_eval_reports()["b0/validation"]
        assert chosen["overall"]["graders"]["root_cause"]["accuracy"] == 0.143

    def test_a_split_with_only_an_undated_report_still_renders(self, reports_root: Path) -> None:
        reports_root.mkdir(parents=True, exist_ok=True)
        (reports_root / "old.json").write_text(json.dumps({"overall": {}}), encoding="utf-8")
        assert "b0/validation" in web_app.latest_eval_reports()

    def test_unreadable_files_are_skipped_rather_than_chosen(self, reports_root: Path) -> None:
        reports_root.mkdir(parents=True, exist_ok=True)
        (reports_root / "2026-12-31_broken.json").write_text("{not json", encoding="utf-8")
        write_report(reports_root, "2026-09-26_bbb.json", "2026-09-26T23:31:48+00:00", 0.143)
        chosen = web_app.latest_eval_reports()["b0/validation"]
        assert chosen["overall"]["graders"]["root_cause"]["accuracy"] == 0.143

    def test_a_split_whose_only_report_is_unreadable_is_absent(self, reports_root: Path) -> None:
        reports_root.mkdir(parents=True, exist_ok=True)
        (reports_root / "broken.json").write_text("{not json", encoding="utf-8")
        assert "b0/validation" not in web_app.latest_eval_reports()


class TestAgainstTheRealRepository:
    def test_the_chosen_validation_report_is_the_recorded_one(self) -> None:
        """The case that motivated this, against the reports actually committed here. If
        this ever selects the fixture run, the Evaluation page is claiming 1.000."""
        reports = web_app.latest_eval_reports()
        chosen = reports.get("b0/validation")
        if chosen is None:
            pytest.skip("no b0/validation report committed")
        assert chosen["data_source"] == "recorded bundles"
        assert chosen["overall"]["graders"]["root_cause"]["accuracy"] < 1.0
