"""Tests for firebreak.evals.runner and firebreak.evals.report.

The runner's most important property is not a number it computes. It is that a
label cannot reach a configuration, and that a report says what kind of data
produced it. Both are asserted here, because both are the sort of thing that
works right up until somebody adds a convenient parameter.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from firebreak.evals.metrics import compute_metrics
from firebreak.evals.outcome import InvestigationOutcome
from firebreak.evals.report import (
    SYNTHETIC_WARNING,
    TUNED_SPLIT_WARNING,
    build_report,
    render_markdown,
    report_paths,
    write_report,
)
from firebreak.evals.runner import (
    CONFIGURATIONS,
    RunnerError,
    collect_tasks,
    run_b0,
    run_configuration,
)
from firebreak.lab.scenario import Split

# Two tasks is enough for every structural property here and keeps the suite
# fast. The numbers themselves are measured by `firebreak eval run`, not here.
LIMIT = 2


@pytest.fixture(scope="module")
def validation_run(tmp_path_factory):  # type: ignore[no-untyped-def]
    workspace = tmp_path_factory.mktemp("eval-validation")
    return run_configuration("b0", Split.VALIDATION, workspace, limit=LIMIT)


class TestTheConfigurationCannotSeeTheAnswer:
    def test_a_configuration_takes_only_a_bundle_directory(self) -> None:
        """The leakage control, asserted structurally rather than trusted.

        If a configuration ever grew a `label` or `spec` parameter, the runner
        would be able to pass one and nothing else would notice.
        """
        for name, configuration in CONFIGURATIONS.items():
            parameters = list(inspect.signature(configuration).parameters)
            assert parameters == ["bundle_dir"], f"{name} takes {parameters}"

    def test_b0_returns_an_outcome_and_the_evidence_it_gathered(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        tasks, _ = collect_tasks(Split.VALIDATION, tmp_path, limit=1)
        outcome, gathered = run_b0(tasks[0].bundle_dir)
        assert outcome.bundle_id == tasks[0].bundle_id
        assert gathered
        # Every citation must resolve in what was gathered, or the evidence
        # grader would be scoring against an incomplete set rather than
        # against a fabrication.
        assert set(outcome.cited_evidence) <= gathered

    def test_the_runner_refuses_an_outcome_for_the_wrong_bundle(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A configuration returning someone else's answer would grade clean."""
        from firebreak.evals import runner as module

        def confused(bundle_dir: Path) -> tuple[InvestigationOutcome, set[str]]:
            return InvestigationOutcome(bundle_id="inc_ffffffffffff"), set()

        original = dict(module.CONFIGURATIONS)
        module.CONFIGURATIONS["confused"] = confused
        try:
            with pytest.raises(RunnerError, match="returned an outcome for"):
                run_configuration("confused", Split.VALIDATION, tmp_path, limit=1)
        finally:
            module.CONFIGURATIONS.clear()
            module.CONFIGURATIONS.update(original)


class TestCollectingTasks:
    def test_it_builds_fixtures_when_nothing_is_recorded(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        tasks, using_recorded = collect_tasks(Split.VALIDATION, tmp_path, limit=LIMIT)
        assert len(tasks) == LIMIT
        assert not using_recorded
        for task in tasks:
            assert task.bundle_dir.is_dir()

    def test_every_task_carries_its_own_label(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        tasks, _ = collect_tasks(Split.VALIDATION, tmp_path, limit=LIMIT)
        for task in tasks:
            assert task.label.bundle_id == task.bundle_id

    def test_a_synthetic_label_claims_no_onset(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A fixture has no flag convergence to measure.

        Claiming one would give the onset grader a target invented by the same
        code being graded.
        """
        tasks, _ = collect_tasks(Split.VALIDATION, tmp_path, limit=1)
        assert tasks[0].label.fault_onset is None

    def test_tasks_come_only_from_the_requested_split(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        tasks, _ = collect_tasks(Split.TEST_OOD, tmp_path, limit=4)
        assert {task.label.split for task in tasks} == {"test_ood"}

    def test_an_unknown_configuration_is_refused(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(RunnerError, match="unknown configuration"):
            run_configuration("nonexistent", Split.VALIDATION, tmp_path)

    def test_zero_trials_is_refused(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(RunnerError, match="trials must be at least 1"):
            run_configuration("b0", Split.VALIDATION, tmp_path, trials_per_task=0)


class TestRunning:
    def test_it_grades_every_trial(self, validation_run) -> None:  # type: ignore[no-untyped-def]
        assert len(validation_run.trials) == LIMIT
        assert len(validation_run.sheets) == len(validation_run.outcomes)

    def test_more_trials_multiply_the_rows(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        result = run_configuration("b0", Split.VALIDATION, tmp_path, trials_per_task=3, limit=LIMIT)
        assert len(result.trials) == LIMIT * 3

    def test_b0_is_identical_across_trials(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """No sampling in it, so it is perfectly reliable at whatever it does.

        That makes pass^3 exactly 1.0 or 0.0 for B0, which is correct rather
        than a limitation of the measure.
        """
        result = run_configuration("b0", Split.VALIDATION, tmp_path, trials_per_task=3, limit=1)
        verdicts = {sheet.correct_root_cause for sheet in result.sheets}
        assert len(verdicts) == 1

    def test_reliability_is_computed_only_with_enough_trials(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A pass^3 from single trials would equal pass@1 and mislead."""
        single = run_configuration("b0", Split.VALIDATION, tmp_path, limit=LIMIT)
        assert compute_metrics(single.sheets, single.outcomes, trials_per_task=1).reliability is (
            None
        )

        triple = run_configuration("b0", Split.VALIDATION, tmp_path, trials_per_task=3, limit=LIMIT)
        metrics = compute_metrics(triple.sheets, triple.outcomes, trials_per_task=3)
        assert metrics.reliability is not None
        assert metrics.reliability.k == 3


class TestMetrics:
    def test_parallel_inputs_are_required(self, validation_run) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ValueError, match="must be parallel"):
            compute_metrics(validation_run.sheets, validation_run.outcomes[:-1])

    def test_a_grader_that_applied_to_nothing_reports_no_accuracy(self, validation_run) -> None:  # type: ignore[no-untyped-def]
        """None rather than zero, which would read as always wrong."""
        metrics = compute_metrics(validation_run.sheets, validation_run.outcomes)
        assert "fault_class" not in metrics.graders

    def test_every_rate_carries_its_denominator(self, validation_run) -> None:  # type: ignore[no-untyped-def]
        metrics = compute_metrics(validation_run.sheets, validation_run.outcomes)
        for summary in metrics.graders.values():
            payload = summary.as_dict()
            assert payload["applicable"] is not None
            assert payload["trials"] == len(validation_run.sheets)


class TestTheIntervalIsOverTasksNotTrials:
    """The defect this class exists to prevent, caught after it shipped once.

    `statistics.bootstrap_mean` documents that the resampling unit is the task,
    and the first version of `compute_metrics` passed it one observation per
    trial. Running three trials of B0, which is deterministic and therefore
    produces three identical trials, narrowed the reported interval on test_id
    from [77.4, 100.0] to [83.9, 95.7]. The extra confidence came from nowhere.
    """

    def test_extra_trials_of_a_deterministic_system_do_not_narrow_it(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        single = run_configuration("b0", Split.VALIDATION, tmp_path, limit=4)
        triple = run_configuration("b0", Split.VALIDATION, tmp_path, trials_per_task=3, limit=4)

        one = compute_metrics(single.sheets, single.outcomes, resamples=2000).headline
        three = compute_metrics(
            triple.sheets, triple.outcomes, trials_per_task=3, resamples=2000
        ).headline
        assert one is not None and three is not None
        assert one.as_dict() == three.as_dict()

    def test_the_observation_count_is_the_task_count(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The number that makes the unit visible in the report itself."""
        result = run_configuration("b0", Split.VALIDATION, tmp_path, trials_per_task=3, limit=4)
        metrics = compute_metrics(result.sheets, result.outcomes, trials_per_task=3, resamples=500)
        assert metrics.trials == 12
        assert metrics.tasks == 4
        assert metrics.headline is not None
        assert metrics.headline.observations == 4

    def test_grader_intervals_use_the_same_unit(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        result = run_configuration("b0", Split.VALIDATION, tmp_path, trials_per_task=3, limit=4)
        metrics = compute_metrics(result.sheets, result.outcomes, trials_per_task=3, resamples=500)
        for summary in metrics.graders.values():
            if summary.interval is not None:
                assert summary.interval.observations <= 4

    def test_the_counts_stay_per_trial_because_that_is_what_a_reader_wants(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """Interval over tasks, counts over trials. Both, deliberately."""
        result = run_configuration("b0", Split.VALIDATION, tmp_path, trials_per_task=3, limit=4)
        metrics = compute_metrics(result.sheets, result.outcomes, trials_per_task=3, resamples=500)
        assert all(s.total_trials == 12 for s in metrics.graders.values())


class TestReport:
    def test_a_fixture_run_is_not_quotable(self, validation_run) -> None:  # type: ignore[no-untyped-def]
        report = build_report(validation_run, resamples=200)
        assert not report.quotable
        assert SYNTHETIC_WARNING in report.caveats

    def test_a_tunable_split_carries_its_own_warning(self, validation_run) -> None:  # type: ignore[no-untyped-def]
        """Validation was available for tuning, so it cannot support a claim."""
        report = build_report(validation_run, resamples=200)
        assert TUNED_SPLIT_WARNING in report.caveats

    def test_the_caveats_lead_the_markdown(self, validation_run) -> None:  # type: ignore[no-untyped-def]
        """A reader who stops after the first table must already know."""
        markdown = render_markdown(build_report(validation_run, resamples=200))
        assert markdown.index("Read this first") < markdown.index("## Headline")

    def test_json_and_markdown_agree_on_the_data_source(self, validation_run) -> None:  # type: ignore[no-untyped-def]
        report = build_report(validation_run, resamples=200)
        payload = report.as_dict()
        assert payload["data_source"] == "synthetic fixtures"
        assert "synthetic fixtures" in render_markdown(report)

    def test_the_path_names_the_configuration_split_and_commit(
        self, validation_run, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        report = build_report(validation_run, resamples=200)
        json_path, markdown_path = report_paths(report, tmp_path)
        assert json_path.parent == tmp_path / "b0" / "validation"
        assert json_path.suffix == ".json"
        assert markdown_path.suffix == ".md"
        assert report.commit[:12] in json_path.stem

    def test_writing_produces_both_formats(self, validation_run, tmp_path) -> None:  # type: ignore[no-untyped-def]
        report = build_report(validation_run, resamples=200)
        json_path, markdown_path = write_report(report, tmp_path)
        assert json.loads(json_path.read_text())["configuration"] == "b0"
        assert markdown_path.read_text().startswith("# Eval report")

    def test_the_report_records_a_commit(self, validation_run) -> None:  # type: ignore[no-untyped-def]
        """A report that cannot be traced to a commit cannot be reproduced."""
        report = build_report(validation_run, resamples=200)
        assert report.commit
        assert report.commit != ""

    def test_it_is_deterministic_apart_from_the_timestamp(self, validation_run) -> None:  # type: ignore[no-untyped-def]
        first = build_report(validation_run, resamples=200).as_dict()
        second = build_report(validation_run, resamples=200).as_dict()
        del first["generated_at"], second["generated_at"]
        assert first == second
