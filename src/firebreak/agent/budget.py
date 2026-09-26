"""Budgets, repetition limits, and stall detection.

SPEC.md Section 6.6 gives an investigation three ways to stop: it is finished,
it has spent its budget, or it has stopped learning anything. This module is
the second and third, and both are code rather than a prompt, because a model
asked to respect a budget is a model that will exceed it politely.

**What each control is actually for.**

A budget stops an investigation that would otherwise cost more than the
incident. It is not a safety mechanism against a bad answer; it is a bound on
what a wrong answer is allowed to cost.

A repetition limit stops the failure that costs most per token: a model asking
the same question again because it did not like the answer. SPEC.md Section 6.6
returns the cached evidence and counts the repeat, so the second identical call
is cheap and the third ends the round.

Stall detection stops the investigation that is still spending but no longer
learning. Two rounds that add no new evidence means the tools have said all
they are going to say, and more rounds will not change that.

**Every stop is recorded, not silent.** SPEC.md Section 6.6 requires that a
report produced under a budget stop or a stall says so and lowers its
confidence. A report that hit its ceiling and read like a finished one would be
the most misleading artefact this system could produce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# SPEC.md Section 6.6: three repeated calls end the round. The second identical
# call is served from cache and counted; the third is the signal that the
# investigation is going in circles.
MAX_IDENTICAL_CALLS = 3

# Two rounds with no new evidence. One round can legitimately add nothing, for
# instance when a specialist confirms an absence, so one is not a stall.
STALL_ROUNDS = 2


class StopReason(StrEnum):
    """Why an investigation ended.

    Kept as a closed vocabulary because the report writer and the eval both
    read it, and a free-form string would let the two disagree about whether a
    run finished or was cut off.
    """

    COMPLETE = "complete"
    BUDGET_STEPS = "budget_steps"
    BUDGET_TOOL_CALLS = "budget_tool_calls"
    BUDGET_TOKENS = "budget_tokens"
    BUDGET_USD = "budget_usd"
    BUDGET_WALL_CLOCK = "budget_wall_clock"
    STALLED = "stalled"
    REPETITION = "repetition"
    MODEL_UNAVAILABLE = "model_unavailable"

    @property
    def is_budget(self) -> bool:
        return self.value.startswith("budget_")

    @property
    def lowers_confidence(self) -> bool:
        """Whether a report produced under this reason must hedge.

        Anything other than completing normally means the investigation was
        cut short, and SPEC.md Section 6.6 requires the report to say so.
        """
        return self is not StopReason.COMPLETE


@dataclass(frozen=True)
class BudgetLimits:
    """What one investigation may spend.

    Separate from the running state so a configuration can be compared against
    another on the same limits, and so the limits can be read from
    `config/budgets.yaml` without the spend travelling with them.
    """

    max_rounds: int = 4
    max_tool_calls: int = 40
    max_tokens: int = 200_000
    max_usd: float = 1.00
    max_wall_clock_seconds: float = 600.0

    def __post_init__(self) -> None:
        for name, value in (
            ("max_rounds", self.max_rounds),
            ("max_tool_calls", self.max_tool_calls),
            ("max_tokens", self.max_tokens),
        ):
            if value < 1:
                raise ValueError(f"{name} must be at least 1, got {value}")
        if self.max_usd <= 0:
            raise ValueError(f"max_usd must be positive, got {self.max_usd}")
        if self.max_wall_clock_seconds <= 0:
            raise ValueError(
                f"max_wall_clock_seconds must be positive, got {self.max_wall_clock_seconds}"
            )


@dataclass
class BudgetState:
    """What one investigation has spent, and whether it may continue.

    Mutable on purpose: this is the one piece of investigation state that every
    node writes to, and threading a new copy through each node would mean a
    node that forgot to return it would silently reset the spend.
    """

    limits: BudgetLimits = field(default_factory=BudgetLimits)
    rounds: int = 0
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    elapsed_seconds: float = 0.0

    # How many times each identical tool call has been made. Keyed by a
    # fingerprint the caller supplies, so this module does not need to know how
    # a tool call is spelled.
    call_counts: dict[str, int] = field(default_factory=dict)

    # Evidence ids seen at the end of each round, so a round that added nothing
    # can be recognised. Stored as counts rather than sets of ids because the
    # question is only ever "did this grow".
    evidence_by_round: list[int] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def exhausted(self) -> StopReason | None:
        """The first limit that has been reached, or None.

        Checked in the order a reader would want to be told about: the cheapest
        explanation first. Rounds and tool calls are what a developer can act
        on; cost and wall clock are consequences of them.
        """
        if self.rounds >= self.limits.max_rounds:
            return StopReason.BUDGET_STEPS
        if self.tool_calls >= self.limits.max_tool_calls:
            return StopReason.BUDGET_TOOL_CALLS
        if self.tokens >= self.limits.max_tokens:
            return StopReason.BUDGET_TOKENS
        if self.usd >= self.limits.max_usd:
            return StopReason.BUDGET_USD
        if self.elapsed_seconds >= self.limits.max_wall_clock_seconds:
            return StopReason.BUDGET_WALL_CLOCK
        return None

    def note_tool_call(self, fingerprint: str) -> int:
        """Count one tool call and return how many times it has now been made.

        The caller decides what to do with a repeat. This only counts, because
        the cache that makes a repeat cheap lives in the evidence store and
        putting the policy here as well would split one decision across two
        modules.
        """
        self.tool_calls += 1
        self.call_counts[fingerprint] = self.call_counts.get(fingerprint, 0) + 1
        return self.call_counts[fingerprint]

    def is_repetitive(self, fingerprint: str, limit: int = MAX_IDENTICAL_CALLS) -> bool:
        """Whether this exact call has been made too many times."""
        return self.call_counts.get(fingerprint, 0) >= limit

    def note_tokens(self, tokens_in: int, tokens_out: int, usd: float = 0.0) -> None:
        if tokens_in < 0 or tokens_out < 0 or usd < 0:
            raise ValueError("token counts and cost cannot be negative")
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.usd += usd

    def end_round(self, evidence_count: int) -> None:
        """Record that a round finished with this much evidence gathered."""
        self.rounds += 1
        self.evidence_by_round.append(evidence_count)

    def stalled(self, rounds: int = STALL_ROUNDS) -> bool:
        """Whether the last `rounds` rounds added no new evidence.

        Compares the evidence count at the end of each round. One flat round is
        not a stall: a specialist confirming that a signal is absent is a real
        finding that adds no new evidence record. Two in a row means the tools
        have said everything they are going to.
        """
        if len(self.evidence_by_round) < rounds + 1:
            return False
        window = self.evidence_by_round[-(rounds + 1) :]
        return all(count == window[0] for count in window[1:])

    def stop_reason(self) -> StopReason | None:
        """The reason to stop now, if there is one.

        Budget before stall, because a run that is both out of budget and
        stalled is more usefully described as out of budget: the stall may well
        be a consequence of having no budget left to spend.
        """
        exhausted = self.exhausted()
        if exhausted is not None:
            return exhausted
        if self.stalled():
            return StopReason.STALLED
        return None

    def as_dict(self) -> dict[str, object]:
        return {
            "rounds": self.rounds,
            "tool_calls": self.tool_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "usd": round(self.usd, 6),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "limits": {
                "max_rounds": self.limits.max_rounds,
                "max_tool_calls": self.limits.max_tool_calls,
                "max_tokens": self.limits.max_tokens,
                "max_usd": self.limits.max_usd,
                "max_wall_clock_seconds": self.limits.max_wall_clock_seconds,
            },
            "repeated_calls": {
                fingerprint: count
                for fingerprint, count in sorted(self.call_counts.items())
                if count > 1
            },
            "evidence_by_round": list(self.evidence_by_round),
        }
