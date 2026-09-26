# ADR-0010: Exit gate with re-execution, and abstention as a separate rule

- Status: Accepted
- Date: 2026-09-26
- Phase: 7

## Context

SPEC.md Section 6.9 specifies six checks between a written report and a
published one: coverage, re-execution, numbers, consistency, confidence sanity,
and abstention. The project's whole claim is that its reports cite re-runnable
evidence, so this is the component that makes the claim true or makes it
marketing.

The threat is not a model that lies. It is a model that writes a fluent,
plausible paragraph in which one number is wrong, or one citation points at
nothing, and an operator acts on it. Section 11 lists the failure as ASI04.

## Decision

**All six checks are code, with no model and no I/O.** `agent/gates.py` imports
no client, holds no backend and reads no clock. It is a pure function from a
report, a notebook and an evidence store to a list of named results. That is why
it runs in microseconds, why it is testable without a stack, and why nothing in
it can be talked out of a verdict.

**Every route to a published report runs through one function.** `_finish` in
`agent/graph.py` is the single funnel, and it calls the gate. The first version
called the gate once at the end of the main loop, which left two short-circuit
paths, a malformed incident and an abstaining triage, returning reports nothing
had checked. "Cannot be bypassed" has to be a property of the control flow, not a
rule a future node is trusted to remember.

**Re-execution happens outside the gate, and is handed in.** `agent/reexecute.py`
re-runs each cited record through the tool that produced it, using a fresh tool
context over the same backend, and returns the fresh records keyed by the
original id. Outside because the gate calls nothing; a fresh context because
sharing the investigation's would spend its per-tool caps on verification, so a
thorough investigation would fail its own gate for having looked too hard.

**An absent re-execution is a failure, not a pass.** A gate that read "nothing
was re-run" as "everything matched" would become a no-op the first time a caller
forgot, and this is the check the other five depend on: coverage, numbers and
confidence sanity are all satisfied by a report citing evidence that no longer
says what it said.

**Evidence records carry how to re-run themselves, stamped by the registry.**
Neither `query` nor `parameters` could do it. `query` is a description a reader
understands, such as `compare_windows:latency`, and `parameters` are
canonicalised, so no tool would accept either. `ToolRegistry.call` now stamps the
tool name and the validated arguments onto the record the handler recorded. In
the registry rather than in each of the fourteen tools, because the thirteenth
tool written would be the one that forgot, and because only `call` has the parsed
arguments.

**Live evidence may drift within tolerance; bundle evidence may not drift at
all.** Section 6.9 allows a live record whose numbers still match to pass. A live
query re-run a minute later has a minute of new samples, so a hash comparison
alone would fail every report `make live` produces. A bundle is frozen, so a
bundle whose hash moved means the bundle changed underneath the run or the record
was never real. A record nobody quoted a number from cannot pass by the tolerance
route, or drift would be free for the evidence most reports rest on, which is
cited as prose support and quotes nothing.

**A failed check triggers one repair pass, then removal, then a re-check.**
Repair means removing the offending claims, not asking the model to try harder: a
claim that failed verification is not made true by rewording, and the gate cannot
call a model anyway. The re-check matters because removal can break a check that
passed on the larger report; a high-confidence report that loses its second
signal type is still a high-confidence report resting on one signal.

**Abstention is applied, not repaired.** Checks 1 to 5 say a claim is
unverifiable. Check 6 says the report may not name a root cause at all, so
removing claims would not address it. The report's root cause and confidence are
cleared and its claims are kept, because the ranked candidates and the record of
what was checked are exactly what makes an abstention useful rather than a shrug.

**The abstention threshold is configuration, read from
`config/thresholds.yaml`.** Two abstention rules exist, triage's on the top
anomaly score and the gate's on hypothesis support, and both live in that file.
A system that abstains at triage on one rule and at the gate on another has two
answers to the same question.

**Blast radius is checked for provenance, not recomputed.** Section 6.9 check 4
says the blast radius matches the graph query. The gate holds no graph client, so
it cannot recompute the set. What it checks is that a blast radius claim cites a
topology record, after which check 3 verifies its numbers against that record's
facts. A blast radius asserted from prose alone fails, which is the failure the
clause is for.

**Procedural text is not a claim.** Two reports are written by code rather than
by a model: one for a malformed incident and one for an abstaining triage.
Neither describes telemetry and neither has anything to cite, so their text lives
in `Report.notes`, which the gate does not check. This is not an exemption a
model can reach for: `ReporterOutput` has no such field and `reporter` builds the
`Report` field by field, so only code can write a note. A statement about what
the telemetry showed belongs in `claims`, where the gate can reach it.

## Alternatives considered

**A `claim_type` the gate skips, for procedural statements.** Rejected. Claim
types come from the model's structured output, so a model could emit that type
and walk past check 1. The distinction has to be structural, which is what a
field the model cannot write gives.

**Verify only the numbers, not the hashes.** Cheaper, and it was tempting because
tolerance comparison is what live mode needs anyway. Rejected because most claims
quote no number, so most evidence would go unverified and the gate would mostly
be checking that ids exist.

**Ask a model to check the report.** Rejected on the same reasoning as the other
two gates in ADR-0008: a model enforces a rule right up until it does not, and a
checker sharing a family with the writer agrees with it [R8].

**Let the gate re-run the evidence itself.** Simpler to call. Rejected because it
would give the gate a backend, and the gate's testability and immunity to being
misled both come from it having nothing.

## Consequences

- The gate found its own bypass. `repair=False` was meant to inspect without
  changing anything; it stripped the failing claims anyway and then published the
  stripped report as passing. A caller asking what was wrong got a silently
  shortened report claiming it was fine.
- Check 2 works end to end against a real bundle, proven by B2: the cited
  evidence re-runs to identical hashes through the stamped tool and arguments.
- In stub mode B2 scores identically to B1 on every grader, because the stub never
  fabricates. The gate's value cannot be measured without a model, and the eval
  report says so rather than presenting "the gate changed nothing" as a finding
  about the gate.
- The four rejections SPEC.md Section 17 Phase 7 names are tested by hand: a
  fabricated number, a fabricated evidence id, a timeline out of order, and a
  high-confidence single-signal report.
- B2 needed a notebook the single agent does not have. Its report is translated
  into one hypothesis supported by the distinct evidence its claims cite. That is
  a judgement, not a derivation, and B2's abstention rate is therefore a claim
  about how much evidence B1 cited rather than about a hypothesis board B1 never
  had.

## Sources

- SPEC.md Sections 6.9, 6.10, 9.5, 11, 17.
- `src/firebreak/agent/gates.py` for the six checks, `agent/reexecute.py` for the
  re-run, `tools/base.py` for the stamping.
- `tests/unit/test_exit_gate.py` for the four named rejections and the bypass
  tests.
- ADR-0008 for what is code and what is a model, and why the gates are code.
- [R8] on self-review agreeing with itself.
