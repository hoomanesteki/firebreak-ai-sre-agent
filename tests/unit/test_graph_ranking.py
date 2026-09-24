"""Tests for firebreak.graph.ranking on hand-built graphs with known answers.

Hand-built rather than generated, because the point of these is that a
reader can work out the right answer themselves. A test over a fixture whose
correct ranking nobody can derive by hand would only assert that the code
keeps doing what it does today.
"""

from __future__ import annotations

import math
from itertools import pairwise

import pytest

from firebreak.graph.ranking import (
    BACKWARD_RATIO,
    ONSET_BONUS,
    RESTART_ALPHA,
    RESTART_SHARPNESS,
    SELF_LOOP_RATIO,
    Candidate,
    PageRankConvergenceError,
    add_backward_edges,
    add_self_loops,
    build_call_graph,
    collapse_scores,
    edges_from_topology,
    personalised_pagerank,
    rank_candidates,
)
from firebreak.lab.bundle import EdgeRecord
from firebreak.signals import MetricName
from firebreak.triage.anomaly import MIN_BASELINE_POINTS, AnomalyScore, Direction


def score(
    subject: str,
    value: float,
    metric: MetricName = MetricName.SPAN_DURATION_P95_MS,
    baseline_points: int = 20,
) -> AnomalyScore:
    """An anomaly score for `subject`, trustworthy unless told otherwise."""
    return AnomalyScore(
        subject=subject,
        metric=str(metric),
        baseline_median=10.0,
        incident_median=10.0 + value,
        score=value,
        direction=Direction.UP,
        baseline_points=baseline_points,
        incident_points=10,
    )


def edge(client: str, server: str, rps: float = 100.0, failures: float = 0.0) -> EdgeRecord:
    return EdgeRecord(
        client=client, server=server, requests_per_second=rps, failures_per_second=failures
    )


def order(candidates: list[Candidate]) -> list[str]:
    return [candidate.service for candidate in candidates]


# The case the whole module exists for. A three service chain, the fault in
# the leaf, and the largest metric swing on its caller. Ranking by anomaly
# alone answers "cart"; the correct answer is "redis".
CHAIN = [edge("frontend", "cart"), edge("cart", "redis")]
CHAIN_SCORES = [score("frontend", 2.0), score("cart", 3.0), score("redis", 1.0)]


class TestPropagation:
    def test_leaf_culprit_outranks_its_louder_caller(self) -> None:
        assert order(rank_candidates(CHAIN, CHAIN_SCORES))[0] == "redis"

    def test_anomaly_alone_would_have_answered_the_caller(self) -> None:
        """The premise of the test above, asserted so it cannot rot.

        If the fixture ever stopped being a case where anomaly scoring gets
        it wrong, the test above would still pass while proving nothing.
        """
        loudest = max(CHAIN_SCORES, key=lambda s: s.score)
        assert loudest.subject == "cart"

    def test_walking_the_transpose_buries_the_culprit(self) -> None:
        """SPEC.md Section 6.4 is ambiguous and one reading is wrong.

        "Reversed dependency graph" means the reverse of depends-on, which
        is the call direction. Read instead as the transpose of the call
        graph, the walk carries score away from the culprit and into the
        services that merely called it. eIRWR measured that as catastrophic
        and this asserts the same thing on a graph small enough to check by
        hand.
        """
        transposed = [edge(e.server, e.client, e.requests_per_second) for e in CHAIN]
        assert order(rank_candidates(transposed, CHAIN_SCORES))[-1] == "redis"

    def test_a_service_with_no_edges_can_still_be_ranked(self) -> None:
        """Anomalous but absent from the topology is not the same as absent.

        Dropping it would make the ranking structurally unable to name a
        service whose traffic the collector happened to miss.
        """
        ranked = order(rank_candidates(CHAIN, [*CHAIN_SCORES, score("orphan", 9.0)]))
        assert "orphan" in ranked


class TestScoreCollapse:
    def test_a_service_is_as_bad_as_its_worst_signal(self) -> None:
        collapsed = collapse_scores(
            [
                score("cart", 1.0, MetricName.SPAN_DURATION_P50_MS),
                score("cart", 7.0, MetricName.SPAN_DURATION_P95_MS),
                score("cart", 2.0, MetricName.CONTAINER_CPU_UTILISATION),
            ]
        )
        assert collapsed == {"cart": 7.0}

    def test_untrustworthy_scores_are_dropped_not_zeroed(self) -> None:
        """Too little baseline is "cannot say", which is not "healthy"."""
        collapsed = collapse_scores([score("cart", 50.0, baseline_points=MIN_BASELINE_POINTS - 1)])
        assert collapsed == {}


class TestGraphConstruction:
    def test_repeated_observations_of_an_edge_are_summed(self) -> None:
        graph = build_call_graph([edge("a", "b", 10.0), edge("a", "b", 15.0)])
        assert graph["a"]["b"] == 25.0

    def test_a_callee_is_a_node_even_with_no_outgoing_calls(self) -> None:
        assert build_call_graph([edge("a", "b")])["b"] == {}

    def test_self_loops_make_a_leaf_absorbing(self) -> None:
        looped = add_self_loops(build_call_graph([edge("a", "b")]), ratio=0.5)
        assert looped["b"] == {"b": 1.0}
        assert looped["a"]["a"] == pytest.approx(50.0)

    def test_backward_edges_are_added_at_the_given_ratio(self) -> None:
        mirrored = add_backward_edges(build_call_graph([edge("a", "b", 100.0)]), ratio=0.3)
        assert mirrored["b"]["a"] == pytest.approx(30.0)
        assert mirrored["a"]["b"] == pytest.approx(100.0)

    def test_a_ratio_of_zero_changes_nothing(self) -> None:
        graph = build_call_graph([edge("a", "b")])
        assert add_backward_edges(graph, 0.0) == graph
        assert add_self_loops(graph, 0.0) == graph

    def test_shaping_does_not_mutate_the_input_graph(self) -> None:
        """A shared mutable graph would make the ablation sweep meaningless.

        The harness ranks one bundle under eight configurations in a row. If
        any of these wrote through to the graph it was handed, every
        configuration after the first would be measuring a different system.
        """
        graph = build_call_graph([edge("a", "b", 100.0)])
        before = {node: dict(successors) for node, successors in graph.items()}
        add_backward_edges(graph, 0.3)
        add_self_loops(graph, 0.5)
        assert graph == before


class TestPageRank:
    def test_probability_is_conserved(self) -> None:
        """Including at a dangling node, which is where leaks show up."""
        ranked = rank_candidates(CHAIN, CHAIN_SCORES)
        assert sum(c.graph_score for c in ranked) == pytest.approx(1.0)

    def test_a_cycle_converges(self) -> None:
        cyclic = [edge("a", "b"), edge("b", "c"), edge("c", "a")]
        ranked = rank_candidates(cyclic, [score("a", 3.0), score("b", 1.0), score("c", 1.0)])
        assert sum(c.graph_score for c in ranked) == pytest.approx(1.0)

    def test_an_edge_carrying_no_traffic_is_treated_as_dangling(self) -> None:
        """Dividing by a zero out-weight would be a crash, not a ranking."""
        ranked = rank_candidates([edge("a", "b", 0.0)], [score("a", 3.0), score("b", 1.0)])
        assert sum(c.graph_score for c in ranked) == pytest.approx(1.0)

    def test_refusing_to_converge_is_raised_not_hidden(self) -> None:
        """A partial answer presented as a ranking is worse than an error."""
        # A chain with all the restart mass at one end, so the score has to
        # travel the whole way before it settles. A symmetric graph would
        # start at its own fixed point and converge immediately, which would
        # make this test pass for the wrong reason.
        chain = ["a", "b", "c", "d", "e", "f"]
        graph = {node: {nxt: 1.0} for node, nxt in pairwise(chain)}
        graph[chain[-1]] = {}
        restart = {node: (1.0 if node == "a" else 0.0) for node in chain}
        with pytest.raises(PageRankConvergenceError, match="did not settle"):
            personalised_pagerank(graph, restart, damping=0.85, max_iterations=2)

    def test_an_impossible_restart_probability_is_refused(self) -> None:
        for alpha in (0.0, -0.1, 1.5):
            with pytest.raises(ValueError, match="restart probability"):
                rank_candidates(CHAIN, CHAIN_SCORES, alpha=alpha)


class TestQuietSystem:
    def test_no_anomalies_falls_back_to_a_defined_ranking(self) -> None:
        """A uniform restart is plain centrality, which claims no fault.

        The ranking must still be defined, because the caller that decides
        nothing is wrong is the abstention rule in the pipeline, not this
        function returning an empty list.
        """
        ranked = rank_candidates(CHAIN, [])
        assert sum(c.graph_score for c in ranked) == pytest.approx(1.0)
        assert len(ranked) == 3

    def test_nothing_at_all_ranks_nothing(self) -> None:
        assert rank_candidates([], []) == []


class TestDeterminism:
    def test_identical_inputs_give_identical_output(self) -> None:
        assert rank_candidates(CHAIN, CHAIN_SCORES) == rank_candidates(CHAIN, CHAIN_SCORES)

    def test_edge_order_does_not_change_the_ranking(self) -> None:
        assert order(rank_candidates(CHAIN, CHAIN_SCORES)) == order(
            rank_candidates(list(reversed(CHAIN)), list(reversed(CHAIN_SCORES)))
        )

    def test_ties_break_on_service_name(self) -> None:
        """Without this a pass^3 reliability measure would be noise."""
        symmetric = [edge("zebra", "middle"), edge("alpha", "middle")]
        scores = [score("zebra", 2.0), score("alpha", 2.0), score("middle", 2.0)]
        ranked = rank_candidates(symmetric, scores)
        tied = [c.service for c in ranked if c.score == ranked[0].score]
        assert tied == sorted(tied)


class TestOnsetBonus:
    def test_the_service_that_started_first_is_promoted(self) -> None:
        """Causes precede symptoms (SPEC.md Section 6.5)."""
        edges = [edge("frontend", "cart"), edge("frontend", "ad")]
        scores = [score("frontend", 1.0), score("cart", 5.0), score("ad", 5.0)]
        without = rank_candidates(edges, scores, onset_bonus=1.0)
        with_bonus = rank_candidates(edges, scores, onsets={"ad": 0.0, "cart": 60.0})
        assert order(without)[0] == "ad"  # the name tiebreak, nothing more
        promoted = next(c for c in with_bonus if c.service == "ad")
        assert promoted.score == pytest.approx(promoted.graph_score * ONSET_BONUS)

    def test_a_service_with_no_recorded_onset_gets_no_bonus(self) -> None:
        ranked = rank_candidates(CHAIN, CHAIN_SCORES, onsets={"cart": 10.0})
        redis = next(c for c in ranked if c.service == "redis")
        assert redis.score == pytest.approx(redis.graph_score)


class TestTopologyParsing:
    def test_a_recorded_topology_round_trips(self) -> None:
        payload = {"edges": [edge("a", "b", 12.5, 0.5).as_row()]}
        assert edges_from_topology(payload) == [edge("a", "b", 12.5, 0.5)]

    def test_a_drifted_edge_shape_is_refused(self) -> None:
        """The Phase 3 defect, asserted so it cannot return quietly.

        Two producers once wrote `call_count` and `requests_per_second` for
        the same field, and the reader that tolerated both reported every
        edge as carrying no traffic.
        """
        with pytest.raises(ValueError, match=r"call_count|extra"):
            edges_from_topology({"edges": [{"client": "a", "server": "b", "call_count": 10}]})

    def test_a_topology_without_edges_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no edges"):
            edges_from_topology({})


class TestDefaultsMatchWhatWasMeasured:
    """The defaults encode a measurement, so they are asserted, not assumed.

    `reports/triage/ranking_ablation.json` records that self-loops and
    backward edges both made the ranking worse on this system, against their
    published behaviour. Someone restoring the published values because the
    paper says so would silently undo that, so the measured choice is
    pinned here where the change has to be deliberate.
    """

    def test_the_eirwr_enhancements_are_off(self) -> None:
        assert SELF_LOOP_RATIO == 0.0
        assert BACKWARD_RATIO == 0.0

    def test_the_restart_vector_is_not_sharpened(self) -> None:
        assert RESTART_SHARPNESS == 1.0

    def test_restart_probability_is_the_conventional_damping(self) -> None:
        assert pytest.approx(0.15) == RESTART_ALPHA

    def test_turning_them_on_still_ranks_the_culprit_somewhere(self) -> None:
        """Harmful is not the same as broken, and the knobs must still work."""
        ranked = order(
            rank_candidates(CHAIN, CHAIN_SCORES, self_loop_ratio=0.5, backward_ratio=0.3)
        )
        assert set(ranked) == {"frontend", "cart", "redis"}


class TestEndpointScaling:
    def test_a_jointly_anomalous_edge_is_strengthened(self) -> None:
        """MonitorRank's diag(sqrt(s)) W diag(sqrt(s)), checked by hand.

        Two callers, equal traffic, one calling an anomalous service and one
        calling a healthy one. The anomalous pair should carry more of the
        walk.
        """
        edges = [edge("front", "hot", 100.0), edge("front", "cold", 100.0)]
        scores = [score("front", 1.0), score("hot", 8.0), score("cold", 0.0)]
        scaled = rank_candidates(edges, scores, use_endpoint_scaling=True)
        plain = rank_candidates(edges, scores, use_endpoint_scaling=False)
        hot_scaled = next(c for c in scaled if c.service == "hot").graph_score
        hot_plain = next(c for c in plain if c.service == "hot").graph_score
        assert hot_scaled > hot_plain

    def test_the_scaling_factor_is_the_documented_one(self) -> None:
        """sqrt((1 + s_client) * (1 + s_server)) on the edge weight."""
        from firebreak.graph.ranking import _scale_by_endpoint_anomaly

        scaled = _scale_by_endpoint_anomaly({"a": {"b": 10.0}}, {"a": 3.0, "b": 8.0})
        assert scaled["a"]["b"] == pytest.approx(10.0 * math.sqrt(4.0 * 9.0))
