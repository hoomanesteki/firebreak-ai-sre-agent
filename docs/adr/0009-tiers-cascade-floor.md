# ADR-0009: Model tiers, a rule-based cascade, and a deterministic floor

- Status: Accepted
- Date: 2026-09-26
- Phase: 8

## Context

SPEC.md Section 6.7 defines three tiers, a cascade that escalates between them,
and a floor for when none of them works. FrugalGPT showed learned cascades across
LLMs matching the best single model at much lower cost [R11], and RouteLLM showed
learned routing cutting cost by more than 2x without a large quality loss [R12].

Both results are about learned routers. Firebreak has no training data for one:
the incident library is being recorded, and a router trained on eight incidents
would be a router fitted to eight incidents.

## Decision

**Rules first, learning later, and the eval reports what the rules cost.** The
cascade is three conditions in `config/models.yaml`, and the eval reports cost and
accuracy for `all-strong`, `all-small` and `cascade`. That table is what would
justify a learned router, and it is also what would show the rules are enough. A
learned router built before the table exists would be optimising against a number
nobody had measured.

**Escalation and fallback are separate mechanisms, and confusing them is expensive
in both directions.** Escalation moves a call to a stronger tier because the
*answer* was unsatisfactory: the shape was wrong twice, the model reported low
confidence, or two specialists contradict each other on one hypothesis. Fallback
retries the same model and then tries the next one in the tier because the *call*
did not complete: a timeout, a 429, a 5xx. Escalating a timeout spends a
strong-tier call on a network problem. Retrying a schema error spends the budget
discovering that a small model still cannot produce the shape.

That rule is enforced twice: `FallbackRules` refuses a configuration listing any
4xx but 429 as retryable, and `is_retryable` reads only what the configuration
allows. Enforcing it once, in the code, would leave a file somebody could edit to
undo it.

**Full jitter, not equal jitter.** [R27] measured both. Full jitter, a uniform
draw from zero to the exponential ceiling, spreads a herd of simultaneous retries
best. That is the case that matters here rather than the unlucky one: four
specialists fan out in the same round and hit the same provider at the same
moment, so correlated retries are normal.

**The tier override lives on the LLM client.** Ablations A3 and A4 force every
call to one tier, and the client is the object that routes a call to a model, so
one substitution covers the graph, the baselines and anything added later. Putting
it on the nodes would mean every node added later could forget it, and an ablation
that silently did not ablate would produce a cost comparison between two identical
systems. What the graph asked for is recorded alongside what it got, because an
ablation reporting only the override would make the cascade's own decisions
invisible.

**No models are configured, and that is the shipped state.** SPEC.md's own tier
table says "owner chooses; verify names", and this repository forbids inventing
model ids or prices. An invented id fails at the first call with a confusing error;
an invented price silently multiplies every cost figure in every report. So the
tiers are empty, an empty tier is valid, `stub` and `replay` need none, and asking
an empty tier for a model raises an error naming the file to edit.

**A price is rejected without a source and a date it was checked.** This is
SPEC.md Section 17 Phase 8's reviewer focus turned into a rule. A cost table built
from unverifiable prices is worse than one with no prices, because it looks like a
measurement. An unpriced model is allowed, because a local model through Ollama has
no per-token price and calling it zero would hide the hardware it needs, and the
configuration reports such models by name so a cost figure can say what it omits.

**The deterministic floor is a feature, not an error path.** When every tier fails,
Firebreak publishes B0's findings labelled "Automated triage only, no AI analysis",
with claims citing B0's own re-runnable evidence. At three in the morning the
useful behaviour is a ranked list with evidence; the alternative is a stack trace,
and an operator who gets a stack trace once stops opening the tool.

The label is not a caveat at the bottom. It lives in `Report.notes`, which the
exit gate does not check and the reporter model cannot write, because
`ReporterOutput` has no such field. A floor report that looked like a full one is
the most misleading thing this system could publish: the claims are weaker, the
mechanism is absent, and the confidence means something different.

## Alternatives considered

**A learned router now, per [R11] and [R12].** Rejected on data, not on merit.
Eight recorded incidents is not a training set, and the rule-based cascade is the
thing a learned router would have to beat. Revisited when the library is recorded
and the cost table has real numbers in it.

**Escalate on any failure, including transport failures.** Simpler: one path for
everything that goes wrong. Rejected because it prices a network problem at the
strong tier, and the cascade's whole purpose is to spend strong-tier calls on the
decisions that need them.

**Retry a schema error at the same tier a few times.** Tempting, because models
sometimes produce the shape on the second attempt, and one repair pass does exactly
that. Rejected beyond the first attempt: SPEC.md Section 6.7 is explicit, and the
failure mode is a budget disappearing into a model that cannot do it.

**Let the floor raise instead.** Rejected. An on-call engineer with a stack trace
has less than they had before they opened the tool.

**Put prices in code.** Rejected: an operator changing a provider should not edit
Python, and a price in code is a price nobody re-checks when it moves.

## Consequences

- Wiring the floor through the exit gate found a real bug. `seed_hypotheses` fills
  the notebook from triage with hypotheses no specialist has supported, so check 6
  saw support of zero and stripped the floor's named service, leaving the floor
  delivering nothing. The floor now passes the notebook its own report implies.
- Writing the eval-gate workflow found that its own credentials would not have
  worked: `llm.py` told users to set `LLM_BASE_URL` and `LLM_API_KEY` while
  `Settings` read only the FIREBREAK_ prefixed forms. Following the instruction did
  nothing, and nothing reported it, because stub is a valid mode.
- The cost table cannot be filled in without credentials, and it says so rather
  than printing three zeros. `make cost-table` produces the accuracy half today.
- In stub mode A3 and A4 produce identical outcomes to FB, because the stub ignores
  which tier it was asked for. The tier collapse is asserted in tests rather than
  inferred from the outcomes, so the ablations are known to work before there is a
  model to show it with.

## Sources

- SPEC.md Sections 6.7, 9.3, 9.5, 17.
- `src/firebreak/agent/models.py`, `cascade.py`, `floor.py`, `config/models.yaml`.
- `tests/unit/test_cascade.py` for escalation against fallback,
  `tests/unit/test_floor.py` for the floor's acceptance criterion.
- ADR-0008 for what is code and what is a model.
- [R11] FrugalGPT, [R12] RouteLLM, [R27] exponential backoff and jitter.
