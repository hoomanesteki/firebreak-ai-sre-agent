# ADR-0001: LangGraph as the agent orchestration framework

- Status: Accepted
- Date: 2026-09-19
- Phase: 0

## Context

Firebreak runs a multi-step investigation: an entry gate, deterministic
triage, a commander, four specialists in parallel, a hypothesis board, a
critic loop, a reporter, and an exit gate (SPEC.md Section 6.6). Three
properties decide the framework choice:

1. Durable state. An investigation must survive a process restart and be
   replayable step by step, because the eval harness reads transcripts and
   the Console streams progress (SPEC.md principles H7 and H8).
2. A pause for human approval. Remediation proposals stop the graph and
   wait for a person, possibly for hours (SPEC.md Section 6.10).
3. Parallel fan-out to sub-agents that return condensed findings rather
   than raw tool output (SPEC.md principle H6).

## Decision

Use LangGraph, with its Postgres checkpointer for durable state and its
interrupt mechanism for the approval step.

## Alternatives considered

**Microsoft AutoGen.** AutoGen entered maintenance mode in October 2025 and
now receives only bug fixes, security patches, and documentation from
community contributors. Building a project meant to run for a year on a
framework that takes no new features is a poor trade.

**Microsoft Agent Framework.** The named successor to AutoGen and Semantic
Kernel, at version 1.0 general availability since 2 April 2026. It is a
credible option. It was rejected for v1.0 on age: the checkpointing and
human-in-the-loop patterns Firebreak depends on have less field use and
fewer worked examples than LangGraph's, and the migration notes from
AutoGen describe a rewrite rather than a port, which signals the API surface
is still settling.

**CrewAI.** Role-based crews fit a fixed division of labour. Firebreak needs
an explicit state machine with conditional edges, a repair loop, budget
checks between steps, and an interrupt, which is graph work rather than crew
work.

**No framework, plain Python.** Rejected, but not obviously wrong. The parts
that would need writing are a checkpointer, resumption, and streaming. That
is a month of work on infrastructure rather than on the harness and the
eval, which are what this project is about. Recorded here so the reviewer
can challenge it: if LangGraph turns out to cost more than it saves during
Phase 6, this ADR is superseded rather than defended.

## Consequences

- Postgres becomes a hard dependency of the core Compose profile, since it
  stores checkpoints, reports, feedback, and the audit chain.
- The exact LangGraph API for parallel branches, the Postgres checkpointer,
  and interrupts is confirmed against the installed package at the start of
  Phase 6, not assumed from these notes.
- Agent logic stays in plain functions with Pydantic state, so the framework
  holds the control flow and nothing else. That keeps the cost of replacing
  it bounded.
- LangGraph is added as a dependency in Phase 6, not in Phase 0, so the
  Phase 0 lock file stays small.

## Sources

- LangChain, "LangChain vs. AutoGen in 2026: what the maintenance
  announcement changed". https://www.langchain.com/resources/langchain-vs-autogen
- microsoft/autogen repository README, maintenance mode notice.
  https://github.com/microsoft/autogen
- LangChain docs, "Interrupts" in LangGraph.
  https://docs.langchain.com/oss/python/langgraph/interrupts
- SPEC.md Sections 6.6, 6.10, and 7, references R20 and R21.
