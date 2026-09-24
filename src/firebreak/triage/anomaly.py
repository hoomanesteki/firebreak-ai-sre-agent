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
# still, the deviation is floored at a fraction of the baseline level.
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


def robust_scale(values: list[float]) -> float:
    """A standard deviation estimate that extreme values cannot inflate."""
    if len(values) < 2:
        return ABSOLUTE_MIN_SCALE
    median = statistics.median(values)
    deviations = [abs(value - median) for value in values]
    mad = statistics.median(deviations)
    scale = mad * MAD_TO_SIGMA
    # A perfectly flat baseline has a MAD of zero, which would make every
    # later point infinitely anomalous. Floor it relative to the level, so a
    # flat series at 1000 needs a bigger jump than a flat series at 1.
    floor = max(abs(median) * MIN_SCALE_FRACTION, ABSOLUTE_MIN_SCALE)
    return max(scale, floor)


def robust_z_score(baseline: list[float], incident: list[float]) -> float:
    """How many robust deviations the incident median sits from the baseline.

    Returns a signed score: positive when the incident is higher.
    """
    if not baseline or not incident:
        return 0.0
    baseline_median = statistics.median(baseline)
    incident_median = statistics.median(incident)
    return (incident_median - baseline_median) / robust_scale(baseline)


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
