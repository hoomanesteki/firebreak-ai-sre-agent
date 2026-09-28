"""Tests for the only thing allowed to put a number on the site.

SPEC.md Section 15 says numbers on the site come only from `reports/site_stats.json`, and
Section 19.4 rule 5 makes the README's table match it. So this script is the boundary between
the reports and every public surface, and the tests that matter are about what it refuses to
publish.

**The failure that matters is publishing a real number that means the wrong thing.** Two such
numbers are committed in this repository today: a synthetic-fixture run reporting 1.000
root-cause accuracy, and a partly recorded tuning split reporting 0.143. Both are honest inside
their own reports and both would be a lie on a front page. A leak is not a crash, so nothing
downstream would catch it.

**Staleness is the second.** A report is a snapshot, and `reports/eval/b0/test_ood/` holds one
describing a bundle that was later deleted for recording corruption. "Every number traces to a
report" is satisfied by a stale report, which is why that phrasing is not enough on its own.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_site_stats.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("build_site_stats_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


stats = _load()


def a_report(
    *,
    configuration: str = "fb-v1",
    split: str = "test_id",
    quotable: bool = True,
    data_source: str = "recorded bundles",
    accuracy: float = 0.75,
    recorded: int | None = 29,
    total: int | None = 29,
    tokens: int = 1000,
    median_usd: float = 0.0123,
) -> Any:
    payload: dict[str, Any] = {
        "generated_at": "2026-09-27T10:00:00+00:00",
        "quotable_as_a_result": quotable,
        "data_source": data_source,
        "recorded_scenarios": recorded,
        "total_scenarios": total,
        "caveats": [],
        "overall": {
            "graders": {
                "root_cause": {
                    "accuracy": accuracy,
                    "interval": {"lower": accuracy - 0.1, "upper": accuracy + 0.1},
                },
                "abstention": {"accuracy": accuracy},
            },
            "cost": {
                "total_tokens_in": tokens,
                "total_tokens_out": 0,
                "median_usd": median_usd,
            },
        },
    }
    return stats.Report(
        configuration=configuration,
        split=split,
        path=REPO_ROOT / "reports" / "eval" / configuration / split / "2026-09-27_abc.json",
        payload=payload,
    )


class TestOnlyAQuotableReportBecomesANumber:
    def test_a_quotable_report_publishes_its_accuracy(self) -> None:
        metric = stats.accuracy_metric("k", "Label", a_report(), "root_cause", None)
        assert metric.quotable
        assert metric.value == 0.75
        assert "0.750" in metric.display

    def test_a_non_quotable_report_publishes_no_number(self) -> None:
        metric = stats.accuracy_metric("k", "Label", a_report(quotable=False), "root_cause", None)
        assert not metric.quotable
        assert metric.value is None
        assert "not a result" in metric.display

    def test_a_synthetic_fixture_run_is_never_published(self) -> None:
        """The 1.000 accuracy report committed in this repository. Publishing it would put
        perfect accuracy on the front page of a system measured at one of seven."""
        metric = stats.accuracy_metric(
            "k",
            "Label",
            a_report(quotable=False, data_source="synthetic fixtures", accuracy=1.0),
            "root_cause",
            None,
        )
        assert metric.value is None
        assert "synthetic fixtures" in metric.display

    def test_a_partly_recorded_split_is_never_published(self) -> None:
        metric = stats.accuracy_metric(
            "k",
            "Label",
            a_report(quotable=False, recorded=8, total=16, accuracy=0.143),
            "root_cause",
            None,
        )
        assert metric.value is None
        assert "8 of this split's 16" in metric.display

    def test_a_tuning_split_is_never_published(self) -> None:
        metric = stats.accuracy_metric(
            "k",
            "Label",
            a_report(quotable=False, split="validation", recorded=16, total=16),
            "root_cause",
            None,
        )
        assert metric.value is None
        assert "available for tuning" in metric.display

    def test_the_reason_names_the_report_rather_than_the_repository(self) -> None:
        """A fixture run's reason must be about the fixtures, not about the library being
        unrecorded. Both are true and only one is about the report being cited, which is
        the kind of near-miss that survives review."""
        reason = stats.not_a_result_because(
            a_report(quotable=False, data_source="synthetic fixtures")
        )
        assert "synthetic fixtures" in reason
        assert "recordings" not in reason

    def test_a_missing_report_publishes_no_number(self) -> None:
        metric = stats.accuracy_metric("k", "Label", None, "root_cause", None)
        assert metric.value is None
        assert metric.source == ""

    def test_a_missing_grader_publishes_no_number(self) -> None:
        """A quotable report that never ran this grader must not become a silent zero."""
        metric = stats.accuracy_metric("k", "Label", a_report(), "remediation", None)
        assert metric.value is None


class TestAStaleReportIsRefused:
    def test_a_report_whose_bundles_are_gone_is_stale(self) -> None:
        report = a_report(split="test_ood", recorded=1, total=30)
        assert stats.stale_reason(report, {"test_ood": 0}) is not None

    def test_a_report_matching_the_bundles_on_disk_is_not_stale(self) -> None:
        report = a_report(split="test_ood", recorded=1, total=30)
        assert stats.stale_reason(report, {"test_ood": 1}) is None

    def test_a_fixture_report_cannot_go_stale(self) -> None:
        """Fixtures are rebuilt from specs, so there are no bundles for them to outlive."""
        report = a_report(data_source="synthetic fixtures", recorded=None)
        assert stats.stale_reason(report, {"test_id": 0}) is None

    def test_a_stale_report_publishes_no_number_even_when_quotable(self) -> None:
        """The important case. A fully recorded split that was later emptied leaves a
        quotable report describing nothing, and quotable is the flag everything else
        trusts."""
        report = a_report(quotable=True, accuracy=0.9)
        metric = stats.accuracy_metric("k", "Label", report, "root_cause", "the bundles are gone")
        assert metric.value is None
        assert metric.mode == "stale"
        assert "stale" in metric.display

    def test_the_stale_reason_says_both_counts(self) -> None:
        reason = stats.stale_reason(
            a_report(split="test_ood", recorded=1, total=30), {"test_ood": 0}
        )
        assert reason is not None
        assert "1 recorded" in reason
        assert "0 are on disk" in reason


class TestCostIsNotZeroWhenNoModelRan:
    def test_zero_tokens_publishes_no_cost(self) -> None:
        """Printing 0.0000 USD would say the system is free rather than unmeasured, which
        is the same mistake the Evaluation page already avoids."""
        metric = stats.cost_metric(a_report(tokens=0), None)
        assert metric.value is None
        assert "no model has run" in metric.display

    def test_a_real_token_count_publishes_the_cost(self) -> None:
        metric = stats.cost_metric(a_report(tokens=5000, median_usd=0.0123), None)
        assert metric.quotable
        assert metric.value == 0.0123
        assert "USD" in metric.display

    def test_a_non_quotable_report_publishes_no_cost(self) -> None:
        metric = stats.cost_metric(a_report(quotable=False, tokens=5000), None)
        assert metric.value is None


class TestOrderingUsesGeneratedAt:
    def test_the_newest_by_generated_at_wins(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Not modification time, which git rewrites on checkout, and not the filename,
        which carries a commit hash."""
        import os

        directory = tmp_path / "eval" / "b0" / "validation"
        directory.mkdir(parents=True)
        old = directory / "2026-12-31_aaa.json"
        new = directory / "2026-01-01_bbb.json"
        old.write_text(json.dumps({"generated_at": "2026-09-25T00:00:00+00:00", "id": "old"}))
        new.write_text(json.dumps({"generated_at": "2026-09-26T00:00:00+00:00", "id": "new"}))
        os.utime(old, (2_000_000, 2_000_000))
        os.utime(new, (1_000_000, 1_000_000))
        monkeypatch.setattr(stats, "EVAL_DIR", tmp_path / "eval")
        found = stats.newest_reports()
        assert found[("b0", "validation")].payload["id"] == "new"

    def test_an_unreadable_report_is_fatal_rather_than_skipped(
        self, tmp_path: Path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """The Console skips a broken report so a page still renders. This script must not:
        a site built from a partial stats file publishes whatever survived, and nobody would
        know a report was dropped."""
        directory = tmp_path / "eval" / "b0" / "validation"
        directory.mkdir(parents=True)
        (directory / "broken.json").write_text("{not json")
        monkeypatch.setattr(stats, "EVAL_DIR", tmp_path / "eval")
        with pytest.raises(stats.StatsError, match="cannot be read"):
            stats.newest_reports()


class TestTheGeneratedFileMatchesTheRepository:
    def test_building_it_produces_the_committed_file(self) -> None:
        """`--check` in CI relies on this being reproducible.

        `local` is excluded for the same reason `--check` excludes it: it is derived from
        bundles on disk, so it differs between a laptop with recordings and CI with none.
        Comparing it here made this test pass locally and fail in CI, which is the precise
        failure the `local` section was introduced to avoid and which I reintroduced by
        leaving this assertion comparing everything.
        """
        committed = json.loads((REPO_ROOT / "reports" / "site_stats.json").read_text())
        fresh = stats.build()
        for payload in (committed, fresh):
            for key in ("generated_at", "commit", "local"):
                payload.pop(key, None)
        assert committed == fresh, "run scripts/build_site_stats.py"

    def test_every_readme_row_has_the_keys_hygiene_requires(self) -> None:
        committed = json.loads((REPO_ROOT / "reports" / "site_stats.json").read_text())
        rows = committed["readme_rows"]
        assert rows
        for row in rows:
            assert set(row) == {"metric", "value", "source"}

    def test_no_row_publishes_a_number_this_project_cannot_stand_behind(self) -> None:
        """The end-to-end version of every test above, against what is actually committed.
        Right now no metric is quotable, so no row should read as a measurement."""
        committed = json.loads((REPO_ROOT / "reports" / "site_stats.json").read_text())
        for key, metric in committed["metrics"].items():
            if not metric["quotable"]:
                assert metric["value"] is None, f"{key} is not quotable and carries a number"
                assert "not " in metric["display"], f"{key} does not say it is unmeasured"

    def test_the_stale_report_is_reported_when_bundles_are_present(self) -> None:
        """Under `local`, because it is derived from bundles on disk. On a machine with no
        bundles the check cannot run, and the file says so rather than reporting nothing
        stale, which would read as nothing being wrong."""
        committed = json.loads((REPO_ROOT / "reports" / "site_stats.json").read_text())
        local = committed["local"]
        if not local["bundles_present"]:
            assert "could not be checked" in local["note"]
            return
        assert "b0/test_ood" in local["stale_reports"]

    def test_the_published_file_carries_no_count_of_local_bundles(self) -> None:
        """A recording count is a fact about one machine: bundles average 7.6 MB and the
        directory is git-ignored, so a reader who clones this has none. Publishing one would
        describe the build machine while reading as a property of the artefact, and it would
        make this file differ between CI and a laptop."""
        import re

        committed = json.loads((REPO_ROOT / "reports" / "site_stats.json").read_text())
        assert "recorded" not in committed["library"]
        # "<number> recorded" is the shape of a bundle count. The cassette row's "recorded
        # from mode stub" is about cassettes, which are committed, so it is fine.
        counted = re.compile(r"\d+\s+recorded\b")
        for row in committed["readme_rows"]:
            assert not counted.search(row["value"]), f"{row['metric']} publishes a local count"

    def test_the_file_is_reproducible_without_any_bundles(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """What CI does. Everything published must come from committed inputs, so a build
        with no bundles present has to match the committed file outside `local`."""
        monkeypatch.setattr(stats, "BUNDLES_DIR", REPO_ROOT / "does-not-exist")
        committed = json.loads((REPO_ROOT / "reports" / "site_stats.json").read_text())
        fresh = stats.build()
        for payload in (committed, fresh):
            for key in ("generated_at", "commit", "local"):
                payload.pop(key, None)
        assert committed == fresh
