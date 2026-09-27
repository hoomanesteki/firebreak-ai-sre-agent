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
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
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


def head_sha() -> str:
    return _git(["rev-parse", "HEAD"], "could not read HEAD")


def find_run(repo: str, sha: str) -> RunResult | None:
    """The most recent run for one commit, or None if none exists yet."""
    payload = _get(f"/repos/{repo}/actions/runs?per_page=30")
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise CiError("the runs endpoint returned no workflow_runs list")
    for raw in runs:
        if not isinstance(raw, dict) or raw.get("head_sha") != sha:
            continue
        conclusion = raw.get("conclusion")
        return RunResult(
            id=int(raw["id"]),
            sha=str(raw["head_sha"]),
            branch=str(raw.get("head_branch") or "unknown"),
            status=str(raw.get("status") or "unknown"),
            conclusion=str(conclusion) if conclusion else None,
            title=str(raw.get("display_title") or ""),
            url=str(raw.get("html_url") or ""),
        )
    return None


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
        sha=run.sha,
        branch=run.branch,
        status=run.status,
        conclusion=run.conclusion,
        title=run.title,
        url=run.url,
        jobs=tuple(jobs),
    )


def describe(run: RunResult) -> str:
    """What CI said, in the form a reader can act on."""
    lines = [
        f"{run.sha[:8]} on {run.branch}: {run.status}"
        + (f" ({run.conclusion})" if run.conclusion else ""),
        f"  {run.title}",
    ]
    for job in run.jobs:
        lines.append(f"  job {job.name}: {job.conclusion or job.status}")
        for step in job.failed_steps:
            lines.append(f"    failed step: {step.name}")
    if run.failed_jobs:
        lines.append("")
        lines.append(
            "Job logs need a token this environment does not have. Reproduce the "
            "failing step locally: the commands are in .github/workflows/ci.yml, "
            "and make verify covers the verify job."
        )
        lines.append(f"  full logs: {run.url}")
    return "\n".join(lines)


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
    while True:
        try:
            found = find_run(repo, sha)
        except CiError as error:
            print(f"{error}", file=sys.stderr)
            return 2

        if found is None:
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
            run = with_jobs(repo, found)
        except CiError as error:
            print(f"{error}", file=sys.stderr)
            return 2

        if run.finished:
            print(describe(run))
            return 0 if run.passed else 1

        if not arguments.watch:
            print(describe(run))
            print("still running; add --watch to wait")
            return 3

        if time.monotonic() >= deadline:
            print(describe(run))
            print(f"still running after {arguments.timeout}s, giving up waiting")
            return 3

        print(f"{run.status}, checking again in {POLL_SECONDS}s", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
