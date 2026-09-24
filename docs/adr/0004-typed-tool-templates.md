# ADR-0004: Typed tool templates instead of free-form queries

- Status: Accepted
- Date: 2026-09-23
- Phase: 3

## Context

The agent needs to ask questions of Prometheus, Jaeger, OpenSearch and a
dependency graph. The obvious design is to hand it a query language: give it
a `run_promql` tool and let it write whatever it needs. That is what most
agent frameworks do, and it is genuinely more flexible.

Four things make it the wrong choice here.

**A verifier has to re-run the query.** Firebreak's central claim is that
every sentence in a report cites evidence that can be re-executed and
matched (SPEC.md Section 6.9). An arbitrary query string can be re-run, but
nothing about it can be checked in advance, and a query that was expensive
or wrong the first time is expensive or wrong again at verification.

**An arbitrary query cannot be capped meaningfully.** A window limit is easy
to enforce on a typed argument and impossible to enforce on a string that
may contain its own time selectors.

**It is code execution.** SPEC.md Section 11 maps this to ASI05. Log bodies
and span attributes are attacker influenced, and a tool that accepts a query
composed from them is a path from untrusted input to backend execution.

**Two backends have to agree.** The same question must return the same shape
from a live Prometheus and from a Parquet file read by DuckDB. That is
achievable for a fixed set of named questions and not achievable for
arbitrary PromQL.

## Decision

Every tool is a typed function: a frozen Pydantic input model with
`extra="forbid"`, a bounded result, and a description written for a model to
read. Metric names come from a declared vocabulary in `firebreak.signals`,
and a name outside it is rejected by validation rather than passed through.

Backends build their own SQL from typed arguments with bound parameters. No
caller supplies a query string, in either backend.

Caps live in `firebreak.backends.base` rather than in each tool: a maximum
window, a maximum row count, a maximum log pattern length. A request over a
cap is refused rather than truncated.

Each tool call produces an evidence record whose id is a hash of the query,
the window and the backend, so the same question always has the same id.

## Alternatives considered

**A `run_promql` tool with a validating parser.** Rejected on effort and on
honesty. Writing a PromQL parser strict enough to bound cost is a project of
its own, and a parser that is merely strict enough to look safe is worse
than no parser, because it produces confidence.

**An allowlist of query templates with string substitution.** Closer, and
rejected because substitution into a query string is where injection bugs
live. A typed argument that never becomes part of a query string cannot be
escaped wrongly.

**Letting the agent read whole files and reason over them.** This is what a
person does with a log viewer. It does not survive contact with the context
budget: a twenty minute window of one service is tens of thousands of lines,
and SPEC.md principle H4 exists because spending the whole budget on one
call is the common failure.

**Fewer, more general tools.** Considered seriously. Three metric tools
could be one with a mode argument. Kept separate because SPEC.md principle
H5 asks for tools that are unambiguous and non-overlapping, and a mode
argument moves the ambiguity from the tool list into the arguments, where a
description cannot help.

## Consequences

- Adding a question means adding a tool or a vocabulary entry, deliberately.
  A new metric is a one line change to `firebreak.signals` plus a query in
  the exporter, and both are reviewable.
- The agent cannot ask something nobody anticipated. That is a real loss.
  The mitigation is that the fourteen tools were chosen from what an
  investigation actually needs, and a gap shows up as a failed eval case
  rather than as an agent improvising.
- Tools return a summary and an evidence id, never raw rows. `get_evidence`
  expands a record by id when detail is genuinely needed.
- Descriptions carry a "when not to use this" clause, because fourteen tools
  with fuzzy boundaries is how an agent calls four of them to answer one
  question. These are graded in the phase report against SPEC.md Section 6.3.
- Each specialist gets an allowlist rather than the whole registry, enforced
  by `ToolRegistry.subset` rather than by asking a prompt nicely.

## Sources

- SPEC.md Sections 6.3, 6.6, 6.9, 11, and principles H4, H5, H12.
- `src/firebreak/tools/base.py` for the registry, caps and result shape.
- `src/firebreak/backends/bundle_duckdb.py` for parameter binding.
- `reports/triage/anomaly_only_ranking.json` for what the metric tools can
  and cannot do on their own.
