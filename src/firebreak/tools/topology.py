"""Topology tools: who a service talks to, and who would feel it if it broke.

Two tools over the same graph, `backend.topology()`, with different bounds
on how far they walk it.

- `service_dependencies` answers "what is near this service", upstream
  callers, downstream callees, or both, out to a depth the caller chooses
  and that is capped at 3. It is for understanding one service's immediate
  neighbourhood, for example before deciding whether an error in it is
  really its own or inherited from something upstream.
- `blast_radius` answers "who is affected if this breaks", the full,
  uncapped set of services that transitively depend on it. It is for
  scoping the consequences of a root cause candidate once one is named.

Neither tool takes a window. The graph itself is a structural snapshot,
already scoped to whichever window it was recorded or queried over
(SPEC.md Section 8, `topology.json`), and not every backend's payload even
carries that window explicitly back out, so a fixed sentinel stands in for
the evidence record's window field rather than a value this code cannot
reliably reconstruct.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from firebreak.lab.bundle import EdgeRecord
from firebreak.tools.base import (
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
    summarise_rows,
)
from firebreak.tools.evidence import (
    NO_WINDOW,
    EvidenceKind,
    Fact,
    build_record,
)

# See the module docstring: topology has no caller-chosen window, and not
# every backend's topology payload carries one back out (a synthetic bundle
# built for testing has none at all). A fixed sentinel keeps the evidence id
# for the same question deterministic without depending on a field that
# cannot be assumed present.

# SPEC.md Section 6.3: a window longer than the cap is refused rather than
# answered, because answering it politely would hide the mistake. The same
# reasoning bounds depth here: a graph query with no limit can return most
# of the system and stop being a neighbourhood query at all.
MAX_DEPTH = 3

# How many affected services a blast_radius summary names before falling
# back to "and N more".
TOP_AFFECTED_IN_SUMMARY = 10


class Direction(StrEnum):
    """Which way to walk the call graph from the named service."""

    UPSTREAM = "upstream"
    DOWNSTREAM = "downstream"
    BOTH = "both"


class ServiceDependenciesInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    service: str
    direction: Direction = Direction.BOTH
    depth: int = Field(default=1, ge=1, le=MAX_DEPTH)


class BlastRadiusInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    service: str


def _edges(topology: dict[str, Any]) -> list[dict[str, Any]]:
    raw = topology.get("edges")
    return raw if isinstance(raw, list) else []


def _edge_row(edge: dict[str, Any]) -> dict[str, Any]:
    """One edge, validated against the single declared edge schema.

    Deliberately strict. An earlier version read the fields defensively so
    it would work against either of two shapes two producers happened to
    write, and the cost of that kindness was that every edge from one of
    them silently reported no traffic at all. Validating here means a
    producer that drifts fails loudly, which is the only way the drift gets
    fixed rather than accommodated.
    """
    try:
        return EdgeRecord.model_validate(
            {k: v for k, v in edge.items() if k != "error_ratio"}
        ).as_row()
    except ValueError as error:
        raise ToolError(f"topology edge does not match the bundle schema: {error}") from error


def _neighbours(
    edges: list[dict[str, Any]], service: str, direction: Direction
) -> list[dict[str, Any]]:
    """One hop of edges in the given direction from `service`."""
    if direction is Direction.UPSTREAM:
        return [edge for edge in edges if edge["server"] == service]
    return [edge for edge in edges if edge["client"] == service]


def _walk(
    edges: list[dict[str, Any]], service: str, direction: Direction, depth: int
) -> list[dict[str, Any]]:
    """Breadth-first, one direction, up to `depth` hops, each service once."""
    visited = {service}
    frontier = {service}
    rows: list[dict[str, Any]] = []
    for level in range(1, depth + 1):
        next_frontier: set[str] = set()
        for current in sorted(frontier):
            for edge in _neighbours(edges, current, direction):
                neighbour = edge["client"] if direction is Direction.UPSTREAM else edge["server"]
                if neighbour in visited:
                    continue
                rows.append(
                    {**edge, "neighbour": neighbour, "direction": direction.value, "depth": level}
                )
                next_frontier.add(neighbour)
        visited |= next_frontier
        frontier = next_frontier
        if not frontier:
            break
    return rows


def _transitive_upstream(edges: list[dict[str, Any]], service: str) -> list[str]:
    """Every service that transitively depends on `service`.

    Not depth-capped the way _walk is: blast_radius answers "how many", and
    an artificial depth limit could hide a real dependent one hop past it.
    The visited set is what stops this looping forever on a cyclic graph,
    not a depth bound.
    """
    visited = {service}
    frontier = {service}
    reached: list[str] = []
    while frontier:
        next_frontier: set[str] = set()
        for current in frontier:
            for edge in edges:
                if edge["server"] == current and edge["client"] not in visited:
                    next_frontier.add(edge["client"])
        for neighbour in sorted(next_frontier):
            reached.append(neighbour)
            visited.add(neighbour)
        frontier = next_frontier
    return reached


def service_dependencies(context: ToolContext, arguments: ServiceDependenciesInput) -> ToolResult:
    """Walk the call graph from one service, up to a bounded depth."""
    topology = context.backend.topology()
    edges = [_edge_row(edge) for edge in _edges(topology)]

    directions = (
        (Direction.UPSTREAM, Direction.DOWNSTREAM)
        if arguments.direction is Direction.BOTH
        else (arguments.direction,)
    )
    rows: list[dict[str, Any]] = []
    for direction in directions:
        rows.extend(_walk(edges, arguments.service, direction, arguments.depth))

    facts = [Fact(field="reached", value=float(len(rows)), unit="count", subject=arguments.service)]

    record = context.record(
        build_record(
            kind=EvidenceKind.TOPOLOGY,
            query="service_dependencies",
            parameters={
                "service": arguments.service,
                "direction": arguments.direction.value,
                "depth": arguments.depth,
            },
            window=NO_WINDOW,
            fingerprint=context.backend.fingerprint(),
            rows=rows,
            facts=facts,
        )
    )
    summary = (
        f"{len(rows)} services {arguments.direction.value} of {arguments.service} "
        f"within depth {arguments.depth}: {summarise_rows(rows)}"
        if rows
        else f"nothing {arguments.direction.value} of {arguments.service} within depth "
        f"{arguments.depth}"
    )
    return ToolResult(
        tool="service_dependencies",
        summary=summary[:600],
        evidence_id=record.id,
        data={"dependencies": rows[:20]},
        truncated=len(rows) > 20,
    )


def blast_radius(context: ToolContext, arguments: BlastRadiusInput) -> ToolResult:
    """Every service that transitively depends on one service."""
    topology = context.backend.topology()
    edges = [_edge_row(edge) for edge in _edges(topology)]
    reached = _transitive_upstream(edges, arguments.service)

    rows = [{"service": name} for name in reached]
    facts = [
        Fact(field="reached", value=float(len(reached)), unit="count", subject=arguments.service)
    ]

    record = context.record(
        build_record(
            kind=EvidenceKind.TOPOLOGY,
            query="blast_radius",
            parameters={"service": arguments.service},
            window=NO_WINDOW,
            fingerprint=context.backend.fingerprint(),
            rows=rows,
            facts=facts,
        )
    )
    if reached:
        shown = reached[:TOP_AFFECTED_IN_SUMMARY]
        more = len(reached) - len(shown)
        tail = f", and {more} more" if more > 0 else ""
        summary = f"{len(reached)} services transitively depend on {arguments.service}: " + (
            ", ".join(shown) + tail
        )
    else:
        summary = f"nothing transitively depends on {arguments.service}"

    return ToolResult(
        tool="blast_radius",
        summary=summary[:600],
        evidence_id=record.id,
        data={"affected_services": rows[:20], "count": len(reached)},
        truncated=len(reached) > 20,
    )


SERVICE_DEPENDENCIES = ToolSpec(
    name="service_dependencies",
    description=(
        "Walk the service call graph from one named service: who calls it "
        "(upstream), who it calls (downstream), or both, out to a depth of at most "
        "3 hops. Use this to understand a service's immediate neighbourhood, for "
        "example before reading its logs, so an error caused by something it "
        "depends on is not mistaken for a fault inside the service itself. Depth "
        "above 3 is refused rather than answered, since an unbounded graph walk can "
        "return most of the system and stop being a neighbourhood. Use blast_radius "
        "instead when the question is the full set of services put at risk by one "
        "failing, not its nearby neighbours."
    ),
    input_model=ServiceDependenciesInput,
    handler=service_dependencies,
)

BLAST_RADIUS = ToolSpec(
    name="blast_radius",
    description=(
        "Every service that transitively depends on one service, however many hops "
        "away, so a reader can see the full set of things at risk if it fails. Use "
        "this once a root cause candidate is named, to scope the consequences of its "
        "failure. It has no depth limit by design, since a dependent three hops away "
        "is still a dependent that will feel the outage. Use service_dependencies "
        "instead when only the immediate neighbourhood matters, when the direction "
        "should be downstream, or when a bounded depth is what the question needs."
    ),
    input_model=BlastRadiusInput,
    handler=blast_radius,
)

TOPOLOGY_TOOLS = (SERVICE_DEPENDENCIES, BLAST_RADIUS)
