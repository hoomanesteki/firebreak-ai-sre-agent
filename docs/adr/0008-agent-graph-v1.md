# ADR-0008: Agent graph v1, and why the loop is not yet a LangGraph

- Status: Accepted, partial
- Date: 2026-09-25
- Phase: 6

Partial by design: SPEC.md Section 17 marks this record as partial in Phase 6
and completes it in Phase 9, when the approval interrupt exists and the
question this ADR defers can actually be answered.

## Context

SPEC.md Section 6.6 specifies a LangGraph graph with thirteen nodes, durable
state through a checkpointer, and an interrupt for human approval. ADR-0001
chose LangGraph over AutoGen and Semantic Kernel for exactly those properties.

v1 needs a subset: entry gate, triage, commander, four specialists, hypothesis
board, critic, reporter and exit gate. Remediation, approval and recovery
verification arrive in Phase 9.

## Decision

**The node functions have LangGraph's signature. The loop that calls them does
not use `StateGraph` yet.**

Each node in `agent/nodes.py` takes a state and returns one, which is what
`add_node` expects. `agent/graph.py` calls them in sequence with one conditional
edge, in plain Python.

**The framework is confirmed and pinned.** LangGraph 1.2.12, checked against
the installed package rather than assumed:

- `StateGraph(state_schema, ...)` with `add_node`, `add_edge`,
  `add_conditional_edges` and `compile(checkpointer=...)`.
- `InMemorySaver` at `langgraph.checkpoint.memory`.
- **The Postgres checkpointer is a separate package**,
  `langgraph-checkpoint-postgres`, not bundled with `langgraph`. SPEC.md
  Section 6.6 asks for a Postgres checkpointer and this is what that will cost.

It is a major version past what ADR-0001 compared. ADR-0001 pinned no version,
so nothing it concluded is invalidated, but the API it described is not the one
that shipped.

## Why the loop is plain Python in v1

LangGraph's value is durable checkpoints and interrupts. v1 exercises neither.

The v1 control flow is a fixed sequence with one conditional edge: run a round,
check whether to run another. Expressed in `StateGraph` that is the same logic
plus a graph construction, a compile step, and a state schema that has to be
reconciled with the Pydantic state already in use. It would be the cost of the
abstraction with none of the benefit, and it would make the loop harder to read
top to bottom, which matters most while the loop is still changing.

The moment that changes is the approval interrupt. `await_approval` in SPEC.md
Section 6.10 has to survive a process restart, and that is precisely what a
checkpointer is for. Phase 9 wires these nodes into a `StateGraph` rather than
rewriting them, because the signatures already fit.

**The risk of deferring is real and bounded.** The risk is that the nodes turn
out not to compose the way `StateGraph` wants, and the wiring is more than
plumbing. It is bounded because the nodes are pure functions of state with no
shared mutable context except the budget, and the budget is deliberately the
one mutable thing: every node spends from it, and a node that forgot to return
it would silently reset the spend.

## What is code and what is a model

| Node | Kind | Why |
|---|---|---|
| `entry_gate` | Code | Cheapest possible rejection. A malformed incident reaching the commander spends a strong-tier call discovering it has no window. |
| `triage` | Code | Already deterministic, already measured, already the B0 baseline. |
| `seed_hypotheses` | Code | The candidates are ranked. Asking a model to restate a ranked list loses information and costs a call. |
| `commander` | Strong | The one v1 decision worth a strong model: where to spend the next four tool calls. |
| specialists | Small | A narrow question against one signal, with an allowlisted tool subset. |
| `hypothesis_board` | Code | Where the investigation decides what it believes. Counting cannot flatter anybody; a model weighing evidence it produced agrees with itself. |
| `critic` | Strong | A different prompt, and where configured a different family, because self-review agrees with itself [R8]. |
| `reporter` | Strong | Prose that has to cite, from the notebook only. |
| `exit_gate` | Code | It enforces a rule. A model enforces a rule right up until it does not. |

The three gates being code is the load-bearing part. They decide whether an
investigation starts, what it believes, and what it is allowed to say.

## A specialist gets a brief, not the state

`Brief` is a separate type, not a subset of `InvestigationState`. Handing a
specialist the whole investigation is therefore a type error rather than
something a reviewer has to notice, which is the structural form of the advice
in [R9] rather than a sentence in a prompt.

Each specialist also gets only its own tools, enforced by `ToolRegistry.subset`
and asserted in `_gather` rather than requested politely.

## The stub is an implementation, not a mock

`stub` mode answers from the payload it is given: the commander reads the
hypothesis list, a specialist reads the tool summaries it was handed, the critic
compares support across hypotheses, the reporter writes one claim per finding
citing that finding's evidence ids.

Three things follow.

**An integration test in stub mode is a real test.** It exercises the wiring,
the budgets, the repetition limit, the evidence store and the exit gate against
real bundles. That is what SPEC.md Section 17's "every node path covered in
stub mode" is worth having.

**The deterministic floor from SPEC.md Section 6.7 is reachable.** When every
model tier fails there is something to fall back to that is not a stack trace.

**Stub completions report zero tokens and zero cost.** Reporting invented
figures would put fiction into the cost column of every eval run made in stub
mode, and those runs are the ones CI makes.

## Alternatives considered

**Wire `StateGraph` now and leave the interrupt for later.** Rejected on the
reasoning above: the abstraction's cost without its benefit. Revisited in
Phase 9, not deferred indefinitely.

**One agent with every tool, no specialists.** That is baseline B1 in SPEC.md
Section 9.5 and it exists to answer whether the harness is worth its
complexity. It is a baseline rather than an alternative, and it is not built
yet.

**Let the commander decide which tools each specialist calls.** Rejected for
v1. The tool plans in `_gather` are fixed per specialist, which is less
flexible and far easier to verify: a test validates every planned call against
the tools' own input models. That test exists because the first version passed
`service` where `find_traces` takes `services`, the tool refused every call, and
the traces analyst gathered nothing for an entire run without complaining,
because a tool that cannot answer is deliberately treated as a fact about the
incident rather than a crash. A wrong argument name is not a fact about the
incident.

**A numeric confidence from each specialist.** Rejected in favour of three
levels. A small model asked for a probability produces a number with no
calibration behind it; three levels a model can distinguish are mapped to
numbers once, in one place, where the mapping can be calibrated against
outcomes.

## Consequences

- **FB-v1 is an eval configuration**, so it is measured through the same
  graders and statistics as B0 rather than by hand.
- **In stub mode FB-v1 scores exactly what B0 scores**, 18.2% on the eleven
  recorded validation incidents. That is expected rather than disappointing:
  the only thing separating them is a keyword rule standing in for reasoning,
  and both rest on the same deterministic ranking. A real comparison needs a
  real model, and this number is the reference it will be made against.
- **No model has run yet.** SPEC.md Section 17 Phase 6 asks for FB-v1 and B1 on
  validation in `local` or `api` mode, and that needs credentials this
  environment does not have. The graph is complete and exercised; the
  acceptance criterion that requires a model is not met and is recorded as not
  met.
- Running the graph through the eval gate immediately found two defects in the
  gate, which is the argument for making a new configuration go through the
  harness rather than a bespoke script.

## Sources

- SPEC.md Sections 6.6, 6.7, 6.8, 6.9, 9.5, 17, and principles H1 and H12.
- ADR-0001 for the framework comparison, and for the version it did not pin.
- `src/firebreak/agent/` for the nodes, the loop and the stub.
- `tests/unit/test_agent_graph.py` for the node path coverage.
- [R8] on self-review agreeing with itself; [R9] on not handing a subagent the
  full transcript.
