# ADR-0007: Candidate ranking by personalised PageRank over the call graph

- Status: Accepted
- Date: 2026-09-24
- Phase: 4

## Context

Anomaly scoring alone does not find root causes reliably, and Phase 3
measured how badly. Over 147 trials, ranking services by their worst metric
anomaly put the true culprit first 92 times. The failures were not spread
evenly. Resource faults were found first almost always, because a container
metric moves on the faulty service and nowhere else. Error injection and
unreachable dependency were found first about one time in five, because the
fault propagates and the caller shows the larger swing.

That is the whole problem in one sentence: **the service that alerts is
usually not the service that failed.** A metric detector has no way to know
this, because nothing in a single time series says whether a service is sick
or merely downstream of something sick. The dependency graph is where that
information lives.

SPEC.md Section 6.4 requires this phase to find and read a published paper
that uses the approach, and to cite nothing it has not read.

## Decision

Rank candidates with a personalised PageRank over the service call graph:

- **Edges point from caller to callee**, weighted by observed traffic.
- **The restart vector is the anomaly scores**, normalised, so the walk
  keeps returning to the services that actually look unwell.
- **Edge weights are scaled by the anomaly of both endpoints**, as
  `diag(sqrt(s)) W diag(sqrt(s))`, which strengthens a transition between
  two jointly unwell services.
- **A service that started earlier gets a bonus**, since causes precede
  symptoms.

Implemented in `src/firebreak/graph/ranking.py`, with the parameters in
`config/thresholds.yaml` and the measurement behind every one of them in
`reports/triage/ranking_ablation.json`.

## Sources, both read rather than cited from a summary

- **[R28] eIRWR: Enhanced Iterative Random Walk with Restart for Scalable
  Root Cause Analysis in Microservices.** https://arxiv.org/abs/2608.08073
  The paper this design is taken from. It gives the row stochastic
  construction `w_ij = lambda_ij / sum_k lambda_ik` from call frequencies,
  the restart formulation `r = (1 - a) M r + a v`, MonitorRank's endpoint
  scaling, and a comparison against MicroRCA, CloudRanger, MonitorRank,
  MicroHECL and TraceDiag by precision at k and mean reciprocal rank.
- **[R29] A Comprehensive Survey on Root Cause Analysis in (Micro)
  Services.** https://arxiv.org/abs/2408.00803
  Read for context rather than mechanics. It places random walks among
  their peers and confirms the approach is standard rather than exotic:
  MonitorRank, MicroRCA, CloudRanger, MS-Rank, AutoMAP and TraceRank all
  walk a dependency or causal graph.

## The direction question, which is not a matter of taste

SPEC.md Section 6.4 says "a personalised PageRank on the reversed dependency
graph". That phrase has two readings, and only one of them works.

Read as the reverse of the depends-on relation, it means the call direction,
caller to callee, which is what this implementation does. Read as the
transpose of the call graph, it means walking callee to caller.

The second reading is not a stylistic variant. eIRWR tested it and reports
it as catastrophic, at a mean reciprocal rank of about 0.01 [R28]. The
reason is visible on a three node chain: with the transpose, score flows
from the broken leaf into the services that merely called it, which is
precisely the upstream symptom the ranking exists to demote.
`tests/unit/test_graph_ranking.py` asserts this on a graph small enough to
check by hand, so the ambiguity cannot quietly be resolved the wrong way
later.

## What the paper got right here, and what it did not

This is the part worth recording, because the honest answer is mixed.

The core method transferred. Forward edges, an anomaly restart vector and
MonitorRank's endpoint scaling took top-1 accuracy from 92 of 147 to 147 of
147. Removing the endpoint scaling costs 4 trials, so it earns its place.

**The two eIRWR enhancements did not transfer. Both made the ranking worse,
and one made it far worse than using no graph at all.**

| Configuration | Top-1 of 147 | Mean reciprocal rank |
|---|---|---|
| anomaly scoring only, no graph | 92 | 0.813 |
| **as shipped** | **147** | **1.000** |
| without endpoint scaling | 143 | 0.986 |
| with self-loops (ratio 0.5) | 135 | 0.950 |
| with backward edges (ratio 0.3) | 10 | 0.534 |
| eIRWR as published | 56 | 0.691 |
| restart vector sharpened, exponent 2 | 143 | 0.986 |
| restart vector sharpened, exponent 3 | 141 | 0.980 |

Backward edges are the striking one. They exist so that "the caller is
hammering it" stays reachable as an explanation, which is a real failure
mode. On this system they feed score straight back into the upstream
symptoms, and top-1 collapses from 147 to 10, well below the 92 that no
graph at all achieves.

Sharpening the restart vector fails for a related reason. Raising the
anomaly scores to a power concentrates the restart mass on the loudest
service, and the loudest service is usually the symptom, so the walk is
handed a quadratic head start in the wrong direction and asked to overcome
it.

Both mechanisms are kept in the code, defaulting to off, with the measured
cost recorded next to each constant. Deleting them would make the decision
unfalsifiable, and it is a decision that should be re-examined on recorded
incidents.

## Alternatives considered

**Anomaly ranking with a hand written rule for propagation.** Something like
"if an anomalous service calls another anomalous service, prefer the
callee". This is roughly what the PageRank does, and it was rejected because
it does not compose: with three services in a chain the rule needs a
tie-break, with a diamond it needs a weighting, and at that point it is a
worse version of an algorithm that already exists and has been evaluated by
other people.

**Correlation or causal discovery between service metrics.** Several systems
in [R29] build a causal graph from the telemetry rather than using the
observed call graph. Rejected for this project because the call graph is
already known exactly, from the `service_graph` connector (ADR-0005).
Inferring a structure that is directly observable would add a large source
of error to gain nothing.

**NetworkX, as SPEC.md Section 6.4 suggests.** Rejected after implementation
began, on two grounds. Its `pagerank` dispatches to a SciPy sparse
implementation, so it pulls numpy and scipy in for twenty lines of
arithmetic. More importantly it puts the summation order outside this
project's control, and Firebreak's central claim is that a citation re-runs
to the same value; Phase 3 already lost a day to evidence ids differing
between machines. The power iteration is written out in
`personalised_pagerank`, iterating over sorted nodes so the same terms are
added in the same order everywhere, and a reader can check it against [R28]
without leaving the file.

## Consequences

- **The measurement is the justification, not the citation.** Every
  parameter has an ablation, and two of them contradict the paper they came
  from. `make measure-ranking` regenerates the table above.
- **The numbers are from synthetic fixtures and 147 of 147 is a warning,
  not a triumph.** The synthetic builder makes the target and its call path
  symptomatic, and a forward walk accumulates at the end of that path, so
  the fixture and the algorithm are close to built for each other. The
  no-fault separation says the same thing more plainly: the loudest quiet
  system scored 7.7 and the quietest real incident scored 66.8, a gap no
  production telemetry would show. These results must be re-derived on
  recorded incidents before any of them is quoted.
- **Ranking is deterministic**, with ties broken on service name, so a
  reliability measure like pass^3 measures the agent rather than the noise
  in its inputs.
- **A quiet system needs a separate answer.** PageRank returns an order
  whatever it is given, so the ranking cannot say "nothing is wrong". That
  is the abstention threshold in `config/thresholds.yaml`, decided on the
  magnitude of the largest anomaly rather than on any ranking.
- **The graph source is interchangeable.** `rank_candidates` takes
  `list[EdgeRecord]`, the same type a bundle's `topology.json` yields and
  the same type `fetch_call_edges` returns from Neo4j, so replay and live
  cannot drift into ranking different graphs.

## Sources

- SPEC.md Sections 6.4, 6.5, 9.5, and principles H1 and H11.
- `src/firebreak/graph/ranking.py` for the implementation and the
  measured defaults.
- `reports/triage/ranking_ablation.json` for the ablation.
- `reports/triage/anomaly_only_ranking.json` for the no-graph baseline.
- `config/thresholds.yaml` for the tuned values and their justification.
