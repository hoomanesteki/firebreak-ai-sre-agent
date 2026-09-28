"""Run the checks CI runs, in a fresh clone, before pushing.

**Why this exists.** `make verify` runs against the working tree on the machine that built it,
and CI runs against a fresh clone. Everything git-ignored is the difference: eight recorded
bundles, a `.env`, a warm `.venv`, and any file that was written but never committed. A test
that reads one of those passes locally and fails in CI, which costs a push, a wait, and a
commit to fix.

That happened: a test compared the generated site stats against a fresh build including a
section derived from bundles on disk. Green here, red there.

This does what CI does. It clones the current commit into a temporary directory, installs from
the lock file, and runs the same steps in the same order. What it deliberately does not do is
start Neo4j, so the graph integration tests skip here and CI remains the only place they run.
It says so at the end rather than reporting a pass that covered less than it appears to.

Also the Phase 12 acceptance criterion in SPEC.md Section 17, mostly: a fresh clone runs the
offline demo. The owner still has to confirm it on another machine, because a clone on this one
shares the uv cache and the Python install.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The steps CI's verify job runs, in order, minus the ones that need a service container.
# Named the same way so a failure here points at the same line of ci.yml.
STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Lint", ("uv", "run", "ruff", "check", ".")),
    ("Format", ("uv", "run", "ruff", "format", "--check", ".")),
    ("Types darwin", ("uv", "run", "mypy", "--platform", "darwin", "src", "scripts", "site")),
    ("Types linux", ("uv", "run", "mypy", "--platform", "linux", "src", "scripts", "site")),
    ("Types win32", ("uv", "run", "mypy", "--platform", "win32", "src", "scripts", "site")),
    ("Test", ("uv", "run", "pytest", "-q", "--no-header")),
    ("Ground truth leakage", ("uv", "run", "python", "scripts/check_leakage.py")),
    ("Repository hygiene", ("uv", "run", "python", "scripts/check_repo_hygiene.py")),
    ("Spec conformance", ("uv", "run", "python", "scripts/check_spec_conformance.py")),
    ("Site stats", ("uv", "run", "python", "scripts/build_site_stats.py", "--check")),
    ("Site", ("uv", "run", "python", "site/build.py")),
    ("Offline demo", ("uv", "run", "python", "scripts/demo_offline.py")),
)

# Not run here, with the reason. A pass that silently covers less than CI is worse than no
# check at all, so these are printed every time rather than left implicit.
NOT_COVERED = (
    "Neo4j integration tests, which need the service container CI provides. They skip here.",
    "The dependency audit, which needs the network.",
)

# Variables that point at the original checkout and would make the clone use its virtualenv.
# uv warns about the mismatch and carries on, so without this the clone installs nothing and
# runs against the parent's environment, which is the opposite of the point.
LOCAL_ONLY_VARIABLES = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONPATH", "PYTHONHOME")

# Filled in at run time by whatever the flags turned off, so the closing report names every gap
# rather than only the permanent ones.
NOT_COVERED_EXTRA: list[str] = []

# The pinned target system. Named once, because the copy and the status check have to agree.
SUBMODULE = "vendor/otel-demo"


@dataclass
class Result:
    name: str
    code: int
    seconds: float
    output: str


def run(name: str, command: tuple[str, ...], cwd: Path, environment: dict[str, str]) -> Result:
    started = time.monotonic()
    completed = subprocess.run(
        command, cwd=cwd, env=environment, capture_output=True, text=True, check=False
    )
    return Result(
        name=name,
        code=completed.returncode,
        seconds=time.monotonic() - started,
        output=(completed.stdout + completed.stderr),
    )


def copy_submodule(clone: Path) -> str | None:
    """Put the pinned demo into the clone, from this checkout rather than from GitHub.

    Returns None on success, or why it could not, so the caller can report the gap.

    **Cloned from the local path rather than fetched.** `git submodule update --init` pulls the
    whole OpenTelemetry Demo over the network, which took minutes here and then died with a
    reset connection. This check is meant to run before every push, so it cannot depend on a
    large fetch succeeding.

    **A clone rather than a directory copy.** Copying the working tree was the first attempt and
    left two tests failing: they run `git describe --tags --exact-match` inside the submodule,
    and a copied tree's `.git` file points at the parent's module directory, which does not exist
    in the clone. Cloning from the local path brings the refs and tags with it, so `describe`
    answers.

    The recorded pin is checked out explicitly, because the local checkout could be sitting
    somewhere else and a check whose job is to notice drift must not quietly inherit it.
    """
    source = REPO_ROOT / SUBMODULE
    if not (source / ".git").exists():
        return f"{SUBMODULE} is not checked out here, so it could not be copied"

    pinned = subprocess.run(
        ("git", "rev-parse", f"HEAD:{SUBMODULE}"),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not pinned:
        return f"the recorded commit for {SUBMODULE} could not be read"

    say(f"  cloning {SUBMODULE} from this checkout at {pinned[:8]}, which needs no network")
    target = clone / SUBMODULE
    target.parent.mkdir(parents=True, exist_ok=True)
    cloned = subprocess.run(
        ("git", "clone", "--no-hardlinks", "--quiet", str(source), str(target)),
        capture_output=True,
        text=True,
        check=False,
    )
    if cloned.returncode != 0:
        return f"cloning it from the local path failed: {cloned.stderr.strip()[:200]}"

    checked_out = subprocess.run(
        ("git", "checkout", "--quiet", pinned),
        cwd=target,
        capture_output=True,
        text=True,
        check=False,
    )
    if checked_out.returncode != 0:
        return f"the recorded commit {pinned[:8]} could not be checked out in the clone"
    return None


def working_tree_is_clean() -> bool:
    completed = subprocess.run(
        ("git", "status", "--porcelain"), cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    return completed.returncode == 0 and not completed.stdout.strip()


def say(message: str) -> None:
    """Print and flush.

    Flushed because this takes minutes and Python buffers stdout when it is redirected, so
    without it a run into a log file shows nothing at all until the end and looks hung.
    """
    print(message, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="clone anyway; the clone still gets committed state only, so uncommitted work "
        "is not tested",
    )
    parser.add_argument("--keep", action="store_true", help="leave the clone for inspection")
    parser.add_argument(
        "--no-submodules",
        action="store_true",
        help="skip submodule init. Faster, and about fourteen tests then fail rather than "
        "skip, because they read the pinned demo's own files on purpose",
    )
    arguments = parser.parse_args()
    NOT_COVERED_EXTRA.clear()

    if not working_tree_is_clean() and not arguments.allow_dirty:
        print(
            "the working tree has uncommitted changes, so a clone would not contain them.\n"
            "Commit first, or pass --allow-dirty to check the committed state anyway.",
            file=sys.stderr,
        )
        return 2

    temporary = Path(tempfile.mkdtemp(prefix="firebreak-clean-"))
    clone = temporary / "clone"
    say(f"cloning the current commit into {clone}")
    # --no-hardlinks so the clone cannot share object files with the original, and no
    # --shared for the same reason. This is meant to behave like somebody else's machine.
    cloned = subprocess.run(
        ("git", "clone", "--no-hardlinks", "--quiet", str(REPO_ROOT), str(clone)),
        capture_output=True,
        text=True,
        check=False,
    )
    if cloned.returncode != 0:
        print(cloned.stdout + cloned.stderr, file=sys.stderr)
        shutil.rmtree(temporary, ignore_errors=True)
        return 2

    head = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=REPO_ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()
    subprocess.run(("git", "checkout", "--quiet", head), cwd=clone, check=False)

    # On by default, because CI checks out with submodules and roughly fourteen tests read the
    # pinned demo's own files deliberately, so that they fail when the pin moves and something
    # upstream changed underneath. Without the submodule those tests fail rather than skip, and
    # a check that is always red is a check nobody runs.
    if arguments.no_submodules:
        NOT_COVERED_EXTRA.append(
            "Submodule drift tests: the clone has no submodules, so they fail rather than skip."
        )
    else:
        reason = copy_submodule(clone)
        if reason is not None:
            NOT_COVERED_EXTRA.append(f"Submodule drift tests: {reason}")

    # A clean environment, because inherited FIREBREAK_ and LLM_ variables are exactly the
    # kind of local state this is looking for.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("FIREBREAK_", "LLM_")) and key not in LOCAL_ONLY_VARIABLES
    }
    environment["PYTHONPATH"] = str(clone / "src")

    results = [run("Install", ("uv", "sync", "--frozen"), clone, environment)]
    if results[0].code == 0:
        for name, command in STEPS:
            result = run(name, command, clone, environment)
            results.append(result)
            say(f"  {'ok  ' if result.code == 0 else 'FAIL'} {name} ({result.seconds:.0f}s)")
            if result.code != 0:
                break
    else:
        say("  FAIL Install")

    failed = [result for result in results if result.code != 0]
    if failed:
        print(f"\n{failed[0].name} failed in a clean clone:\n")
        print("\n".join(failed[0].output.splitlines()[-40:]))

    print("\nnot covered here:")
    for line in (*NOT_COVERED, *NOT_COVERED_EXTRA):
        print(f"  {line}")

    if arguments.keep:
        print(f"\nclone kept at {clone}")
    else:
        shutil.rmtree(temporary, ignore_errors=True)

    if failed:
        return 1
    print("\na clean clone passes every check this script runs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
