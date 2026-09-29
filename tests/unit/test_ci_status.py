"""Tests for the tool that says whether CI passed.

**Why this file exists.** Every phase gate in this project is "CI green, then merge", and
this script is what answers that. An untested tool in that position can report a pass that
did not happen, and nothing downstream would catch it: the whole point of asking is that
nobody is watching the web page.

**The two failures that matter are both silent.** A run that has not started yet must never
read as a pass, because the window between pushing and the workflow starting is exactly when
somebody asks. And a short SHA must find its run, because a short SHA is what a person
copies out of `git log`, and an exact-match miss reported itself as "no run yet", which is
the one answer indistinguishable from the tool working.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci_status.py"


def _load() -> Any:
    """Import the script by path, since `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location("ci_status_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ci = _load()

FULL_SHA = "9272208a1b2c3d4e5f60718293a4b5c6d7e8f900"


def a_run(**overrides: Any) -> dict[str, Any]:
    payload = {
        "id": 42,
        "name": "ci",
        "head_sha": FULL_SHA,
        "head_branch": "feat/p11-console-observability",
        "status": "completed",
        "conclusion": "success",
        "display_title": "a commit",
        "html_url": "https://example.invalid/run/42",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Replace the HTTP call, so no test reaches the network."""

    served: dict[str, dict[str, Any]] = {}

    def fake_get(path: str) -> dict[str, Any]:
        for prefix, payload in served.items():
            if path.startswith(prefix):
                return payload
        raise ci.CiError(f"nothing served for {path}")

    monkeypatch.setattr(ci, "_get", fake_get)
    return served


class TestAShortShaFindsItsRun:
    """The defect this file was written for.

    `--sha 9272208` compared a 7 character string with a 40 character one, found nothing,
    and printed "no run yet". A tool whose job is to distinguish "not started" from
    "failed" reported a lookup bug as the former.
    """

    def test_a_seven_character_sha_matches(self, runs) -> None:  # type: ignore[no-untyped-def]
        runs["/repos/o/r/actions/runs"] = {"workflow_runs": [a_run()]}
        found = ci.find_run("o/r", FULL_SHA[:7])
        assert found is not None
        assert found.sha == FULL_SHA

    def test_the_full_sha_still_matches(self, runs) -> None:  # type: ignore[no-untyped-def]
        runs["/repos/o/r/actions/runs"] = {"workflow_runs": [a_run()]}
        assert ci.find_run("o/r", FULL_SHA) is not None

    def test_a_different_commit_does_not_match(self, runs) -> None:  # type: ignore[no-untyped-def]
        """Prefix matching must not become "matches anything"."""
        runs["/repos/o/r/actions/runs"] = {"workflow_runs": [a_run()]}
        assert ci.find_run("o/r", "deadbee") is None

    def test_a_sha_too_short_to_identify_a_commit_is_refused(self, runs) -> None:  # type: ignore[no-untyped-def]
        """Loudly, rather than by matching the first run in the list. A one character
        prefix would match roughly one run in sixteen and report another commit's
        result as this commit's."""
        runs["/repos/o/r/actions/runs"] = {"workflow_runs": [a_run()]}
        with pytest.raises(ci.CiError, match="too short"):
            ci.find_run("o/r", "92")

    def test_it_takes_the_most_recent_matching_run(self, runs) -> None:  # type: ignore[no-untyped-def]
        """The API returns newest first, so a re-run of the same commit must win over the
        original. Reporting the first attempt would call a fixed commit broken."""
        runs["/repos/o/r/actions/runs"] = {
            "workflow_runs": [
                a_run(id=99, conclusion="success"),
                a_run(id=42, conclusion="failure"),
            ]
        }
        found = ci.find_run("o/r", FULL_SHA[:7])
        assert found is not None
        assert found.id == 99


class TestEveryWorkflowForTheCommitIsChecked:
    """The defect this class exists for.

    This repository has two workflows: `ci`, and `site` on pushes to main. The tool returned
    only the newest run, so a green site deployment finishing after a red `ci` run reported the
    commit as passing. A tool whose whole job is "did this commit pass" cannot answer for one
    workflow and be read as answering for all of them, and the first time both ran it did
    exactly that.
    """

    def test_both_workflows_are_returned(self, runs) -> None:  # type: ignore[no-untyped-def]
        runs["/repos/o/r/actions/runs"] = {
            "workflow_runs": [
                a_run(id=2, name="site", conclusion="success"),
                a_run(id=1, name="ci", conclusion="success"),
            ]
        }
        found = ci.find_runs("o/r", FULL_SHA[:7])
        assert [run.workflow for run in found] == ["site", "ci"]

    def test_a_newer_passing_workflow_does_not_hide_an_older_failure(self, runs) -> None:  # type: ignore[no-untyped-def]
        """The exact shape of the bug: `site` succeeded after `ci` failed on the same commit."""
        runs["/repos/o/r/actions/runs"] = {
            "workflow_runs": [
                a_run(id=2, name="site", conclusion="success"),
                a_run(id=1, name="ci", conclusion="failure"),
            ]
        }
        found = ci.find_runs("o/r", FULL_SHA[:7])
        assert any(run.finished and not run.passed for run in found)

    def test_the_description_names_the_failing_workflow(self, runs) -> None:  # type: ignore[no-untyped-def]
        """A reader with two workflows needs to know which one to look at."""
        runs["/repos/o/r/actions/runs"] = {
            "workflow_runs": [
                a_run(id=2, name="site", conclusion="success"),
                a_run(id=1, name="ci", conclusion="failure"),
            ]
        }
        text = ci.describe_all(ci.find_runs("o/r", FULL_SHA[:7]))
        assert "workflow ci" in text
        assert "workflow site" in text
        assert "failed workflow(s): ci" in text

    def test_a_re_run_wins_over_the_original_for_the_same_workflow(self, runs) -> None:  # type: ignore[no-untyped-def]
        """One entry per workflow, newest first, so a fixed commit is not called broken by its
        first attempt."""
        runs["/repos/o/r/actions/runs"] = {
            "workflow_runs": [
                a_run(id=3, name="ci", conclusion="success"),
                a_run(id=1, name="ci", conclusion="failure"),
            ]
        }
        found = ci.find_runs("o/r", FULL_SHA[:7])
        assert len(found) == 1
        assert found[0].id == 3

    def test_no_runs_is_an_empty_list_rather_than_a_pass(self, runs) -> None:  # type: ignore[no-untyped-def]
        runs["/repos/o/r/actions/runs"] = {"workflow_runs": [a_run(head_sha="f" * 40)]}
        assert ci.find_runs("o/r", FULL_SHA[:7]) == []

    def test_a_workflow_still_running_is_not_a_pass(self, runs) -> None:  # type: ignore[no-untyped-def]
        """Half the answer is not the answer. A commit has passed only when every workflow
        has finished and every one of them succeeded."""
        runs["/repos/o/r/actions/runs"] = {
            "workflow_runs": [
                a_run(id=2, name="site", status="in_progress", conclusion=None),
                a_run(id=1, name="ci", conclusion="success"),
            ]
        }
        found = ci.find_runs("o/r", FULL_SHA[:7])
        assert not all(run.finished for run in found)

    def test_an_unnamed_workflow_is_labelled_rather_than_dropped(self, runs) -> None:  # type: ignore[no-untyped-def]
        runs["/repos/o/r/actions/runs"] = {"workflow_runs": [a_run(name=None)]}
        found = ci.find_runs("o/r", FULL_SHA[:7])
        assert found[0].workflow == "unknown"


class TestAMissingRunIsNotAPass:
    def test_no_run_for_the_commit_returns_none(self, runs) -> None:  # type: ignore[no-untyped-def]
        runs["/repos/o/r/actions/runs"] = {"workflow_runs": [a_run(head_sha="f" * 40)]}
        assert ci.find_run("o/r", FULL_SHA[:7]) is None

    def test_an_unfinished_run_is_neither_passed_nor_finished(self, runs) -> None:  # type: ignore[no-untyped-def]
        runs["/repos/o/r/actions/runs"] = {
            "workflow_runs": [a_run(status="in_progress", conclusion=None)]
        }
        found = ci.find_run("o/r", FULL_SHA[:7])
        assert found is not None
        assert not found.finished
        assert not found.passed

    def test_a_queued_run_is_not_passed(self, runs) -> None:  # type: ignore[no-untyped-def]
        runs["/repos/o/r/actions/runs"] = {
            "workflow_runs": [a_run(status="queued", conclusion=None)]
        }
        found = ci.find_run("o/r", FULL_SHA[:7])
        assert found is not None
        assert not found.passed

    def test_a_malformed_response_is_an_error_rather_than_no_run(self, runs) -> None:  # type: ignore[no-untyped-def]
        """ "No runs list" and "no run for this commit" mean different things, and only
        one of them is worth waiting through."""
        runs["/repos/o/r/actions/runs"] = {"unexpected": []}
        with pytest.raises(ci.CiError, match="workflow_runs"):
            ci.find_run("o/r", FULL_SHA[:7])


class TestOnlySuccessCountsAsPassed:
    @pytest.mark.parametrize(
        "conclusion", ["failure", "cancelled", "timed_out", "action_required", "startup_failure"]
    )
    def test_every_other_conclusion_is_a_failure(self, conclusion: str) -> None:
        run = ci.RunResult(
            id=1,
            workflow="ci",
            sha=FULL_SHA,
            branch="b",
            status="completed",
            conclusion=conclusion,
            title="t",
            url="u",
        )
        assert run.finished
        assert not run.passed

    def test_success_is_a_pass(self) -> None:
        run = ci.RunResult(
            id=1,
            workflow="ci",
            sha=FULL_SHA,
            branch="b",
            status="completed",
            conclusion="success",
            title="t",
            url="u",
        )
        assert run.passed


class TestItNamesWhatFailed:
    """Without this, a red build sends a reader to a web page to find out which step
    broke, and the whole reason for a CLI is not doing that."""

    def _run_with_jobs(self, runs: dict[str, Any]) -> Any:
        runs["/repos/o/r/actions/runs/42/jobs"] = {
            "jobs": [
                {
                    "name": "verify",
                    "status": "completed",
                    "conclusion": "failure",
                    "steps": [
                        {"name": "lint", "conclusion": "success"},
                        {"name": "tests", "conclusion": "failure"},
                        {"name": "offline demo", "conclusion": "skipped"},
                    ],
                },
                {"name": "security", "status": "completed", "conclusion": "success", "steps": []},
            ]
        }
        base = ci.RunResult(
            id=42,
            workflow="ci",
            sha=FULL_SHA,
            branch="b",
            status="completed",
            conclusion="failure",
            title="a commit",
            url="https://example.invalid/run/42",
        )
        return ci.with_jobs("o/r", base)

    def test_the_failed_job_is_named(self, runs) -> None:  # type: ignore[no-untyped-def]
        run = self._run_with_jobs(runs)
        assert [job.name for job in run.failed_jobs] == ["verify"]

    def test_the_failed_step_is_named(self, runs) -> None:  # type: ignore[no-untyped-def]
        run = self._run_with_jobs(runs)
        assert [step.name for step in run.failed_jobs[0].failed_steps] == ["tests"]

    def test_a_skipped_step_is_not_a_failed_step(self, runs) -> None:  # type: ignore[no-untyped-def]
        """A skipped step is the normal state of a conditional job, and calling it a
        failure would make every green run look broken."""
        run = self._run_with_jobs(runs)
        names = [step.name for step in run.failed_jobs[0].failed_steps]
        assert "offline demo" not in names

    def test_a_skipped_job_is_not_a_failed_job(self, runs) -> None:  # type: ignore[no-untyped-def]
        runs["/repos/o/r/actions/runs/7/jobs"] = {
            "jobs": [{"name": "model gate", "status": "completed", "conclusion": "skipped"}]
        }
        base = ci.RunResult(
            id=7,
            workflow="ci",
            sha=FULL_SHA,
            branch="b",
            status="completed",
            conclusion="success",
            title="t",
            url="u",
        )
        assert ci.with_jobs("o/r", base).failed_jobs == ()

    def test_the_description_says_where_the_logs_are(self, runs) -> None:  # type: ignore[no-untyped-def]
        """Job logs need a token this environment does not have, so the tool has to say
        so rather than appear to have looked."""
        text = ci.describe(self._run_with_jobs(runs))
        assert "job verify: failure" in text
        assert "failed step: tests" in text
        assert "make verify" in text
        assert "https://example.invalid/run/42" in text


class TestItSurvivesTheApiBeingUnhelpful:
    def test_jobs_missing_from_the_response_leave_the_run_usable(self, runs) -> None:  # type: ignore[no-untyped-def]
        """A run with no job detail is still a pass or a failure, and losing the verdict
        over a missing list would turn a green build into an error."""
        runs["/repos/o/r/actions/runs/42/jobs"] = {"jobs": None}
        base = ci.RunResult(
            id=42,
            workflow="ci",
            sha=FULL_SHA,
            branch="b",
            status="completed",
            conclusion="success",
            title="t",
            url="u",
        )
        assert ci.with_jobs("o/r", base).passed

    def test_a_job_entry_that_is_not_an_object_is_skipped(self, runs) -> None:  # type: ignore[no-untyped-def]
        runs["/repos/o/r/actions/runs/42/jobs"] = {"jobs": ["not an object", {"name": "verify"}]}
        base = ci.RunResult(
            id=42,
            workflow="ci",
            sha=FULL_SHA,
            branch="b",
            status="completed",
            conclusion="success",
            title="t",
            url="u",
        )
        assert [job.name for job in ci.with_jobs("o/r", base).jobs] == ["verify"]
