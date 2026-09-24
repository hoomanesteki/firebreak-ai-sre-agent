"""The shape every tool has, and the rules none of them can opt out of.

A tool is a typed function with a description written for a model to read.
Four properties are enforced here rather than in each tool, because a rule
that lives in fourteen places is a rule that is wrong in one of them.

**Bounded output.** A tool returns a compact summary and an evidence id, not
the rows it found. SPEC.md principle H4: context is a finite resource, and a
tool that returns everything spends it all on one call. The rows stay in the
evidence store and are fetched by id when actually needed.

**Evidence for everything.** Every call that touches data records what it
asked and what came back. That is what makes the exit gate possible: a claim
cites an id, and the gate re-runs it.

**A cap per tool per investigation.** SPEC.md Section 11 lists tool misuse
as ASI02. An agent in a loop calling the same tool five hundred times is not
a bug in the tool.

**Read only.** No tool here writes anything. The single write path in
Firebreak is the approval service, which is a separate process holding the
only credentials that can change the target system.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from firebreak.backends.base import QueryBackend
from firebreak.tools.evidence import EvidenceRecord, EvidenceStore

# How many times one tool may be called during a single investigation.
# Generous enough that a real investigation never notices, small enough that
# a loop is stopped long before it costs anything.
MAX_CALLS_PER_TOOL = 40

# A summary longer than this is a tool returning its result rather than
# describing it.
MAX_SUMMARY_CHARS = 600


class ToolError(Exception):
    """A tool could not answer, or was asked something it must refuse."""


class ToolCapExceededError(ToolError):
    """This tool has already been called as many times as it may be."""


class UnknownToolError(ToolError):
    """No tool is registered under that name."""


class ToolResult(BaseModel):
    """What a tool hands back.

    The summary is what a model reads. `data` is small, structured, and
    meant for code: the hypothesis board reads it, a prompt does not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    summary: str = Field(max_length=MAX_SUMMARY_CHARS)
    evidence_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False

    def cite(self) -> tuple[str, ...]:
        """The evidence ids a claim built on this result may cite."""
        return (self.evidence_id,) if self.evidence_id else ()


@dataclass
class ToolContext:
    """Everything a tool is allowed to reach.

    Deliberately small. A tool gets a backend to ask questions of and a
    store to record answers in, and nothing else: no filesystem, no network
    client, no configuration it could reinterpret. SPEC.md principle H12,
    least agency.
    """

    backend: QueryBackend
    evidence: EvidenceStore = field(default_factory=EvidenceStore)
    max_calls_per_tool: int = MAX_CALLS_PER_TOOL
    call_counts: dict[str, int] = field(default_factory=dict)

    def record(self, record: EvidenceRecord) -> EvidenceRecord:
        return self.evidence.add(record)

    def note_call(self, tool_name: str) -> int:
        """Count a call, refusing once the cap is reached."""
        used = self.call_counts.get(tool_name, 0)
        if used >= self.max_calls_per_tool:
            raise ToolCapExceededError(
                f"{tool_name} has been called {used} times, the limit is "
                f"{self.max_calls_per_tool}; the question is not being answered by "
                "asking it again"
            )
        self.call_counts[tool_name] = used + 1
        return used + 1

    def calls_made(self, tool_name: str) -> int:
        return self.call_counts.get(tool_name, 0)

    def total_calls(self) -> int:
        return sum(self.call_counts.values())


Handler = Callable[[ToolContext, Any], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    """One tool: its name, what it is for, its input type, and its body.

    The description is written to the rules in SPEC.md Section 6.3: what the
    tool is for, when to use it, and when not to. The last part matters
    most. Fourteen tools with overlapping purposes and no guidance on which
    to reach for is how an agent ends up calling four of them to answer one
    question.
    """

    name: str
    description: str
    input_model: type[BaseModel]
    handler: Handler

    def parse(self, arguments: dict[str, Any]) -> BaseModel:
        """Validate arguments, refusing anything the tool does not declare."""
        try:
            return self.input_model.model_validate(arguments)
        except ValueError as error:
            raise ToolError(f"{self.name} rejected its arguments: {error}") from error


class ToolRegistry:
    """The set of tools available, and the only way to call one.

    Going through the registry is what makes the caps and the argument
    validation unavoidable. A node given a bare function could skip both.
    """

    def __init__(self, specs: list[ToolSpec] | None = None) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs or []:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ToolError(f"a tool named {spec.name!r} is already registered")
        self._specs[spec.name] = spec

    def __contains__(self, name: str) -> bool:
        return name in self._specs

    def __len__(self) -> int:
        return len(self._specs)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def spec(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as error:
            raise UnknownToolError(
                f"no tool named {name!r}; available: {', '.join(self.names())}"
            ) from error

    def subset(self, names: tuple[str, ...]) -> ToolRegistry:
        """A registry holding only some tools.

        Each specialist gets its own tools and no others, so a logs analyst
        cannot quietly start reading traces and report a finding nobody
        asked it for. SPEC.md Section 6.6.
        """
        return ToolRegistry([self.spec(name) for name in names])

    def describe(self) -> list[dict[str, str]]:
        """Name and description of every tool, for a prompt or a review."""
        return [
            {"name": spec.name, "description": spec.description}
            for spec in sorted(self._specs.values(), key=lambda s: s.name)
        ]

    def call(self, name: str, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        """Validate, count, and run one tool call."""
        spec = self.spec(name)
        parsed = spec.parse(arguments)
        context.note_call(name)
        result = spec.handler(context, parsed)
        if result.tool != name:
            raise ToolError(f"{name} returned a result labelled {result.tool!r}")
        return result


def summarise_rows(rows: list[dict[str, Any]], limit: int = 5) -> str:
    """A short, readable description of the top few rows.

    Used by tools whose answer is a ranking. Deliberately terse: the point
    of a summary is to let a model decide whether to look closer, not to
    save it the trouble.
    """
    if not rows:
        return "no matching rows"
    shown = rows[:limit]
    parts = [", ".join(f"{k}={v}" for k, v in row.items()) for row in shown]
    more = len(rows) - len(shown)
    text = "; ".join(parts)
    return f"{text}; and {more} more" if more > 0 else text
