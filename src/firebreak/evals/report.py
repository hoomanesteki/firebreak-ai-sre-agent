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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from firebreak.evals.metrics import GraderSummary, SplitMetrics, compute_metrics, group_by
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
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return "unknown"
    return result.stdout.strip() or "unknown"


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

    @property
    def quotable(self) -> bool:
        """Whether these numbers support a claim about performance."""
        return self.using_recorded_bundles and self.split in RESULT_SPLITS

    @property
    def caveats(self) -> tuple[str, ...]:
        notes = []
        if not self.using_recorded_bundles:
            notes.append(SYNTHETIC_WARNING)
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
            "quotable_as_a_result": self.quotable,
            "caveats": list(self.caveats),
            "generated_at": self.generated_at,
            "commit": self.commit,
            "mode": "bundle",
            "overall": self.overall.as_dict(),
            "by_family": {name: m.as_dict() for name, m in self.by_family.items()},
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
        configuration=result.configuration,
        split=result.split,
        trials_per_task=result.trials_per_task,
        using_recorded_bundles=result.using_recorded_bundles,
        generated_at=datetime.now(UTC).isoformat(),
        commit=git_commit(),
        overall=overall,
        by_family=by_family,
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
        f"- Data source: {report.data_source}",
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
    """
    day = report.generated_at[:10]
    stem = f"{day}_{report.commit[:12]}"
    directory = root / report.configuration / report.split
    return directory / f"{stem}.json", directory / f"{stem}.md"


def write_report(report: EvalReport, root: Path = REPORTS_DIR) -> tuple[Path, Path]:
    """Write both formats and return where they went."""
    json_path, markdown_path = report_paths(report, root)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path
