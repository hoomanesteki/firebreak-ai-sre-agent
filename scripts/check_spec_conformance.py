"""Check the repository against SPEC.md's own tables.

SPEC.md is the source of truth, and it states its requirements as tables: the
configurations in Section 9.5, the architecture decision records in Section 14,
and the phases in Section 17. Nothing was reading them. A configuration named in
the spec and never built, or an ADR due three phases ago and never written, was
something a person had to notice.

**What this fails on, and what it only reports.** It fails when a phase that is
already complete left something out, because that is a gap in work claimed to be
done. It reports without failing on anything due in the current or a later phase,
because that is a plan rather than a defect. The distinction is the whole point:
a checker that failed on future work would have to be silenced, and a silenced
checker is not a checker.

**How it knows which phase is current.** The count of phase reports. Phase N is
complete when `docs/phase-reports/PNN.md` exists, so the current phase is the
first number without one. That makes the marker the same artefact the review gate
produces, rather than a version string somebody has to remember to bump.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC = REPO_ROOT / "SPEC.md"
ADR_DIR = REPO_ROOT / "docs" / "adr"
PHASE_REPORT_DIR = REPO_ROOT / "docs" / "phase-reports"

# Section 9.5's ids are written as B0, FB, A1. The runner spells them lower case
# with a version suffix for the main system. Declared here because two files
# disagreeing about an id is the failure this whole script exists to catch, and
# the mapping is a fact about naming rather than something to infer.
CONFIGURATION_IDS = {
    "B0": "b0",
    "B1": "b1",
    "B2": "b2",
    "FB": "fb-v1",
    "A1": "a1",
    "A2": "a2",
    "A3": "a3",
    "A4": "a4",
    "A5": "a5",
    "A6": "a6",
}

# Which phase each configuration and record is due in, from Section 17's task
# lists. Read from the spec below rather than trusted from here; this is only the
# fallback for the ones Section 17 does not name explicitly.
IMPLIED_PHASE = {"B0": 4, "FB": 6}


@dataclass
class Findings:
    """What is missing, split by whether it is late or merely planned."""

    late: list[str] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.late


def read_spec() -> str:
    if not SPEC.is_file():
        raise SystemExit(f"{SPEC} is missing, so there is nothing to check against")
    return SPEC.read_text(encoding="utf-8")


def current_phase() -> int:
    """The first phase with no report, which is the one in progress."""
    phase = 0
    while (PHASE_REPORT_DIR / f"P{phase:02d}.md").is_file():
        phase += 1
    return phase


def phase_tasks(spec: str) -> dict[int, str]:
    """Each phase number mapped to its task and acceptance text.

    Used to find which phase names a configuration or an ADR, so the checker does
    not need its own copy of the schedule.
    """
    sections: dict[int, str] = {}
    pattern = re.compile(r"^### Phase (\d+):", re.MULTILINE)
    matches = list(pattern.finditer(spec))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(spec)
        sections[int(match.group(1))] = spec[start:end]
    return sections


def spec_configurations(spec: str) -> list[str]:
    """The ids in Section 9.5's baselines and ablations table."""
    table = _section(spec, "### 9.5 Baselines and ablations")
    found = []
    for line in table.splitlines():
        match = re.match(r"^\|\s*(B\d|FB|A\d)\s*\|", line)
        if match:
            found.append(match.group(1))
    return found


def spec_adrs(spec: str) -> list[str]:
    """The four digit numbers in Section 14's table."""
    table = _section(spec, "## 14. Architecture decision records")
    return re.findall(r"^\|\s*(\d{4})\s*\|", table, re.MULTILINE)


def _section(spec: str, heading: str) -> str:
    if heading not in spec:
        raise SystemExit(f"SPEC.md has no section headed {heading!r}; the checker is stale")
    start = spec.index(heading)
    rest = spec[start + len(heading) :]
    # Up to the next heading of the same or higher level.
    match = re.search(r"^#{1,3} ", rest, re.MULTILINE)
    return rest[: match.start()] if match else rest


def due_phase(tasks: dict[int, str], pattern: str) -> int | None:
    """The earliest phase whose text mentions this thing."""
    for phase in sorted(tasks):
        if re.search(pattern, tasks[phase]):
            return phase
    return None


def ablation_due_phase(spec_id: str, tasks: dict[int, str]) -> int | None:
    """Which phase owns one ablation, including when a range names it.

    Section 17 writes "ablations A2 to A4", so A3 is claimed by Phase 8 without
    appearing in it. Reading the literal id only would report A3 as claimed by
    nobody, which is how a checker teaches people to ignore it.
    """
    direct = due_phase(tasks, rf"\b{re.escape(spec_id)}\b")
    if direct is not None:
        return direct
    if not re.fullmatch(r"A\d", spec_id):
        return None
    wanted = int(spec_id[1:])
    for phase in sorted(tasks):
        for low, high in re.findall(r"A(\d)\s+to\s+A(\d)", tasks[phase]):
            if int(low) <= wanted <= int(high):
                return phase
    return None


def check_configurations(spec: str, tasks: dict[int, str], phase: int) -> Findings:
    """Every configuration in Section 9.5 is runnable, or not due yet."""
    findings = Findings()
    try:
        from firebreak.evals.runner import CONFIGURATIONS
    except ImportError as error:  # pragma: no cover - a broken import fails elsewhere
        findings.late.append(f"cannot import the configuration registry: {error}")
        return findings

    for spec_id in spec_configurations(spec):
        runner_id = CONFIGURATION_IDS.get(spec_id)
        if runner_id is None:
            findings.late.append(
                f"SPEC.md Section 9.5 names configuration {spec_id}, which this checker "
                "has no runner id for; add it to CONFIGURATION_IDS"
            )
            continue
        if runner_id in CONFIGURATIONS:
            continue
        due = ablation_due_phase(spec_id, tasks) or IMPLIED_PHASE.get(spec_id)
        message = (
            f"configuration {spec_id} ({runner_id}) is in Section 9.5 and not in the "
            "runner's registry"
        )
        if due is None:
            findings.late.append(f"{message}, and no phase claims it")
        elif due < phase:
            findings.late.append(f"{message}; Phase {due} was meant to build it")
        else:
            findings.planned.append(f"{message}; due in Phase {due}")
    return findings


def check_adrs(spec: str, tasks: dict[int, str], phase: int) -> Findings:
    """Every ADR in Section 14 is written, or not due yet, and is indexed."""
    findings = Findings()
    on_disk = {path.name[:4]: path for path in ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md")}
    readme = ADR_DIR / "README.md"
    index = readme.read_text(encoding="utf-8") if readme.is_file() else ""

    for number in spec_adrs(spec):
        due = due_phase(tasks, rf"ADR-{number}")
        if number in on_disk:
            if f"({on_disk[number].name})" not in index:
                findings.late.append(
                    f"ADR-{number} exists as {on_disk[number].name} and is not in "
                    "docs/adr/README.md, so nobody browsing the index will find it"
                )
            continue
        message = f"ADR-{number} is required by Section 14 and not written"
        if due is None:
            findings.late.append(f"{message}, and no phase claims it")
        elif due < phase:
            findings.late.append(f"{message}; Phase {due} was meant to write it")
        else:
            findings.planned.append(f"{message}; due in Phase {due}")

    for number, path in sorted(on_disk.items()):
        if number not in spec_adrs(spec):
            findings.late.append(
                f"{path.name} is not in Section 14's table; either add it there or "
                "explain why it exists"
            )
    return findings


def check_phase_reports(spec: str, phase: int) -> Findings:
    """Every completed phase has a report, and no report jumps ahead."""
    findings = Findings()
    declared = sorted(phase_tasks(spec))
    for number in declared:
        report = PHASE_REPORT_DIR / f"P{number:02d}.md"
        if number < phase and not report.is_file():
            findings.late.append(f"Phase {number} has no report at {report.name}")
    stray = [
        path.name
        for path in PHASE_REPORT_DIR.glob("P[0-9][0-9].md")
        if int(path.stem[1:]) not in declared
    ]
    for name in sorted(stray):
        findings.late.append(f"{name} is a report for a phase SPEC.md does not declare")
    return findings


def check_make_targets(spec: str, phase: int) -> Findings:
    """The commands CLAUDE.md promises exist, once their phase has passed.

    `make demo-offline` is the one that matters: CLAUDE.md requires it from Phase
    11, and a promised command that does not exist is a broken instruction for
    whoever reads it next.
    """
    del spec
    findings = Findings()
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    promised = {"demo": 11, "demo-offline": 11, "eval": 5, "optimize": 10}
    for target, due in sorted(promised.items()):
        if re.search(rf"^{re.escape(target)}:", makefile, re.MULTILINE):
            continue
        message = f"CLAUDE.md promises `make {target}` and the Makefile has no such target"
        if due < phase:
            findings.late.append(f"{message}; Phase {due} was meant to add it")
        else:
            findings.planned.append(f"{message}; due in Phase {due}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        type=int,
        default=0,
        help="treat this phase as the one in progress, default is derived from the reports",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail on planned gaps too, for a release check",
    )
    arguments = parser.parse_args()

    spec = read_spec()
    phase = arguments.phase or current_phase()
    tasks = phase_tasks(spec)

    # `len(tasks)` counts phases 0 to N, so the last phase number is one less. Past it there
    # is no phase in progress, and saying "Phase 13 is in progress" of a thirteen-phase spec
    # invents a phase and makes every later check compare against nothing.
    last_phase = max(tasks) if tasks else 0
    if phase > last_phase:
        print(
            f"SPEC.md declares {len(tasks)} phases, 0 to {last_phase}; "
            f"every one has a report, so no phase is in progress"
        )
        phase = last_phase
    else:
        print(f"SPEC.md declares {len(tasks)} phases; Phase {phase} is in progress")

    all_findings = [
        ("configurations", check_configurations(spec, tasks, phase)),
        ("decision records", check_adrs(spec, tasks, phase)),
        ("phase reports", check_phase_reports(spec, phase)),
        ("promised commands", check_make_targets(spec, phase)),
    ]

    late = [(area, item) for area, found in all_findings for item in found.late]
    planned = [(area, item) for area, found in all_findings for item in found.planned]

    if planned:
        print(f"\n{len(planned)} thing(s) the spec plans and this phase has not reached:")
        for area, item in planned:
            print(f"  [{area}] {item}")

    if late:
        sys.stdout.flush()
        print(f"\n{len(late)} thing(s) a completed phase left out:", file=sys.stderr)
        for area, item in late:
            print(f"  [{area}] {item}", file=sys.stderr)
        return 1

    if arguments.strict and planned:
        print("\nstrict mode: planned gaps count as failures", file=sys.stderr)
        return 1

    print("\nspec conformance: nothing a completed phase left out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
