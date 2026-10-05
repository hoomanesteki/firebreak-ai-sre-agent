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
from typing import Any, ClassVar

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


def test_site_refuses_stub_even_when_report_claims_to_be_quotable():
    report = a_report()
    report.payload["model_modes"] = ["stub"]
    assert not stats.accuracy_metric("k", "Label", report, "root_cause", None).quotable


def test_legacy_report_without_model_provenance_is_not_publishable():
    report = a_report()
    del report.payload["model_modes"]
    assert not report.quotable


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
        "model_modes": ["api"],
        "model_ids": ["test-model"],
        "model_calls": 1,
        "fallback_trials": 0,
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


class TestTheExplainerSiteTypesNoNumber:
    """The explainer site reads `explainer/_variables.yml`, which this script generates.

    **Why a generated variable file rather than an executable block in the page.** A Quarto page
    can run Python, and the first version of the overview did. That made the site render depend
    on a kernel being available, and CI would have been the place that discovered it was not,
    which is the failure mode `make verify-clean` exists to catch. A variable file needs nothing
    but Quarto.
    """

    def test_it_writes_the_variable_file(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        target = tmp_path / "_variables.yml"
        monkeypatch.setattr(stats, "VARIABLES_PATH", target)
        payload = stats.build()
        payload["local"] = stats.local_diagnostics()
        stats.write_variables(payload)
        text = target.read_text(encoding="utf-8")
        assert "specs: 114" in text
        assert "Do not edit" in text

    def test_it_carries_no_count_of_local_bundles(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Same rule as the published stats file. A recorded count describes the build machine,
        and the explainer site is read by people who cloned nothing."""
        target = tmp_path / "_variables.yml"
        monkeypatch.setattr(stats, "VARIABLES_PATH", target)
        stats.write_variables(stats.build())
        lines = target.read_text(encoding="utf-8").splitlines()
        keys = [line.split(":")[0] for line in lines if line and not line.startswith("#")]
        assert "recorded" not in keys

    def test_the_adr_count_is_counted_rather_than_typed(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """So writing an ADR updates the site."""
        target = tmp_path / "_variables.yml"
        monkeypatch.setattr(stats, "VARIABLES_PATH", target)
        stats.write_variables(stats.build())
        on_disk = len(list((REPO_ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")))
        assert f"adrs: {on_disk}" in target.read_text(encoding="utf-8")

    def test_missing_quality_data_becomes_a_stated_absence(
        self, tmp_path: Path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """A page then shows "not recorded" rather than a number, which is the rule every other
        figure on these sites follows. Inventing a test count would be the worst kind of lie:
        entirely plausible and impossible to notice."""
        monkeypatch.setattr(stats, "QUALITY_PATH", tmp_path / "absent.json")
        target = tmp_path / "_variables.yml"
        monkeypatch.setattr(stats, "VARIABLES_PATH", target)
        payload = stats.build()
        assert payload["quality"]["available"] is False
        stats.write_variables(payload)
        assert 'tests_passed: "not recorded"' in target.read_text(encoding="utf-8")

    def test_quality_facts_read_the_summary(self) -> None:
        facts = stats.quality_facts()
        if not facts["available"]:
            pytest.skip("reports/quality.json not written yet; run make test")
        assert facts["tests_passed"] > 1000
        # Not compared against the floor. pytest enforces that, and asserting it here too made
        # the check circular: the summary is only written by a run that passed.
        assert 0.0 < facts["coverage_percent"] <= 100.0
        assert facts["source"] == "reports/quality.json"

    def test_the_committed_variable_file_matches_a_fresh_build(
        self, tmp_path: Path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """The explainer site is built from the committed file, so a stale one publishes stale
        numbers with nothing to notice it."""
        committed = REPO_ROOT / "explainer" / "_variables.yml"
        if not committed.is_file():
            pytest.skip("explainer/_variables.yml not generated yet")
        target = tmp_path / "_variables.yml"
        monkeypatch.setattr(stats, "VARIABLES_PATH", target)
        payload = stats.build()
        payload["local"] = stats.local_diagnostics()
        stats.write_variables(payload)
        assert target.read_text(encoding="utf-8") == committed.read_text(encoding="utf-8"), (
            "run scripts/build_site_stats.py"
        )


class TestTheBaselineFiguresComeFromTheReport:
    """The figures the whole project turns on: the same triage names nearly every fault on
    fixtures and a small fraction on real recordings. Typed into prose on a web page, a number
    that important goes stale and then gets quoted."""

    def test_it_reads_the_recorded_validation_report(self) -> None:
        facts = stats.baseline_facts(stats.newest_reports())
        assert facts["available"]
        assert facts["root_cause_applicable"] > 0
        assert facts["root_cause_correct"] <= facts["root_cause_applicable"]
        assert "reports/eval/b0/validation" in facts["source"]

    def test_it_is_absent_when_the_report_is(self) -> None:
        assert stats.baseline_facts({})["available"] is False

    def test_a_report_with_no_counts_is_absent_rather_than_zero(self) -> None:
        """Zero correct and zero applicable would render as a real and terrible figure."""
        empty = stats.Report(
            configuration="b0",
            split="validation",
            path=REPO_ROOT / "reports" / "eval" / "b0" / "validation" / "x.json",
            payload={"overall": {"graders": {}}},
        )
        assert stats.baseline_facts({("b0", "validation"): empty})["available"] is False


class TestBuildCarriesNoMachineState:
    """The structural version of the bug CI caught.

    `local` used to be a key inside `build()`, and a test compared `build()` against the
    committed file while popping only the timestamp and commit. It passed on a machine with
    eight recordings and failed in CI with none. Commenting the exclusion was not enough,
    because the next comparison would have to remember it too, so the disk-derived part moved
    out of `build()` entirely. These tests keep it out.
    """

    def test_build_returns_no_local_section(self) -> None:
        assert "local" not in stats.build()

    def test_build_is_identical_with_and_without_bundles(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """The property that matters: nothing `build()` returns depends on what is on disk
        beyond the committed files. Any comparison of its output is then correct by default
        rather than correct if the author remembered to exclude something."""
        with_bundles = stats.build()
        monkeypatch.setattr(stats, "BUNDLES_DIR", REPO_ROOT / "does-not-exist")
        without = stats.build()
        for payload in (with_bundles, without):
            payload.pop("generated_at", None)
            payload.pop("commit", None)
        assert with_bundles == without

    def test_the_diagnostics_are_available_separately(self) -> None:
        local = stats.local_diagnostics()
        assert set(local) == {"bundles_present", "recorded_by_split", "stale_reports", "note"}

    def test_the_diagnostics_say_so_when_there_are_no_bundles(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Silence and "nothing is stale" look identical, and only one of them means the
        check ran."""
        monkeypatch.setattr(stats, "BUNDLES_DIR", REPO_ROOT / "does-not-exist")
        local = stats.local_diagnostics()
        assert local["bundles_present"] is False
        assert "could not be checked" in local["note"]


class TestRunningItTwiceLeavesTheTreeClean:
    """The twin of the check in `test_record_quality.py`, for the same defect in the other
    generator. `make verify` regenerates this file, so writing unconditionally meant every verify
    run left it modified over a timestamp."""

    def test_an_unchanged_file_is_not_rewritten(self, tmp_path: Path) -> None:
        output = tmp_path / "site_stats.json"
        provenance = ("generated_at", "commit", "local")
        assert stats.write_if_changed(output, {"metrics": {}, "generated_at": "a"}, provenance)
        assert not stats.write_if_changed(output, {"metrics": {}, "generated_at": "b"}, provenance)

    def test_a_changed_metric_is_written(self, tmp_path: Path) -> None:
        output = tmp_path / "site_stats.json"
        provenance = ("generated_at", "commit", "local")
        stats.write_if_changed(output, {"metrics": {"a": 1}, "generated_at": "a"}, provenance)
        assert stats.write_if_changed(
            output, {"metrics": {"a": 2}, "generated_at": "b"}, provenance
        )

    def test_the_local_section_alone_does_not_trigger_a_write(self, tmp_path: Path) -> None:
        """`local` describes the build machine, so a laptop with recordings and CI without must not
        fight over the file."""
        output = tmp_path / "site_stats.json"
        provenance = ("generated_at", "commit", "local")
        stats.write_if_changed(
            output, {"metrics": {}, "local": {"bundles_present": True}}, provenance
        )
        assert not stats.write_if_changed(
            output, {"metrics": {}, "local": {"bundles_present": False}}, provenance
        )

    def test_the_committed_file_is_stable_against_a_fresh_build(self) -> None:
        """End to end over the real file: a fresh build must not want to change it, provenance
        aside. If this fails, `make verify` will leave the tree dirty."""
        committed = json.loads((REPO_ROOT / "reports" / "site_stats.json").read_text())
        fresh = stats.build()
        for payload in (committed, fresh):
            for key in ("generated_at", "commit", "local"):
                payload.pop(key, None)
        assert committed == fresh, "run scripts/build_site_stats.py"


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


class TestTheReadmeRegionsAreGeneratedNotTyped:
    """The README's engineering line was typed by hand and said 2131 tests when the run
    reported 2246. `scripts/check_repo_hygiene.py` can now catch that, but a rule with no
    generator just moves the work: somebody still has to retype the figure correctly. This
    writes it, from the same renderers the checker compares against."""

    SAMPLE: ClassVar[dict[str, Any]] = {
        "readme_rows": [{"metric": "m", "value": "1", "source": "s"}],
        "quality": {
            "available": True,
            "tests_passed": 7,
            "tests_skipped": 1,
            "coverage_percent": 90.0,
        },
    }

    def readme(self, tmp_path: Path, stats_body: str, engineering_body: str) -> Path:
        path = tmp_path / "README.md"
        path.write_text(
            f"# Title\n\n<!-- stats:start -->\n{stats_body}\n<!-- stats:end -->\n\n"
            f"## Development\n\n<!-- engineering:start -->\n{engineering_body}\n"
            "<!-- engineering:end -->\n\nTrailing prose.\n",
            encoding="utf-8",
        )
        return path

    def test_it_replaces_a_stale_figure(self, tmp_path: Path) -> None:
        path = self.readme(tmp_path, "stale table", "1 test passing")
        assert stats.write_readme_regions(self.SAMPLE, path) is True
        body = path.read_text(encoding="utf-8")
        assert "7 tests passing, 1 skipped, 90.0% coverage" in body
        assert "1 test passing" not in body

    def test_it_leaves_everything_outside_the_markers_alone(self, tmp_path: Path) -> None:
        path = self.readme(tmp_path, "stale table", "1 test passing")
        stats.write_readme_regions(self.SAMPLE, path)
        body = path.read_text(encoding="utf-8")
        assert body.startswith("# Title\n")
        assert body.endswith("Trailing prose.\n")
        assert "## Development" in body

    def test_it_reports_no_change_when_the_regions_already_match(self, tmp_path: Path) -> None:
        """So `make site-stats` does not dirty the tree on a run that changed nothing, which
        is the same reason `write_if_changed` exists for the JSON."""
        path = self.readme(tmp_path, "stale table", "1 test passing")
        stats.write_readme_regions(self.SAMPLE, path)
        before = path.read_text(encoding="utf-8")
        assert stats.write_readme_regions(self.SAMPLE, path) is False
        assert path.read_text(encoding="utf-8") == before

    def test_missing_markers_are_an_error_rather_than_a_silent_skip(self, tmp_path: Path) -> None:
        path = tmp_path / "README.md"
        path.write_text("# Title\n\nNo markers here.\n", encoding="utf-8")
        with pytest.raises(stats.StatsError, match="markers"):
            stats.write_readme_regions(self.SAMPLE, path)

    def test_what_it_writes_is_what_the_hygiene_check_accepts(self, tmp_path: Path) -> None:
        """The contract test. Two functions had to agree about the README and did not; this
        asserts the agreement rather than trusting it."""
        import check_repo_hygiene

        path = self.readme(tmp_path, "stale table", "1 test passing")
        stats.write_readme_regions(self.SAMPLE, path)
        stats_path = tmp_path / "site_stats.json"
        stats_path.write_text(json.dumps(self.SAMPLE), encoding="utf-8")
        assert check_repo_hygiene.check_readme_stats(path, stats_path) == []

    def test_the_committed_readme_is_what_the_generator_would_write(self) -> None:
        """Not an assertion about this run's numbers, which would be circular. It asserts
        that the committed README matches the committed report, which are both inputs."""
        import check_repo_hygiene

        committed = json.loads((REPO_ROOT / "reports" / "site_stats.json").read_text())
        assert (
            check_repo_hygiene.check_readme_stats(
                REPO_ROOT / "README.md", REPO_ROOT / "reports" / "site_stats.json"
            )
            == []
        )
        assert committed["quality"]["available"] is True
