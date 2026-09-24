# ADR-0006: A deterministic masker for log templates, not Drain

- Status: Accepted
- Date: 2026-09-24
- Phase: 4

## Context

`top_error_signatures` groups log lines into templates so an investigation
counts failure modes instead of failure lines. Twenty thousand lines reading
`upstream call to payment failed after 3091ms request_id=6063c393` are one
fact, and an agent that reads them individually spends its whole context
budget learning it.

SPEC.md Section 6.5 specifies a simple deterministic masker, names Drain as
the stronger option, and requires ADR-0006 to decide between them on a
comparison against validation data rather than on preference.

## Decision

Keep the deterministic masker in `src/firebreak/triage/log_templates.py`.
Drain is not adopted.

## The comparison

`scripts/compare_log_templates.py`, regenerated with
`make compare-log-templates`, writes
`reports/triage/log_template_comparison.json`. Corpus: 1,920 lines from the
same templates the fixtures use, each labelled with the event that produced
it. Drain is the `drain3` package at 0.9.11, default configuration.

**What counts as one event** decides the result, so the rule is stated
rather than assumed: an event is its template plus every entity valued
field, which here means the service, the peer it called, and the route.
Numbers, durations, addresses and request ids are noise. Two lines differing
only in a request id are the same event; two lines differing in which
upstream failed are not, because "checkout cannot reach payment" and
"checkout cannot reach email" send an operator to different places.

| | Clusters found (320 true) | Purity | Merged | Fragmented | Stable under reordering |
|---|---|---|---|---|---|
| **Masker** | 280 | **0.875** | 40 | 0 | **Yes** |
| Drain | 88 | 0.275 | 40 | 0 | No |

Restricted to error and fatal severities, which is the only condition
`top_error_signatures` ever runs in, because it filters before it clusters:

| | Clusters found (112 true) | Purity | Merged |
|---|---|---|---|
| **Masker** | **112** | **1.000** | **0** |
| Drain | 32 | 0.286 | 16 |

Three findings, in order of how much they matter.

**Drain is not stable under reordering, and that is disqualifying here.**
Feeding the identical corpus in a shuffled order moved 180 of its 1,920
lines into different clusters on one of three shuffles, and left the other
two unchanged. The fact that it is intermittent is the point: two of three
shuffles agreeing is exactly what makes this the kind of defect that ships.

This is not a fault in Drain; it is what an incrementally learned parse tree
does, and for most purposes it is an acceptable price. It is not acceptable
here. An evidence id is a hash over a query and its result, and the exit
gate re-runs a citation and requires it to match (SPEC.md Section 6.9). A
clusterer whose output depends on the order rows came back in cannot make
that promise.

**Drain merges events this system needs kept apart.** It found 88 clusters
where there were 320 events, and 32 where there were 112. The cause is
visible in the templates it learns: it generalises the service name and the
peer name into wildcards, so `checkout upstream call to payment failed` and
`cart upstream call to email failed` become one signature. That is
reasonable behaviour for a corpus from one service and wrong for a corpus
spanning sixteen, which is what an incident investigation reads.

**The masker's one weakness is HTTP status codes.** All 40 of its merges are
the same shape: `GET /cart 200 in 43ms` and `GET /cart 503 in 43ms` mask to
the same template, because a status code is a bare number and the number
rule eats it. In the condition that matters this never bites, since a 200
line and a 503 line arrive at different severities and
`top_error_signatures` has already filtered. It is recorded here because the
next tool to cluster logs without filtering first will meet it.

## Alternatives considered

**Drain, tuned rather than default.** Raising the similarity threshold would
reduce the over-merging. Rejected because it does not touch the
disqualifying property: a tuned Drain is still order dependent, and no
threshold makes a learned tree reproducible. Tuning it would also mean
tuning it per corpus, and the corpus here is every service at once.

**Drain with a fixed pre-trained tree, loaded rather than learned.** This
would be deterministic, and it was the most serious alternative. Rejected on
the grounds that the tree becomes a build artefact nobody can review, and
that any log line whose shape the training corpus did not contain gets
assigned by similarity to whatever is nearest, silently. A regex list is
worse at generalising and much better at being read.

**Masking status codes by position rather than as bare numbers.** A rule
like "a three digit number following an HTTP method and a path is a status
code" would fix the one weakness found. Not adopted now, because the
weakness has no effect on the only caller, and a positional rule is exactly
the kind of pattern that starts matching durations of 200ms the first time a
log format changes. Revisit if a second caller clusters unfiltered lines.

## Consequences

- The same line always yields the same template, in any process, in any
  order, which is what lets a log citation be re-run and matched.
- Adding a mask rule is a reviewable one line change with an ordering
  comment, rather than retraining anything.
- The masker will not generalise to a log shape nobody anticipated. It will
  leave such a line almost unmasked and produce a near unique template,
  which shows up as a suspiciously large distinct template count rather than
  as a quiet mis-grouping. Failing visibly is the intended trade.
- **This comparison is on synthetic log lines and understates Drain.** Real
  service logs are more varied, and variety is where a learned parser earns
  its keep. The stability result does not depend on the corpus and is the
  decisive one, but the purity numbers should be re-derived on recorded
  incidents before being quoted anywhere.
- Finding this required fixing the fixtures first. The synthetic log bodies
  were fixed strings with no variable parts, so `mask_log_line` changed
  nothing on any of 293 lines in a bundle, and every test of the masker and
  of `top_error_signatures` had been passing against data that could not
  tell a working implementation from a broken one. That is recorded in
  `docs/phase-reports/P04.md`.

## Sources

- SPEC.md Sections 6.5 and 6.9.
- `src/firebreak/triage/log_templates.py` for the masker and its ordering.
- `scripts/compare_log_templates.py` for the comparison.
- `reports/triage/log_template_comparison.json` for the numbers above.
- Drain is `drain3` 0.9.11, a dev dependency used only by the comparison and
  not by anything that ships.
