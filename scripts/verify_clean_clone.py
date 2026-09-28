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
    "Submodule drift tests, if the clone has no submodules.",
)


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


def working_tree_is_clean() -> bool:
    completed = subprocess.run(
        ("git", "status", "--porcelain"), cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    return completed.returncode == 0 and not completed.stdout.strip()


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
        "--submodules", action="store_true", help="initialise submodules, as CI does"
    )
    arguments = parser.parse_args()

    if not working_tree_is_clean() and not arguments.allow_dirty:
        print(
            "the working tree has uncommitted changes, so a clone would not contain them.\n"
            "Commit first, or pass --allow-dirty to check the committed state anyway.",
            file=sys.stderr,
        )
        return 2

    temporary = Path(tempfile.mkdtemp(prefix="firebreak-clean-"))
    clone = temporary / "clone"
    print(f"cloning the current commit into {clone}")
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

    if arguments.submodules:
        subprocess.run(
            ("git", "submodule", "update", "--init", "--recursive", "--quiet"),
            cwd=clone,
            check=False,
        )

    # A clean environment, because inherited FIREBREAK_ and LLM_ variables are exactly the
    # kind of local state this is looking for.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("FIREBREAK_", "LLM_"))
    }
    environment["PYTHONPATH"] = str(clone / "src")

    results = [run("Install", ("uv", "sync", "--frozen"), clone, environment)]
    if results[0].code == 0:
        for name, command in STEPS:
            result = run(name, command, clone, environment)
            results.append(result)
            print(f"  {'ok  ' if result.code == 0 else 'FAIL'} {name} ({result.seconds:.0f}s)")
            if result.code != 0:
                break
    else:
        print("  FAIL Install")

    failed = [result for result in results if result.code != 0]
    if failed:
        print(f"\n{failed[0].name} failed in a clean clone:\n")
        print("\n".join(failed[0].output.splitlines()[-40:]))

    print("\nnot covered here:")
    for line in NOT_COVERED:
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
