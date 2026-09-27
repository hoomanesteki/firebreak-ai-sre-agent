"""`firebreak eval`: run a configuration over a split and write the report.

SPEC.md Section 9.6 defines the command. `run` measures one configuration on one
split; `compare` puts two of them side by side with a paired interval, which is
the only form a claim that one is better can honestly take.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from firebreak.evals.compare import ComparisonError, LoadedReport, load_report
from firebreak.evals.compare import compare as compare_reports
from firebreak.evals.gate import GateResult, Side, evaluate_gate
from firebreak.evals.report import REPORTS_DIR, build_report, write_report
from firebreak.evals.runner import CONFIGURATIONS, RunnerError, run_configuration
from firebreak.evals.statistics import BOOTSTRAP_RESAMPLES
from firebreak.lab.scenario import Split

eval_app = typer.Typer(help="Run evaluations and write reports.", no_args_is_help=True)
console = Console()


@eval_app.command("run")
def run(
    config: str = typer.Option(..., "--config", help="configuration id, for example b0"),
    split: str = typer.Option(..., "--split", help="train, validation, test_id, test_ood"),
    trials: int = typer.Option(1, "--trials", help="trials per task"),
    limit: int = typer.Option(0, "--limit", help="only the first N tasks, for a quick check"),
    resamples: int = typer.Option(BOOTSTRAP_RESAMPLES, "--resamples", help="bootstrap resamples"),
) -> None:
    """Run one configuration over one split and write JSON and Markdown."""
    try:
        chosen = Split(split)
    except ValueError:
        console.print(
            f"[red]unknown split {split!r}[/red]; known: {', '.join(s.value for s in Split)}"
        )
        raise typer.Exit(code=2) from None

    if config not in CONFIGURATIONS:
        console.print(
            f"[red]unknown configuration {config!r}[/red]; known: "
            f"{', '.join(sorted(CONFIGURATIONS))}"
        )
        raise typer.Exit(code=2)

    # Fixtures are built into a temporary directory and discarded. They are
    # reproducible from the spec and the seed, so keeping them would store
    # something derivable while making the repository larger. A recorded
    # library is read in place and never copied.
    with tempfile.TemporaryDirectory(prefix="firebreak-eval-") as workspace:
        try:
            result = run_configuration(
                config, chosen, Path(workspace), trials_per_task=trials, limit=limit
            )
        except RunnerError as error:
            console.print(f"[red]{error}[/red]")
            raise typer.Exit(code=1) from error
        report = build_report(result, resamples=resamples)

    json_path, markdown_path = write_report(report)

    table = Table(title=f"{report.configuration} on {report.split}")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("tasks", str(report.overall.tasks))
    table.add_row("trials", str(report.overall.trials))
    if report.overall.headline is not None:
        interval = report.overall.headline
        table.add_row(
            "correct",
            f"{interval.estimate:.1%} [{interval.lower:.1%}, {interval.upper:.1%}]",
        )
    if report.overall.selective is not None:
        table.add_row("coverage", f"{report.overall.selective.coverage:.0%}")
        table.add_row("selective accuracy", f"{report.overall.selective.selective_accuracy:.1%}")
    if report.overall.calibration is not None:
        table.add_row("ECE", f"{report.overall.calibration.expected_calibration_error:.4f}")
        table.add_row("Brier", f"{report.overall.calibration.brier:.4f}")
    if report.overall.reliability is not None:
        table.add_row(
            f"pass^{report.overall.reliability.k}",
            f"{report.overall.reliability.pass_hat_k:.1%}",
        )
    table.add_row("data source", report.data_source)
    console.print(table)

    for caveat in report.caveats:
        console.print(f"[yellow]{caveat}[/yellow]")

    console.print(f"wrote {json_path}")
    console.print(f"wrote {markdown_path}")


@eval_app.command("configs")
def configs() -> None:
    """List the configurations that can be run."""
    for name in sorted(CONFIGURATIONS):
        console.print(name)


@eval_app.command("compare")
def compare(
    treatment: str = typer.Option(..., "--treatment", help="the configuration being judged"),
    control: str = typer.Option(..., "--control", help="what to judge it against"),
    split: str = typer.Option(..., "--split", help="train, validation, test_id, test_ood"),
    resamples: int = typer.Option(BOOTSTRAP_RESAMPLES, "--resamples", help="bootstrap resamples"),
) -> None:
    """Compare two written reports on the tasks both covered.

    Reads the reports rather than re-running, so a comparison can be made against
    a stored baseline and in CI. Run both configurations first.
    """
    try:
        first = _latest_report(treatment, split)
        second = _latest_report(control, split)
        result = compare_reports(first, second, resamples=resamples)
    except ComparisonError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=2) from error

    table = Table(title=f"{result.treatment} against {result.control} on {result.split}")
    table.add_column("grader")
    table.add_column(result.treatment, justify="right")
    table.add_column(result.control, justify="right")
    table.add_column("difference [95%]", justify="right")
    table.add_column("tasks", justify="right")
    for grader in result.graders:
        verdict = "" if grader.separates else " (spans zero)"
        table.add_row(
            grader.grader,
            f"{grader.treatment_mean:.3f}",
            f"{grader.control_mean:.3f}",
            f"{grader.difference.estimate:+.3f} "
            f"[{grader.difference.lower:+.3f}, {grader.difference.upper:+.3f}]{verdict}",
            str(grader.paired_tasks),
        )
    console.print(table)
    console.print(f"{result.tasks_in_common} task(s) in common")
    if result.treatment_only or result.control_only:
        console.print(
            f"{len(result.treatment_only)} task(s) only {result.treatment} covered, "
            f"{len(result.control_only)} only {result.control}; those are excluded"
        )
    if not any(g.separates for g in result.graders):
        console.print(
            "No grader separates these two configurations on this data. That is a "
            "result about the sample size as much as about the systems."
        )


def _latest_report(configuration: str, split: str) -> LoadedReport:
    """The most recently written report for one configuration and split.

    By modification time rather than by filename. The filename carries a date and
    a commit, and two runs on the same day sort by commit hash, which has nothing
    to do with which came last.
    """
    try:
        chosen = Split(split)
    except ValueError as error:
        raise ComparisonError(
            f"unknown split {split!r}; known: {', '.join(s.value for s in Split)}"
        ) from error
    directory = REPORTS_DIR / configuration / chosen.value
    if not directory.is_dir():
        raise ComparisonError(f"no reports for {configuration} on {chosen.value}; run it first")
    written = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime)
    if not written:
        raise ComparisonError(f"no reports for {configuration} on {chosen.value}; run it first")
    return load_report(written[-1])


@eval_app.command("gate")
def gate(
    candidate: str = typer.Option(..., "--candidate", help="the configuration being judged"),
    baseline: str = typer.Option(..., "--baseline", help="what it must not be worse than"),
    split: str = typer.Option(..., "--split", help="train, validation, test_id, test_ood"),
    trials: int = typer.Option(1, "--trials", help="trials per task"),
    limit: int = typer.Option(0, "--limit", help="only the first N tasks"),
    comment: Path = typer.Option(
        None, "--comment", help="write a Markdown verdict here, for a CI comment"
    ),
) -> None:
    """Run both configurations and apply the non-inferiority gate.

    SPEC.md Section 9.4. Runs them rather than reading reports, because the gate
    needs both sides graded on the same tasks in the same run, and two reports
    written at different times may cover different tasks.

    Exits non-zero on anything but PASS, so CI can gate on it. INCONCLUSIVE exits
    non-zero too: a gate that passes for lack of evidence is worse than no gate,
    because it produces a signed statement that nothing was checked.
    """
    try:
        chosen = Split(split)
    except ValueError:
        console.print(f"[red]unknown split {split!r}[/red]")
        raise typer.Exit(code=2) from None

    with tempfile.TemporaryDirectory(prefix="firebreak-gate-") as workspace:
        try:
            sides = {
                name: run_configuration(
                    name, chosen, Path(workspace), trials_per_task=trials, limit=limit
                )
                for name in (candidate, baseline)
            }
        except RunnerError as error:
            console.print(f"[red]{error}[/red]")
            raise typer.Exit(code=2) from error

    result = evaluate_gate(
        Side(
            sheets=tuple(sides[candidate].sheets),
            outcomes=tuple(sides[candidate].outcomes),
        ),
        Side(
            sheets=tuple(sides[baseline].sheets),
            outcomes=tuple(sides[baseline].outcomes),
        ),
    )

    colour = "green" if result.passed else "red"
    console.print(f"[{colour}]{result.verdict.value}[/{colour}] {result.summary}")
    table = Table(title=f"{candidate} against {baseline} on {chosen.value}")
    table.add_column("metric")
    table.add_column("difference [95%]", justify="right")
    table.add_column("margin", justify="right")
    table.add_column("verdict")
    for check in result.checks:
        table.add_row(
            check.name,
            f"{check.difference.estimate:+.3f} "
            f"[{check.difference.lower:+.3f}, {check.difference.upper:+.3f}]",
            f"{check.margin:.3f}",
            "ok" if check.passed else "fail",
        )
    console.print(table)

    if comment is not None:
        comment.parent.mkdir(parents=True, exist_ok=True)
        comment.write_text(_gate_comment(result, candidate, baseline, chosen), encoding="utf-8")
        console.print(f"wrote {comment}")

    if not result.passed:
        raise typer.Exit(code=1)


def _gate_comment(result: GateResult, candidate: str, baseline: str, split: Split) -> str:
    """The Markdown a CI job posts on a pull request.

    Written to a file rather than posted from here, so the credential that can
    comment on a pull request stays in the workflow and never enters this process.
    """
    lines = [
        f"## Eval gate: `{result.verdict.value}`",
        "",
        f"`{candidate}` against `{baseline}` on `{split.value}`.",
        "",
        result.summary,
        "",
        "| metric | difference [95%] | margin | verdict |",
        "|---|---|---|---|",
    ]
    for check in result.checks:
        lines.append(
            f"| {check.name} | {check.difference.estimate:+.3f} "
            f"[{check.difference.lower:+.3f}, {check.difference.upper:+.3f}] "
            f"| {check.margin:.3f} | {'ok' if check.passed else '**fail**'} |"
        )
    lines += [
        "",
        "The test is on the lower bound of a paired difference, not the point "
        "estimate: a candidate two points behind with a wide interval has not been "
        "shown to be worse, and one two points behind with a narrow interval has.",
    ]
    return "\n".join(lines) + "\n"
