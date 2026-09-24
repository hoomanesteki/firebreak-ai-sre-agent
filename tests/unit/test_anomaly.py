"""Tests for firebreak.triage.anomaly: robust anomaly scoring."""

from __future__ import annotations

import random
import statistics

from firebreak.triage.anomaly import (
    MIN_BASELINE_POINTS,
    MIN_SCALE_FRACTION,
    AnomalyScore,
    Direction,
    onset_index,
    rank_anomalies,
    robust_scale,
    robust_z_score,
    score_series,
)

# --- robust_scale --------------------------------------------------------


def test_robust_scale_of_a_flat_series_floors_at_a_fraction_of_the_level():
    """A flat baseline must not make every later point infinitely anomalous."""
    flat = [100.0] * 10

    scale = robust_scale(flat)

    assert scale > 0.0
    assert scale == statistics.median(flat) * MIN_SCALE_FRACTION


def test_robust_scale_with_one_huge_outlier_stays_close_to_without_it():
    """The point of MAD over standard deviation: one extreme value barely moves it."""
    values = [10.0, 11.0, 9.0, 10.0, 11.0, 9.0, 10.0, 11.0, 9.0, 10.0]
    with_outlier = [*values, 1000.0]

    scale_without = robust_scale(values)
    scale_with = robust_scale(with_outlier)

    stdev_without = statistics.stdev(values)
    stdev_with = statistics.stdev(with_outlier)

    # A mean-based spread is dragged badly by the outlier...
    assert stdev_with > stdev_without * 10
    # ...while the robust scale barely notices it.
    assert scale_with < scale_without * 2


# --- robust_z_score --------------------------------------------------------


def test_robust_z_score_is_zero_for_identical_windows():
    same = [5.0, 6.0, 4.0, 5.0]

    assert robust_z_score(same, same) == 0.0


def test_robust_z_score_is_positive_when_the_incident_is_higher():
    baseline = [1.0, 1.0, 1.0, 1.0]
    incident = [10.0, 10.0, 10.0, 10.0]

    assert robust_z_score(baseline, incident) > 0.0


def test_robust_z_score_is_negative_when_the_incident_is_lower():
    baseline = [10.0, 10.0, 10.0, 10.0]
    incident = [1.0, 1.0, 1.0, 1.0]

    assert robust_z_score(baseline, incident) < 0.0


def test_robust_z_score_is_zero_when_either_side_is_empty():
    assert robust_z_score([], [1.0, 2.0]) == 0.0
    assert robust_z_score([1.0, 2.0], []) == 0.0
    assert robust_z_score([], []) == 0.0


# --- score_series ------------------------------------------------------------


def test_score_series_direction_up_clamps_a_fall_to_zero():
    score = score_series(
        subject="payment",
        metric="errors",
        baseline=[10.0, 10.0, 10.0, 10.0],
        incident=[1.0, 1.0, 1.0, 1.0],
        direction=Direction.UP,
    )

    assert score.score == 0.0


def test_score_series_direction_down_clamps_a_rise_to_zero():
    score = score_series(
        subject="payment",
        metric="requests",
        baseline=[1.0, 1.0, 1.0, 1.0],
        incident=[10.0, 10.0, 10.0, 10.0],
        direction=Direction.DOWN,
    )

    assert score.score == 0.0


def test_score_series_direction_either_takes_the_magnitude():
    baseline = [10.0, 10.0, 10.0, 10.0]
    incident = [1.0, 1.0, 1.0, 1.0]  # a fall, which Direction.UP would clamp to zero

    score = score_series(
        subject="payment",
        metric="requests",
        baseline=baseline,
        incident=incident,
        direction=Direction.EITHER,
    )

    signed = robust_z_score(baseline, incident)
    assert signed < 0.0
    assert score.score == round(abs(signed), 4)


# --- AnomalyScore ------------------------------------------------------------


def test_is_trustworthy_is_false_below_min_baseline_points():
    score = AnomalyScore(
        subject="payment",
        metric="errors",
        baseline_median=1.0,
        incident_median=5.0,
        score=3.0,
        direction=Direction.UP,
        baseline_points=MIN_BASELINE_POINTS - 1,
        incident_points=5,
    )

    assert score.is_trustworthy is False


def test_is_trustworthy_is_false_with_no_incident_points():
    score = AnomalyScore(
        subject="payment",
        metric="errors",
        baseline_median=1.0,
        incident_median=0.0,
        score=0.0,
        direction=Direction.UP,
        baseline_points=MIN_BASELINE_POINTS,
        incident_points=0,
    )

    assert score.is_trustworthy is False


def test_is_trustworthy_is_true_with_enough_of_both():
    score = AnomalyScore(
        subject="payment",
        metric="errors",
        baseline_median=1.0,
        incident_median=5.0,
        score=3.0,
        direction=Direction.UP,
        baseline_points=MIN_BASELINE_POINTS,
        incident_points=1,
    )

    assert score.is_trustworthy is True


def test_delta_and_relative_delta():
    score = AnomalyScore(
        subject="payment",
        metric="errors",
        baseline_median=4.0,
        incident_median=6.0,
        score=1.0,
        direction=Direction.UP,
        baseline_points=MIN_BASELINE_POINTS,
        incident_points=1,
    )

    assert score.delta == 2.0
    assert score.relative_delta == 0.5


def test_relative_delta_is_none_for_a_zero_baseline():
    score = AnomalyScore(
        subject="payment",
        metric="errors",
        baseline_median=0.0,
        incident_median=6.0,
        score=1.0,
        direction=Direction.UP,
        baseline_points=MIN_BASELINE_POINTS,
        incident_points=1,
    )

    assert score.relative_delta is None


# --- rank_anomalies ------------------------------------------------------------


def _score(subject: str, metric: str, value: float, trustworthy: bool = True) -> AnomalyScore:
    return AnomalyScore(
        subject=subject,
        metric=metric,
        baseline_median=1.0,
        incident_median=1.0 + value,
        score=value,
        direction=Direction.UP,
        baseline_points=MIN_BASELINE_POINTS if trustworthy else 0,
        incident_points=1,
    )


def test_rank_anomalies_sorts_worst_first():
    scores = [_score("a", "m", 1.0), _score("b", "m", 9.0), _score("c", "m", 5.0)]

    ranked = rank_anomalies(scores)

    assert [s.subject for s in ranked] == ["b", "c", "a"]


def test_rank_anomalies_drops_untrustworthy_scores():
    scores = [_score("a", "m", 9.0, trustworthy=False), _score("b", "m", 5.0)]

    ranked = rank_anomalies(scores)

    assert [s.subject for s in ranked] == ["b"]


def test_rank_anomalies_drops_scores_below_the_minimum():
    scores = [_score("a", "m", 1.0), _score("b", "m", 9.0)]

    ranked = rank_anomalies(scores, minimum_score=3.0)

    assert [s.subject for s in ranked] == ["b"]


def test_rank_anomalies_ties_break_by_subject_then_metric_stably():
    scores = [
        _score("beta", "m2", 5.0),
        _score("alpha", "m2", 5.0),
        _score("alpha", "m1", 5.0),
        _score("gamma", "m1", 5.0),
    ]
    expected = [(s.subject, s.metric) for s in rank_anomalies(scores)]

    shuffled = list(scores)
    random.Random(0).shuffle(shuffled)
    reshuffled_result = [(s.subject, s.metric) for s in rank_anomalies(shuffled)]

    assert expected == [("alpha", "m1"), ("alpha", "m2"), ("beta", "m2"), ("gamma", "m1")]
    assert reshuffled_result == expected


# --- onset_index ------------------------------------------------------------


def test_onset_index_returns_the_first_crossing_index():
    baseline = [1.0, 1.0, 1.0, 1.0]
    values = [1.0, 1.0, 50.0, 50.0]

    assert onset_index(values, baseline, threshold=3.0) == 2


def test_onset_index_is_none_when_nothing_crosses():
    baseline = [1.0, 1.0, 1.0, 1.0]
    values = [1.0, 1.0, 1.0, 1.0]

    assert onset_index(values, baseline, threshold=3.0) is None


def test_onset_index_is_none_for_an_empty_series():
    assert onset_index([], [1.0, 1.0, 1.0, 1.0], threshold=3.0) is None
    assert onset_index([1.0, 2.0], [], threshold=3.0) is None
