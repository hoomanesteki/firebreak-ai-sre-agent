"""Tests for the recovery check after a remediation.

SPEC.md Section 6.10. The behaviour worth testing hardest is the third outcome: a
remediation whose effect could not be measured has not been shown to work, and
collapsing that into "not recovered" would blame the fix for a measurement problem
while collapsing it into "recovered" would credit it for one.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from firebreak.remediation.recovery import (
    CONSECUTIVE_SAMPLES,
    RECOVERY_TOLERANCE,
    WATCH_SECONDS,
    Recovery,
    verify_recovery,
)

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def clock(step_seconds: float = 15.0) -> Callable[[], datetime]:
    """A clock that advances by one poll interval each time it is read.

    Advancing on read rather than on sleep, because `verify_recovery` reads the
    clock once per loop and a clock that only moved on sleep would never reach the
    deadline when every sample was unreadable.
    """
    state = {"calls": 0}

    def read() -> datetime:
        state["calls"] += 1
        return START + timedelta(seconds=step_seconds * (state["calls"] - 1))

    return read


def samples(values: list[float | None]) -> Callable[[], float | None]:
    remaining = list(values)

    def read() -> float | None:
        return remaining.pop(0) if remaining else values[-1]

    return read


class TestRecovered:
    def test_a_signal_back_at_baseline_recovers(self) -> None:
        result = verify_recovery(baseline=0.01, sample=samples([0.01, 0.01, 0.01]), now=clock())
        assert result.outcome is Recovery.RECOVERED
        assert result.outcome.acted_usefully

    def test_one_good_sample_is_not_enough(self) -> None:
        """A signal can dip for a reason unrelated to the fix, and declaring victory
        on it is how a remediation gets credit for a coincidence."""
        result = verify_recovery(
            baseline=0.01,
            sample=samples([0.01, 0.9, 0.9, 0.9, 0.9, 0.9]),
            now=clock(),
            watch_seconds=60.0,
        )
        assert result.outcome is Recovery.NOT_RECOVERED

    def test_it_returns_as_soon_as_the_verdict_is_certain(self) -> None:
        """A remediation that worked in forty seconds should be reported in forty
        seconds. The person waiting is why the window is five minutes and not an
        hour."""
        result = verify_recovery(baseline=0.01, sample=samples([0.01] * 10), now=clock())
        assert result.outcome is Recovery.RECOVERED
        assert len(result.samples) == CONSECUTIVE_SAMPLES
        assert result.waited_seconds < WATCH_SECONDS

    def test_slightly_above_baseline_still_counts(self) -> None:
        """A signal that returned to exactly its baseline median would be a
        coincidence; normal variation moves it either way."""
        just_inside = 0.01 * RECOVERY_TOLERANCE
        result = verify_recovery(baseline=0.01, sample=samples([just_inside] * 3), now=clock())
        assert result.outcome is Recovery.RECOVERED

    def test_a_zero_baseline_gets_a_floor_rather_than_a_ceiling_of_zero(self) -> None:
        """A service whose error rate was exactly zero would otherwise have a ceiling
        of zero, and no real measurement is ever exactly zero, so every recovery
        would read as a failure."""
        result = verify_recovery(baseline=0.0, sample=samples([0.0005] * 3), now=clock())
        assert result.outcome is Recovery.RECOVERED

    def test_a_zero_baseline_still_rejects_a_real_error_rate(self) -> None:
        result = verify_recovery(
            baseline=0.0, sample=samples([0.4] * 30), now=clock(), watch_seconds=60.0
        )
        assert result.outcome is Recovery.NOT_RECOVERED


class TestNotRecovered:
    def test_a_signal_still_high_at_the_deadline_has_not_recovered(self) -> None:
        result = verify_recovery(
            baseline=0.01, sample=samples([0.8] * 30), now=clock(), watch_seconds=60.0
        )
        assert result.outcome is Recovery.NOT_RECOVERED
        assert not result.outcome.acted_usefully
        assert "above the" in result.detail

    def test_the_detail_names_both_numbers(self) -> None:
        """So somebody can argue with the verdict rather than only read it."""
        result = verify_recovery(
            baseline=0.02, sample=samples([0.5] * 30), now=clock(), watch_seconds=45.0
        )
        assert "0.5000" in result.detail
        assert f"{0.02 * RECOVERY_TOLERANCE:.4f}" in result.detail

    def test_the_final_sample_is_reported(self) -> None:
        result = verify_recovery(
            baseline=0.01,
            sample=samples([0.9, 0.7, 0.6, 0.55]),
            now=clock(),
            watch_seconds=60.0,
        )
        assert result.final == pytest.approx(0.55)


class TestCouldNotTell:
    """The outcome that keeps the other two honest."""

    def test_no_baseline_is_unknown_rather_than_a_failure(self) -> None:
        result = verify_recovery(baseline=None, sample=samples([0.0]), now=clock())
        assert result.outcome is Recovery.UNKNOWN
        assert "nothing to compare" in result.detail

    def test_an_unreadable_signal_is_unknown_rather_than_a_failure(self) -> None:
        """Blaming a remediation for a measurement problem would make the check a
        liability rather than a control."""
        result = verify_recovery(
            baseline=0.01, sample=samples([None] * 30), now=clock(), watch_seconds=60.0
        )
        assert result.outcome is Recovery.UNKNOWN
        assert "could not be read" in result.detail

    def test_unknown_is_not_a_partial_yes(self) -> None:
        assert not Recovery.UNKNOWN.acted_usefully

    def test_a_signal_that_became_readable_is_still_judged(self) -> None:
        """One failed read must not poison the whole watch."""
        result = verify_recovery(
            baseline=0.01, sample=samples([None, 0.01, 0.01, 0.01]), now=clock()
        )
        assert result.outcome is Recovery.RECOVERED


class TestItDoesNotWaitInTests:
    def test_sleep_is_injected_so_a_five_minute_watch_runs_instantly(self) -> None:
        waits: list[float] = []
        verify_recovery(
            baseline=0.01,
            sample=samples([0.9] * 100),
            sleep=waits.append,
            now=clock(),
            watch_seconds=60.0,
            poll_seconds=15.0,
        )
        assert waits, "the watch should have waited between polls"
        assert all(wait == 15.0 for wait in waits)

    def test_the_default_window_is_the_one_spec_names(self) -> None:
        assert WATCH_SECONDS == 300.0


class TestItSerialises:
    def test_the_result_carries_everything_a_report_needs(self) -> None:
        payload = verify_recovery(baseline=0.01, sample=samples([0.01] * 3), now=clock()).as_dict()
        assert payload["outcome"] == "recovered"
        assert payload["baseline"] == 0.01
        assert payload["samples"]
        assert isinstance(payload["detail"], str)
