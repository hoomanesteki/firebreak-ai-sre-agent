"""`firebreak graph`: validate the knowledge files and load them into Neo4j.

Three commands, in the order a person uses them. `knowledge` reads and cross
checks the hand written files and needs no database, which matters because
that is the check that fails most often and waiting for a container to
answer it would be wasted time. `load` applies the schema and writes.
`check` proves the load was idempotent.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from firebreak.graph.knowledge import KnowledgeError, load_knowledge
from firebreak.graph.load import (
    GraphError,
    apply_schema,
    connect,
    graph_fingerprint,
    load_knowledge_graph,
)
from firebreak.settings import load_settings

graph_app = typer.Typer(help="Knowledge graph: validate, load, and verify.", no_args_is_help=True)
console = Console()


@graph_app.command("knowledge")
def check_knowledge() -> None:
    """Validate knowledge/ and print what it contains. No database needed."""
    try:
        facts = load_knowledge()
    except KnowledgeError as error:
        console.print(f"[red]knowledge files are not valid:[/red] {error}")
        raise typer.Exit(code=1) from error

    table = Table(title="Knowledge files")
    table.add_column("Kind")
    table.add_column("Count", justify="right")
    table.add_row("services", str(len(facts.services)))
    table.add_row("teams", str(len(facts.teams)))
    table.add_row("remediations", str(len(facts.remediations)))
    table.add_row("runbooks", str(len(facts.runbooks)))
    console.print(table)

    # A service nobody wrote a runbook for is not an error, because not every
    # service needs one. It is worth naming, since the gap only becomes
    # visible at three in the morning otherwise.
    uncovered = sorted(name for name in facts.service_names if not facts.runbooks_for(name))
    if uncovered:
        console.print(f"[yellow]no runbook covers:[/yellow] {', '.join(uncovered)}")
    console.print("[green]knowledge files are valid and cross referenced[/green]")


@graph_app.command("load")
def load() -> None:
    """Apply the schema and load the knowledge files into Neo4j."""
    settings = load_settings()
    try:
        facts = load_knowledge()
    except KnowledgeError as error:
        console.print(f"[red]knowledge files are not valid:[/red] {error}")
        raise typer.Exit(code=1) from error

    try:
        with connect(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password) as driver:
            statements = apply_schema(driver, settings.neo4j_database)
            counts = load_knowledge_graph(driver, facts, settings.neo4j_database)
            fingerprint = graph_fingerprint(driver, settings.neo4j_database)
    except GraphError as error:
        console.print(f"[red]{error}[/red]")
        console.print("start one with: make graph-up")
        raise typer.Exit(code=1) from error

    table = Table(title="Loaded into Neo4j")
    table.add_column("Kind")
    table.add_column("Count", justify="right")
    for name, count in counts.as_dict().items():
        table.add_row(name, str(count))
    console.print(table)
    console.print(f"schema statements applied: {statements}")
    console.print(f"graph fingerprint: {fingerprint[:16]}")


@graph_app.command("check")
def check() -> None:
    """Load a second time and prove nothing changed.

    SPEC.md Section 6.4 asks for idempotence to be tested by comparing a
    checksum after two loads. A load that appended rather than merged would
    double every edge, which changes no proportion and so changes no
    ranking, right up until a partial reload doubled some edges and not
    others.
    """
    settings = load_settings()
    try:
        facts = load_knowledge()
        with connect(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password) as driver:
            before = graph_fingerprint(driver, settings.neo4j_database)
            load_knowledge_graph(driver, facts, settings.neo4j_database)
            after = graph_fingerprint(driver, settings.neo4j_database)
    except (GraphError, KnowledgeError) as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from error

    if before != after:
        console.print("[red]the load is not idempotent[/red]")
        console.print(f"before: {before}")
        console.print(f"after:  {after}")
        raise typer.Exit(code=1)
    console.print(f"[green]idempotent[/green], fingerprint unchanged at {before[:16]}")
