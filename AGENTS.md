# Working rules for this repository

This repository builds Firebreak, a multi-agent AI SRE with an eval
harness. SPEC.md is the source of truth. Read the section for the
current phase before starting.

## How to work
- Only the current phase in SPEC.md Section 17. Stop at the review
  gate.
- Small conventional commits (SPEC.md Section 18.3).
- Confirm every library API in the installed package or official docs,
  especially LangGraph, DSPy, OpenTelemetry, and the pinned
  OpenTelemetry Demo files.
- Never invent numbers, prices, model IDs, flag names, or citations.
  Missing numbers are written "TBD (produced in Phase N)".
- Never let the agent under test read labels or fault configuration.
- Tests first for gates, graders, budgets, cascade, and approvals.
- When blocked, write the question in the phase report and stop.

## Commands
- make setup | make verify | make demo | make demo-offline
- make live | make lab-record SPEC=... | make eval CONFIG=... SPLIT=...
- make optimize NODE=...

## Git
- Commit as the configured owner. No --author, no date changes, no
  --no-verify.
- No AI attribution anywhere: no co-author trailers, no "Generated
  with" lines, no mention of AI assistants in commits, PRs, code,
  comments, or docs.
- Branch per phase: <type>/pNN-<name>. PR template. Rebase and merge
  after the review gate approves.

## Writing
- No em dashes or en dashes. Plain, specific English. Comments explain
  why.
- No emoji. No filler words (see config/repo_hygiene.yaml).

## End of phase
1. make verify passes; from Phase 11, make demo-offline passes.
2. Write docs/phase-reports/PNN.md (SPEC.md Section 21.1).
3. Open the PR, wait for green CI, then stop and tell the owner:
   "Phase NN is ready for review: PR #<n>".
