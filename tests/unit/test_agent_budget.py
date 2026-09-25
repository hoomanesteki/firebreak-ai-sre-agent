"""Tests for firebreak.agent.budget.

These controls are the only thing standing between a wrong answer and an
unbounded bill, so the cases that matter are the boundaries and the precedence:
what happens exactly at a limit, and which reason is reported when two apply at
once.
"""

from __future__ import annotations

import pytest

from firebreak.agent.budget import (
    MAX_IDENTICAL_CALLS,
    STALL_ROUNDS,
    BudgetLimits,
    BudgetState,
    StopReason,
)


def _state(**limits: float | int) -> BudgetState:
    return BudgetState(limits=BudgetLimits(**limits))  # type: ignore[arg-type]


class TestLimitsAreValidated:
    @pytest.mark.parametrize("field", ["max_rounds", "max_tool_calls", "max_tokens"])
    def test_a_limit_below_one_is_refused(self, field: str) -> None:
        with pytest.raises(ValueError, match=f"{field} must be at least 1"):
            BudgetLimits(**{field: 0})  # type: ignore[arg-type]

    def test_a_non_positive_cost_ceiling_is_refused(self) -> None:
        with pytest.raises(ValueError, match="max_usd must be positive"):
            BudgetLimits(max_usd=0.0)

    def test_a_non_positive_wall_clock_is_refused(self) -> None:
        with pytest.raises(ValueError, match="max_wall_clock_seconds must be positive"):
            BudgetLimits(max_wall_clock_seconds=0.0)


class TestExhaustion:
    def test_a_fresh_budget_is_not_exhausted(self) -> None:
        assert _state().exhausted() is None

    def test_reaching_a_limit_exhausts_it_rather_than_exceeding_it(self) -> None:
        """At the limit, not past it. Off by one here is a free extra round."""
        state = _state(max_rounds=2)
        state.end_round(0)
        assert state.exhausted() is None
        state.end_round(1)
        assert state.exhausted() is StopReason.BUDGET_STEPS

    def test_each_limit_reports_its_own_reason(self) -> None:
        cases = [
            (
                _state(max_tool_calls=1),
                lambda s: s.note_tool_call("a"),
                StopReason.BUDGET_TOOL_CALLS,
            ),
            (_state(max_tokens=10), lambda s: s.note_tokens(10, 0), StopReason.BUDGET_TOKENS),
            (_state(max_usd=0.5), lambda s: s.note_tokens(0, 0, 0.5), StopReason.BUDGET_USD),
        ]
        for state, spend, expected in cases:
            spend(state)
            assert state.exhausted() is expected

    def test_wall_clock_is_reported(self) -> None:
        state = _state(max_wall_clock_seconds=10.0)
        state.elapsed_seconds = 10.0
        assert state.exhausted() is StopReason.BUDGET_WALL_CLOCK

    def test_tokens_count_both_directions(self) -> None:
        state = _state(max_tokens=10)
        state.note_tokens(6, 4)
        assert state.tokens == 10
        assert state.exhausted() is StopReason.BUDGET_TOKENS

    def test_rounds_are_reported_before_cost(self) -> None:
        """The order is which explanation a reader can act on.

        A developer can shorten a loop; they cannot directly act on a dollar
        figure that is a consequence of it.
        """
        state = _state(max_rounds=1, max_usd=0.01)
        state.end_round(0)
        state.note_tokens(0, 0, 1.0)
        assert state.exhausted() is StopReason.BUDGET_STEPS

    def test_negative_spend_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            _state().note_tokens(-1, 0)


class TestRepetition:
    def test_counting_returns_the_running_total(self) -> None:
        state = _state()
        assert [state.note_tool_call("same") for _ in range(3)] == [1, 2, 3]

    def test_different_calls_are_counted_separately(self) -> None:
        state = _state()
        state.note_tool_call("a")
        state.note_tool_call("b")
        assert not state.is_repetitive("a", limit=2)
        assert state.tool_calls == 2

    def test_the_third_identical_call_is_repetitive(self) -> None:
        """SPEC.md Section 6.6: three repeated calls end the round.

        The second is served from cache and counted; the third is the signal
        the investigation is going in circles.
        """
        state = _state()
        for _ in range(MAX_IDENTICAL_CALLS - 1):
            state.note_tool_call("same")
        assert not state.is_repetitive("same")
        state.note_tool_call("same")
        assert state.is_repetitive("same")

    def test_a_call_never_made_is_not_repetitive(self) -> None:
        assert not _state().is_repetitive("never")

    def test_repeats_appear_in_the_report_and_singles_do_not(self) -> None:
        """A report listing every call would bury the one that repeated."""
        state = _state()
        state.note_tool_call("once")
        state.note_tool_call("twice")
        state.note_tool_call("twice")
        assert state.as_dict()["repeated_calls"] == {"twice": 2}


class TestStallDetection:
    def test_one_flat_round_is_not_a_stall(self) -> None:
        """A specialist confirming an absence is a real finding with no evidence."""
        state = _state()
        state.end_round(5)
        state.end_round(5)
        assert not state.stalled()

    def test_two_flat_rounds_are_a_stall(self) -> None:
        state = _state()
        state.end_round(5)
        state.end_round(5)
        state.end_round(5)
        assert state.stalled()

    def test_a_round_that_adds_evidence_clears_it(self) -> None:
        state = _state()
        for count in (5, 5, 5, 7):
            state.end_round(count)
        assert not state.stalled()

    def test_too_few_rounds_cannot_stall(self) -> None:
        state = _state()
        for _ in range(STALL_ROUNDS):
            state.end_round(3)
        assert not state.stalled()

    def test_growing_evidence_never_stalls(self) -> None:
        state = _state(max_rounds=10)
        for count in range(1, 6):
            state.end_round(count)
        assert not state.stalled()


class TestStopReason:
    def test_nothing_wrong_means_no_reason_to_stop(self) -> None:
        assert _state().stop_reason() is None

    def test_a_budget_is_reported_before_a_stall(self) -> None:
        """A stall may be a consequence of having no budget left to spend."""
        state = _state(max_rounds=3)
        for _ in range(3):
            state.end_round(4)
        assert state.stalled()
        assert state.stop_reason() is StopReason.BUDGET_STEPS

    def test_a_stall_is_reported_when_budget_remains(self) -> None:
        state = _state(max_rounds=10)
        for _ in range(3):
            state.end_round(4)
        assert state.stop_reason() is StopReason.STALLED

    def test_every_reason_but_completion_lowers_confidence(self) -> None:
        """SPEC.md Section 6.6 requires a cut-short report to say so.

        A report that hit its ceiling and read like a finished one would be the
        most misleading thing this system could produce.
        """
        for reason in StopReason:
            if reason is StopReason.COMPLETE:
                assert not reason.lowers_confidence
            else:
                assert reason.lowers_confidence

    def test_budget_reasons_identify_themselves(self) -> None:
        assert StopReason.BUDGET_TOKENS.is_budget
        assert not StopReason.STALLED.is_budget
        assert not StopReason.COMPLETE.is_budget


class TestSerialisation:
    def test_it_reports_spend_against_limits(self) -> None:
        state = _state(max_rounds=4)
        state.note_tool_call("a")
        state.note_tokens(100, 50, 0.02)
        state.end_round(3)
        payload = state.as_dict()
        assert payload["rounds"] == 1
        assert payload["tool_calls"] == 1
        assert payload["tokens_in"] == 100
        assert payload["usd"] == 0.02
        assert payload["limits"]["max_rounds"] == 4  # type: ignore[index]
        assert payload["evidence_by_round"] == [3]
