"""Tests for the cascade between tiers and the retry policy inside one.

CLAUDE.md requires tests first for the cascade, and the reason is in SPEC.md
Section 6.7: escalation and fallback look similar and are opposites. Escalating a
timeout wastes a strong-tier call on a network problem. Retrying a schema error
spends the budget discovering that a small model still cannot produce the shape.
Most of this file is about keeping those two apart.
"""

from __future__ import annotations

import pytest

from firebreak.agent.cascade import (
    ESCALATION_ORDER,
    AllModelsFailedError,
    Reason,
    TransportError,
    backoff_delays,
    call_with_fallback,
    is_retryable,
    next_tier,
    should_escalate,
)
from firebreak.agent.llm import Tier
from firebreak.agent.models import FallbackRules, ModelSpec

RULES = FallbackRules(
    max_attempts_per_model=3,
    base_delay_seconds=0.5,
    max_delay_seconds=20.0,
    retry_on_status=(429, 500, 502, 503, 504),
    retry_on_timeout=True,
)


def model(name: str) -> ModelSpec:
    return ModelSpec(id=name, provider="test")


class TestEscalationIsAboutTheAnswer:
    def test_two_schema_failures_escalate(self) -> None:
        """One is repaired by showing the model its error. Two means it cannot."""
        assert (
            should_escalate(
                Tier.SMALL,
                schema_failures=2,
                after_schema_failures=2,
                below_confidence=0.3,
                on_contradiction=True,
            )
            is Reason.SCHEMA_FAILURES
        )

    def test_one_schema_failure_does_not(self) -> None:
        assert (
            should_escalate(
                Tier.SMALL,
                schema_failures=1,
                after_schema_failures=2,
                below_confidence=0.3,
                on_contradiction=True,
            )
            is None
        )

    def test_low_confidence_escalates(self) -> None:
        assert (
            should_escalate(
                Tier.SMALL,
                confidence=0.2,
                after_schema_failures=2,
                below_confidence=0.3,
                on_contradiction=True,
            )
            is Reason.LOW_CONFIDENCE
        )

    def test_confidence_exactly_at_the_threshold_does_not(self) -> None:
        """The rule is "below", and a boundary that drifted would change the cost
        of every run without anybody choosing to."""
        assert (
            should_escalate(
                Tier.SMALL,
                confidence=0.3,
                after_schema_failures=2,
                below_confidence=0.3,
                on_contradiction=True,
            )
            is None
        )

    def test_no_stated_confidence_is_not_low_confidence(self) -> None:
        """Absent and zero are different, and treating them alike would escalate
        every call that did not report one."""
        assert (
            should_escalate(
                Tier.SMALL,
                confidence=None,
                after_schema_failures=2,
                below_confidence=0.3,
                on_contradiction=True,
            )
            is None
        )

    def test_a_contradiction_escalates_when_the_rule_is_on(self) -> None:
        assert (
            should_escalate(
                Tier.SMALL,
                contradicts_another_specialist=True,
                after_schema_failures=2,
                below_confidence=0.3,
                on_contradiction=True,
            )
            is Reason.CONTRADICTION
        )

    def test_a_contradiction_is_ignored_when_the_rule_is_off(self) -> None:
        assert (
            should_escalate(
                Tier.SMALL,
                contradicts_another_specialist=True,
                after_schema_failures=2,
                below_confidence=0.3,
                on_contradiction=False,
            )
            is None
        )

    def test_the_strongest_tier_never_escalates(self) -> None:
        """There is nowhere higher, so reporting a reason would put an escalation
        in the transcript that never happened."""
        assert (
            should_escalate(
                Tier.STRONG,
                schema_failures=99,
                confidence=0.0,
                contradicts_another_specialist=True,
                after_schema_failures=2,
                below_confidence=0.3,
                on_contradiction=True,
            )
            is None
        )

    def test_schema_failures_win_over_confidence(self) -> None:
        """Both apply; the transcript needs one reason, and the shape being wrong
        is the more actionable of the two."""
        assert (
            should_escalate(
                Tier.SMALL,
                schema_failures=2,
                confidence=0.0,
                after_schema_failures=2,
                below_confidence=0.3,
                on_contradiction=True,
            )
            is Reason.SCHEMA_FAILURES
        )


class TestTheEscalationOrder:
    def test_small_escalates_to_strong(self) -> None:
        assert next_tier(Tier.SMALL) is Tier.STRONG

    def test_strong_has_nowhere_to_go(self) -> None:
        assert next_tier(Tier.STRONG) is None

    def test_the_judge_tier_is_not_in_the_order(self) -> None:
        """A judge runs offline. Escalating into it would let the graded system
        spend the grader's budget, and the grader would then be part of what it
        is grading."""
        assert Tier.JUDGE not in ESCALATION_ORDER
        assert next_tier(Tier.JUDGE) is None


class TestFallbackIsAboutTheCall:
    def test_a_timeout_is_retryable(self) -> None:
        assert is_retryable(TransportError("timed out"), RULES)

    def test_a_429_is_retryable(self) -> None:
        assert is_retryable(TransportError("slow down", status=429), RULES)

    def test_a_5xx_is_retryable(self) -> None:
        assert is_retryable(TransportError("bad gateway", status=502), RULES)

    def test_a_404_is_not(self) -> None:
        """It will be a 404 every time. The next provider is what might work."""
        assert not is_retryable(TransportError("no such model", status=404), RULES)

    def test_a_422_is_not(self) -> None:
        """A malformed request resent unchanged fails the same way."""
        assert not is_retryable(TransportError("unprocessable", status=422), RULES)

    def test_a_timeout_is_not_retried_when_the_rule_is_off(self) -> None:
        rules = RULES.model_copy(update={"retry_on_timeout": False})
        assert not is_retryable(TransportError("timed out"), rules)


class TestTheConfigurationRefusesAnUnretryableRetry:
    """The rule from SPEC.md Section 6.7 that is easiest to lose, so it is
    enforced where the number is written as well as where it is read."""

    def test_a_4xx_in_the_retry_list_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="which a retry cannot fix"):
            FallbackRules(
                max_attempts_per_model=3,
                base_delay_seconds=0.5,
                max_delay_seconds=20.0,
                retry_on_status=(422,),
            )

    def test_429_is_allowed_because_waiting_is_the_fix(self) -> None:
        rules = FallbackRules(
            max_attempts_per_model=3,
            base_delay_seconds=0.5,
            max_delay_seconds=20.0,
            retry_on_status=(429,),
        )
        assert rules.retry_on_status == (429,)

    def test_a_shrinking_backoff_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="backoff would shrink"):
            FallbackRules(
                max_attempts_per_model=3,
                base_delay_seconds=10.0,
                max_delay_seconds=1.0,
            )


class TestBackoff:
    def test_there_is_one_delay_per_retry_not_per_attempt(self) -> None:
        """The first attempt does not wait."""
        assert len(list(backoff_delays(RULES, seed=1))) == RULES.max_attempts_per_model - 1

    def test_every_delay_is_within_its_exponential_ceiling(self) -> None:
        """Full jitter: a uniform draw from zero to the ceiling, which is what
        [R27] measured as best at spreading a herd of correlated retries."""
        rules = RULES.model_copy(update={"max_attempts_per_model": 6})
        for index, delay in enumerate(backoff_delays(rules, seed=7)):
            ceiling = min(rules.base_delay_seconds * (2**index), rules.max_delay_seconds)
            assert 0.0 <= delay <= ceiling

    def test_the_ceiling_is_capped(self) -> None:
        """Without the cap, the eighth retry of a 0.5s base would wait a minute."""
        rules = RULES.model_copy(update={"max_attempts_per_model": 12, "max_delay_seconds": 2.0})
        assert all(delay <= 2.0 for delay in backoff_delays(rules, seed=3))

    def test_a_seed_makes_it_reproducible(self) -> None:
        assert list(backoff_delays(RULES, seed=42)) == list(backoff_delays(RULES, seed=42))

    def test_jitter_is_actually_random(self) -> None:
        """Equal delays every time would be exponential backoff with no jitter,
        which is the thing [R27] says not to do."""
        rules = RULES.model_copy(update={"max_attempts_per_model": 8})
        assert list(backoff_delays(rules, seed=1)) != list(backoff_delays(rules, seed=2))


class TestCallingWithFallback:
    def test_a_first_try_that_works_costs_one_attempt(self) -> None:
        answer, outcome = call_with_fallback(
            Tier.SMALL, (model("a"), model("b")), RULES, lambda spec: f"from {spec.id}"
        )
        assert answer == "from a"
        assert outcome.tries == 1
        assert outcome.model == "a"
        assert not outcome.failed_over

    def test_a_retryable_failure_retries_the_same_model(self) -> None:
        seen: list[str] = []

        def call(spec):  # type: ignore[no-untyped-def]
            seen.append(spec.id)
            if len(seen) < 2:
                raise TransportError("bad gateway", status=502)
            return "recovered"

        answer, outcome = call_with_fallback(Tier.SMALL, (model("a"), model("b")), RULES, call)
        assert answer == "recovered"
        assert seen == ["a", "a"]
        assert not outcome.failed_over

    def test_a_model_that_keeps_failing_falls_over_to_the_next(self) -> None:
        def call(spec):  # type: ignore[no-untyped-def]
            if spec.id == "a":
                raise TransportError("gone", status=503)
            return "from b"

        answer, outcome = call_with_fallback(Tier.SMALL, (model("a"), model("b")), RULES, call)
        assert answer == "from b"
        assert outcome.model == "b"
        assert outcome.failed_over
        # Three attempts on a, then one on b.
        assert outcome.tries == 4

    def test_an_unretryable_failure_skips_straight_to_the_next_model(self) -> None:
        """Retrying a 404 three times wastes three round trips to learn nothing."""

        def call(spec):  # type: ignore[no-untyped-def]
            if spec.id == "a":
                raise TransportError("no such model", status=404)
            return "from b"

        answer, outcome = call_with_fallback(Tier.SMALL, (model("a"), model("b")), RULES, call)
        assert answer == "from b"
        assert outcome.tries == 2

    def test_every_model_failing_raises_for_the_floor(self) -> None:
        """SPEC.md Section 6.7's deterministic floor takes over here, so this has
        to be a distinguishable failure rather than a generic one."""

        def call(spec):  # type: ignore[no-untyped-def]
            raise TransportError("down", status=500)

        with pytest.raises(AllModelsFailedError, match="all 2 model"):
            call_with_fallback(Tier.SMALL, (model("a"), model("b")), RULES, call)

    def test_the_failure_names_the_tier_and_the_last_error(self) -> None:
        def call(spec):  # type: ignore[no-untyped-def]
            raise TransportError("rate limited", status=429)

        with pytest.raises(AllModelsFailedError) as raised:
            call_with_fallback(Tier.STRONG, (model("a"),), RULES, call)
        assert "tier strong" in str(raised.value)
        assert "rate limited" in str(raised.value)

    def test_no_candidates_is_a_failure_not_a_silent_pass(self) -> None:
        with pytest.raises(AllModelsFailedError, match="no models to try"):
            call_with_fallback(Tier.SMALL, (), RULES, lambda spec: "never")

    def test_it_waits_between_retries_and_reports_how_long(self) -> None:
        waits: list[float] = []

        def call(spec):  # type: ignore[no-untyped-def]
            if len(waits) < 2:
                raise TransportError("timeout")
            return "ok"

        answer, outcome = call_with_fallback(
            Tier.SMALL, (model("a"),), RULES, call, sleep=waits.append, seed=5
        )
        assert answer == "ok"
        assert len(waits) == 2
        assert outcome.total_waited_seconds == pytest.approx(sum(waits))

    def test_it_does_not_wait_after_the_last_attempt_on_a_model(self) -> None:
        """Sleeping before giving up on a model delays the fallback for nothing."""
        waits: list[float] = []

        def call(spec):  # type: ignore[no-untyped-def]
            raise TransportError("down", status=500)

        with pytest.raises(AllModelsFailedError):
            call_with_fallback(Tier.SMALL, (model("a"),), RULES, call, sleep=waits.append, seed=5)
        assert len(waits) == RULES.max_attempts_per_model - 1

    def test_the_attempts_record_the_tier_they_ran_at(self) -> None:
        """The first version hardcoded small, which would have made a cost table
        grouped by tier wrong in the one column it exists for."""
        _, outcome = call_with_fallback(Tier.STRONG, (model("a"),), RULES, lambda spec: "ok")
        assert [attempt.tier for attempt in outcome.attempts] == [Tier.STRONG]
