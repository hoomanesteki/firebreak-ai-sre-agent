"""Record the incident library from the running stack, resumably.

The whole library is about thirty six hours of wall clock, because a scenario
takes as long as it takes: eighteen minutes of warmup, fault and cooldown that
cannot be compressed, since the baseline, the incident and the recovery all
have to actually happen.

So this is built to be interrupted. It skips anything already recorded, records
in a priority order chosen so partial progress is useful at every point, and
writes its state after each scenario rather than at the end.

**Priority order, and why.** Validation and train first, then the held out
splits. That is the reverse of the obvious order and the reason is in
`SPLIT_ORDER`: thresholds tuned on synthetic fixtures do not transfer to real
telemetry, so a held out recording made before they are re-tuned scores zero and
measures nothing.

Run with `make lab-record-library`. Stop it with Ctrl-C and run it again
whenever; it picks up where it left off.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _reporting import describe_path
from firebreak.lab.bundle import derive_bundle_id
from firebreak.lab.scenario import ScenarioSpec, Split, load_library

REPO_ROOT = Path(__file__).resolve().parent.parent
SPECS_DIR = REPO_ROOT / "scenarios" / "specs"
BUNDLES_DIR = REPO_ROOT / "bundles"
PROGRESS_PATH = REPO_ROOT / "reports" / "lab" / "recording_progress.json"

DEFAULT_RUN = "run1"

# Tunable splits first, which is the reverse of the obvious order, and the
# first real recording is what changed it.
#
# The obvious order is held out splits first, since they are the only ones whose
# numbers may be quoted. That was the original order here and it is wrong,
# because a quotable number from a miscalibrated system is zero and measures
# nothing about the system.
#
# The first recorded incident showed why. Deterministic triage ranked the true
# culprit first on real telemetry, and then abstained, because the abstention
# threshold was tuned on synthetic fixtures whose anomaly magnitudes are several
# times larger than real ones. Every held out recording would have scored zero
# until that threshold was re-tuned, and it can only be re-tuned on validation
# and train.
#
# So: validation first because it is the split SPEC.md Section 9 designates for
# tuning and the smallest at fifteen scenarios, then train, then the held out
# splits once the numbers they produce can mean something.
SPLIT_ORDER: tuple[Split, ...] = (
    Split.VALIDATION,
    Split.TRAIN,
    Split.TEST_ID,
    Split.TEST_OOD,
)

# A recording failure is usually transient: the stack is briefly busy, a
# healthcheck flaps. Retried once, then left for the next run rather than
# retried forever, because a scenario that fails twice is a scenario with a
# problem and eighteen minutes is too long to spend rediscovering that.
ATTEMPTS_PER_SCENARIO = 2

# Between recordings, so the previous fault has drained out of the metric
# windows the next one will use as its baseline. The recorder resets flags
# itself; this is about the rate windows, which are two minutes wide.
SETTLE_SECONDS = 150


@dataclass
class Progress:
    """What has been recorded, and what went wrong."""

    recorded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    started_at: str = ""
    updated_at: str = ""
    minutes_spent: float = 0.0

    @classmethod
    def load(cls, path: Path = PROGRESS_PATH) -> Progress:
        if not path.is_file():
            return cls(started_at=datetime.now(UTC).isoformat())
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            recorded=list(raw.get("recorded", [])),
            failed=dict(raw.get("failed", {})),
            started_at=str(raw.get("started_at", "")),
            updated_at=str(raw.get("updated_at", "")),
            minutes_spent=float(raw.get("minutes_spent", 0.0)),
        )

    def save(self, path: Path = PROGRESS_PATH) -> None:
        """Written after every scenario, not at the end.

        A run that is interrupted after four hours must not lose the record of
        what it recorded, and the bundles on disk are the truth anyway: this
        file only saves re-checking them.
        """
        self.updated_at = datetime.now(UTC).isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "recorded": sorted(self.recorded),
                    "failed": dict(sorted(self.failed.items())),
                    "started_at": self.started_at,
                    "updated_at": self.updated_at,
                    "minutes_spent": round(self.minutes_spent, 1),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


def already_recorded(spec: ScenarioSpec, run: str) -> bool:
    """True when a complete bundle for this scenario and run is on disk.

    The filesystem rather than the progress file is the authority. A progress
    file can be stale or deleted; a bundle either exists or it does not.

    **A manifest, not just a directory.** The first batch run found the
    difference: one export failed partway and left a directory holding a single
    parquet file, and a check for the directory alone would have treated that as
    a finished recording and skipped the scenario for ever, leaving a silent gap
    in the library. The recorder now stages and moves atomically so a partial
    bundle cannot appear, and this asks for the manifest anyway, because the
    cost of being wrong is eighteen minutes of a scenario that never gets
    recorded.
    """
    return (BUNDLES_DIR / derive_bundle_id(spec.id, run) / "manifest.json").is_file()


def ordered_scenarios(splits: tuple[Split, ...] = SPLIT_ORDER) -> list[ScenarioSpec]:
    """Every scenario, in recording priority order.

    Sorted by id within a split so two runs agree about what comes next, which
    matters for resumability: an interrupted run should continue rather than
    start somewhere new.
    """
    library = load_library(SPECS_DIR)
    ordered: list[ScenarioSpec] = []
    for split in splits:
        ordered.extend(
            sorted((s for s in library.values() if s.split is split), key=lambda s: s.id)
        )
    return ordered


def record_one(spec: ScenarioSpec, run: str) -> tuple[bool, str]:
    """Record one scenario through the CLI, returning success and any message.

    Through a subprocess rather than by importing the recorder, for one reason
    that matters: a scenario is eighteen minutes of a live stack, and a crash
    or a memory problem in one recording must not take down the loop that has
    another thirty five hours to go.
    """
    command = [
        sys.executable,
        "-m",
        "firebreak.cli.app",
        "lab",
        "record",
        "--spec",
        spec.id,
        "--run",
        run,
    ]
    environment = {"PYTHONPATH": str(REPO_ROOT / "src")}
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={**_inherited_environment(), **environment},
            # Generous: the scenario itself is eighteen minutes and the export
            # afterwards pulls several thousand spans. Half an hour is a hang,
            # not a slow recording.
            timeout=1800,
        )
    except subprocess.TimeoutExpired:
        return False, "timed out after 30 minutes"
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()
        return False, tail[-1] if tail else f"exit code {result.returncode}"
    return True, ""


def _inherited_environment() -> dict[str, str]:
    return dict(os.environ)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", default=DEFAULT_RUN, help="run id for every recording")
    parser.add_argument("--limit", type=int, default=0, help="stop after N recordings this session")
    parser.add_argument(
        "--split",
        default="",
        help="only this split, for example test_ood; default is every split in priority order",
    )
    parser.add_argument(
        "--settle",
        type=int,
        default=SETTLE_SECONDS,
        help="seconds to wait between recordings so rate windows drain",
    )
    arguments = parser.parse_args()

    splits = SPLIT_ORDER
    if arguments.split:
        try:
            splits = (Split(arguments.split),)
        except ValueError:
            print(
                f"unknown split {arguments.split!r}; known: {', '.join(s.value for s in Split)}",
                file=sys.stderr,
            )
            return 2

    scenarios = ordered_scenarios(splits)
    progress = Progress.load()

    outstanding = [s for s in scenarios if not already_recorded(s, arguments.run)]
    done = len(scenarios) - len(outstanding)
    estimate_hours = sum(s.timing.total_seconds for s in outstanding) / 3600
    print(
        f"{len(scenarios)} scenarios in scope, {done} already recorded, "
        f"{len(outstanding)} to go, about {estimate_hours:.1f} hours of recording"
    )
    if arguments.limit:
        outstanding = outstanding[: arguments.limit]
        print(f"limited to {len(outstanding)} this session")

    for index, spec in enumerate(outstanding, start=1):
        minutes = spec.timing.total_seconds / 60
        print(
            f"[{index}/{len(outstanding)}] {spec.id} ({spec.split.value}, {minutes:.0f} min)",
            flush=True,
        )
        started = time.monotonic()
        for attempt in range(1, ATTEMPTS_PER_SCENARIO + 1):
            ok, message = record_one(spec, arguments.run)
            if ok:
                progress.recorded.append(spec.id)
                progress.failed.pop(spec.id, None)
                print(f"    recorded in {(time.monotonic() - started) / 60:.1f} min")
                break
            if attempt < ATTEMPTS_PER_SCENARIO:
                print(f"    attempt {attempt} failed: {message}; retrying")
                time.sleep(arguments.settle)
            else:
                progress.failed[spec.id] = message
                print(f"    failed: {message}", file=sys.stderr)
        progress.minutes_spent += (time.monotonic() - started) / 60
        progress.save()

        if index < len(outstanding) and arguments.settle:
            time.sleep(arguments.settle)

    print(
        f"\nrecorded {len(progress.recorded)}, failed {len(progress.failed)}, "
        f"{progress.minutes_spent / 60:.1f} hours spent in total"
    )
    if progress.failed:
        print("failures:", file=sys.stderr)
        for name, message in sorted(progress.failed.items()):
            print(f"  {name}: {message}", file=sys.stderr)
    print(f"progress in {describe_path(PROGRESS_PATH)}")
    return 1 if progress.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
