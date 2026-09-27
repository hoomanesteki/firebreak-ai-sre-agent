# ADR-0013: Incident memory, and GEPA prompt optimization gated by evals

- Status: Accepted
- Date: 2026-09-27
- Phase: 10

## Context

SPEC.md Sections 10.1 to 10.4 give Firebreak two ways to improve itself: remembering
confirmed incidents, and optimizing its prompts offline with DSPy's GEPA, which reported
better results than GRPO with far fewer rollouts [R13].

Both are ways for the system to feed its own output back into its own input, and that is
the same hazard twice: a measurement that is no longer measuring what it claims. Phase
10's reviewer focus says it in one line, "no validation or test data in optimization or
memory", and everything below is that line taken seriously.

## Decision

**Memory holds the train split only, enforced at three layers.** The model validator
refuses a non-train entry, the store's `admit` refuses one again and raises its own
`LeakageError`, and a test reads the file on disk without validating it.

Three layers for one rule looks excessive until you notice this rule is different from
every other leakage control in the project. The recorded split assignment, the opaque
bundle names and the runner's narrow signature all guard against a person doing the
wrong thing. Memory is written by the system itself, so the way this one fails is a run
confirming an incident and every later run on that incident reading the answer instead of
investigating. The report would cite real evidence and be right for the wrong reason, the
score would measure recall, and nothing downstream could tell. The third layer exists
because an entry written by a tool that did not exist when the validator was added is
exactly what the first two cannot see.

**Retrieval is rarity-weighted lexical overlap plus graph overlap, not embeddings.**
SPEC.md Section 10.3 says embedding similarity. An embedding model is a download, a
dependency and a source of nondeterminism in a harness whose every measurement assumes
reproducibility: `pass^3` measures reliability, and a retrieval that reshuffled would
make it measure the shuffling. The symptom summaries are short, share a controlled
vocabulary of service names and fault classes, and number in the dozens, so token overlap
weighted by rarity does the same job and can be read by whoever doubts a result.

What would change this: summaries long enough that paraphrase matters, or a library large
enough that lexical overlap misses obviously related incidents. Neither is true at eight
recorded bundles, and both are measurable when they become true.

**What the weighting actually buys, which is not what it first looked like.** The score
is normalised by the query, so a query whose every token appears in an entry scores 1.0
whether those tokens are rare or common. Rarity decides which entry a mixed query ranks
first, not the absolute score. The first version of the test asserted the wrong thing and
failed, which is how this was learned rather than assumed.

**Memory is advisory in the type system, not only in the prose.** An entry becomes a
`Hypothesis` with no supporting evidence. That is what makes it advisory in practice: the
exit gate's abstention check counts support, so a memory-derived hypothesis cannot carry a
report until a specialist confirms it against this incident's own data. The advisory
wording lives inside the statement the commander reads, because a prompt saying "these are
advisory" and a statement reading like a finding will lose that argument.

At most two memory hypotheses are added, below the three from triage, so a board cannot
be dominated by the last outage.

**Feedback keeps three verdicts, and the middle one is the point.** Correct, partly,
incorrect. Naming the frontend when payment was broken is wrong, and it is not as wrong as
naming the recommendation service; collapsing that into "incorrect" throws away the
distinction the ranking metrics exist to measure, and collapsing it into "correct" flatters
the system. Only `correct` confirms a root cause, so `partly` cannot put a service that was
merely in the call path into memory as the cause.

**A report marked wrong with no correction is refused.** It is a complaint rather than
feedback and cannot become a test case, which is the entire point of collecting it [R6].

**Prompts are versioned files with hashes over the body.** A prompt is an input to every
measurement this project produces: a report quoting top-1 accuracy is quoting it for one
set of prompts, and if those are literals scattered through the code then no number is
reproducible six months later. The hash covers the body rather than the file, because a
hash that moved for a comment would make every past report look stale after a typo fix. A
new version is a new file, for the same reason bundles are immutable.

**The optimizer reads train and nothing else, and refuses the whole set on one
violation.** Filtering a non-train task out silently would optimize on a smaller set than
the caller asked for and report the larger count. A prompt tuned on validation would be
measured by the eval gate on the split it was tuned for, and the gate would pass it for
the wrong reason.

**The metric's weights are a judgement, written down as one.** Correctness 0.55, evidence
validity 0.30, calibration 0.15. Correctness dominates because a prompt that cites
beautifully and names the wrong service is worse than useless. Calibration is light and
not zero: a prompt optimized on correctness alone learns to always claim high confidence,
and that term is the only thing stopping the optimizer discovering it. The weights are
stated rather than tuned, because a weight fitted on train would be a second thing
optimized on train.

**The report distinguishes "GEPA ran and found nothing" from "GEPA never ran".** The
first is a result about the prompt; the second is a result about the configuration. One
sentence for both would have been the exact kind of statement this project must not make,
and the first version of the verdict made it.

**Nothing ships from here.** `optimize_node` writes a candidate file and a report. SPEC.md
Section 10.4 requires a pull request and a pass on validation first, and a function that
could ship a prompt would make that a convention rather than a rule.

**A6 refuses to run while no prompt carries an `optimized_from` lineage.** An A6 row
identical to the FB row would be read as "optimization did not help", which is a claim
about GEPA. The truth is that no optimized prompt exists, which is a claim about this
repository. Those must not look the same in a table.

## Alternatives considered

**Embedding retrieval, as SPEC.md Section 10.3 specifies.** Rejected for now on
determinism and dependency weight, with the conditions that would reverse it stated
above. Not rejected on quality: at this library size the two would retrieve the same
entries, and when they stop doing so that is measurable.

**One store for memory and for labels.** Rejected. Labels are ground truth the agent must
never reach, memory is a lead the agent is meant to read, and a single store would make
the difference a matter of which field somebody queried.

**Memory entries as findings with evidence ids.** Tempting, because it would let the
reporter cite a past incident. Rejected: evidence is something the exit gate can re-run,
and a past incident is not re-runnable in this incident's window. A citation the gate
cannot verify is exactly what the gate exists to remove.

**Let promotion also snapshot the bundle.** Rejected for now. Recording a bundle is an
eighteen minute live-stack operation, and a function that silently did nothing when the
stack was absent would be worse than one that returns the missing step. Promotion writes
the label, which is the part a person cannot reconstruct later, and says what remains.

**Skip DSPy and hand-tune prompts.** Genuinely viable, and the honest position is that it
may win: the hand written prompts are the baseline every candidate must beat, and no
candidate exists yet. Rejected as a decision because SPEC.md Section 10.4 asks for the
comparison to be measurable rather than assumed either way.

**Drop drain3 to make room for DSPy.** The conflict was real: drain3 pins
`cachetools==4.2.1`, dspy 3 needs 5.5 or newer, and dspy 3 is the first release with
GEPA. Rejected in favour of moving drain3 to its own group and declaring the two
conflicting, because ADR-0006 is a decision somebody may want to re-derive and a
dependency removed is a script quietly broken.

## Consequences

- The leakage scanner caught a route added casually: the optimizer imported
  `firebreak.lab.scenario` to read one enum value, and that module holds
  `target_service`. It now spells the split as a string with a test asserting the two
  agree, which is what `memory/store.py` already did for the same reason.
- `COMMANDER_TOOLS` had been declared in Phase 3 and never consumed, so `similar_incidents`
  was registered and unreachable and A5 would have been a switch that changed nothing.
  That is the failure the A1 to A4 tests were written against, found in the next ablation
  built.
- Adding DSPy introduced an unfixable advisory, `PYSEC-2026-2447` in `diskcache 5.6.3`.
  CI now audits in two passes and the exception is by id with its reasoning; the threat
  model records it as an accepted risk the owner may reverse. It is also a concrete
  instance of the gap that model already named.
- The tool registry's count guard fired twice this phase and once last phase, each time
  catching a new tool before it became a capability every specialist had.
- GEPA cannot improve anything yet for two reasons, both recorded in the report it
  writes: no credentials to reflect with, and prompt bodies are not threaded into the
  model calls, because `LlmClient` serves stub and replay and raises for api.

## Sources

- SPEC.md Sections 10.1 to 10.4, 9.5, 17.
- `src/firebreak/memory/`, `src/firebreak/prompts.py`, `src/firebreak/optimize/gepa.py`,
  `prompts/`.
- `tests/leakage/test_memory_is_train_only.py` for the three layers,
  `tests/unit/test_prompts_and_optimize.py` for the train-only rule and the metric.
- ADR-0006 for the log template decision drain3 was kept for, ADR-0012 for the split
  design this extends.
- [R6] on sourcing eval tasks from real failures, [R13] on GEPA.
