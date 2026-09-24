"""The tuned thresholds and the code defaults must be the same numbers.

`config/thresholds.yaml` is where an operator changes a threshold, and the
module-level constants in `firebreak.triage` and `firebreak.graph` are what
those modules use when no config is supplied, which is how their unit tests
can run without a config file existing at all.

That is one number written in two places, which is precisely the shape of
the defect that cost Phase 3 four separate bugs: two components agreed on a
type and disagreed on a value, and nothing failed. Here the failure would be
quieter still. The ablation harness would keep reporting the measured
behaviour of the constants while the shipped pipeline used whatever the YAML
said, and the report would be describing a system that was not running.

So the duplication is allowed and this test is the price of it.
"""

from __future__ import annotations

import pytest

from firebreak.graph.ranking import (
    BACKWARD_RATIO,
    ONSET_BONUS,
    RESTART_ALPHA,
    RESTART_SHARPNESS,
    SELF_LOOP_RATIO,
)
from firebreak.triage.pipeline import BASELINE_FRACTION, GUARD_SECONDS
from firebreak.triage.scoring import ONSET_THRESHOLD
from firebreak.triage.thresholds import THRESHOLDS_PATH, load_thresholds


@pytest.fixture(scope="module")
def tuned():  # type: ignore[no-untyped-def]
    return load_thresholds()


def test_the_thresholds_file_exists_where_the_loader_looks() -> None:
    """A missing file would fall back to defaults and measure the wrong thing."""
    assert THRESHOLDS_PATH.is_file(), f"{THRESHOLDS_PATH} is missing"


class TestRankingDefaults:
    def test_restart_alpha(self, tuned) -> None:  # type: ignore[no-untyped-def]
        assert tuned.ranking.restart_alpha == pytest.approx(RESTART_ALPHA)

    def test_restart_sharpness(self, tuned) -> None:  # type: ignore[no-untyped-def]
        assert tuned.ranking.restart_sharpness == pytest.approx(RESTART_SHARPNESS)

    def test_self_loop_ratio(self, tuned) -> None:  # type: ignore[no-untyped-def]
        assert tuned.ranking.self_loop_ratio == pytest.approx(SELF_LOOP_RATIO)

    def test_backward_ratio(self, tuned) -> None:  # type: ignore[no-untyped-def]
        assert tuned.ranking.backward_ratio == pytest.approx(BACKWARD_RATIO)

    def test_onset_bonus(self, tuned) -> None:  # type: ignore[no-untyped-def]
        assert tuned.ranking.onset_bonus == pytest.approx(ONSET_BONUS)

    def test_onset_threshold(self, tuned) -> None:  # type: ignore[no-untyped-def]
        assert tuned.ranking.onset_threshold_z == pytest.approx(ONSET_THRESHOLD)

    def test_endpoint_scaling_is_on(self, tuned) -> None:  # type: ignore[no-untyped-def]
        """Off measured worse, so it being off would be a silent regression."""
        assert tuned.ranking.use_endpoint_scaling is True


class TestWindowDefaults:
    def test_baseline_fraction(self, tuned) -> None:  # type: ignore[no-untyped-def]
        assert tuned.windows.baseline_fraction == pytest.approx(BASELINE_FRACTION)

    def test_guard_seconds(self, tuned) -> None:  # type: ignore[no-untyped-def]
        assert tuned.windows.guard_seconds == pytest.approx(GUARD_SECONDS)


class TestEveryRankingOptionIsForwarded:
    """`as_ranking_options` must name every parameter the ranking accepts.

    A parameter added to `rank_candidates` but forgotten here would be
    tunable in the YAML, appear to be configured, and silently keep its
    hard coded value in production.
    """

    def test_option_names_match_the_ranking_signature(self) -> None:
        import inspect

        from firebreak.graph.ranking import rank_candidates
        from firebreak.triage.thresholds import RankingThresholds

        accepted = set(inspect.signature(rank_candidates).parameters)
        tunable = {name for name in accepted if name not in {"edges", "scores", "onsets"}}
        forwarded = set(
            RankingThresholds.model_construct(
                restart_alpha=0.15,
                restart_sharpness=1.0,
                use_endpoint_scaling=True,
                self_loop_ratio=0.0,
                backward_ratio=0.0,
                onset_bonus=1.0,
                onset_threshold_z=3.0,
            ).as_ranking_options()
        )
        assert forwarded == tunable, (
            "config/thresholds.yaml does not cover every ranking parameter; "
            f"missing {tunable - forwarded}, unknown {forwarded - tunable}"
        )


class TestAbstentionIsJustified:
    def test_the_threshold_sits_inside_the_measured_gap(self, tuned) -> None:  # type: ignore[no-untyped-def]
        """The file records the measurement, so the two must agree.

        A threshold outside the gap it claims to sit in would mean the
        recorded justification belongs to a different measurement than the
        number being shipped.
        """
        import yaml

        raw = yaml.safe_load(THRESHOLDS_PATH.read_text(encoding="utf-8"))
        measured = raw["abstention"]["measured"]
        chosen = tuned.abstention.minimum_top_anomaly_z
        assert measured["quiet_max"] < chosen < measured["faulted_min"]
