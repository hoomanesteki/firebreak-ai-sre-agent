"""Agent spans, following the OpenTelemetry GenAI conventions, carrying no prompts.

SPEC.md Section 6.12 and Section 17 Phase 11. One investigation yields one trace, with
a child span per node, per tool call and per model call, using the `gen_ai.*` attribute
names from the installed semantic conventions package rather than names invented here.

**No prompt content in spans by default, and that is the reviewer focus for this
phase.** Three reasons, in increasing order of how much they matter:

1. Volume. A prompt is kilobytes and a span attribute is meant to be a label; a trace
   backend holding every prompt of every investigation is a trace backend nobody queries.
2. Cost. Those kilobytes are stored, indexed and retained.
3. **Telemetry is the wrong place for content, because it goes somewhere else.** Traces
   are shipped to a collector, often a shared one, often retained longer than anything
   else, and read by people who were never given access to the incident. An incident's
   logs can contain customer identifiers, internal hostnames, and whatever an attacker
   put in a product name. Firebreak reads all of that and puts some of it in a prompt. A
   span carrying the prompt carries all of it, outward, by default.

So spans carry shapes and sizes: which node, which tool, which tier, how many tokens,
how many rows, whether it succeeded. `include_content=True` exists for a developer
debugging one run locally, is off by default, and is named for what it does.

**What is recorded instead of content.** A hash of the prompt, which is enough to tell
whether two calls sent the same thing, and the prompt's own version stamp, which is
enough to know which prompt file produced a run. Both answer the questions a trace is
actually asked without carrying the text.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from opentelemetry import trace
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as gen_ai
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

# The name every Firebreak trace is created under. One tracer, so a collector can route
# on it and a dashboard can filter on it without matching on span names.
TRACER_NAME = "firebreak.agent"

# Attribute names this project adds beyond the GenAI conventions. Prefixed and declared
# here in one place, because an attribute name typed twice is a dashboard that silently
# shows nothing. Anything with a conventional name uses the conventional name instead.
ATTR_INCIDENT = "firebreak.incident.id"
ATTR_NODE = "firebreak.node"
ATTR_CONFIGURATION = "firebreak.configuration"
ATTR_TOOL_ROWS = "firebreak.tool.rows"
ATTR_TOOL_EVIDENCE = "firebreak.tool.evidence_id"
ATTR_TOOL_TRUNCATED = "firebreak.tool.truncated"
ATTR_PROMPT_STAMP = "firebreak.prompt.stamp"
ATTR_PROMPT_HASH = "firebreak.prompt.hash"
ATTR_ESCALATED = "firebreak.model.escalated"
ATTR_REPAIRED = "firebreak.model.repaired"
ATTR_GATE_PASSED = "firebreak.gate.passed"
ATTR_GATE_REMOVED = "firebreak.gate.removed_claims"
ATTR_CONTENT_INCLUDED = "firebreak.content_included"

HASH_LENGTH = 12


class Operation(StrEnum):
    """What a span represents.

    Values match the GenAI convention's `gen_ai.operation.name` vocabulary where one
    exists, so a backend that understands the convention groups them correctly.
    """

    INVOKE_AGENT = "invoke_agent"
    EXECUTE_TOOL = "execute_tool"
    CHAT = "chat"


@dataclass
class TracingOptions:
    """What spans carry.

    `include_content` defaults to False and every call site takes it from here rather
    than deciding for itself, so there is one place to look when asking whether a
    deployment is shipping prompts.
    """

    enabled: bool = True
    include_content: bool = False
    # Recorded on the root span, so a trace can be filtered to one configuration. The
    # ablations differ only in switches, and without this a dashboard would show them
    # as one system.
    configuration: str = "fb-v1"


def hash_text(text: str) -> str:
    """A short hash of some text, for a span attribute.

    Enough to tell whether two calls sent the same prompt, which is the question a trace
    is asked about prompts. Not enough to recover the prompt, which is the point.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:HASH_LENGTH]


def tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)


@dataclass
class SpanRecorder:
    """Creates the spans for one investigation.

    Holds the options rather than taking them per call, so a single object decides
    whether content is included and the decision cannot vary between spans in one trace.
    """

    options: TracingOptions = field(default_factory=TracingOptions)

    @contextmanager
    def investigation(self, incident_id: str) -> Iterator[Span | None]:
        """The root span. One investigation, one trace.

        SPEC.md Section 17 Phase 11's first acceptance criterion: one investigation
        yields one trace with node, tool and model child spans. That holds because this
        is a context manager and everything else runs inside it.
        """
        if not self.options.enabled:
            yield None
            return
        with tracer().start_as_current_span(
            f"invoke_agent firebreak {incident_id}",
            kind=SpanKind.INTERNAL,
        ) as span:
            span.set_attribute(gen_ai.GEN_AI_OPERATION_NAME, Operation.INVOKE_AGENT.value)
            span.set_attribute(gen_ai.GEN_AI_AGENT_NAME, "firebreak")
            span.set_attribute(ATTR_INCIDENT, incident_id)
            span.set_attribute(ATTR_CONFIGURATION, self.options.configuration)
            # Recorded on every trace, so a reader can tell whether a trace with no
            # prompt content is configured that way or simply had no prompts.
            span.set_attribute(ATTR_CONTENT_INCLUDED, self.options.include_content)
            yield span

    @contextmanager
    def node(self, name: str) -> Iterator[Span | None]:
        """One graph node: the commander, a specialist, the critic, the reporter."""
        if not self.options.enabled:
            yield None
            return
        with tracer().start_as_current_span(f"node {name}", kind=SpanKind.INTERNAL) as span:
            span.set_attribute(ATTR_NODE, name)
            yield span

    @contextmanager
    def tool_call(
        self, tool: str, arguments: dict[str, Any] | None = None
    ) -> Iterator[Span | None]:
        """One tool call.

        Arguments are recorded as a hash and a count, not as values. A tool's arguments
        include service names and time windows, which are not secret, and also include
        log search patterns, which a person may have typed. Recording the shape rather
        than the values keeps one rule for the whole trace instead of a judgement per
        field.
        """
        if not self.options.enabled:
            yield None
            return
        with tracer().start_as_current_span(f"execute_tool {tool}", kind=SpanKind.INTERNAL) as span:
            span.set_attribute(gen_ai.GEN_AI_OPERATION_NAME, Operation.EXECUTE_TOOL.value)
            span.set_attribute(gen_ai.GEN_AI_TOOL_NAME, tool)
            if arguments is not None:
                span.set_attribute("firebreak.tool.argument_count", len(arguments))
                if self.options.include_content:
                    span.set_attribute("firebreak.tool.arguments", repr(arguments)[:2000])
            yield span

    @contextmanager
    def model_call(
        self,
        purpose: str,
        tier: str,
        model: str = "",
        prompt_stamp: str = "",
        prompt_body: str = "",
    ) -> Iterator[Span | None]:
        """One model call.

        `prompt_body` is hashed and discarded unless content is included. The stamp is
        recorded either way, because knowing which prompt version produced a run is the
        thing a report needs and it carries no content.
        """
        if not self.options.enabled:
            yield None
            return
        with tracer().start_as_current_span(f"chat {purpose}", kind=SpanKind.CLIENT) as span:
            span.set_attribute(gen_ai.GEN_AI_OPERATION_NAME, Operation.CHAT.value)
            span.set_attribute(ATTR_NODE, purpose)
            span.set_attribute("firebreak.model.tier", tier)
            if model:
                span.set_attribute(gen_ai.GEN_AI_REQUEST_MODEL, model)
            if prompt_stamp:
                span.set_attribute(ATTR_PROMPT_STAMP, prompt_stamp)
            if prompt_body:
                span.set_attribute(ATTR_PROMPT_HASH, hash_text(prompt_body))
                if self.options.include_content:
                    span.set_attribute(gen_ai.GEN_AI_INPUT_MESSAGES, prompt_body[:8000])
            yield span


def record_tool_result(
    span: Span | None,
    rows: int,
    evidence_id: str | None,
    truncated: bool = False,
) -> None:
    """What a tool call returned, as sizes rather than contents."""
    if span is None:
        return
    span.set_attribute(ATTR_TOOL_ROWS, rows)
    span.set_attribute(ATTR_TOOL_TRUNCATED, truncated)
    if evidence_id:
        span.set_attribute(ATTR_TOOL_EVIDENCE, evidence_id)


def record_usage(
    span: Span | None,
    tokens_in: int,
    tokens_out: int,
    usd: float = 0.0,
    escalated: bool = False,
    repaired: bool = False,
) -> None:
    """What a model call cost, using the conventional token attribute names."""
    if span is None:
        return
    span.set_attribute(gen_ai.GEN_AI_USAGE_INPUT_TOKENS, tokens_in)
    span.set_attribute(gen_ai.GEN_AI_USAGE_OUTPUT_TOKENS, tokens_out)
    # Cost has no conventional attribute, so it takes a prefixed name. Recorded even
    # when zero, because a zero from a stub and an absent attribute mean different
    # things and a dashboard should be able to tell them apart.
    span.set_attribute("firebreak.model.usd", usd)
    span.set_attribute(ATTR_ESCALATED, escalated)
    span.set_attribute(ATTR_REPAIRED, repaired)


def record_gate(span: Span | None, passed: bool, removed_claims: int) -> None:
    """What the exit gate concluded, on the root span.

    On the trace rather than only in the report, so a dashboard can show how often
    claims are being removed. That rate is the most useful single number about report
    quality available without a model.
    """
    if span is None:
        return
    span.set_attribute(ATTR_GATE_PASSED, passed)
    span.set_attribute(ATTR_GATE_REMOVED, removed_claims)


def record_failure(span: Span | None, error: BaseException) -> None:
    """Mark a span failed, with the exception type and no message.

    The type, not the message. An exception message from a tool can quote the data that
    caused it, which would put content into a span through the one path that looks like
    an error path rather than a content path.
    """
    if span is None:
        return
    span.set_status(Status(StatusCode.ERROR, type(error).__name__))
    span.set_attribute("firebreak.error.type", type(error).__name__)
