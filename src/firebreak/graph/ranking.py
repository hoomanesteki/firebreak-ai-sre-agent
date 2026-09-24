"""Rank candidate culprits with a personalised PageRank over the call graph.

Anomaly scoring alone is not enough, and there is a number for that. Over
100 synthetic trials (`reports/triage/anomaly_only_ranking.json`) ranking
services by metric anomaly put the true culprit first 60 times and in the
top three 100 times. The families it fails on are the ones where the fault
propagates: error injection and unreachable dependency were ranked first in
4 of 20 each, because the caller shows the larger swing while the callee is
the thing that broke.

So the graph's job is precisely defined: reorder within a top three that
already contains the answer.

**Direction.** Edges run caller to callee and the walk follows them. This is
worth stating because SPEC.md Section 6.4 describes it as "a personalised
PageRank on the reversed dependency graph", which is ambiguous: it means the
reverse of the depends-on relation, which is the call direction. Read the
other way, as the transpose of the call graph, it is not a stylistic choice
but an empirically wrong one. eIRWR tested walking the transpose and reports
it as catastrophic, at a mean reciprocal rank of about 0.01 [R28].

**Restart vector.** The anomaly scores, normalised. A walk restarts at the
services that actually look unwell, so score concentrates on what they
depend on rather than spreading over the whole graph [R28].

**Edge weights.** Traffic share, so a caller sending most of its requests to
one dependency pushes most of its suspicion there. MonitorRank additionally
scales the matrix by the anomaly of both endpoints, as diag(sqrt(s)) W
diag(sqrt(s)), to strengthen transitions between jointly anomalous services
[R28]. That scaling is the mechanism doing the work here: removing it drops
top-1 accuracy from 143 of 147 to 4 of 147, which is worse than chance.

**What did not transfer.** eIRWR also adds self-loops and backward edges,
and reports both as improvements [R28]. Measured on this system they are
both harmful, together taking top-1 from 143 to 56, which is worse than
using no graph at all. Both therefore default to off, and both are kept as
parameters so the question can be re-asked against recorded incidents
rather than settled on synthetic ones. `reports/triage/ranking_ablation.json`
has the numbers, `make measure-ranking` regenerates them.

An edge also records its failure rate, and that is deliberately not a walk
weight. How unwell a service is already enters through the restart vector
and the endpoint scaling, and adding it a third time on the edge would
count the same evidence three times while looking like three signals.

Sources, both read rather than cited from a summary:

- [R28] eIRWR: Enhanced Iterative Random Walk with Restart for Scalable Root
  Cause Analysis in Microservices. https://arxiv.org/abs/2608.08073
  Gives the row-stochastic construction w_ij = lambda_ij / sum_k lambda_ik,
  the restart formulation r = (1 - a) M r + a v, the transpose result, and
  the MonitorRank scaling.
- [R29] A Comprehensive Survey on Root Cause Analysis in (Micro) Services.
  https://arxiv.org/abs/2408.00803
  Places the approach among its peers: MonitorRank, MicroRCA, CloudRanger,
  MS-Rank, AutoMAP and TraceRank all apply random walks over dependency or
  causal graphs.

**Why the iteration is written out here** rather than called from a library.
NetworkX's `pagerank` dispatches to a SciPy sparse implementation, which
means numpy and scipy as dependencies for twenty lines of arithmetic, and,
more importantly, a summation order this code does not control. Firebreak's
claim is that a citation re-runs to the same value; Phase 3 already lost a
day to evidence ids differing between machines. Iterating over sorted nodes
adds the same terms in the same order everywhere, and a reader can check the
algorithm against the paper without leaving the file.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from firebreak.lab.bundle import EdgeRecord
from firebreak.triage.anomaly import AnomalyScore

# Restart probability. The walk returns to an anomalous service this often,
# which is what keeps score near the observed symptoms instead of drifting
# to whatever the graph's most central node happens to be.
RESTART_ALPHA = 0.15

# An exponent on the restart vector, concentrating it on the worst
# offenders. eIRWR raises its belief vector to a power for that reason
# [R28], and one is the value that says not to.
#
# Measured, sharpening hurts: at 2.0 top-1 is 143 of 147 and at 3.0 it is
# 141, against 147 at 1.0. The mechanism is easy to see once stated. The
# service with the largest metric swing is usually a symptom rather than a
# cause, so squaring the restart vector hands the symptom a quadratic head
# start and asks the walk to overcome it.
RESTART_SHARPNESS = 1.0

# A service whose anomaly started earlier is more likely the cause, since
# causes precede symptoms (SPEC.md Section 6.5). Applied as a multiplier on
# the final score rather than inside the walk, so the two effects stay
# separable when measuring which one helps.
ONSET_BONUS = 1.25

# Both of the following default to off, and the reason is a measurement
# rather than a preference. eIRWR adds self-loops and backward edges to a
# random walk with restart and reports both as improvements [R28]. Neither
# transferred: on 147 trials, turning them on took top-1 accuracy from 143
# to 56, which is worse than using no graph at all
# (`reports/triage/ranking_ablation.json`, regenerate with
# `make measure-ranking`).
#
# The mechanisms are kept, and kept measurable, rather than deleted. The
# ablation runs on synthetic fixtures whose faults propagate along a single
# call path, which is a shape that flatters a purely forward walk, and the
# honest position is that this result is provisional until it is re-run on
# recorded incidents. Deleting the code would make that re-run impossible.

# A self-loop makes a leaf absorbing rather than dangling, so suspicion that
# reaches a database or a cache stays there instead of being redistributed
# along the restart vector. Sound in principle and harmful in practice here:
# a loud upstream symptom also retains its own mass, and that effect is the
# larger one (143 to 135 top-1 with self-loops alone).
SELF_LOOP_RATIO = 0.0

# Suspicion flowing back from callee to caller, so that "the caller is
# hammering it" stays reachable as an explanation. The most damaging of the
# two on this data (143 to 56), because it feeds score straight back into
# the upstream symptoms the ranking exists to demote.
BACKWARD_RATIO = 0.0

PAGERANK_MAX_ITER = 200
PAGERANK_TOLERANCE = 1.0e-8


class PageRankConvergenceError(RuntimeError):
    """The power iteration ran out of iterations before settling."""


@dataclass(frozen=True)
class Candidate:
    """One service, with why it is suspected and how strongly.

    Both inputs are kept alongside the result, because the interesting
    reports are the ones where they disagree: a service with a high anomaly
    score and a low rank is the shape of an upstream symptom, and that is
    something a report should be able to say out loud.
    """

    service: str
    score: float
    anomaly_score: float
    graph_score: float
    earliest_onset_seconds: float | None = None


# A directed weighted graph as nested dicts: graph[client][server] = weight.
# Deliberately not a library graph type. The only operations needed are "sum
# a node's out-weight" and "iterate a node's successors", and a plain mapping
# does both while keeping iteration order under this module's control.
CallGraph = dict[str, dict[str, float]]


def build_call_graph(edges: list[EdgeRecord]) -> CallGraph:
    """A call graph with edges pointing from caller to callee.

    Weights are traffic rather than a count of edges, so a caller that sends
    most of its requests to one dependency pushes most of its suspicion
    there. Repeated observations of the same edge are summed, which is what
    makes the result independent of how the recorder batched them.

    Every service mentioned gets a key, including one that only ever
    receives calls, so that a leaf is a node with no out-edges rather than a
    missing entry.
    """
    graph: CallGraph = {}
    for edge in edges:
        graph.setdefault(edge.client, {})
        graph.setdefault(edge.server, {})
        out = graph[edge.client]
        out[edge.server] = out.get(edge.server, 0.0) + edge.requests_per_second
    return graph


def collapse_scores(scores: list[AnomalyScore]) -> dict[str, float]:
    """One anomaly number per service: its worst signal.

    A service is as suspicious as its most alarming metric. Summing across
    metrics instead would reward a service that is slightly unusual in many
    ways over one that is badly broken in a single way, which is backwards.
    Untrustworthy scores are dropped rather than counted as zero, because
    "not enough baseline to say" is not the same claim as "healthy".
    """
    worst: dict[str, float] = {}
    for score in scores:
        if not score.is_trustworthy:
            continue
        worst[score.subject] = max(worst.get(score.subject, 0.0), score.score)
    return worst


def _restart_vector(
    anomalies: dict[str, float], nodes: list[str], sharpness: float
) -> dict[str, float]:
    """Normalised anomaly scores, sharpened, over the graph's nodes.

    Falls back to a uniform vector when nothing is anomalous, so the walk is
    still defined. A uniform restart reduces the result to plain graph
    centrality, which is the honest answer to "nothing looks wrong": it ranks
    by structural importance and claims nothing about a fault.
    """
    if not nodes:
        return {}
    raw = {node: max(anomalies.get(node, 0.0), 0.0) ** sharpness for node in nodes}
    total = sum(raw.values())
    if total <= 0.0:
        return dict.fromkeys(nodes, 1.0 / len(nodes))
    return {node: value / total for node, value in raw.items()}


def add_backward_edges(graph: CallGraph, ratio: float) -> CallGraph:
    """Mirror every call edge at a fraction of its weight [R28].

    A callee is not always the culprit. A service can be slow because a
    caller is sending it far more traffic than usual, and with forward edges
    only the walk can never reach that explanation. The ratio keeps the
    forward direction dominant.

    An existing forward edge between the same pair is added to rather than
    replaced, so a genuinely bidirectional pair of services keeps both of
    its observed weights.
    """
    if ratio <= 0.0:
        return {client: dict(successors) for client, successors in graph.items()}
    mirrored: CallGraph = {client: dict(successors) for client, successors in graph.items()}
    for client, successors in graph.items():
        for server, weight in successors.items():
            back = mirrored.setdefault(server, {})
            back[client] = back.get(client, 0.0) + weight * ratio
    return mirrored


def add_self_loops(graph: CallGraph, ratio: float) -> CallGraph:
    """Give every node a self-loop, so a leaf absorbs rather than recycles.

    Two cases, and the difference is the point:

    - A node with out-edges keeps `ratio` of its out-flow, holding back some
      of its own suspicion instead of passing all of it downstream.
    - A node with none is a leaf, and gets a self-loop of weight one, making
      it fully absorbing. This is the case that matters. Without it the leaf
      is dangling, and the mass that reached it is redistributed along the
      restart vector to whichever service looks worst, which is exactly the
      upstream symptom the ranking is supposed to demote.
    """
    if ratio <= 0.0:
        return {client: dict(successors) for client, successors in graph.items()}
    looped: CallGraph = {}
    for node, successors in graph.items():
        out_weight = sum(successors.values())
        looped[node] = dict(successors)
        self_weight = out_weight * ratio if out_weight > 0.0 else 1.0
        looped[node][node] = looped[node].get(node, 0.0) + self_weight
    return looped


def _scale_by_endpoint_anomaly(graph: CallGraph, anomalies: dict[str, float]) -> CallGraph:
    """MonitorRank's diag(sqrt(s)) W diag(sqrt(s)) scaling [R28].

    Strengthens a transition between two services that are both unwell,
    which is what a propagating fault looks like, and leaves a busy edge
    between two healthy ones comparatively weaker. The scores are shifted by
    one before the square root so that a healthy endpoint scales an edge by
    one rather than erasing it.
    """
    scaled: CallGraph = {}
    for client, successors in graph.items():
        client_factor = math.sqrt(1.0 + anomalies.get(client, 0.0))
        scaled[client] = {
            server: weight * client_factor * math.sqrt(1.0 + anomalies.get(server, 0.0))
            for server, weight in successors.items()
        }
    return scaled


def personalised_pagerank(
    graph: CallGraph,
    restart: dict[str, float],
    damping: float,
    max_iterations: int = PAGERANK_MAX_ITER,
    tolerance: float = PAGERANK_TOLERANCE,
) -> dict[str, float]:
    """Random walk with restart, as r = (1 - a) M r + a v [R28].

    `damping` is 1 - a, the probability of following an edge rather than
    restarting. Written as an explicit power iteration over sorted nodes so
    the same inputs produce bit-identical results on any machine.

    A node with no out-edges is a dangling node, and a leaf is exactly where
    a root cause tends to sit, so its mass cannot simply be dropped: doing
    so leaks probability every iteration and understates precisely the
    services the ranking exists to find. It is redistributed along the
    restart vector, the standard treatment.
    """
    nodes = sorted(graph)
    if not nodes:
        return {}

    out_weight = {node: sum(graph[node].values()) for node in nodes}
    # Zero-weight out-edges are dangling too: an edge observed carrying no
    # traffic transfers no suspicion, and dividing by it would be a crash.
    dangling = [node for node in nodes if out_weight[node] <= 0.0]

    rank = dict(restart)
    for _ in range(max_iterations):
        nxt = dict.fromkeys(nodes, 0.0)

        for client in nodes:
            total_out = out_weight[client]
            if total_out <= 0.0:
                continue
            contribution = damping * rank[client]
            for server in sorted(graph[client]):
                nxt[server] += contribution * graph[client][server] / total_out

        leaked = damping * sum(rank[node] for node in dangling)
        for node in nodes:
            nxt[node] += leaked * restart[node] + (1.0 - damping) * restart[node]

        delta = sum(abs(nxt[node] - rank[node]) for node in nodes)
        rank = nxt
        if delta < tolerance:
            return rank
    # Not converging is worth reporting rather than hiding, since a silent
    # partial result would be presented as a ranking with the same
    # confidence as a converged one.
    raise PageRankConvergenceError(
        f"power iteration did not settle within {max_iterations} iterations "
        f"(last change {delta:.3e}, tolerance {tolerance:.3e})"
    )


def rank_candidates(
    edges: list[EdgeRecord],
    scores: list[AnomalyScore],
    onsets: dict[str, float] | None = None,
    alpha: float = RESTART_ALPHA,
    sharpness: float = RESTART_SHARPNESS,
    use_endpoint_scaling: bool = True,
    onset_bonus: float = ONSET_BONUS,
    self_loop_ratio: float = SELF_LOOP_RATIO,
    backward_ratio: float = BACKWARD_RATIO,
) -> list[Candidate]:
    """Rank services as likely culprits, worst first.

    Deterministic for the same inputs. Ties break on service name, so a
    reliability measure like pass^3 means something: a ranking that
    reshuffled between identical runs would make repeated trials disagree
    for no reason at all.
    """
    if not 0.0 < alpha <= 1.0:
        raise ValueError(f"restart probability must be in (0, 1], got {alpha}")

    anomalies = collapse_scores(scores)
    graph = build_call_graph(edges)
    # A service can be anomalous without appearing in any observed edge, and
    # dropping it would leave the ranking unable to name it at all.
    for service in anomalies:
        graph.setdefault(service, {})

    nodes = sorted(graph)
    if not nodes:
        return []

    # Order matters. Backward edges are mirrored from the observed traffic,
    # so they are added first, then the anomaly scaling applies to forward
    # and backward alike, and self-loops are added last so that the ratio is
    # a share of the scaled out-flow rather than the raw one.
    walked = add_backward_edges(graph, backward_ratio)
    if use_endpoint_scaling:
        walked = _scale_by_endpoint_anomaly(walked, anomalies)
    walked = add_self_loops(walked, self_loop_ratio)
    restart = _restart_vector(anomalies, nodes, sharpness)
    graph_scores = personalised_pagerank(walked, restart, damping=1.0 - alpha)

    earliest = min(onsets.values()) if onsets else None
    candidates = []
    for node in nodes:
        graph_score = graph_scores.get(node, 0.0)
        onset = onsets.get(node) if onsets else None
        started_first = onset is not None and earliest is not None and onset <= earliest
        candidates.append(
            Candidate(
                service=node,
                score=round(graph_score * (onset_bonus if started_first else 1.0), 10),
                anomaly_score=round(anomalies.get(node, 0.0), 4),
                graph_score=round(graph_score, 10),
                earliest_onset_seconds=onset,
            )
        )
    return sorted(candidates, key=lambda c: (-c.score, c.service))


def edges_from_topology(topology: dict[str, Any]) -> list[EdgeRecord]:
    """Read call edges out of a bundle's recorded topology, strictly.

    Parsed through `EdgeRecord.from_row`, the same single parse the topology
    tools use, rather than read field by field. An earlier reader in this
    codebase accepted either of two shapes two producers happened to write,
    and the cost of that tolerance was every edge silently reporting no
    traffic. A producer that drifts should fail here, loudly.
    """
    raw = topology.get("edges")
    if not isinstance(raw, list):
        raise ValueError("topology has no edges list")
    edges = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(f"topology edge is not an object: {entry!r}")
        edges.append(EdgeRecord.from_row(entry))
    return edges
