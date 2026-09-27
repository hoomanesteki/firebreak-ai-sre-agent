"""Offline prompt optimization with GEPA, on the train split and nothing else.

SPEC.md Section 10.4. `firebreak optimize --node <name> --budget <rollouts>` runs
DSPy's GEPA optimizer against the train split with a metric combining top-1
correctness, evidence validity and calibration. GEPA reflects on trajectories in
natural language to propose prompt updates, and reported better results than GRPO with
far fewer rollouts [R13].

**The split rule is enforced here, not trusted to the caller.** Phase 10's reviewer
focus is that no validation or test data reaches optimization or memory. An optimizer
that saw validation tasks would produce a prompt tuned to the split the eval gate then
measures it on, and the gate would pass it for the wrong reason. So `build_trainset`
refuses any split but train, and `tests/leakage/` asserts it.

**What this module does and does not do.** It builds the task set, defines the metric,
runs GEPA, and writes a candidate prompt file plus a report. It does not ship anything:
SPEC.md Section 10.4 requires the candidate to go through a normal pull request and
pass the eval gate on validation first, and a function that could ship a prompt would
make that a convention rather than a rule.

**DSPy is an optional dependency, deliberately.** It pulls a large tree and conflicts
with `drain3`, which pins `cachetools==4.2.1` where dspy 3 needs 5.5 or newer.
`pyproject.toml` declares the two groups as conflicting and `drain3` moved to its own,
because resolving it the other way would have meant no prompt optimization at all. The
import is local to the function that needs it so that importing this module, reading its
metric, or running its tests needs none of that.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from firebreak.evals.graders import GradeSheet
from firebreak.evals.outcome import InvestigationOutcome
from firebreak.prompts import PROMPT_NODES, Prompt, PromptError, latest_for

REPO_ROOT = Path(__file__).resolve().parents[3]
REPORTS_DIR = REPO_ROOT / "reports" / "optimize"

# The only split optimization may read. SPEC.md Section 10.4 and Phase 10's reviewer
# focus.
#
# A string rather than `Split.TRAIN`, and the leakage scanner is why: `Split` lives in
# `firebreak.lab.scenario` alongside `target_service` and `fault_class`, so importing it
# here would give the optimizer an import path to ground truth. The scanner flagged
# exactly that on the first attempt. `firebreak.memory.store` does the same thing for
# the same reason, and a test asserts this string equals `Split.TRAIN.value` so the two
# cannot drift.
ALLOWED_SPLIT = "train"

# How the metric weighs its three parts. SPEC.md Section 10.4 names the three and not
# their weights, so these are a judgement and are written down as one.
#
# Correctness dominates because a prompt that cites beautifully and names the wrong
# service is worse than useless. Evidence validity is next and is nearly as heavy: a
# fabricated citation is the one failure this project claims not to ship. Calibration
# is lightest and is not zero, because a prompt optimized on correctness alone learns
# to claim high confidence always, and the calibration term is what stops the
# optimizer discovering that.
WEIGHT_CORRECTNESS = 0.55
WEIGHT_EVIDENCE = 0.30
WEIGHT_CALIBRATION = 0.15


class OptimizeError(Exception):
    """Optimization could not be set up or run."""


class LeakageError(OptimizeError):
    """A task from outside the train split reached optimization.

    Its own type for the same reason incident memory has one: a malformed task is a bug
    to fix, and this is a measurement that would have been silently invalidated.
    """


@dataclass(frozen=True)
class OptimizationTask:
    """One train task as the optimizer sees it."""

    bundle_id: str
    split: str
    symptoms: str
    # The answer. Present because optimization is supervised and train labels are
    # exactly what train is for. This is also why the split check matters: the same
    # field on a test task would be the answer leaking.
    target_service: str


def build_trainset(
    tasks: Sequence[tuple[str, str, str, str]],
) -> list[OptimizationTask]:
    """Turn (bundle id, split, symptoms, target) tuples into tasks, refusing non-train.

    Takes tuples rather than the runner's `Task` objects so this function has no import
    path to the label store, which is the same reasoning as the runner's own signature:
    the narrower the input, the fewer ways an answer can arrive.
    """
    built = []
    offenders = []
    for bundle_id, split, symptoms, target in tasks:
        if split != ALLOWED_SPLIT:
            offenders.append((bundle_id, split))
            continue
        built.append(
            OptimizationTask(
                bundle_id=bundle_id, split=split, symptoms=symptoms, target_service=target
            )
        )
    if offenders:
        raise LeakageError(
            f"optimization reads {ALLOWED_SPLIT} only, and these tasks are not: "
            f"{offenders}. A prompt tuned on validation would be measured by the eval "
            "gate on the split it was tuned for, and the gate would pass it for the "
            "wrong reason."
        )
    if not built:
        raise OptimizeError(
            f"no {ALLOWED_SPLIT} tasks to optimize on; record the train split first"
        )
    return built


def score(sheet: GradeSheet, outcome: InvestigationOutcome) -> float:
    """The metric GEPA maximises, between zero and one.

    Three parts, weighted as the module constants explain. Returns a single number
    because that is what an optimizer takes, and the weights are stated rather than
    tuned: a weight fitted on train would be a second thing optimized on train.
    """
    root_cause = sheet.verdict_for("root_cause")
    evidence = sheet.verdict_for("evidence_validity")

    correctness = 1.0 if root_cause is not None and root_cause.correct else 0.0
    validity = 1.0 if evidence is not None and evidence.correct else 0.0

    return (
        WEIGHT_CORRECTNESS * correctness
        + WEIGHT_EVIDENCE * validity
        + WEIGHT_CALIBRATION * _calibration_term(outcome.confidence, correctness)
    )


def _calibration_term(stated: float | None, correctness: float) -> float:
    """A per-task Brier term: how well the stated confidence matched the outcome.

    A confident right answer and an unconfident wrong one both score well, which is the
    behaviour worth rewarding. Squared error rather than absolute, for the same reason
    the eval gate uses it: absolute error cannot separate a system that is 99 percent
    confident and wrong from one that is 60 percent confident and wrong.

    No stated confidence scores a full mark rather than zero. A report that declines to
    claim one is behaving correctly, and penalising it would teach the optimizer to
    always claim something, which is the opposite of what this term exists for.
    """
    if stated is None:
        return 1.0
    return 1.0 - (stated - correctness) ** 2


@dataclass
class OptimizationReport:
    """What a run produced, and everything needed to argue with it."""

    node: str
    baseline_prompt: str
    baseline_score: float
    candidate_score: float
    rollouts: int
    train_tasks: int
    candidate_path: str | None = None
    notes: list[str] = field(default_factory=list)
    generated_at: str = ""
    # Whether GEPA actually ran. Without this the verdict for "ran and found nothing"
    # and for "never ran" would be the same sentence, and the first is a result about
    # the prompt while the second is a result about the configuration. Reporting the
    # second as the first is precisely the kind of statement this project must not make.
    optimizer_ran: bool = False

    @property
    def improved(self) -> bool:
        return self.optimizer_ran and self.candidate_score > self.baseline_score

    @property
    def verdict(self) -> str:
        """What to do with this candidate.

        An optimization that did not help is a result to report, not a failure to hide.
        SPEC.md Section 17 Phase 10's acceptance says the report shows the effect
        "including if the optimization did not help". An optimization that never ran is
        a different statement and gets a different sentence.
        """
        if not self.optimizer_ran:
            return (
                "GEPA did not run, so nothing was optimized and no candidate exists. "
                "The baseline score is a real measurement of the current prompt on "
                "train; the candidate score is the same number and carries no "
                "information. The notes say what is missing."
            )
        if not self.improved:
            return (
                "GEPA ran and found no improvement on train, so there is nothing to "
                "submit. This is a result: the hand written prompt is at least as good "
                "as what GEPA found within this budget."
            )
        return (
            "improved on train. It must now pass the eval gate on validation through a "
            "normal pull request, and the OOD result must be shown next to the ID "
            "result, because a prompt that improved on train may have fitted it."
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "node": self.node,
            "baseline_prompt": self.baseline_prompt,
            "baseline_score": round(self.baseline_score, 4),
            "candidate_score": round(self.candidate_score, 4),
            "optimizer_ran": self.optimizer_ran,
            "improved": self.improved,
            "verdict": self.verdict,
            "rollouts": self.rollouts,
            "train_tasks": self.train_tasks,
            "candidate_path": self.candidate_path,
            "notes": list(self.notes),
            "generated_at": self.generated_at,
        }


def write_candidate(
    baseline: Prompt,
    body: str,
    directory: Path | None = None,
) -> Path:
    """Write an optimized prompt as the next version of its node.

    A new file rather than an edit, so the prompt that produced every past report still
    exists and its hash still resolves. `optimized_from` records the lineage, because a
    candidate with no lineage is indistinguishable from a prompt somebody wrote.
    """
    root = directory or (REPO_ROOT / "prompts")
    version = baseline.version + 1
    path = root / f"{baseline.node}.v{version}.md"
    if path.exists():
        raise OptimizeError(
            f"{path.name} already exists; bump the version or remove the previous "
            "candidate rather than overwriting a prompt a report may cite"
        )
    frontmatter = (
        "---\n"
        f"id: {baseline.node}/v{version}\n"
        f"node: {baseline.node}\n"
        f"version: {version}\n"
        f"optimized_from: {baseline.stamp}\n"
        "notes: >-\n"
        "  Produced by firebreak optimize with DSPy GEPA on the train split. Not\n"
        "  shipped until it passes the eval gate on validation through a pull request,\n"
        "  with the OOD result shown beside the ID result.\n"
        "---\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter + body.strip() + "\n", encoding="utf-8")
    return path


def optimize_node(
    node: str,
    trainset: list[OptimizationTask],
    rollouts: int,
    run_candidate: Callable[[str, OptimizationTask], tuple[GradeSheet, InvestigationOutcome]],
    reflection_model: Any = None,
    prompts_dir: Path | None = None,
) -> OptimizationReport:
    """Run GEPA on one node's prompt and report what it found.

    `run_candidate` is injected: it takes a prompt body and a task, and returns the
    graded outcome. That is the whole coupling to the rest of the system, and it is a
    function rather than a module import so this can be tested without a model, a
    backend, or DSPy installed.

    `reflection_model` is GEPA's own model, which it uses to propose prompt updates. It
    is required for a real run and absent in a dry run, and a dry run reports the
    baseline score with a note rather than pretending to optimize.
    """
    if node not in PROMPT_NODES:
        raise OptimizeError(f"{node!r} is not a node with a prompt; known: {PROMPT_NODES}")
    try:
        baseline = latest_for(node, prompts_dir)
    except PromptError as error:
        raise OptimizeError(str(error)) from error

    notes: list[str] = []
    baseline_score = _mean_score(baseline.body, trainset, run_candidate)

    if reflection_model is None:
        notes.append(
            "no reflection model was configured, so GEPA did not run. The baseline "
            "score above is a real measurement of the current prompt on train; the "
            "candidate score is the same number and means nothing. Configure a model "
            "and run again."
        )
        return OptimizationReport(
            node=node,
            baseline_prompt=baseline.stamp,
            baseline_score=baseline_score,
            candidate_score=baseline_score,
            rollouts=0,
            train_tasks=len(trainset),
            notes=notes,
            generated_at=datetime.now(UTC).isoformat(),
            optimizer_ran=False,
        )

    body, rollouts_used, gepa_notes = _run_gepa(
        baseline.body, trainset, rollouts, run_candidate, reflection_model
    )
    notes.extend(gepa_notes)
    candidate_score = _mean_score(body, trainset, run_candidate)
    report = OptimizationReport(
        node=node,
        baseline_prompt=baseline.stamp,
        baseline_score=baseline_score,
        candidate_score=candidate_score,
        rollouts=rollouts_used,
        train_tasks=len(trainset),
        notes=notes,
        generated_at=datetime.now(UTC).isoformat(),
        optimizer_ran=True,
    )
    if report.improved:
        report.candidate_path = str(write_candidate(baseline, body, prompts_dir))
    return report


def _mean_score(
    body: str,
    trainset: list[OptimizationTask],
    run_candidate: Callable[[str, OptimizationTask], tuple[GradeSheet, InvestigationOutcome]],
) -> float:
    if not trainset:
        return 0.0
    total = 0.0
    for task in trainset:
        sheet, outcome = run_candidate(body, task)
        total += score(sheet, outcome)
    return total / len(trainset)


def _run_gepa(
    body: str,
    trainset: list[OptimizationTask],
    rollouts: int,
    run_candidate: Callable[[str, OptimizationTask], tuple[GradeSheet, InvestigationOutcome]],
    reflection_model: Any,
) -> tuple[str, int, list[str]]:
    """Call DSPy's GEPA, confirmed against dspy 3.4.0.

    `GEPA(metric=..., max_metric_calls=..., reflection_lm=...)` then
    `compile(student, trainset=[Example, ...])`, which is what the installed package's
    signatures say rather than what a blog post said. The import is local so nothing
    else in this module needs DSPy present.
    """
    try:
        import dspy
        from dspy import GEPA
    except ImportError as error:
        raise OptimizeError(
            "DSPy is not installed. It is an optional dependency because it conflicts "
            "with drain3 over cachetools: run `uv sync --group optimize`."
        ) from error

    notes = [f"DSPy {dspy.__version__} with GEPA, max_metric_calls={rollouts}"]

    class PromptModule(dspy.Module):  # type: ignore[misc]
        """The thing GEPA optimizes: one instruction, used by `run_candidate`."""

        def __init__(self, instruction: str) -> None:
            super().__init__()
            self.instruction = instruction

        def forward(self, task: OptimizationTask) -> Any:
            sheet, outcome = run_candidate(self.instruction, task)
            return dspy.Prediction(sheet=sheet, outcome=outcome)

    def metric(example: Any, prediction: Any, *_: Any, **__: Any) -> float:
        del example
        return score(prediction.sheet, prediction.outcome)

    student = PromptModule(body)
    examples = [
        dspy.Example(task=task, target=task.target_service).with_inputs("task") for task in trainset
    ]
    optimizer = GEPA(
        metric=metric,
        max_metric_calls=rollouts,
        reflection_lm=reflection_model,
        track_stats=True,
    )
    optimized = optimizer.compile(student, trainset=examples)
    improved_body = getattr(optimized, "instruction", body)
    if improved_body == body:
        notes.append("GEPA returned the baseline instruction unchanged")
    return str(improved_body), rollouts, notes


def write_report(report: OptimizationReport, root: Path | None = None) -> tuple[Path, Path]:
    """Write the optimization report as JSON and Markdown."""
    directory = (root or REPORTS_DIR) / report.node
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{report.generated_at[:10]}_{report.baseline_prompt.split('@')[-1]}"
    json_path = directory / f"{stem}.json"
    json_path.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")

    lines = [
        f"# Optimization report: {report.node}",
        "",
        f"- Baseline prompt: `{report.baseline_prompt}`",
        f"- Train tasks: {report.train_tasks}",
        f"- Rollouts: {report.rollouts}",
        f"- Baseline score: {report.baseline_score:.4f}",
        f"- Candidate score: {report.candidate_score:.4f}"
        + ("" if report.optimizer_ran else " (GEPA did not run; this is the baseline)"),
        "",
        f"**{report.verdict}**",
        "",
        "The score combines top-1 correctness, evidence validity and calibration, "
        f"weighted {WEIGHT_CORRECTNESS}, {WEIGHT_EVIDENCE} and {WEIGHT_CALIBRATION}. "
        "Those weights are a judgement, not a measurement: correctness dominates "
        "because a prompt that cites beautifully and names the wrong service is worse "
        "than useless, and calibration is present but light because a prompt optimized "
        "on correctness alone learns to always claim high confidence.",
        "",
        "Measured on the train split only. A number here says nothing about held-out "
        "performance, which is what the eval gate on validation and the OOD report are "
        "for.",
        "",
    ]
    if report.notes:
        lines.append("## Notes")
        lines.append("")
        lines += [f"- {note}" for note in report.notes]
        lines.append("")
    markdown_path = directory / f"{stem}.md"
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path
