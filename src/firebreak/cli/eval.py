"""`firebreak eval`: run a configuration over a split and write the report.

SPEC.md Section 9.6 defines the command. One subcommand today, `run`, because
one configuration exists; the comparison subcommand arrives with B1 in Phase 6,
when there is something to compare against.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from firebreak.evals.report import build_report, write_report
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
