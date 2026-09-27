"""Did the remediation work? Watch the alerting signal and say.

SPEC.md Section 6.10: after execution, `verify_recovery` watches the alerting
signal for up to five minutes and records whether it returned to baseline. The
outcome is part of the report and part of the eval.

**Why this is not optional and not a nicety.** A system that proposes changes and
never checks them teaches its operators that its proposals are guesses. Worse, an
unverified remediation that did nothing looks identical to one that worked, so the
next incident gets the same suggestion with the same confidence.

**Three outcomes, not two.** Recovered, not recovered, and could not tell. The
third is the honest answer when the watch window expired with the signal still
moving, or when the signal was never available, and collapsing it into "not
recovered" would blame a remediation for a measurement problem.

**Compared against the incident's own baseline, not against zero.** A healthy
system has a non-zero error rate and a non-zero latency. "Returned to baseline"
means back inside the range the same service showed before the fault, which is the
only definition that works across services with different normal behaviour.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

# SPEC.md Section 6.10 names five minutes.
WATCH_SECONDS = 300.0
POLL_SECONDS = 15.0

# How close to baseline counts as recovered, as a multiple of the baseline value.
# Not 1.0: a signal that returned to exactly its baseline median would be a
# coincidence, and normal variation moves it either way. 1.25 allows a quarter
# above the baseline, which is inside the noise the anomaly scorer treats as
# unremarkable.
RECOVERY_TOLERANCE = 1.25

# A run of consecutive samples inside tolerance before calling it recovered. One
# sample can dip for a reason unrelated to the fix, and declaring victory on it is
# how a remediation gets credit for a coincidence.
CONSECUTIVE_SAMPLES = 3


class Recovery(StrEnum):
    """What the watch concluded."""

    RECOVERED = "recovered"
    NOT_RECOVERED = "not_recovered"
    UNKNOWN = "unknown"

    @property
    def acted_usefully(self) -> bool:
        """Whether the remediation can be credited with a fix.

        `UNKNOWN` is not a partial yes. A remediation whose effect could not be
        measured has not been shown to work, and the eval scores it as such.
        """
        return self is Recovery.RECOVERED


@dataclass(frozen=True)
class RecoveryResult:
    """The verdict, and enough to argue with it."""

    outcome: Recovery
    baseline: float | None
    samples: tuple[float, ...]
    waited_seconds: float
    detail: str

    @property
    def final(self) -> float | None:
        return self.samples[-1] if self.samples else None

    def as_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "baseline": self.baseline,
            "samples": list(self.samples),
            "final": self.final,
            "waited_seconds": self.waited_seconds,
            "detail": self.detail,
        }


def verify_recovery(
    baseline: float | None,
    sample: Callable[[], float | None],
    sleep: Callable[[float], None] = lambda _: None,
    now: Callable[[], datetime] | None = None,
    watch_seconds: float = WATCH_SECONDS,
    poll_seconds: float = POLL_SECONDS,
    tolerance: float = RECOVERY_TOLERANCE,
    consecutive: int = CONSECUTIVE_SAMPLES,
) -> RecoveryResult:
    """Watch one signal until it settles, the window expires, or it cannot be read.

    `sample` returns the signal's current value, or None when it cannot be read.
    `sleep` and `now` are injected so a five minute watch tests in microseconds;
    the real caller passes `time.sleep` and `datetime.now`.

    Returns as soon as the verdict is certain rather than always waiting out the
    window. A remediation that worked in forty seconds should be reported in forty
    seconds, because the person waiting is the reason the window is five minutes and
    not an hour.
    """
    clock = now or (lambda: datetime.now(UTC))
    started = clock()
    deadline = started + timedelta(seconds=watch_seconds)

    if baseline is None:
        return RecoveryResult(
            outcome=Recovery.UNKNOWN,
            baseline=None,
            samples=(),
            waited_seconds=0.0,
            detail=(
                "the incident recorded no baseline for this signal, so there is nothing "
                "to compare a recovery against"
            ),
        )

    ceiling = _ceiling(baseline, tolerance)
    samples: list[float] = []
    inside = 0
    unreadable = 0

    while True:
        value = sample()
        if value is None:
            unreadable += 1
        else:
            samples.append(value)
            inside = inside + 1 if value <= ceiling else 0
            if inside >= consecutive:
                return RecoveryResult(
                    outcome=Recovery.RECOVERED,
                    baseline=baseline,
                    samples=tuple(samples),
                    waited_seconds=(clock() - started).total_seconds(),
                    detail=(
                        f"{consecutive} consecutive samples at or below {ceiling:.4f}, "
                        f"which is {tolerance:.2f} times the baseline of {baseline:.4f}"
                    ),
                )

        if clock() >= deadline:
            break
        sleep(poll_seconds)

    waited = (clock() - started).total_seconds()
    if not samples:
        return RecoveryResult(
            outcome=Recovery.UNKNOWN,
            baseline=baseline,
            samples=(),
            waited_seconds=waited,
            detail=(
                f"the signal could not be read in {unreadable} attempt(s) over "
                f"{waited:.0f}s, so whether it recovered is unknown rather than no"
            ),
        )
    return RecoveryResult(
        outcome=Recovery.NOT_RECOVERED,
        baseline=baseline,
        samples=tuple(samples),
        waited_seconds=waited,
        detail=(
            f"after {waited:.0f}s the signal was {samples[-1]:.4f}, above the "
            f"{ceiling:.4f} it would need to be at or below"
        ),
    )


def _ceiling(baseline: float, tolerance: float) -> float:
    """The value at or below which the signal counts as recovered.

    A zero baseline needs a floor rather than a multiple: a service whose error rate
    was exactly zero before the fault would have a ceiling of zero, and no real
    measurement is ever exactly zero, so every recovery would read as a failure.
    The floor is the same tolerance applied to the smallest rate worth distinguishing
    from zero, one error in a thousand requests.
    """
    if baseline <= 0.0:
        return 0.001 * tolerance
    return baseline * tolerance
