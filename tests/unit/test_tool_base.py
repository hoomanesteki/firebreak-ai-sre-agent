"""Tests for firebreak.tools.base: the shape every tool has, and its shared rules."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from firebreak.tools.base import (
    MAX_SUMMARY_CHARS,
    ToolCapExceededError,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    UnknownToolError,
    summarise_rows,
)


class ValueInput(BaseModel):
    """A trivial input model, used to exercise the registry without a real backend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: int = Field(ge=0, le=10)


def echo_handler(context: ToolContext, arguments: ValueInput) -> ToolResult:
    del context
    return ToolResult(
        tool="echo",
        summary=f"echoed {arguments.value}",
        evidence_id="ev_metric_000000000000",
        data={"value": arguments.value},
    )


def mislabelling_handler(context: ToolContext, arguments: ValueInput) -> ToolResult:
    del context, arguments
    return ToolResult(tool="not-the-registered-name", summary="mislabeled on purpose")


def other_handler(context: ToolContext, arguments: ValueInput) -> ToolResult:
    del context, arguments
    return ToolResult(tool="other", summary="a second, unrelated tool")


ECHO_SPEC = ToolSpec(
    name="echo",
    description="echoes a bounded integer",
    input_model=ValueInput,
    handler=echo_handler,
)
MISLABEL_SPEC = ToolSpec(
    name="mislabel",
    description="a handler that lies about its own name",
    input_model=ValueInput,
    handler=mislabelling_handler,
)
OTHER_SPEC = ToolSpec(
    name="other", description="a second tool", input_model=ValueInput, handler=other_handler
)


def registry_with(*specs: ToolSpec) -> ToolRegistry:
    return ToolRegistry(list(specs))


# --- ToolContext.note_call ---------------------------------------------------


def test_note_call_increments_and_reports_calls_made_and_total_calls():
    context = ToolContext(backend=object())

    assert context.note_call("echo") == 1
    assert context.note_call("echo") == 2
    assert context.note_call("other") == 1

    assert context.calls_made("echo") == 2
    assert context.calls_made("other") == 1
    assert context.calls_made("never-called") == 0
    assert context.total_calls() == 3


def test_note_call_raises_tool_cap_exceeded_at_the_cap_naming_the_tool():
    context = ToolContext(backend=object(), max_calls_per_tool=2)

    context.note_call("echo")
    context.note_call("echo")

    with pytest.raises(ToolCapExceededError, match="echo") as excinfo:
        context.note_call("echo")

    assert "echo" in str(excinfo.value)
    assert context.calls_made("echo") == 2


# --- ToolRegistry: register, spec, names, __contains__, __len__, describe ----


def test_register_rejects_a_duplicate_name():
    registry = registry_with(ECHO_SPEC)

    with pytest.raises(ToolError, match="echo"):
        registry.register(ECHO_SPEC)


def test_spec_raises_unknown_tool_error_listing_available_names():
    registry = registry_with(ECHO_SPEC, OTHER_SPEC)

    with pytest.raises(UnknownToolError) as excinfo:
        registry.spec("does-not-exist")

    message = str(excinfo.value)
    assert "does-not-exist" in message
    assert "echo" in message
    assert "other" in message


def test_names_returns_a_sorted_tuple():
    registry = registry_with(OTHER_SPEC, ECHO_SPEC)

    assert registry.names() == ("echo", "other")


def test_contains_and_len():
    registry = registry_with(ECHO_SPEC, OTHER_SPEC)

    assert "echo" in registry
    assert "other" in registry
    assert "missing" not in registry
    assert len(registry) == 2


def test_describe_returns_name_and_description_per_tool():
    registry = registry_with(OTHER_SPEC, ECHO_SPEC)

    described = registry.describe()

    assert described == [
        {"name": "echo", "description": ECHO_SPEC.description},
        {"name": "other", "description": OTHER_SPEC.description},
    ]


# --- ToolRegistry.subset -----------------------------------------------------


def test_subset_returns_only_the_named_tools():
    registry = registry_with(ECHO_SPEC, OTHER_SPEC)

    subset = registry.subset(("echo",))

    assert subset.names() == ("echo",)
    assert "other" not in subset


def test_subset_raises_for_an_unknown_tool():
    registry = registry_with(ECHO_SPEC)

    with pytest.raises(UnknownToolError):
        registry.subset(("other",))


def test_a_specialist_given_a_subset_cannot_call_a_tool_outside_it():
    """The whole point of subset: a caller holding it has no path to the rest."""
    full = registry_with(ECHO_SPEC, OTHER_SPEC)
    specialist_tools = full.subset(("echo",))
    context = ToolContext(backend=object())

    with pytest.raises(UnknownToolError):
        specialist_tools.call("other", context, {"value": 1})


# --- ToolRegistry.call --------------------------------------------------------


def test_call_validates_arguments_and_raises_tool_error_naming_the_tool():
    registry = registry_with(ECHO_SPEC)
    context = ToolContext(backend=object())

    with pytest.raises(ToolError, match="echo") as excinfo:
        registry.call("echo", context, {"value": 999})

    assert "echo" in str(excinfo.value)
    assert context.calls_made("echo") == 0


def test_call_rejects_an_undeclared_extra_argument():
    registry = registry_with(ECHO_SPEC)
    context = ToolContext(backend=object())

    with pytest.raises(ToolError, match="echo"):
        registry.call("echo", context, {"value": 1, "bogus": True})


def test_call_counts_the_call():
    registry = registry_with(ECHO_SPEC)
    context = ToolContext(backend=object())

    registry.call("echo", context, {"value": 1})
    registry.call("echo", context, {"value": 2})

    assert context.calls_made("echo") == 2
    assert context.total_calls() == 2


def test_call_raises_when_a_handler_returns_a_result_labelled_with_a_different_tool_name():
    registry = registry_with(MISLABEL_SPEC)
    context = ToolContext(backend=object())

    with pytest.raises(ToolError, match="mislabel"):
        registry.call("mislabel", context, {"value": 1})


# --- ToolResult ---------------------------------------------------------------


def test_tool_result_summary_longer_than_600_chars_is_rejected():
    with pytest.raises(ValidationError):
        ToolResult(tool="echo", summary="x" * (MAX_SUMMARY_CHARS + 1))


def test_tool_result_cite_returns_the_evidence_id():
    result = ToolResult(tool="echo", summary="ok", evidence_id="ev_metric_000000000000")

    assert result.cite() == ("ev_metric_000000000000",)


def test_tool_result_cite_returns_empty_tuple_without_an_evidence_id():
    result = ToolResult(tool="echo", summary="ok")

    assert result.cite() == ()


# --- summarise_rows -------------------------------------------------------


def test_summarise_rows_of_empty_rows():
    assert summarise_rows([]) == "no matching rows"


def test_summarise_rows_fewer_than_limit_has_no_more_suffix():
    rows = [{"a": 1}, {"a": 2}]

    assert summarise_rows(rows, limit=5) == "a=1; a=2"


def test_summarise_rows_more_than_limit_adds_and_n_more_suffix():
    rows = [{"a": i} for i in range(7)]

    result = summarise_rows(rows, limit=5)

    assert result == "a=0; a=1; a=2; a=3; a=4; and 2 more"
