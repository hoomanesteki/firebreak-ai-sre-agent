"""The `firebreak` command line application.

Subcommands are added as their phases land. Phase 0 ships `version` and
`config`, which are enough to prove the package installs and reads settings.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from firebreak import __version__
from firebreak.cli.lab import lab_app
from firebreak.settings import load_settings

app = typer.Typer(
    name="firebreak",
    help="Multi-agent AI SRE that investigates incidents and cites evidence.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
app.add_typer(lab_app, name="lab")


@app.command()
def version() -> None:
    """Print the installed Firebreak version."""
    console.print(__version__)


@app.command()
def config() -> None:
    """Print the resolved settings, without secret values."""
    settings = load_settings()
    table = Table(title="Firebreak settings")
    table.add_column("Setting")
    table.add_column("Value")
    secret_fields = {"llm_api_key"}
    for name, value in settings.model_dump().items():
        shown = "set" if name in secret_fields and value else str(value)
        if name in secret_fields and not value:
            shown = "unset"
        table.add_row(name, shown)
    console.print(table)


def main() -> None:
    """Console script entry point."""
    app()


if __name__ == "__main__":
    main()
