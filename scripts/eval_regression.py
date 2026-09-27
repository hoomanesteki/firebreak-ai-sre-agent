"""Run a stub-mode subset twice and prove the harness is deterministic.

SPEC.md Section 9.6 asks CI to run a 30-task regression subset in `stub` mode
"for correctness of the harness (deterministic)". This is that check, and the
determinism is the whole point rather than a nice property.

**Why determinism is the thing worth asserting.** A stub run involves no model and
no network, so the only way two runs can differ is if the harness itself is
nondeterministic: a set iterated in hash order, a dict whose insertion order
depends on a filesystem listing, a tie broken by object identity. Every one of
those is invisible in a single run and fatal to the measurements this project
exists to produce. `pass^3` measures reliability, and a harness that shuffles
would make it measure the shuffling. An interval computed over a bootstrap of
unstable scores would be narrower or wider for no reason a reader could see.

**What this does not check.** Whether the answers are right. That is what the
graders and the eval reports are for. This checks only that the same input
produces the same output, which is the precondition for any of those numbers
meaning anything.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from firebreak.evals.runner import CONFIGURATIONS, RunnerError, run_configuration  # noqa: E402
from firebreak.lab.scenario import Split  # noqa: E402

# SPEC.md Section 9.6 names thirty. Enough that a nondeterminism in one family
# shows up, small enough to run on every pull request.
DEFAULT_TASKS = 30

# Configurations worth checking for determinism. B0 has no sampling at all, so it
# is the strictest case and the one most likely to expose a harness problem rather
# than a stub problem. FB exercises the whole graph.
DEFAULT_CONFIGURATIONS = ("b0", "fb-v1")


@dataclass(frozen=True)
class Difference:
    """One field that changed between two runs of the same task."""

    bundle_id: str
    field: str
    first: object
    second: object

    def __str__(self) -> str:
        return f"{self.bundle_id} {self.field}: {self.first!r} then {self.second!r}"


def fingerprint(outcome: object) -> dict[str, object]:
    """The parts of an outcome that must not vary between identical runs.

    Wall clock and elapsed time are excluded, because they vary by design and
    comparing them would make this check fail constantly for the one reason that
    does not matter. Everything else is either a measurement or an ordering, and
    both have to be stable.
    """
    return {
        "root_cause_service": getattr(outcome, "root_cause_service", None),
        "ranked_candidates": tuple(getattr(outcome, "ranked_candidates", ()) or ()),
        "abstained": getattr(outcome, "abstained", None),
        "fault_class": getattr(outcome, "fault_class", None),
        "confidence": getattr(outcome, "confidence", None),
        # Sorted, because the order evidence is gathered in is not part of the
        # answer and a tool plan may legitimately reorder. What must not change is
        # which evidence was gathered.
        "cited_evidence": tuple(sorted(getattr(outcome, "cited_evidence", ()) or ())),
        "tool_calls": getattr(outcome, "tool_calls", None),
    }


def compare(first: object, second: object, bundle_id: str) -> list[Difference]:
    left = fingerprint(first)
    right = fingerprint(second)
    return [
        Difference(bundle_id, field, left[field], right[field])
        for field in sorted(left)
        if left[field] != right[field]
    ]


def check(configuration: str, split: Split, tasks: int) -> list[Difference]:
    """Run one configuration twice over the same tasks and diff the outcomes.

    Each run gets its own workspace, which matters: a shared one would let the
    second run read fixtures the first built, so a nondeterminism in fixture
    generation would be hidden by the very caching that made it reproducible.
    """
    runs = []
    for index in range(2):
        with tempfile.TemporaryDirectory(prefix=f"firebreak-regression-{index}-") as workspace:
            runs.append(
                run_configuration(
                    configuration,
                    split,
                    Path(workspace),
                    trials_per_task=1,
                    limit=tasks,
                )
            )
    first, second = runs

    by_bundle_first = {trial.bundle_id: trial.outcome for trial in first.trials}
    by_bundle_second = {trial.bundle_id: trial.outcome for trial in second.trials}

    differences: list[Difference] = []
    if set(by_bundle_first) != set(by_bundle_second):
        only_first = sorted(set(by_bundle_first) - set(by_bundle_second))
        only_second = sorted(set(by_bundle_second) - set(by_bundle_first))
        # A different set of tasks between two runs is the worst version of this
        # failure: every per-task number would then be an average over a different
        # population, and nothing downstream could notice.
        differences.append(Difference("task selection", "bundles", only_first, only_second))
        return differences

    for bundle_id, outcome in sorted(by_bundle_first.items()):
        differences.extend(compare(outcome, by_bundle_second[bundle_id], bundle_id))
    return differences


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        action="append",
        default=[],
        help="configuration to check, repeatable; default b0 and fb-v1",
    )
    parser.add_argument("--split", default="validation", help="split to draw tasks from")
    parser.add_argument("--tasks", type=int, default=DEFAULT_TASKS, help="how many tasks")
    arguments = parser.parse_args()

    configurations = tuple(arguments.config) or DEFAULT_CONFIGURATIONS
    unknown = [name for name in configurations if name not in CONFIGURATIONS]
    if unknown:
        print(
            f"unknown configuration(s) {', '.join(unknown)}; known: "
            f"{', '.join(sorted(CONFIGURATIONS))}",
            file=sys.stderr,
        )
        return 2

    try:
        split = Split(arguments.split)
    except ValueError:
        print(
            f"unknown split {arguments.split!r}; known: {', '.join(s.value for s in Split)}",
            file=sys.stderr,
        )
        return 2

    failures = 0
    for configuration in configurations:
        print(f"{configuration} on {split.value}, {arguments.tasks} task(s), twice")
        try:
            differences = check(configuration, split, arguments.tasks)
        except RunnerError as error:
            print(f"  could not run: {error}", file=sys.stderr)
            return 2
        if not differences:
            print("  identical")
            continue
        failures += 1
        sys.stdout.flush()
        print(f"  {len(differences)} difference(s) between two identical runs:", file=sys.stderr)
        for difference in differences[:10]:
            print(f"    {difference}", file=sys.stderr)

    if failures:
        print(
            "\nThe harness is not deterministic in stub mode. Every measurement this "
            "project produces assumes it is: pass^3 would measure the shuffling, and a "
            "bootstrap interval would widen or narrow for no reason a reader could see.",
            file=sys.stderr,
        )
        return 1

    print("\nstub regression: every configuration reproduced itself exactly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
