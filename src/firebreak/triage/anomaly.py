"""Robust anomaly scoring: how unusual is this window against its baseline.

Deterministic, and deliberately so. SPEC.md principle H11 puts grounding
before generative reasoning: anomaly detection and topology ranking run
before any model call, so the agent reasons over a candidate list instead of
searching blind.

The statistic is a median and median absolute deviation z-score rather than
a mean and standard deviation. The reason is the data: an incident is, by
definition, a period containing extreme values, and a mean is dragged by
exactly the points being looked for. A service whose error rate sits at zero
for ten minutes and then jumps has a baseline standard deviation inflated by
the jump itself, and a z-score that understates it. The median does not
move.

Thresholds are not decided here. They live in `config/thresholds.yaml` and
are tuned on the validation split in Phase 4, with the report that justified
them. A number chosen by eye and hard coded is a number nobody can defend.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from enum import StrEnum

# Scale factor making MAD a consistent estimator of the standard deviation
# for normally distributed data. Without it, scores from this and from a
# conventional z-score are not on the same scale and cannot be compared.
MAD_TO_SIGMA = 1.4826

# When every baseline value is identical, MAD is zero and the z-score is
# undefined. Rather than returning infinity for a series that merely sat
# still, the deviation is floored at a fraction of the level: the baseline's
# own where it has one, the incident's where the baseline is flat at zero.
#
# This fraction also sets the ceiling on a zero-baseline score, at its
# reciprocal of 20, which is what stops "a signal appeared" from outranking
# every measured regression.
MIN_SCALE_FRACTION = 0.05
ABSOLUTE_MIN_SCALE = 1e-9

# Below this many baseline points a score is not trustworthy: two samples
# have a median and a MAD, and neither means anything.
MIN_BASELINE_POINTS = 4


class Direction(StrEnum):
    """Which way a change has to go to count.

    An error rate falling is not an incident. A request rate falling might
    be. Asking for a direction stops a recovery being scored as a fault.
    """

    UP = "up"
    DOWN = "down"
    EITHER = "either"


@dataclass(frozen=True)
class AnomalyScore:
    """How far a window departs from its baseline, and in which direction."""

    subject: str
    metric: str
    baseline_median: float
    incident_median: float
    score: float
    direction: Direction
    baseline_points: int
    incident_points: int

    @property
    def is_trustworthy(self) -> bool:
        """False when there was not enough baseline to say anything."""
        return self.baseline_points >= MIN_BASELINE_POINTS and self.incident_points >= 1

    @property
    def delta(self) -> float:
        return self.incident_median - self.baseline_median

    @property
    def relative_delta(self) -> float | None:
        """Change as a fraction of baseline, or None from a zero baseline."""
        if self.baseline_median == 0:
            return None
        return self.delta / abs(self.baseline_median)


def robust_scale(values: list[float], reference: float | None = None) -> float:
    """A standard deviation estimate that extreme values cannot inflate.

    `reference` is a level to fall back on when the baseline carries no scale
    of its own, which happens whenever it is flat at zero. Pass the incident
    median.

    **Why a zero baseline needs its own answer.** A flat baseline has a MAD of
    zero, so the scale falls to the absolute floor, and the first version used
    1e-9 for that. Any departure from a flat-zero series then scored around
    ten million.

    That is not a large anomaly, it is an undefined one, and on the first real
    recording it wrecked the ranking: an error counter on the edge proxy moving
    from exactly zero to 0.01 per second scored 3.1e7, while a genuine 25x
    latency regression on the true culprit scored 11.8. One event per hundred
    seconds outranked the incident by six orders of magnitude.

    Flooring against the incident level instead bounds the score at
    1 / MIN_SCALE_FRACTION, which is 20. That is the honest statement: a signal
    appearing where there was none is strong evidence, worth about as much as a
    large measured regression and no more. The alternative, capping the z-score
    afterwards, would preserve the same wrong ordering with tidier numbers.

    **A pooled scale was tried here and measured worse, twice.** The objection
    to this floor is real: `incident / (incident * 0.05)` is exactly 20 for
    every service and every magnitude, so it is a constant rather than a cap and
    it erases the size of what appeared. Borrowing a scale from the other
    services on the same metric, which is the standard treatment for a group
    with degenerate variance, should have fixed that.

    On eleven real validation recordings it did not. Pooling as a general floor
    took top-3 from 6 of 9 to 4 of 9, and pooling only as the flat-baseline
    floor also gave 4 of 9. Preserving the magnitude let a loud bystander such
    as product-catalog or frontend-proxy win outright, where the constant makes
    the true culprit and its caller tie on anomaly and lets the dependency
    ranking break the tie, which it does correctly.

    So the constant stays, and it stays for a measured reason rather than a
    principled one. It is worth revisiting on a larger recorded library.
    """
    if len(values) < 2:
        return _zero_baseline_scale(0.0, reference)
    median = statistics.median(values)
    deviations = [abs(value - median) for value in values]
    mad = statistics.median(deviations)
    scale = mad * MAD_TO_SIGMA
    # A flat baseline has no spread, so the floor is what decides the score.
    # Relative to its own level where it has one, so a flat series at 1000
    # needs a bigger jump than a flat series at 1.
    floor = max(abs(median) * MIN_SCALE_FRACTION, _zero_baseline_scale(median, reference))
    return max(scale, floor)


def _zero_baseline_scale(median: float, reference: float | None) -> float:
    """The floor for a baseline that carries no scale of its own.

    Falls back to the absolute minimum only when there is no reference either,
    which means both windows were empty or flat at zero. In that case there is
    genuinely nothing to compare and the score will be zero regardless.
    """
    if median == 0.0 and reference:
        return abs(reference) * MIN_SCALE_FRACTION
    return ABSOLUTE_MIN_SCALE


def robust_z_score(baseline: list[float], incident: list[float]) -> float:
    """How many robust deviations the incident median sits from the baseline.

    Returns a signed score: positive when the incident is higher. The incident
    median is passed to `robust_scale` as the reference level, so a baseline
    that is flat at zero is scored against the size of what appeared rather
    than against a floor of 1e-9.
    """
    if not baseline or not incident:
        return 0.0
    baseline_median = statistics.median(baseline)
    incident_median = statistics.median(incident)
    return (incident_median - baseline_median) / robust_scale(baseline, incident_median)


def score_series(
    subject: str,
    metric: str,
    baseline: list[float],
    incident: list[float],
    direction: Direction = Direction.UP,
) -> AnomalyScore:
    """Score one series for one subject."""
    signed = robust_z_score(baseline, incident)
    if direction is Direction.UP:
        score = max(signed, 0.0)
    elif direction is Direction.DOWN:
        score = max(-signed, 0.0)
    else:
        score = abs(signed)
    return AnomalyScore(
        subject=subject,
        metric=metric,
        baseline_median=statistics.median(baseline) if baseline else 0.0,
        incident_median=statistics.median(incident) if incident else 0.0,
        score=round(score, 4),
        direction=direction,
        baseline_points=len(baseline),
        incident_points=len(incident),
    )


def rank_anomalies(scores: list[AnomalyScore], minimum_score: float = 0.0) -> list[AnomalyScore]:
    """Order scores worst first, dropping untrustworthy and quiet ones.

    Sorted by score, then by subject, so the same input always produces the
    same ranking. A ranking that reshuffles between runs would make a
    reliability measure like pass^3 meaningless.
    """
    kept = [s for s in scores if s.is_trustworthy and s.score >= minimum_score]
    return sorted(kept, key=lambda s: (-s.score, s.subject, s.metric))


def onset_index(values: list[float], baseline: list[float], threshold: float) -> int | None:
    """The first point at which a series crosses the threshold.

    Onset ordering is what separates a cause from a symptom: the service
    that started first is more likely the one that broke. SPEC.md Section
    6.5 gives earlier onsets a ranking bonus for exactly this reason.
    """
    if not values or not baseline:
        return None
    median = statistics.median(baseline)
    scale = robust_scale(baseline)
    for index, value in enumerate(values):
        if (value - median) / scale >= threshold:
            return index
    return None
