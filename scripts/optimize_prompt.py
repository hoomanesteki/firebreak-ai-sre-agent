"""Run GEPA on one node's prompt, against the train split only.

SPEC.md Section 10.4. `make optimize NODE=reporter BUDGET=40`.

**Why this cannot improve anything yet, stated up front rather than discovered.** Two
reasons, and both are honest limits rather than bugs:

1. GEPA proposes prompt updates by reflecting on trajectories in natural language, which
   needs a model. No credentials are configured, so there is nothing to reflect with.
2. The prompt files are versioned, hashed and recorded, and they are not yet threaded
   into the model calls themselves. `LlmClient.complete` serves `stub` and `replay`, and
   raises for `api`, so there is no place a candidate prompt body would land. Wiring
   them in is the same work as implementing `api` mode, and it needs a model to be worth
   doing.

So what this script does today is measure the current configuration's score on train and
report it, with a note saying the candidate score is the same number and means nothing.
That is a real measurement and it is not optimization, and the report says which.

**Why it reads train and nothing else.** Phase 10's reviewer focus. A prompt tuned on
validation would be measured by the eval gate on the split it was tuned for, and the
gate would pass it for the wrong reason.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from firebreak.evals.graders import GradeSheet  # noqa: E402
from firebreak.evals.outcome import InvestigationOutcome  # noqa: E402
from firebreak.evals.runner import RunnerError, run_configuration  # noqa: E402
from firebreak.lab.scenario import Split  # noqa: E402
from firebreak.optimize.gepa import (  # noqa: E402
    ALLOWED_SPLIT,
    OptimizationTask,
    OptimizeError,
    build_trainset,
    optimize_node,
    write_report,
)
from firebreak.prompts import PROMPT_NODES  # noqa: E402
from firebreak.settings import Settings  # noqa: E402


def symptoms_for(bundle_id: str) -> str:
    """A short symptom description for one task.

    Derived from the bundle id alone. A description built from the label would be the
    answer arriving through the task set, which is the same leak the runner's signature
    is shaped to prevent.
    """
    return f"incident {bundle_id}: investigate from its own telemetry"


def reflection_model() -> Any:
    """GEPA's own model, or None when there are no credentials.

    None is a first-class answer here rather than an error: a run with no model still
    produces a real baseline measurement, and refusing to run at all would leave nothing
    to compare a future candidate against.
    """
    settings = Settings()
    if not (settings.llm_base_url and settings.llm_api_key):
        return None
    if not settings.optimize_reflection_model:
        raise OptimizeError(
            "credentials are configured and FIREBREAK_OPTIMIZE_REFLECTION_MODEL is not. "
            "GEPA needs a model id to reflect with, and guessing one here would fail at "
            "the first call with a confusing error."
        )
    try:
        import dspy
    except ImportError as error:  # pragma: no cover - the group is installed or not
        raise OptimizeError(
            "DSPy is not installed; it is an optional group because it conflicts with "
            "drain3 over cachetools. Run `uv sync --group optimize`."
        ) from error
    # The model id is configuration the owner supplies, not something to invent here.
    # An id guessed in code would fail at the first call with a confusing error.
    return dspy.LM(
        settings.optimize_reflection_model,
        api_base=settings.llm_base_url,
        api_key=settings.llm_api_key,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True, help=f"one of {', '.join(PROMPT_NODES)}")
    parser.add_argument("--budget", type=int, default=40, help="rollouts GEPA may spend")
    arguments = parser.parse_args()

    if arguments.node not in PROMPT_NODES:
        print(
            f"unknown node {arguments.node!r}; known: {', '.join(PROMPT_NODES)}",
            file=sys.stderr,
        )
        return 2

    with tempfile.TemporaryDirectory(prefix="firebreak-optimize-") as workspace:
        # Graded once for the whole split, then looked up per task. Re-running the
        # configuration per task would multiply the work by the task count and produce
        # the same answers, because the candidate body does not reach the model yet.
        try:
            run = run_configuration(
                "fb-v1", Split(ALLOWED_SPLIT), Path(workspace), trials_per_task=1, limit=0
            )
        except RunnerError as error:
            print(f"{error}", file=sys.stderr)
            return 1
        if not run.trials:
            print(
                f"no {ALLOWED_SPLIT} tasks; record the train split first",
                file=sys.stderr,
            )
            return 1

        graded: dict[str, tuple[GradeSheet, InvestigationOutcome]] = {
            trial.bundle_id: (trial.sheet, trial.outcome) for trial in run.trials
        }
        try:
            trainset = build_trainset(
                [
                    (
                        trial.bundle_id,
                        trial.split,
                        symptoms_for(trial.bundle_id),
                        trial.outcome.root_cause_service or "",
                    )
                    for trial in run.trials
                ]
            )
        except OptimizeError as error:
            print(f"{error}", file=sys.stderr)
            return 1

        print(f"{len(trainset)} {ALLOWED_SPLIT} task(s) graded once")

        def run_candidate(
            body: str, task: OptimizationTask
        ) -> tuple[GradeSheet, InvestigationOutcome]:
            """Return this task's graded result.

            `body` is ignored, and that is the honest state of things rather than an
            oversight: prompts are not yet threaded into the model calls, so a candidate
            body cannot change an outcome. `optimize_node` is written so that wiring them
            in is the only change needed here.
            """
            del body
            return graded[task.bundle_id]

        try:
            report = optimize_node(
                arguments.node,
                trainset,
                rollouts=arguments.budget,
                run_candidate=run_candidate,
                reflection_model=reflection_model(),
            )
        except OptimizeError as error:
            print(f"{error}", file=sys.stderr)
            return 1

    report.notes.append(
        "the candidate prompt body does not reach the model yet: prompts are versioned "
        "and hashed, and LlmClient serves stub and replay only, so wiring them in is "
        "the same work as implementing api mode"
    )
    json_path, markdown_path = write_report(report)
    print()
    print(
        f"{report.node}: baseline {report.baseline_score:.4f}, "
        f"candidate {report.candidate_score:.4f}"
    )
    print(report.verdict)
    for note in report.notes:
        print(f"  note: {note}")
    print(f"wrote {json_path.relative_to(REPO_ROOT)}")
    print(f"wrote {markdown_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
