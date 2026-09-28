"""Tests for the agent's own spans.

Two things are tested hardest. That one investigation produces one trace with node, tool
and model child spans, which is SPEC.md Section 17 Phase 11's first acceptance criterion.
And that no prompt content leaves the process by default, which is that phase's reviewer
focus and the one failure here with consequences outside this repository: traces go to a
collector, often shared, often retained longest, read by people who were never given
access to the incident.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from firebreak.agent.graph import investigate
from firebreak.lab.bundle import derive_bundle_id
from firebreak.lab.scenario import load_library
from firebreak.lab.synthetic import build_synthetic_bundle
from firebreak.telemetry.spans import (
    ATTR_CONTENT_INCLUDED,
    ATTR_GATE_PASSED,
    ATTR_INCIDENT,
    ATTR_MODEL_PURPOSE,
    ATTR_NODE,
    ATTR_PROMPT_HASH,
    SpanRecorder,
    TracingOptions,
    hash_text,
    record_usage,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SPECS_DIR = REPO_ROOT / "scenarios" / "specs"
FAULTED = "payment-failure-50pct-20u"

# Any attribute name that could carry free text from an incident or a prompt. Listed by
# what they would contain rather than by prefix, because the risk is the content and not
# the naming convention.
CONTENT_ATTRIBUTES = (
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.prompt",
    "gen_ai.completion",
    "firebreak.tool.arguments",
)

A_PROMPT = "You write the report. Cite every claim."
SECRET_LOOKING = "customer 4821 at 10.2.3.4 with token sk-abcdef"


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    """A provider that keeps spans in memory.

    A global provider can only be set once per process, so this reuses whatever is set
    and clears the exporter between tests. Trying to replace it would silently do nothing
    and every assertion would then run against another test's spans.
    """
    memory = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(memory))
    memory.clear()
    yield memory
    memory.clear()


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> Path:  # type: ignore[no-untyped-def]
    spec = load_library(SPECS_DIR)[FAULTED]
    root = tmp_path_factory.mktemp("spans")
    path = root / derive_bundle_id(spec.id, "spans")
    build_synthetic_bundle(path, spec, "spans", seed=11)
    return path


class TestOneInvestigationIsOneTrace:
    """SPEC.md Section 17 Phase 11's first acceptance criterion, by name."""

    def test_every_span_shares_one_trace_id(self, bundle: Path, exporter) -> None:  # type: ignore[no-untyped-def]
        investigate(bundle)
        spans = exporter.get_finished_spans()
        assert spans
        assert len({span.context.trace_id for span in spans}) == 1

    def test_there_is_exactly_one_root(self, bundle: Path, exporter) -> None:  # type: ignore[no-untyped-def]
        investigate(bundle)
        roots = [span for span in exporter.get_finished_spans() if span.parent is None]
        assert len(roots) == 1
        assert roots[0].name.startswith("invoke_agent firebreak")

    def test_it_has_node_tool_and_model_children(self, bundle: Path, exporter) -> None:  # type: ignore[no-untyped-def]
        """All three kinds, which is what the criterion says. A trace with only tool
        spans would pass a looser reading and tell nobody which node called what."""
        investigate(bundle)
        kinds = Counter(span.name.split()[0] for span in exporter.get_finished_spans())
        assert kinds["node"] > 0
        assert kinds["execute_tool"] > 0
        assert kinds["chat"] > 0

    def test_only_node_spans_carry_the_node_attribute(self, bundle: Path, exporter) -> None:  # type: ignore[no-untyped-def]
        """`firebreak.node` means a graph node and nothing else.

        The four analyst nodes all send the `specialist` prompt. When the model span put
        that prompt name in this attribute, a dashboard grouping by node showed a
        `specialist` row naming no node in the graph, sitting beside the four real analyst
        rows and looking exactly like a fifth node. Wrong in the direction that looks
        plausible, which is the kind a dashboard is never audited for.
        """
        investigate(bundle)
        for span in exporter.get_finished_spans():
            if ATTR_NODE in (span.attributes or {}):
                assert span.name.startswith("node "), (
                    f"{span.name} carries {ATTR_NODE}, so grouping by it mixes vocabularies"
                )

    def test_a_model_span_says_what_it_was_called_for(self, bundle: Path, exporter) -> None:  # type: ignore[no-untyped-def]
        """Which prompt it sent still has to be on the span, under its own name. Dropping
        it would fix the collision by losing the information."""
        investigate(bundle)
        purposes = {
            (span.attributes or {}).get(ATTR_MODEL_PURPOSE)
            for span in exporter.get_finished_spans()
            if span.name.startswith("chat ")
        }
        assert purposes, "no model span recorded a purpose"
        assert None not in purposes

    def test_the_root_names_the_incident_and_the_configuration(
        self, bundle: Path, exporter
    ) -> None:  # type: ignore[no-untyped-def]
        """Without the configuration, a dashboard would show every ablation as one
        system, since they differ only in switches."""
        investigate(bundle)
        root = next(s for s in exporter.get_finished_spans() if s.parent is None)
        assert root.attributes[ATTR_INCIDENT]
        assert root.attributes["firebreak.configuration"] == "full"

    def test_the_gate_result_is_on_the_trace(self, bundle: Path, exporter) -> None:  # type: ignore[no-untyped-def]
        """The rate at which claims are removed is the most useful single number about
        report quality available without a model, and a dashboard can only show it if it
        is on a span."""
        investigate(bundle)
        root = next(s for s in exporter.get_finished_spans() if s.parent is None)
        assert ATTR_GATE_PASSED in root.attributes
        assert "firebreak.gate.removed_claims" in root.attributes

    def test_a_tool_span_records_rows_rather_than_results(self, bundle: Path, exporter) -> None:  # type: ignore[no-untyped-def]
        """The row count comes from the evidence record. An earlier version used
        `len(result.data)`, which counts keys in a summary dict and looked like a row
        count in a dashboard."""
        investigate(bundle)
        tool = next(s for s in exporter.get_finished_spans() if s.name.startswith("execute_tool"))
        assert isinstance(tool.attributes["firebreak.tool.rows"], int)
        assert tool.attributes["gen_ai.tool.name"]


class TestNoPromptContentByDefault:
    """SPEC.md Section 17 Phase 11's reviewer focus.

    The consequence of getting this wrong is outside this repository: traces go to a
    collector, often a shared one, retained longer than anything else, and read by people
    who were never given access to the incident. Firebreak reads customer identifiers,
    internal hostnames and whatever an attacker put in a product name, and puts some of it
    in a prompt.
    """

    def test_no_span_carries_a_content_attribute(self, bundle: Path, exporter) -> None:  # type: ignore[no-untyped-def]
        investigate(bundle)
        offenders = [
            f"{span.name}: {name}"
            for span in exporter.get_finished_spans()
            for name in CONTENT_ATTRIBUTES
            if name in span.attributes
        ]
        assert not offenders, offenders

    def test_the_trace_says_content_was_excluded(self, bundle: Path, exporter) -> None:  # type: ignore[no-untyped-def]
        """So a reader can tell a trace configured without content from one that simply
        had no prompts."""
        investigate(bundle)
        root = next(s for s in exporter.get_finished_spans() if s.parent is None)
        assert root.attributes[ATTR_CONTENT_INCLUDED] is False

    def test_a_prompt_is_recorded_as_a_hash(self, exporter) -> None:  # type: ignore[no-untyped-def]
        """Enough to tell whether two calls sent the same thing, which is the question a
        trace is asked about prompts. Not enough to recover it."""
        recorder = SpanRecorder()
        with (
            recorder.investigation("inc_000000000000"),
            recorder.model_call("reporter", "strong", prompt_body=A_PROMPT),
        ):
            pass
        model = next(s for s in exporter.get_finished_spans() if s.name.startswith("chat"))
        assert model.attributes[ATTR_PROMPT_HASH] == hash_text(A_PROMPT)
        assert A_PROMPT not in str(dict(model.attributes))

    def test_the_hash_does_not_reveal_the_prompt(self) -> None:
        assert len(hash_text(A_PROMPT)) == 12
        assert hash_text(A_PROMPT) != hash_text(A_PROMPT + " ")

    def test_tool_arguments_are_counted_not_recorded(self, exporter) -> None:  # type: ignore[no-untyped-def]
        """A log search pattern is a tool argument and may be something a person typed.
        Counting rather than recording keeps one rule for the whole trace instead of a
        judgement per field."""
        recorder = SpanRecorder()
        with (
            recorder.investigation("inc_000000000000"),
            recorder.tool_call("search_logs", {"pattern": SECRET_LOOKING, "limit": 50}),
        ):
            pass
        tool = next(s for s in exporter.get_finished_spans() if s.name.startswith("execute_tool"))
        assert tool.attributes["firebreak.tool.argument_count"] == 2
        assert SECRET_LOOKING not in str(dict(tool.attributes))

    def test_content_can_be_switched_on_deliberately(self, exporter) -> None:  # type: ignore[no-untyped-def]
        """It exists for a developer debugging one run locally. The test is here so the
        switch is known to work, and so the default is known to be the other one."""
        recorder = SpanRecorder(options=TracingOptions(include_content=True))
        with (
            recorder.investigation("inc_000000000000"),
            recorder.model_call("reporter", "strong", prompt_body=A_PROMPT),
        ):
            pass
        model = next(s for s in exporter.get_finished_spans() if s.name.startswith("chat"))
        assert model.attributes["gen_ai.input.messages"] == A_PROMPT

    def test_switching_it_on_is_recorded_on_the_trace(self, exporter) -> None:  # type: ignore[no-untyped-def]
        """So a trace carrying content says so, rather than a reader having to notice."""
        recorder = SpanRecorder(options=TracingOptions(include_content=True))
        with recorder.investigation("inc_000000000000"):
            pass
        root = next(s for s in exporter.get_finished_spans() if s.parent is None)
        assert root.attributes[ATTR_CONTENT_INCLUDED] is True

    def test_an_error_records_the_type_and_not_the_message(self, exporter) -> None:  # type: ignore[no-untyped-def]
        """An exception message from a tool can quote the data that caused it, which
        would put content into a span through the one path that looks like an error path
        rather than a content path."""
        from firebreak.telemetry.spans import record_failure

        recorder = SpanRecorder()
        with recorder.investigation("inc_000000000000"), recorder.tool_call("search_logs") as span:
            record_failure(span, ValueError(SECRET_LOOKING))
        tool = next(s for s in exporter.get_finished_spans() if s.name.startswith("execute_tool"))
        assert tool.attributes["firebreak.error.type"] == "ValueError"
        assert SECRET_LOOKING not in str(dict(tool.attributes))
        assert SECRET_LOOKING not in str(tool.status.description)


class TestUsageUsesTheConventionalNames:
    def test_tokens_use_the_gen_ai_attribute_names(self, exporter) -> None:  # type: ignore[no-untyped-def]
        """So a backend that understands the GenAI conventions charts them without being
        told about Firebreak."""
        recorder = SpanRecorder()
        with (
            recorder.investigation("inc_000000000000"),
            recorder.model_call("reporter", "strong") as span,
        ):
            record_usage(span, tokens_in=120, tokens_out=45, usd=0.002)
        model = next(s for s in exporter.get_finished_spans() if s.name.startswith("chat"))
        assert model.attributes["gen_ai.usage.input_tokens"] == 120
        assert model.attributes["gen_ai.usage.output_tokens"] == 45

    def test_cost_is_recorded_even_when_zero(self, exporter) -> None:  # type: ignore[no-untyped-def]
        """A zero from a stub and an absent attribute mean different things, and a
        dashboard should be able to tell them apart."""
        recorder = SpanRecorder()
        with (
            recorder.investigation("inc_000000000000"),
            recorder.model_call("reporter", "strong") as span,
        ):
            record_usage(span, tokens_in=0, tokens_out=0)
        model = next(s for s in exporter.get_finished_spans() if s.name.startswith("chat"))
        assert model.attributes["firebreak.model.usd"] == 0.0


class TestTracingCanBeSwitchedOff:
    def test_a_disabled_recorder_creates_no_spans(self, exporter) -> None:  # type: ignore[no-untyped-def]
        recorder = SpanRecorder(options=TracingOptions(enabled=False))
        with recorder.investigation("inc_000000000000") as root:
            assert root is None
            with recorder.node("commander") as node:
                assert node is None
        assert exporter.get_finished_spans() == ()

    def test_recording_on_a_none_span_is_harmless(self) -> None:
        """Every recorder helper takes an optional span, so a disabled trace does not
        need a conditional at every call site."""
        from firebreak.telemetry.spans import record_gate, record_tool_result

        record_tool_result(None, 5, "ev_metric_000000000000")
        record_usage(None, 1, 1)
        record_gate(None, True, 0)
