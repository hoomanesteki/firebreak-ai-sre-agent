"""The offline demo: replay a recorded investigation with no model and no network.

SPEC.md Section 17 Phase 11 requires this to be green, and CLAUDE.md requires it from
this phase onward.

**What it actually proves, which is narrower than it sounds.** It proves the whole path
runs end to end from a frozen bundle and a frozen set of model answers: triage, the
graph, every tool, the exit gate including re-execution, and the report. It proves the
result is reproducible, because it compares what the replay produced against what was
recorded when the cassettes were made.

**What it does not prove.** Anything about model quality. The cassettes currently come
from stub mode, so this replays what the stub would have said. The demo says so on its
own output rather than leaving a viewer to assume otherwise, because a demo that looks
like a model investigation and is not would be the most misleading artefact in the
repository.

**Why a mismatch is a failure and not a warning.** Replay exists so a run is
reproducible. If the same bundle and the same answers produce a different report, then
something in the harness is nondeterministic and every measurement in every report is
suspect. That is worth failing a build over.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from firebreak.agent.floor import FLOOR_LABEL  # noqa: E402
from firebreak.agent.graph import investigate  # noqa: E402
from firebreak.agent.llm import LlmClient, LlmError  # noqa: E402
from firebreak.demo.showcase import build_showcase  # noqa: E402
from firebreak.settings import LlmMode  # noqa: E402
from firebreak.web.store import write_console_report  # noqa: E402

CASSETTES_DIR = REPO_ROOT / "recordings" / "cassettes"


def load_manifest(directory: Path) -> dict[str, object]:
    path = directory / "manifest.json"
    if not path.is_file():
        # Not `relative_to`, which raises for a path outside the repository and would
        # turn a helpful message into a traceback about paths.
        raise SystemExit(
            f"no cassette manifest at {path}. Run `make cassettes` first; the offline "
            "demo replays recorded answers and has nothing to replay without them."
        )
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise SystemExit(f"{path.name} is not a manifest")
    return loaded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cassettes", default=str(CASSETTES_DIR), help="where the cassettes are")
    parser.add_argument(
        "--limit", type=int, default=0, help="only the first N incidents, for a quick check"
    )
    arguments = parser.parse_args()

    directory = Path(arguments.cassettes)
    manifest = load_manifest(directory)
    recorded_mode = str(manifest.get("recorded_from_mode", "unknown"))
    incidents = manifest.get("incidents")
    if not isinstance(incidents, dict) or not incidents:
        raise SystemExit("the cassette manifest lists no incidents")

    names = sorted(incidents)
    if arguments.limit:
        names = names[: arguments.limit]

    print(f"Firebreak offline demo: {len(names)} incident(s), replaying recorded answers")
    print(f"cassettes recorded from mode: {recorded_mode}")
    if recorded_mode == LlmMode.STUB.value:
        print(
            "  These replay what the deterministic stub would have said, not a model. "
            "Nothing below is evidence about model quality."
        )
    print()

    mismatches: list[str] = []
    replayed = 0
    # Rebuilt from the scenario specs, deterministically, into a temporary workspace. That
    # is what makes this demo run on a fresh clone and in CI: the recorded library is
    # git-ignored, and a demo that needed it would either fail there or skip and report
    # success having checked nothing.
    with tempfile.TemporaryDirectory(prefix="firebreak-demo-") as workspace:
        built = {incident.bundle_id: incident for incident in build_showcase(Path(workspace))}
        for name in names:
            expected = incidents[name]
            if not isinstance(expected, dict):
                continue
            incident = built.get(name)
            if incident is None:
                mismatches.append(
                    f"{name}: the manifest names an incident the showcase no longer builds. "
                    "Re-record with `make cassettes`."
                )
                continue
            bundle = Path(workspace) / name

            client = LlmClient(mode=LlmMode.REPLAY, recordings_dir=directory / name)
            try:
                result = investigate(bundle, llm=client)
            except LlmError as error:
                # A missing cassette is the interesting failure: it means the graph asked
                # a question the recording does not contain, which happens when the loop
                # changes. The fix is to re-record, and saying so beats a stack trace.
                mismatches.append(
                    f"{incident.spec.id}: the graph asked something the cassettes do not "
                    f"answer ({error}). Re-record with `make cassettes`."
                )
                continue

            # A missing cassette does not reach the `except` above: the graph's own
            # deterministic floor catches the model failure first and publishes B0's
            # triage, which is correct behaviour and means this script sees a report
            # rather than an error. Without this check the demo would report "claims 4
            # against recorded 5" and blame reproduction for a missing recording.
            if any(FLOOR_LABEL in note for note in result.report.notes):
                mismatches.append(
                    f"{incident.spec.id}: the deterministic floor engaged, which in replay "
                    "mode means a cassette the graph asked for is missing. Re-record with "
                    "`make cassettes`."
                )
                continue

            # Stored where the Console can read it. The demo is the only thing that
            # produces a report on a fresh clone, so without this every Console page would
            # be empty even after a successful demo.
            write_console_report(result)

            replayed += 1
            differences = []
            for label, produced, was in (
                (
                    "root cause",
                    result.report.root_cause_service,
                    expected.get("root_cause_service"),
                ),
                ("abstained", result.report.abstained, expected.get("abstained")),
                ("gate passed", result.gate.passed, expected.get("gate_passed")),
                ("claims", len(result.report.claims), expected.get("claims")),
            ):
                if produced != was:
                    differences.append(f"{label} {produced!r} against recorded {was!r}")

            verdict = "ok" if not differences else "DIFFERS"
            named = result.report.root_cause_service or "abstained"
            print(f"  {verdict:7s} {incident.spec.id}")
            print(
                f"            {named:18s} claims={len(result.report.claims)} "
                f"gate={'pass' if result.gate.passed else 'FAIL'}"
            )
            print(f"            why in the showcase: {incident.reason}")
            for difference in differences:
                mismatches.append(f"{incident.spec.id}: {difference}")

    print()
    if mismatches:
        print(f"{len(mismatches)} problem(s):", file=sys.stderr)
        for problem in mismatches:
            print(f"  {problem}", file=sys.stderr)
        print(
            "\nA replay that does not reproduce its recording means something in the "
            "harness is nondeterministic, and every measurement in every report is then "
            "suspect. That is why this fails rather than warns.",
            file=sys.stderr,
        )
        return 1

    print(f"offline demo: {replayed} incident(s) replayed and reproduced their recordings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
