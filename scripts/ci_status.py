"""Report what CI said about a commit, without needing a GitHub token.

Pushing and walking away is how a red build survives for a day. This makes the
check one command, and makes the answer specific enough to act on: which job,
which step, and whether the run has even started yet.

**Why the unauthenticated API.** `gh` needs a login this environment does not
have. The repository is public, so the runs and jobs endpoints answer without a
token. Job *logs* do not, which shapes what this tool can do: it names the
failing step and then says to reproduce it locally, because that is the only
route to the actual error message from here. That is not much of a loss. A CI
failure a developer cannot reproduce locally is a worse problem than the failure.

Anonymous callers get 60 requests an hour, which is why `--watch` polls slowly
and says how long it will wait.
"""

from __future__ import annotations

import argparse
import http.client
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"

# One poll costs two requests, so a check every 30 seconds would exhaust the
# anonymous hourly budget in fifteen minutes and then report rate limits as
# failures.
POLL_SECONDS = 45
DEFAULT_TIMEOUT_SECONDS = 900

# A run that has not appeared yet is not a pass. GitHub takes a few seconds to
# create one after a push, and a tool reporting "no run, all clear" would be
# worse than useless.
STARTUP_GRACE_SECONDS = 90

# How many consecutive network failures a watch tolerates before giving up. A watch runs
# for minutes and one connection reset should not end it: the run is still going and the
# answer is still coming. Bounded, so a genuinely unreachable API is reported rather than
# polled for ever.
MAX_TRANSIENT_FAILURES = 4


class CiError(Exception):
    """CI could not be asked, or answered something unusable."""


@dataclass(frozen=True)
class StepResult:
    name: str
    conclusion: str | None

    @property
    def failed(self) -> bool:
        return self.conclusion in {"failure", "timed_out", "cancelled"}


@dataclass(frozen=True)
class JobResult:
    name: str
    status: str
    conclusion: str | None
    steps: tuple[StepResult, ...]

    @property
    def failed_steps(self) -> tuple[StepResult, ...]:
        return tuple(step for step in self.steps if step.failed)


@dataclass(frozen=True)
class RunResult:
    """One CI run, as much of it as the anonymous API will say."""

    id: int
    # Which workflow produced it. Named, because "did this commit pass" has more than one
    # answer in this repository and a report that does not say which workflow it describes
    # invites the reader to assume it covers all of them.
    workflow: str
    sha: str
    branch: str
    status: str
    conclusion: str | None
    title: str
    url: str
    jobs: tuple[JobResult, ...] = ()

    @property
    def finished(self) -> bool:
        return self.status == "completed"

    @property
    def passed(self) -> bool:
        return self.finished and self.conclusion == "success"

    @property
    def superseded(self) -> bool:
        """Cancelled, which on this repository nearly always means a newer push took over.

        `ci.yml` sets `cancel-in-progress` on a concurrency group keyed by ref, so pushing a
        second commit kills the first commit's run mid-step. That is the setting working, and
        reporting it as a failure sends a reader to look at a build that was never going to
        finish. A check that cries wolf is a check nobody runs.

        It cannot distinguish a superseding push from somebody pressing cancel, and it does not
        try: neither is a failure of the code, and both mean the same thing to a reader, which
        is that this commit has no verdict and a newer one should be checked.
        """
        return self.finished and self.conclusion == "cancelled"

    @property
    def failed_jobs(self) -> tuple[JobResult, ...]:
        return tuple(job for job in self.jobs if job.conclusion not in {"success", "skipped", None})


def _get(path: str) -> dict[str, object]:
    request = urllib.request.Request(
        f"{API}{path}", headers={"Accept": "application/vnd.github+json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 403:
            raise CiError(
                "the GitHub API refused the request, most likely the anonymous rate "
                "limit of 60 an hour; wait and try again"
            ) from error
        raise CiError(f"GitHub returned {error.code} for {path}") from error
    except (
        urllib.error.URLError,
        OSError,
        json.JSONDecodeError,
        # A truncated response raises IncompleteRead, which inherits from HTTPException
        # and ValueError and from neither OSError nor URLError. It reached a user as a
        # traceback once, which is the wrong answer for a transient network fault: the
        # useful response is to say so and try again.
        http.client.HTTPException,
    ) as error:
        raise CiError(f"could not reach the GitHub API: {error}") from error
    if not isinstance(payload, dict):
        raise CiError(f"unexpected response shape from {path}")
    return payload


def _git(arguments: list[str], failure: str) -> str:
    try:
        return subprocess.run(
            ["git", *arguments],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
            timeout=10,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError) as error:
        raise CiError(failure) from error


def repository() -> str:
    """`owner/name` from the git remote, so this works in a fork."""
    url = _git(["remote", "get-url", "origin"], "no git remote named origin to ask about")
    # Both forms: git@github.com:owner/name.git and https://github.com/owner/name
    body = url.split("github.com", 1)[-1].lstrip(":/")
    return body.removesuffix(".git")


# Short enough for a person to copy from `git log`, long enough not to match two commits
# in a repository this size. Git's own default abbreviation is 7.
SHORTEST_SHA = 7


def head_sha() -> str:
    return _git(["rev-parse", "HEAD"], "could not read HEAD")


def find_runs(repo: str, sha: str) -> list[RunResult]:
    """Every workflow run for one commit, newest first, or an empty list if none exists yet.

    **Every run, not the newest one.** This repository has more than one workflow: `ci`, and
    `site` on pushes to main. Returning only the most recent meant a green site deployment
    could report a pass while `ci` had failed on the same commit, because the site workflow
    finished later. A tool whose whole job is "did this commit pass" cannot answer for one
    workflow and be read as answering for all of them.

    Matches by prefix, because a short SHA is what a person copies out of `git log` and an
    exact comparison silently found nothing and reported it as "no run yet". That read exactly
    like CI not having started, which is the one thing this tool exists to distinguish from a
    failure.
    """
    payload = _get(f"/repos/{repo}/actions/runs?per_page=30")
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise CiError("the runs endpoint returned no workflow_runs list")
    if len(sha) < SHORTEST_SHA:
        raise CiError(f"{sha!r} is too short to identify a commit, give at least {SHORTEST_SHA}")
    found: list[RunResult] = []
    seen: set[str] = set()
    for raw in runs:
        if not isinstance(raw, dict) or not str(raw.get("head_sha") or "").startswith(sha):
            continue
        workflow = str(raw.get("name") or "unknown")
        # One entry per workflow: a re-run appears as a newer run of the same workflow, and
        # the newer one is the answer. The list arrives newest first.
        if workflow in seen:
            continue
        seen.add(workflow)
        conclusion = raw.get("conclusion")
        found.append(
            RunResult(
                id=int(raw["id"]),
                workflow=workflow,
                sha=str(raw["head_sha"]),
                branch=str(raw.get("head_branch") or "unknown"),
                status=str(raw.get("status") or "unknown"),
                conclusion=str(conclusion) if conclusion else None,
                title=str(raw.get("display_title") or ""),
                url=str(raw.get("html_url") or ""),
            )
        )
    return found


def find_run(repo: str, sha: str) -> RunResult | None:
    """The newest run for one commit, kept for callers that want one.

    Prefer `find_runs`: this cannot see a second workflow failing.
    """
    runs = find_runs(repo, sha)
    return runs[0] if runs else None


def with_jobs(repo: str, run: RunResult) -> RunResult:
    """The same run, with its jobs and their steps filled in."""
    payload = _get(f"/repos/{repo}/actions/runs/{run.id}/jobs")
    raw_jobs = payload.get("jobs")
    if not isinstance(raw_jobs, list):
        return run
    jobs = []
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            continue
        steps = tuple(
            StepResult(
                name=str(step.get("name") or "?"),
                conclusion=str(step["conclusion"]) if step.get("conclusion") else None,
            )
            for step in raw.get("steps") or []
            if isinstance(step, dict)
        )
        conclusion = raw.get("conclusion")
        jobs.append(
            JobResult(
                name=str(raw.get("name") or "?"),
                status=str(raw.get("status") or "unknown"),
                conclusion=str(conclusion) if conclusion else None,
                steps=steps,
            )
        )
    return RunResult(
        id=run.id,
        workflow=run.workflow,
        sha=run.sha,
        branch=run.branch,
        status=run.status,
        conclusion=run.conclusion,
        title=run.title,
        url=run.url,
        jobs=tuple(jobs),
    )


def describe(run: RunResult) -> str:
    """What one workflow said, in the form a reader can act on."""
    lines = [
        f"{run.sha[:8]} on {run.branch}, workflow {run.workflow}: {run.status}"
        + (f" ({run.conclusion})" if run.conclusion else ""),
        f"  {run.title}",
    ]
    for job in run.jobs:
        lines.append(f"  job {job.name}: {job.conclusion or job.status}")
        for step in job.failed_steps:
            # A cancelled run's in-flight step reports itself as failed, which it did not: it
            # was interrupted. Calling it a failed step sends a reader to debug a step that
            # never finished.
            label = "interrupted at" if run.superseded else "failed step:"
            lines.append(f"    {label} {step.name}")
    if run.failed_jobs and not run.superseded:
        lines.append("")
        lines.append(
            "Job logs need a token this environment does not have. Reproduce the "
            "failing step locally: the commands are in .github/workflows/ci.yml, "
            "and make verify covers the verify job."
        )
        lines.append(f"  full logs: {run.url}")
    return "\n".join(lines)


def describe_all(runs: list[RunResult]) -> str:
    """Every workflow for the commit, so a pass means every one of them passed."""
    parts = [describe(run) for run in runs]
    failing = [
        run.workflow for run in runs if run.finished and not run.passed and not run.superseded
    ]
    superseded = [run.workflow for run in runs if run.superseded]
    if failing:
        parts.append("")
        parts.append(f"failed workflow(s): {', '.join(sorted(failing))}")
    if superseded:
        parts.append("")
        parts.append(
            f"cancelled workflow(s): {', '.join(sorted(superseded))}. "
            "ci.yml cancels a run in progress when a newer commit is pushed to the same ref, "
            "so this commit has no verdict. Check the newer commit."
        )
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", default="", help="commit to ask about, default HEAD")
    parser.add_argument("--watch", action="store_true", help="poll until the run finishes")
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="seconds to wait when watching",
    )
    arguments = parser.parse_args()

    try:
        repo = repository()
        sha = arguments.sha or head_sha()
    except CiError as error:
        print(f"{error}", file=sys.stderr)
        return 2

    deadline = time.monotonic() + arguments.timeout
    waited_for_start = 0.0
    transient = 0
    while True:
        try:
            found = find_runs(repo, sha)
        except CiError as error:
            # A watch tolerates a few network failures, because the run it is watching is
            # still going and the answer is still coming. A single check does not: there
            # is nothing to wait for.
            transient += 1
            if not arguments.watch or transient > MAX_TRANSIENT_FAILURES:
                print(f"{error}", file=sys.stderr)
                return 2
            print(f"{error}; retrying in {POLL_SECONDS}s", flush=True)
            time.sleep(POLL_SECONDS)
            continue
        transient = 0

        if not found:
            if not arguments.watch or waited_for_start >= STARTUP_GRACE_SECONDS:
                print(
                    f"no CI run for {sha[:8]} yet. A missing run is not a pass: check "
                    "the commit is pushed and the branch matches the workflow's "
                    "trigger list.",
                    file=sys.stderr,
                )
                return 1
            print(f"no run for {sha[:8]} yet, waiting {POLL_SECONDS}s", flush=True)
            time.sleep(POLL_SECONDS)
            waited_for_start += POLL_SECONDS
            continue

        try:
            runs = [with_jobs(repo, run) for run in found]
        except CiError as error:
            transient += 1
            if not arguments.watch or transient > MAX_TRANSIENT_FAILURES:
                print(f"{error}", file=sys.stderr)
                return 2
            print(f"{error}; retrying in {POLL_SECONDS}s", flush=True)
            time.sleep(POLL_SECONDS)
            continue

        unfinished = [run for run in runs if not run.finished]

        # Every workflow, not the first one to finish. A commit has passed only when all of
        # them have, and a failure anywhere is the answer even while something else is still
        # running: waiting for a green workflow to join a red one wastes the wait.
        if any(run.finished and not run.passed and not run.superseded for run in runs):
            print(describe_all(runs))
            return 1

        if not unfinished:
            print(describe_all(runs))
            # A cancelled run is not a pass and not a failure: it has no verdict. Exit 3, the
            # same code as "still running", because both mean the caller has not got an answer
            # yet rather than that something is broken.
            return 3 if any(run.superseded for run in runs) else 0

        if not arguments.watch:
            print(describe_all(runs))
            names = ", ".join(run.workflow for run in unfinished)
            print(f"still running ({names}); add --watch to wait")
            return 3

        if time.monotonic() >= deadline:
            print(describe_all(runs))
            print(f"still running after {arguments.timeout}s, giving up waiting")
            return 3

        names = ", ".join(f"{run.workflow} {run.status}" for run in unfinished)
        print(f"{names}, checking again in {POLL_SECONDS}s", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
