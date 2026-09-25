"""Run a configuration over a split and grade what it produced.

`firebreak eval run --config <id> --split <split> --trials 3` comes through
here. The runner owns three things and deliberately no more: choosing the
tasks, invoking the configuration, and pairing each outcome with its label for
the graders.

**The label never reaches the configuration.** That is the whole leakage
control, and it is structural rather than a matter of care: a configuration is
a callable taking a bundle directory and returning an outcome, so there is no
argument through which a label could arrive. The runner holds the labels and
the configuration cannot see the runner.

**Held out splits are read, never tuned on.** `firebreak.evals.splits`
enforces that a family in `test_ood` appears in no tunable split. Running a
configuration against `test_id` or `test_ood` is the point of having them; what
must not happen is a number from either feeding back into a threshold. That is
a discipline this module cannot enforce, so it records which split produced
every figure and the report says so on its face.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from firebreak.evals.graders import GradeSheet, grade_trial
from firebreak.evals.outcome import InvestigationOutcome
from firebreak.graph.knowledge import KnowledgeError, load_knowledge
from firebreak.lab.bundle import derive_bundle_id, iter_bundles
from firebreak.lab.scenario import ScenarioSpec, Split, load_library
from firebreak.lab.synthetic import build_synthetic_bundle
from firebreak.triage.report import build_b0_report
from firebreak_eval_labels import IncidentLabel, new_canary

REPO_ROOT = Path(__file__).resolve().parents[3]
SPECS_DIR = REPO_ROOT / "scenarios" / "specs"
BUNDLES_DIR = REPO_ROOT / "bundles"
LABELS_DIR = REPO_ROOT / "labels"


class RunnerError(Exception):
    """A run could not be set up, or its inputs do not agree."""


@dataclass(frozen=True)
class Trial:
    """One run of one configuration on one task."""

    bundle_id: str
    scenario_id: str
    family: str
    split: str
    trial_index: int
    outcome: InvestigationOutcome
    sheet: GradeSheet


@dataclass(frozen=True)
class Task:
    """One incident to investigate, with the answer kept beside it.

    The label lives here and not in anything handed to a configuration.
    """

    bundle_id: str
    bundle_dir: Path
    label: IncidentLabel
    spec: ScenarioSpec | None

    @property
    def family(self) -> str:
        return self.spec.family if self.spec else "unknown"


# A configuration is anything that investigates a bundle. The signature is the
# leakage control: a bundle directory in, an outcome and the evidence it
# gathered out, and no parameter through which an answer could be passed.
Configuration = Callable[[Path], tuple[InvestigationOutcome, set[str]]]


def run_b0(bundle_dir: Path) -> tuple[InvestigationOutcome, set[str]]:
    """Baseline B0 as a configuration the runner can call.

    Deterministic triage with a template report and no model, so every trial
    of the same bundle produces an identical outcome. That makes pass^3 exactly
    1.0 or 0.0 for B0, which is correct rather than a limitation: a system with
    no sampling in it is perfectly reliable at whatever it does, well or badly.
    """
    started = time.monotonic()
    report = build_b0_report(bundle_dir, verify=False)
    elapsed = time.monotonic() - started

    triage = report.triage
    outcome = InvestigationOutcome(
        bundle_id=report.bundle_id,
        root_cause_service=report.named_service,
        ranked_candidates=tuple(c.service for c in triage.candidates) if triage else (),
        abstained=report.abstained,
        # B0 claims neither a mechanism nor an onset time. It ranks services
        # from anomaly scores and a dependency walk, and inventing a fault
        # class from that would be a guess dressed as a finding. The graders
        # score this as not applicable rather than as wrong.
        fault_class=None,
        fault_onset=None,
        confidence=triage.confidence if triage else None,
        cited_evidence=report.cited_ids,
        remediation=None,
        tool_calls=report.tool_calls,
        wall_clock_seconds=elapsed,
    )
    return outcome, set(report.evidence)


CONFIGURATIONS: dict[str, Configuration] = {"b0": run_b0}


def load_labels(labels_dir: Path = LABELS_DIR) -> dict[str, IncidentLabel]:
    """Every recorded label, keyed by bundle id."""
    if not labels_dir.is_dir():
        return {}
    found: dict[str, IncidentLabel] = {}
    for path in sorted(labels_dir.rglob("*.json")):
        label = IncidentLabel.model_validate_json(path.read_text(encoding="utf-8"))
        found[label.bundle_id] = label
    return found


def _synthetic_label(spec: ScenarioSpec, run_id: str) -> IncidentLabel:
    """A label for a fixture built on the fly.

    Needed because no incident library is recorded yet and the harness has to
    be runnable and testable before one exists. The canary is generated the
    same way a recorded label's is, so the leakage scan over prompts works
    identically against a fixture run.

    `fault_onset` is left unset. A synthetic bundle has no flag convergence to
    measure, so claiming an onset here would give the onset grader a target
    invented by the same code being graded.
    """
    return IncidentLabel(
        scenario_id=spec.id,
        run_id=run_id,
        bundle_id=derive_bundle_id(spec.id, run_id),
        split=spec.split.value,
        target_service=spec.target_service,
        fault_class=spec.fault_class.value,
        fault_flag=spec.fault.flag,
        fault_variant=spec.fault.variant,
        alert_fired=spec.expected_alert is not None,
        canary=new_canary(),
    )


def collect_tasks(
    split: Split,
    workspace: Path,
    limit: int = 0,
    seed: int = 1,
    run_id: str = "eval",
) -> tuple[list[Task], bool]:
    """The tasks for one split, and whether they are recorded or synthetic.

    **Decided per split, not globally.** The first version flipped to recorded
    bundles as soon as any existed anywhere, and the first real recording broke
    it immediately: one test_ood bundle on disk meant validation found no tasks
    and the run refused to start. A library is recorded over days, so the
    partially recorded state is the normal state and has to work.

    **All or nothing within a split**, though. Mixing a recorded bundle and a
    fixture in one report would produce an average across two different kinds of
    claim, and no caveat on the report could tell a reader which half a number
    came from. So a split with any recording uses only recordings, and the
    caller is told how much of the split that covers.
    """
    library = load_library(SPECS_DIR)
    labels = load_labels()
    recorded = (
        {path.name: path for path in iter_bundles(BUNDLES_DIR)} if BUNDLES_DIR.is_dir() else {}
    )

    in_split = [
        (bundle_id, path)
        for bundle_id, path in sorted(recorded.items())
        if (label := labels.get(bundle_id)) is not None and label.split == split.value
    ]

    tasks: list[Task] = []
    if in_split:
        for bundle_id, bundle_dir in in_split:
            label = labels[bundle_id]
            tasks.append(Task(bundle_id, bundle_dir, label, library.get(label.scenario_id)))
        using_recorded = True
    else:
        specs = sorted(
            (spec for spec in library.values() if spec.split is split), key=lambda s: s.id
        )
        for spec in specs:
            label = _synthetic_label(spec, run_id)
            bundle_dir = workspace / label.bundle_id
            build_synthetic_bundle(bundle_dir, spec, run_id, seed=seed)
            tasks.append(Task(label.bundle_id, bundle_dir, label, spec))
        using_recorded = False

    if limit:
        tasks = tasks[:limit]
    if not tasks:
        raise RunnerError(f"no tasks found for split {split.value}")
    return tasks, using_recorded


def split_coverage(split: Split) -> tuple[int, int]:
    """How many of a split's scenarios are recorded, and how many exist.

    Reported on every eval so a figure from three bundles is never mistaken for
    a figure from thirty. A partially recorded split produces a real number
    about a small sample, which is worth having and worth labelling.
    """
    library = load_library(SPECS_DIR)
    total = sum(1 for spec in library.values() if spec.split is split)
    labels = load_labels()
    recorded = {path.name for path in iter_bundles(BUNDLES_DIR)} if BUNDLES_DIR.is_dir() else set()
    covered = sum(
        1
        for bundle_id in recorded
        if (label := labels.get(bundle_id)) is not None and label.split == split.value
    )
    return covered, total


def allowed_remediation_ids() -> set[str]:
    """The remediation allowlist, for the remediation grader.

    Read from `knowledge/remediations.yaml` rather than hard coded, so the
    grader and the system it grades cannot disagree about what is allowed. An
    unreadable knowledge directory yields an empty set, which makes every
    proposal score as outside the allowlist; that is the safe direction to
    fail, since the alternative is silently accepting anything.
    """
    try:
        return {remediation.id for remediation in load_knowledge().remediations}
    except KnowledgeError:
        return set()


@dataclass(frozen=True)
class RunResult:
    """Everything one `eval run` produced."""

    configuration: str
    split: str
    trials_per_task: int
    using_recorded_bundles: bool
    trials: tuple[Trial, ...]
    recorded_scenarios: int = 0
    total_scenarios: int = 0

    @property
    def sheets(self) -> list[GradeSheet]:
        return [trial.sheet for trial in self.trials]

    @property
    def outcomes(self) -> list[InvestigationOutcome]:
        return [trial.outcome for trial in self.trials]

    def family_of_bundle(self) -> dict[str, str]:
        return {trial.bundle_id: trial.family for trial in self.trials}


def run_configuration(
    configuration: str,
    split: Split,
    workspace: Path,
    trials_per_task: int = 1,
    limit: int = 0,
    seed: int = 1,
) -> RunResult:
    """Run one configuration over one split and grade every trial."""
    if configuration not in CONFIGURATIONS:
        raise RunnerError(
            f"unknown configuration {configuration!r}; known: {', '.join(sorted(CONFIGURATIONS))}"
        )
    if trials_per_task < 1:
        raise RunnerError(f"trials must be at least 1, got {trials_per_task}")

    investigate = CONFIGURATIONS[configuration]
    tasks, using_recorded = collect_tasks(split, workspace, limit=limit, seed=seed)
    allowed = allowed_remediation_ids()

    trials: list[Trial] = []
    for task in tasks:
        for index in range(trials_per_task):
            outcome, gathered = investigate(task.bundle_dir)
            if outcome.bundle_id != task.bundle_id:
                raise RunnerError(
                    f"configuration {configuration!r} returned an outcome for "
                    f"{outcome.bundle_id} when asked about {task.bundle_id}"
                )
            sheet = grade_trial(outcome, task.label, gathered, allowed)
            trials.append(
                Trial(
                    bundle_id=task.bundle_id,
                    scenario_id=task.label.scenario_id,
                    family=task.family,
                    split=task.label.split,
                    trial_index=index,
                    outcome=outcome,
                    sheet=sheet,
                )
            )

    covered, total = split_coverage(split)
    return RunResult(
        configuration=configuration,
        split=split.value,
        trials_per_task=trials_per_task,
        using_recorded_bundles=using_recorded,
        trials=tuple(trials),
        recorded_scenarios=covered,
        total_scenarios=total,
    )
