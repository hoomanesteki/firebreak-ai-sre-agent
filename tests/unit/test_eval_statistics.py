"""Tests for firebreak.evals.statistics, against reference implementations.

SPEC.md Section 17 Phase 5 makes this an acceptance criterion: the statistics
must match reference implementations on test vectors. "I wrote a Brier score"
is worth nothing next to "it agrees with scikit-learn to twelve decimal places
on a hundred random vectors", because the failure mode here is not a crash. It
is a number that is wrong by a little, in a report, forever.

scipy and scikit-learn are dev dependencies used only here. Nothing that ships
imports them.

Hand-worked cases sit alongside the reference comparisons. A reference check
proves two implementations agree; a case whose answer can be computed on paper
proves they agree on the right thing.
"""

from __future__ import annotations

import math
import random

import pytest
from sklearn.calibration import calibration_curve
from sklearn.metrics import brier_score_loss

from firebreak.evals.statistics import (
    BOOTSTRAP_SEED,
    Interval,
    StatisticsError,
    bootstrap_mean,
    brier_score,
    calibration,
    mean,
    paired_bootstrap_difference,
    pass_at_k,
    pass_hat_k,
    percentile,
    reliability,
    selective_accuracy,
)

# Enough vectors that an implementation agreeing by luck is not plausible.
REFERENCE_VECTORS = 100
TOLERANCE = 1e-12


def _random_predictions(rng: random.Random, size: int) -> tuple[list[float], list[bool]]:
    confidences = [rng.random() for _ in range(size)]
    outcomes = [rng.random() < confidence for confidence in confidences]
    return confidences, outcomes


class TestBrierAgainstScikitLearn:
    def test_it_matches_on_a_hundred_random_vectors(self) -> None:
        rng = random.Random(1)
        for _ in range(REFERENCE_VECTORS):
            confidences, outcomes = _random_predictions(rng, rng.randint(2, 200))
            mine = brier_score(confidences, outcomes)
            theirs = brier_score_loss([int(o) for o in outcomes], confidences)
            assert mine == pytest.approx(theirs, abs=TOLERANCE)

    def test_a_perfect_forecaster_scores_zero(self) -> None:
        assert brier_score([1.0, 0.0, 1.0], [True, False, True]) == 0.0

    def test_a_perfectly_wrong_forecaster_scores_one(self) -> None:
        assert brier_score([0.0, 1.0], [True, False]) == 1.0

    def test_hedging_everything_scores_a_quarter(self) -> None:
        """Worked on paper: (0.5 - y)^2 is 0.25 whatever y is."""
        assert brier_score([0.5] * 4, [True, False, True, False]) == pytest.approx(0.25)

    def test_a_confident_mistake_is_punished_harder_than_a_hesitant_one(self) -> None:
        """The property that makes this worth reporting next to accuracy."""
        assert brier_score([0.95], [False]) > brier_score([0.55], [False])

    def test_a_confidence_outside_zero_to_one_is_refused(self) -> None:
        with pytest.raises(StatisticsError, match="confidence must be"):
            brier_score([1.5], [True])

    def test_mismatched_lengths_are_refused(self) -> None:
        with pytest.raises(StatisticsError, match="must match"):
            brier_score([0.5, 0.5], [True])


class TestCalibrationAgainstScikitLearn:
    def test_bin_accuracies_match_the_reference_curve(self) -> None:
        """sklearn's calibration_curve is the same binning, so it must agree.

        It drops empty bins and this keeps them, so the comparison is made
        over the bins sklearn chose to return.
        """
        rng = random.Random(2)
        for _ in range(20):
            confidences, outcomes = _random_predictions(rng, rng.randint(50, 400))
            mine = calibration(confidences, outcomes, bins=10)
            true_frequency, predicted_value = calibration_curve(
                [int(o) for o in outcomes], confidences, n_bins=10, strategy="uniform"
            )
            populated = [b for b in mine.bins if b.count > 0]
            assert len(populated) == len(true_frequency)
            for bin_, accuracy, confidence in zip(
                populated, true_frequency, predicted_value, strict=True
            ):
                assert bin_.accuracy == pytest.approx(accuracy, abs=TOLERANCE)
                assert bin_.mean_confidence == pytest.approx(confidence, abs=TOLERANCE)

    def test_ece_is_the_weighted_mean_gap_worked_by_hand(self) -> None:
        """Two bins, four predictions, arithmetic small enough to check.

        Bin [0.0, 0.5): confidences 0.1 and 0.3, both wrong. Mean confidence
        0.2, accuracy 0.0, gap 0.2.
        Bin [0.5, 1.0]: confidences 0.7 and 0.9, both right. Mean confidence
        0.8, accuracy 1.0, gap 0.2.
        Each bin holds half the predictions, so ECE is 0.2.
        """
        result = calibration([0.1, 0.3, 0.7, 0.9], [False, False, True, True], bins=2)
        assert result.expected_calibration_error == pytest.approx(0.2)
        assert result.maximum_calibration_error == pytest.approx(0.2)

    def test_a_perfectly_calibrated_system_scores_zero(self) -> None:
        """Says 50% ten times and is right five, in one bin."""
        confidences = [0.5] * 10
        outcomes = [True] * 5 + [False] * 5
        assert calibration(confidences, outcomes, bins=10).expected_calibration_error == (
            pytest.approx(0.0)
        )

    def test_confidence_of_exactly_one_lands_in_the_last_bin(self) -> None:
        """A perfectly confident prediction is the one most worth scoring.

        Without a closed last bin it falls outside every bin and is silently
        dropped, which flatters exactly the predictions that matter most.
        """
        result = calibration([1.0], [False], bins=10)
        assert result.predictions == 1
        assert result.bins[-1].count == 1
        assert result.expected_calibration_error == pytest.approx(1.0)

    def test_maximum_error_catches_what_the_weighted_mean_washes_out(self) -> None:
        """A large error in a sparse bin is why both numbers are reported."""
        confidences = [0.5] * 99 + [0.95]
        outcomes = [True] * 50 + [False] * 49 + [False]
        result = calibration(confidences, outcomes, bins=10)
        assert result.maximum_calibration_error > result.expected_calibration_error
        assert result.maximum_calibration_error == pytest.approx(0.95)

    def test_empty_bins_are_reported_rather_than_omitted(self) -> None:
        """An empty bin says the system never expressed that confidence."""
        result = calibration([0.05, 0.05], [True, False], bins=10)
        assert len(result.bins) == 10
        assert sum(1 for b in result.bins if b.count == 0) == 9

    def test_the_diagram_is_serialisable(self) -> None:
        """SPEC.md Section 9.3 wants a reliability diagram in every report."""
        payload = calibration([0.2, 0.8], [False, True]).as_dict()
        assert len(payload["reliability_diagram"]) == 10  # type: ignore[arg-type]


class TestPercentileAgainstNumpy:
    def test_it_matches_numpys_linear_interpolation(self) -> None:
        import numpy as np

        rng = random.Random(3)
        for _ in range(REFERENCE_VECTORS):
            values = sorted(rng.random() for _ in range(rng.randint(1, 50)))
            for fraction in (0.0, 0.025, 0.25, 0.5, 0.75, 0.975, 1.0):
                assert percentile(values, fraction) == pytest.approx(
                    float(np.percentile(values, fraction * 100)), abs=TOLERANCE
                )

    def test_a_single_observation_is_its_own_percentile(self) -> None:
        assert percentile([4.2], 0.5) == 4.2

    def test_a_fraction_outside_the_range_is_refused(self) -> None:
        with pytest.raises(StatisticsError, match="fraction must be"):
            percentile([1.0, 2.0], 1.5)


class TestBootstrap:
    def test_the_point_estimate_is_exactly_the_sample_mean(self) -> None:
        """The interval is estimated; the estimate itself is not."""
        values = [1.0, 2.0, 3.0, 4.0]
        assert bootstrap_mean(values, resamples=100).estimate == pytest.approx(2.5)

    def test_the_interval_brackets_the_estimate(self) -> None:
        rng = random.Random(4)
        values = [rng.gauss(10.0, 2.0) for _ in range(40)]
        interval = bootstrap_mean(values, resamples=2000)
        assert interval.lower <= interval.estimate <= interval.upper

    def test_it_is_reproducible(self) -> None:
        """An interval that moves between runs of the same data is not citable."""
        values = [float(i % 7) for i in range(30)]
        first = bootstrap_mean(values, resamples=500)
        second = bootstrap_mean(values, resamples=500)
        assert first == second

    def test_a_different_seed_gives_a_different_interval(self) -> None:
        """Otherwise the reproducibility above would prove nothing."""
        values = [float(i % 7) for i in range(30)]
        assert bootstrap_mean(values, resamples=500, seed=BOOTSTRAP_SEED) != bootstrap_mean(
            values, resamples=500, seed=BOOTSTRAP_SEED + 1
        )

    def test_a_constant_sample_has_a_zero_width_interval(self) -> None:
        """Every resample is identical, so there is nothing to be uncertain about."""
        interval = bootstrap_mean([3.0] * 20, resamples=200)
        assert interval.lower == interval.upper == 3.0

    def test_more_observations_narrow_the_interval(self) -> None:
        rng = random.Random(5)
        small = bootstrap_mean([rng.gauss(0.0, 1.0) for _ in range(20)], resamples=2000)
        rng = random.Random(5)
        large = bootstrap_mean([rng.gauss(0.0, 1.0) for _ in range(400)], resamples=2000)
        assert large.width < small.width

    def test_a_single_observation_gets_a_degenerate_interval(self) -> None:
        """A real situation while developing, not worth an exception."""
        interval = bootstrap_mean([7.0], resamples=10)
        assert (interval.lower, interval.estimate, interval.upper) == (7.0, 7.0, 7.0)

    def test_an_empty_sample_is_refused(self) -> None:
        with pytest.raises(StatisticsError, match="empty sample"):
            bootstrap_mean([])

    def test_it_covers_the_true_mean_about_as_often_as_claimed(self) -> None:
        """The property a 95% interval actually asserts, checked empirically.

        Coverage is approximate for a percentile bootstrap on small samples, so
        the band is generous. A wildly wrong implementation, for instance one
        that halved the interval, would still fail this.
        """
        rng = random.Random(6)
        covered = 0
        runs = 200
        for run in range(runs):
            sample = [rng.gauss(5.0, 1.0) for _ in range(30)]
            interval = bootstrap_mean(sample, resamples=400, seed=run)
            if interval.lower <= 5.0 <= interval.upper:
                covered += 1
        assert 0.85 <= covered / runs <= 1.0


class TestPairedBootstrap:
    def test_the_estimate_is_the_mean_per_task_difference(self) -> None:
        treatment = [1.0, 1.0, 0.0, 1.0]
        control = [0.0, 1.0, 0.0, 0.0]
        result = paired_bootstrap_difference(treatment, control, resamples=200)
        assert result.estimate == pytest.approx(0.5)

    def test_identical_configurations_give_a_zero_difference(self) -> None:
        values = [1.0, 0.0, 1.0, 1.0, 0.0]
        result = paired_bootstrap_difference(values, values, resamples=200)
        assert result.estimate == 0.0
        assert result.lower == result.upper == 0.0

    def test_pairing_is_tighter_than_comparing_two_intervals(self) -> None:
        """The reason SPEC.md Section 9.4 asks for a paired bootstrap.

        Both configurations find the easy tasks easy and the hard ones hard,
        so the per-task difference is nearly constant even though each
        configuration's own scores vary a great deal. Unpaired intervals throw
        that away and report far more uncertainty than the data holds.
        """
        rng = random.Random(7)
        difficulty = [rng.gauss(0.0, 5.0) for _ in range(40)]
        control = difficulty
        treatment = [d + 1.0 for d in difficulty]

        paired = paired_bootstrap_difference(treatment, control, resamples=2000)
        unpaired_width = (
            bootstrap_mean(treatment, resamples=2000).width
            + bootstrap_mean(control, resamples=2000).width
        )
        assert paired.width < unpaired_width
        assert paired.excludes_zero()

    def test_mismatched_lengths_are_refused(self) -> None:
        """Silently truncating would compare different tasks to each other."""
        with pytest.raises(StatisticsError, match="same length"):
            paired_bootstrap_difference([1.0, 2.0], [1.0])

    def test_excludes_zero_reports_what_it_says(self) -> None:
        assert Interval(1.0, 0.5, 1.5, 10, 10).excludes_zero()
        assert Interval(-1.0, -1.5, -0.5, 10, 10).excludes_zero()
        assert not Interval(0.1, -0.2, 0.4, 10, 10).excludes_zero()


class TestPassAtKAndPassHatK:
    def test_pass_at_k_matches_the_combinatorial_definition(self) -> None:
        """Three trials, one correct, k=2: 1 - C(2,2)/C(3,2) = 1 - 1/3."""
        assert pass_at_k(1, 3, 2) == pytest.approx(1.0 - 1.0 / 3.0)

    def test_pass_at_1_is_the_share_of_correct_trials(self) -> None:
        for correct in range(4):
            assert pass_at_k(correct, 3, 1) == pytest.approx(correct / 3)

    def test_pass_hat_k_needs_every_trial_to_pass(self) -> None:
        """Reliability, not capability. Two of three is not good enough."""
        assert pass_hat_k(3, 3, 3) == 1.0
        assert pass_hat_k(2, 3, 3) == 0.0

    def test_pass_hat_k_matches_the_combinatorial_definition(self) -> None:
        """Five trials, four correct, k=2: C(4,2)/C(5,2) = 6/10."""
        assert pass_hat_k(4, 5, 2) == pytest.approx(0.6)

    def test_pass_at_k_is_never_below_pass_hat_k(self) -> None:
        """At least one succeeding is always at least as likely as all of them."""
        for trials in range(1, 8):
            for correct in range(trials + 1):
                for k in range(1, trials + 1):
                    assert pass_at_k(correct, trials, k) >= pass_hat_k(correct, trials, k)

    def test_they_agree_exactly_when_k_is_one(self) -> None:
        """Exactly, not approximately. They are the same quantity at k=1.

        Computing one as `1 - C(n-c,1)/C(n,1)` and the other as `C(c,1)/C(n,1)`
        in binary floating point made them differ in the last bits, which is
        how a metric stops being comparable between runs by equality.
        """
        for trials in range(1, 8):
            for correct in range(trials + 1):
                assert pass_at_k(correct, trials, 1) == pass_hat_k(correct, trials, 1)

    def test_the_estimators_match_a_direct_enumeration(self) -> None:
        """Enumerate every subset of size k and count, which is the definition."""
        from itertools import combinations

        for trials in range(1, 7):
            for correct in range(trials + 1):
                outcomes = [True] * correct + [False] * (trials - correct)
                for k in range(1, trials + 1):
                    subsets = list(combinations(outcomes, k))
                    any_pass = sum(1 for s in subsets if any(s)) / len(subsets)
                    all_pass = sum(1 for s in subsets if all(s)) / len(subsets)
                    assert pass_at_k(correct, trials, k) == pytest.approx(any_pass)
                    assert pass_hat_k(correct, trials, k) == pytest.approx(all_pass)

    def test_impossible_arguments_are_refused(self) -> None:
        with pytest.raises(StatisticsError, match="trials must be"):
            pass_at_k(0, 0, 1)
        with pytest.raises(StatisticsError, match="correct must be"):
            pass_at_k(4, 3, 1)
        with pytest.raises(StatisticsError, match="k must be"):
            pass_at_k(1, 3, 5)


class TestReliability:
    def test_it_averages_per_task_rather_than_pooling_trials(self) -> None:
        """A task run more often must not weigh more heavily."""
        result = reliability({"a": [True, True, True], "b": [True, False, False]}, k=3)
        assert result.pass_at_1 == pytest.approx((1.0 + 1.0 / 3.0) / 2)
        assert result.pass_hat_k == pytest.approx(0.5)

    def test_uneven_trial_counts_are_refused(self) -> None:
        """pass^k is not comparable across tasks with different trial counts."""
        with pytest.raises(StatisticsError, match="same number of trials"):
            reliability({"a": [True, True, True], "b": [True, True]}, k=3)

    def test_too_few_trials_for_k_is_refused(self) -> None:
        with pytest.raises(StatisticsError, match="at least 3 trials"):
            reliability({"a": [True, True]}, k=3)

    def test_no_tasks_is_refused(self) -> None:
        with pytest.raises(StatisticsError, match="no tasks"):
            reliability({}, k=3)

    def test_the_key_names_the_k_it_used(self) -> None:
        payload = reliability({"a": [True, True, True]}, k=3).as_dict()
        assert "pass_hat_3" in payload


class TestSelectiveAccuracy:
    def test_it_scores_only_what_was_answered(self) -> None:
        result = selective_accuracy(
            outcomes=[True, False, False, True], answered=[True, True, False, False]
        )
        assert result.coverage == pytest.approx(0.5)
        assert result.selective_accuracy == pytest.approx(0.5)

    def test_overall_accuracy_counts_an_abstention_as_not_correct(self) -> None:
        """Both numbers are reported because either alone is misleading.

        A system can reach any selective accuracy it likes by abstaining more.
        """
        result = selective_accuracy(outcomes=[True, False], answered=[True, False])
        assert result.selective_accuracy == pytest.approx(1.0)
        assert result.overall_accuracy == pytest.approx(0.5)

    def test_abstaining_everywhere_is_reported_rather_than_crashing(self) -> None:
        result = selective_accuracy(outcomes=[True, True], answered=[False, False])
        assert result.coverage == 0.0
        assert result.selective_accuracy == 0.0

    def test_the_curve_starts_at_full_coverage(self) -> None:
        result = selective_accuracy(
            outcomes=[True, False, True],
            answered=[True, True, True],
            confidences=[0.9, 0.4, 0.7],
        )
        assert result.curve[0].threshold == 0.0
        assert result.curve[0].coverage == pytest.approx(1.0)

    def test_raising_the_threshold_never_raises_coverage(self) -> None:
        rng = random.Random(8)
        size = 60
        confidences = [rng.random() for _ in range(size)]
        outcomes = [rng.random() < c for c in confidences]
        result = selective_accuracy(outcomes, [True] * size, confidences)
        coverages = [point.coverage for point in result.curve]
        assert coverages == sorted(coverages, reverse=True)

    def test_a_well_ordered_system_gains_accuracy_as_coverage_falls(self) -> None:
        """What a risk-coverage curve is for: is the confidence informative?"""
        confidences = [i / 20 for i in range(20)]
        outcomes = [c >= 0.5 for c in confidences]
        result = selective_accuracy(outcomes, [True] * 20, confidences)
        assert result.curve[-1].accuracy >= result.curve[0].accuracy

    def test_risk_is_one_minus_accuracy(self) -> None:
        result = selective_accuracy([True, False], [True, True])
        assert result.curve[0].risk == pytest.approx(1.0 - result.curve[0].accuracy)

    def test_mismatched_lengths_are_refused(self) -> None:
        with pytest.raises(StatisticsError, match="must match"):
            selective_accuracy([True, False], [True])
        with pytest.raises(StatisticsError, match="confidences must match"):
            selective_accuracy([True], [True], [0.5, 0.5])


class TestMean:
    def test_it_is_exact_where_a_naive_sum_would_drift(self) -> None:
        """math.fsum, so a long list of small values does not accumulate error."""
        values = [0.1] * 1000
        assert mean(values) == pytest.approx(0.1, abs=1e-15)
        assert math.fsum(values) == pytest.approx(100.0, abs=1e-12)

    def test_no_observations_is_refused(self) -> None:
        with pytest.raises(StatisticsError, match="no observations"):
            mean([])
