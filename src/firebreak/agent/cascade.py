"""The cascade between tiers and the retry policy inside one.

SPEC.md Section 6.7. Two separate mechanisms that are easy to confuse, and
confusing them is expensive in both directions:

- **Escalation** moves a call to a stronger tier. It happens when the answer is
  unsatisfactory: the shape was wrong twice, the model said it was not confident,
  or two specialists contradict each other. Retrying at the same tier would spend
  the budget learning that a small model still cannot do it.
- **Fallback** tries the next model in the same tier, after retrying the current
  one. It happens when the call did not complete: a timeout, a 429, a 5xx. A
  stronger tier would not help, because nothing is wrong with the answer.

A schema error is never retried and a transport error never escalates. That is
the one rule in this module worth reading twice, and it is asserted in the
configuration as well as here.

**Full jitter, not equal jitter.** [R27] measured both. Full jitter, a uniform
draw from zero to the exponential ceiling, spreads a herd of simultaneous retries
best, which is the case that matters: every specialist in a round hits the same
provider at the same moment, so correlated retries are the normal case here
rather than the unlucky one.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum

from firebreak.agent.llm import Tier
from firebreak.agent.models import FallbackRules, ModelSpec

# The order a call escalates through. `judge` is not in it: a judge runs offline
# and escalating into it would let the graded system spend the grader's budget.
ESCALATION_ORDER: tuple[Tier, ...] = (Tier.SMALL, Tier.STRONG)


class Reason(StrEnum):
    """Why a call moved to a stronger tier, so a transcript can say."""

    SCHEMA_FAILURES = "schema_failures"
    LOW_CONFIDENCE = "low_confidence"
    CONTRADICTION = "contradiction"


class TransportError(Exception):
    """A call did not complete. Retried, then failed over, never escalated.

    Carries the status when there was one. A timeout has none, which is why the
    field is optional rather than a sentinel: `status=0` would sort into the
    retryable set by accident.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AllModelsFailedError(Exception):
    """Every model in the tier failed. The deterministic floor takes over."""


def next_tier(current: Tier) -> Tier | None:
    """The tier to escalate to, or None when there is nowhere higher."""
    if current not in ESCALATION_ORDER:
        return None
    index = ESCALATION_ORDER.index(current)
    if index + 1 >= len(ESCALATION_ORDER):
        return None
    return ESCALATION_ORDER[index + 1]


def should_escalate(
    tier: Tier,
    schema_failures: int = 0,
    confidence: float | None = None,
    contradicts_another_specialist: bool = False,
    *,
    after_schema_failures: int,
    below_confidence: float,
    on_contradiction: bool,
) -> Reason | None:
    """Whether this call should be retried at a stronger tier, and why.

    Returns the reason rather than a boolean, because the transcript has to say
    which rule fired: a run that escalated everything on low confidence and a run
    that escalated everything on contradiction have the same cost and completely
    different meanings.

    A tier with nothing above it never escalates, whatever the reason would have
    been. Reporting a reason that cannot be acted on would put an escalation in
    the transcript that never happened.
    """
    if next_tier(tier) is None:
        return None
    if schema_failures >= after_schema_failures:
        return Reason.SCHEMA_FAILURES
    if confidence is not None and confidence < below_confidence:
        return Reason.LOW_CONFIDENCE
    if on_contradiction and contradicts_another_specialist:
        return Reason.CONTRADICTION
    return None


def is_retryable(error: TransportError, rules: FallbackRules) -> bool:
    """Whether retrying the same model could plausibly work.

    A timeout has no status and is retryable when configured. A status is
    retryable only if the configuration lists it, and the configuration refuses
    to list a 4xx other than 429, so a malformed request cannot be resent
    unchanged however this is called.
    """
    if error.status is None:
        return rules.retry_on_timeout
    return error.status in rules.retry_on_status


def backoff_delays(rules: FallbackRules, seed: int | None = None) -> Iterator[float]:
    """The wait before each retry: exponential ceiling, uniform draw beneath it.

    Yields one delay per retry, so `max_attempts_per_model` of 3 yields two: the
    first attempt does not wait. Seeded for tests, because a test asserting on a
    random delay would either be flaky or would assert nothing.
    """
    rng = random.Random(seed)
    for attempt in range(rules.max_attempts_per_model - 1):
        ceiling = min(rules.base_delay_seconds * (2**attempt), rules.max_delay_seconds)
        yield rng.uniform(0.0, ceiling)


@dataclass
class Attempt:
    """One try at one model, for the transcript."""

    model: str
    tier: Tier
    succeeded: bool
    error: str = ""
    waited_seconds: float = 0.0


@dataclass
class CallOutcome:
    """What it took to get an answer, or to run out of models."""

    attempts: list[Attempt] = field(default_factory=list)
    model: str = ""

    @property
    def tries(self) -> int:
        return len(self.attempts)

    @property
    def failed_over(self) -> bool:
        """Whether more than one model was involved."""
        return len({attempt.model for attempt in self.attempts}) > 1

    @property
    def total_waited_seconds(self) -> float:
        return sum(attempt.waited_seconds for attempt in self.attempts)


def call_with_fallback(
    tier: Tier,
    candidates: tuple[ModelSpec, ...],
    rules: FallbackRules,
    call: Callable[[ModelSpec], object],
    sleep: Callable[[float], None] = lambda _: None,
    seed: int | None = None,
) -> tuple[object, CallOutcome]:
    """Try each model in order, retrying the retryable, and report what happened.

    `sleep` is injected and defaults to doing nothing, so a test of the retry
    policy runs in microseconds rather than waiting out real backoff. The real
    caller passes `time.sleep`.

    A non-retryable transport error moves straight to the next model rather than
    retrying: a 404 on one provider's endpoint will be a 404 every time, and the
    next provider is the thing that might work.

    `tier` is recorded on each attempt rather than inferred. The first version
    hardcoded it, which labelled every strong-tier call as small in the
    transcript, and a cost table grouped by tier would then have been wrong in the
    one column it exists for.
    """
    if not candidates:
        raise AllModelsFailedError(f"no models to try in tier {tier.value}")

    outcome = CallOutcome()
    for model in candidates:
        delays = list(backoff_delays(rules, seed=seed))
        for attempt_index in range(rules.max_attempts_per_model):
            try:
                answer = call(model)
            except TransportError as error:
                retryable = is_retryable(error, rules)
                last_try = attempt_index + 1 >= rules.max_attempts_per_model
                waited = 0.0
                if retryable and not last_try:
                    waited = delays[attempt_index]
                    sleep(waited)
                outcome.attempts.append(
                    Attempt(
                        model=model.id,
                        tier=tier,
                        succeeded=False,
                        error=str(error),
                        waited_seconds=waited,
                    )
                )
                if not retryable:
                    break
                continue
            outcome.attempts.append(Attempt(model=model.id, tier=tier, succeeded=True))
            outcome.model = model.id
            return answer, outcome

    raise AllModelsFailedError(
        f"all {len(candidates)} model(s) in tier {tier.value} failed after "
        f"{outcome.tries} attempt(s); "
        f"last error: {outcome.attempts[-1].error if outcome.attempts else 'none'}"
    )
