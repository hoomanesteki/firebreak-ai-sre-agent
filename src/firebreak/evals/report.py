"""Write an eval report, as JSON and as Markdown, from one computation.

SPEC.md Section 9.6 wants both formats. They are rendered from a single
`EvalReport`, so the two can differ in what they show and never in what they
say: a Markdown table and a JSON field that disagreed would be worse than
having only one of them.

**The report states what it is a report of.** Configuration, split, trials,
commit, and above all whether the bundles were recorded or synthetic. A number
from a fixture is a design signal and a number from a recording is a result,
and the reader of a file six months from now cannot tell them apart unless the
file says.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from firebreak.evals.metrics import (
    GraderSummary,
    SplitMetrics,
    compute_metrics,
    group_by,
    per_task_table,
)
from firebreak.evals.runner import RunResult
from firebreak.evals.statistics import BOOTSTRAP_RESAMPLES

REPO_ROOT = Path(__file__).resolve().parents[3]
REPORTS_DIR = REPO_ROOT / "reports" / "eval"

# Splits whose numbers may be quoted as results. A figure from train or
# validation has been tuned against and is a development signal only.
RESULT_SPLITS = frozenset({"test_id", "test_ood"})

SYNTHETIC_WARNING = (
    "These figures come from synthetic fixtures, not recorded incidents. They are a "
    "design signal for building the harness and must never be quoted as a result. "
    "Record the incident library and re-run to obtain real figures."
)

TUNED_SPLIT_WARNING = (
    "This split was available for tuning, so a figure from it says how well the "
    "system fits data it was developed against. Only test_id and test_ood support a "
    "claim about performance."
)


def git_commit() -> str:
    """The commit this report describes, or `unknown` outside a repository.

    A report that cannot be traced to a commit cannot be reproduced, and a
    missing commit is worth recording as missing rather than omitting.

    Marked `-dirty` when tracked files differ from the commit, because that is
    exactly when the stamp is a lie: the numbers came from code that is not at
    that commit and nobody can re-derive them from it. Five reports were written
    that way before the suffix existed, which is how it was noticed.

    Tracked files only. Untracked ones are not at the commit either, but they
    include every bundle, label and report, so counting them would mark every
    report dirty and the marker would stop meaning anything.
    """
    commit = _git_output(["rev-parse", "HEAD"])
    if commit == "unknown":
        return commit
    return commit if _tree_matches_head() else f"{commit}-dirty"


def _git_output(arguments: list[str]) -> str:
    """One git command's output, or `unknown` when git cannot be run."""
    try:
        result = subprocess.run(
            ["git", *arguments],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def _tree_matches_head() -> bool:
    """Whether tracked files are unmodified.

    `git diff --quiet HEAD` exits non-zero when they differ, which is the answer
    rather than an error. Any other failure counts as modified: over-marking a
    report costs a suffix, and under-marking it claims a reproducibility that was
    never checked.
    """
    try:
        subprocess.run(
            ["git", "diff", "--quiet", "HEAD"],
            capture_output=True,
            check=True,
            cwd=REPO_ROOT,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return True


@dataclass(frozen=True)
class EvalReport:
    """One configuration's results on one split, computed once."""

    configuration: str
    split: str
    trials_per_task: int
    using_recorded_bundles: bool
    generated_at: str
    commit: str
    overall: SplitMetrics
    by_family: dict[str, SplitMetrics]
    recorded_scenarios: int = 0
    total_scenarios: int = 0
    evaluated_scenarios: int = 0
    model_modes: tuple[str, ...] = ()
    model_ids: tuple[str, ...] = ()
    model_calls: int = 0
    fallback_trials: int = 0
    # One score per task per grader, keyed by bundle id, so two runs can be
    # compared after the fact with a paired bootstrap.
    #
    # SPEC.md Section 9.6 asks the JSON to carry per-trial results, and it did
    # not. Without them a comparison could only be made by running both
    # configurations in one process, which means no comparison against a stored
    # baseline and none in CI. Keyed by bundle id rather than positionally,
    # because two runs can cover different tasks and pairing by position would
    # silently compare one configuration's task to another's.
    per_task: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def quotable(self) -> bool:
        """Whether these numbers support a claim about performance.

        Requires a fully recorded held out split. A partly recorded one gives a
        real measurement of a small sample, which is useful for development and
        is not the split's result.
        """
        return (
            self.using_recorded_bundles
            and self.split in RESULT_SPLITS
            and not self.partial_coverage
            and self.recorded_scenarios == self.total_scenarios
            and self.total_scenarios > 0
            and self.evaluated_scenarios == self.total_scenarios
            and self.model_provenance_valid
        )

    @property
    def model_provenance_valid(self) -> bool:
        """Recorded telemetry alone is not evidence that a model ran."""
        return self.configuration == "b0" or (
            bool(self.model_modes)
            and set(self.model_modes) <= {"api", "local"}
            and bool(self.model_ids)
            and self.model_calls > 0
            and self.fallback_trials == 0
        )

    @property
    def partial_coverage(self) -> bool:
        """Whether this split is only partly recorded."""
        return self.using_recorded_bundles and 0 < self.recorded_scenarios < self.total_scenarios

    @property
    def caveats(self) -> tuple[str, ...]:
        notes = []
        if not self.model_provenance_valid:
            notes.append(
                "No complete real-model provenance: stub, replay, unknown mode or fallback."
            )
        if self.evaluated_scenarios != self.total_scenarios:
            notes.append("The run did not evaluate every scenario in the split.")
        if not self.using_recorded_bundles:
            notes.append(SYNTHETIC_WARNING)
        if self.partial_coverage:
            notes.append(
                f"Only {self.recorded_scenarios} of this split's {self.total_scenarios} "
                "scenarios are recorded, so this is a real figure about a small sample "
                "rather than the split's result. Record the rest before quoting it."
            )
        if self.split not in RESULT_SPLITS:
            notes.append(TUNED_SPLIT_WARNING)
        return tuple(notes)

    @property
    def data_source(self) -> str:
        """Named once, so the JSON and the Markdown cannot describe it differently."""
        return "recorded bundles" if self.using_recorded_bundles else "synthetic fixtures"

    def as_dict(self) -> dict[str, object]:
        return {
            "configuration": self.configuration,
            "split": self.split,
            "trials_per_task": self.trials_per_task,
            "data_source": self.data_source,
            "recorded_scenarios": self.recorded_scenarios,
            "total_scenarios": self.total_scenarios,
            "evaluated_scenarios": self.evaluated_scenarios,
            "model_modes": list(self.model_modes),
            "model_ids": list(self.model_ids),
            "model_calls": self.model_calls,
            "fallback_trials": self.fallback_trials,
            "quotable_as_a_result": self.quotable,
            "caveats": list(self.caveats),
            "generated_at": self.generated_at,
            "commit": self.commit,
            "mode": "bundle",
            "overall": self.overall.as_dict(),
            "by_family": {name: m.as_dict() for name, m in self.by_family.items()},
            "per_task": {task: dict(scores) for task, scores in sorted(self.per_task.items())},
        }


def build_report(result: RunResult, resamples: int = BOOTSTRAP_RESAMPLES) -> EvalReport:
    """Compute every metric once, for both renderers."""
    overall = compute_metrics(
        result.sheets,
        result.outcomes,
        trials_per_task=result.trials_per_task,
        resamples=resamples,
    )
    by_family = group_by(
        result.sheets, result.outcomes, result.family_of_bundle(), resamples=resamples
    )
    return EvalReport(
        per_task=per_task_table(result.sheets),
        configuration=result.configuration,
        split=result.split,
        trials_per_task=result.trials_per_task,
        using_recorded_bundles=result.using_recorded_bundles,
        generated_at=datetime.now(UTC).isoformat(),
        commit=git_commit(),
        overall=overall,
        by_family=by_family,
        recorded_scenarios=result.recorded_scenarios,
        total_scenarios=result.total_scenarios,
        evaluated_scenarios=len({t.scenario_id for t in result.trials}),
        model_modes=tuple(sorted({o.model_mode for o in result.outcomes})),
        model_ids=tuple(sorted({name for o in result.outcomes for name in o.model_ids})),
        model_calls=sum(o.model_calls for o in result.outcomes),
        fallback_trials=sum(o.used_floor for o in result.outcomes),
    )


def _rate(summary: GraderSummary) -> str:
    """Render a grader summary as `n/m (xx%)`, or as not claimed."""
    if summary.accuracy is None:
        return "not claimed"
    return f"{summary.correct}/{summary.applicable} ({summary.accuracy:.0%})"


def _interval(metrics: SplitMetrics) -> str:
    if metrics.headline is None:
        return "no trials"
    interval = metrics.headline
    return f"{interval.estimate:.1%} [{interval.lower:.1%}, {interval.upper:.1%}]"


def render_markdown(report: EvalReport) -> str:
    """The human readable form.

    Leads with the caveats rather than ending with them. A reader who stops
    after the first table should already know whether these numbers mean
    anything.
    """
    lines: list[str] = [
        f"# Eval report: {report.configuration} on {report.split}",
        "",
        f"- Configuration: `{report.configuration}`",
        f"- Split: `{report.split}`",
        f"- Trials per task: {report.trials_per_task}",
        f"- Tasks: {report.overall.tasks}, trials: {report.overall.trials}",
        f"- Data source: {report.data_source}"
        + (
            f" ({report.recorded_scenarios} of {report.total_scenarios} scenarios recorded)"
            if report.using_recorded_bundles
            else ""
        ),
        f"- Commit: `{report.commit}`",
        f"- Generated: {report.generated_at}",
        "",
    ]

    if report.caveats:
        lines.append("## Read this first")
        lines.append("")
        for caveat in report.caveats:
            lines.append(f"**{caveat}**")
            lines.append("")

    lines.extend(
        [
            "## Headline",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Correct, with 95% interval | {_interval(report.overall)} |",
        ]
    )
    if report.overall.median_onset_error_seconds is not None:
        lines.append(f"| Median onset error | {report.overall.median_onset_error_seconds:.0f}s |")
    if report.overall.reliability is not None:
        rel = report.overall.reliability
        lines.append(f"| pass@1 | {rel.pass_at_1:.1%} |")
        lines.append(f"| pass^{rel.k} | {rel.pass_hat_k:.1%} |")
    if report.overall.calibration is not None:
        cal = report.overall.calibration
        lines.append(f"| Expected calibration error | {cal.expected_calibration_error:.4f} |")
        lines.append(f"| Brier score | {cal.brier:.4f} |")
    if report.overall.selective is not None:
        sel = report.overall.selective
        lines.append(f"| Coverage | {sel.coverage:.0%} |")
        lines.append(f"| Selective accuracy | {sel.selective_accuracy:.1%} |")
    lines.append(f"| Mean tool calls | {report.overall.cost.mean_tool_calls:.2f} |")
    lines.append("")

    lines.extend(["## Graders", "", "| Grader | Result | Coverage |", "|---|---|---|"])
    for name, summary in report.overall.graders.items():
        lines.append(f"| `{name}` | {_rate(summary)} | {summary.coverage:.0%} |")
    lines.append("")

    if report.by_family:
        lines.extend(
            ["## By family", "", "| Family | Tasks | Correct | Coverage |", "|---|---|---|---|"]
        )
        for family, metrics in report.by_family.items():
            coverage = f"{metrics.selective.coverage:.0%}" if metrics.selective else "n/a"
            lines.append(f"| {family} | {metrics.tasks} | {_interval(metrics)} | {coverage} |")
        lines.append("")

    if report.overall.calibration is not None:
        lines.extend(
            [
                "## Reliability diagram",
                "",
                "| Confidence bin | Predictions | Mean confidence | Accuracy | Gap |",
                "|---|---|---|---|---|",
            ]
        )
        for bin_ in report.overall.calibration.bins:
            if bin_.count == 0:
                continue
            lines.append(
                f"| {bin_.lower:.1f} to {bin_.upper:.1f} | {bin_.count} | "
                f"{bin_.mean_confidence:.3f} | {bin_.accuracy:.3f} | {bin_.gap:+.3f} |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def report_paths(report: EvalReport, root: Path = REPORTS_DIR) -> tuple[Path, Path]:
    """Where a report is written.

    `reports/eval/<config>/<split>/<date>_<commit>.{json,md}`, as SPEC.md
    Section 9.6 specifies. The commit in the filename is what makes two runs of
    different code distinguishable at a glance in a directory listing, which is
    the moment it matters.

    A dirty run keeps the marker in the filename too. Without it a run from a
    modified tree and a run from the committed one would write to the same path,
    and the second would silently replace the first's numbers with different ones
    under a name that claims they came from the same code.
    """
    day = report.generated_at[:10]
    stem = f"{day}_{report.commit[:12]}"
    if report.commit.endswith("-dirty"):
        stem += "-dirty"
    directory = root / report.configuration / report.split
    return directory / f"{stem}.json", directory / f"{stem}.md"


def write_report(report: EvalReport, root: Path = REPORTS_DIR) -> tuple[Path, Path]:
    """Write both formats and return where they went."""
    json_path, markdown_path = report_paths(report, root)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path
