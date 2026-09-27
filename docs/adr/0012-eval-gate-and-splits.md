# ADR-0012: A non-inferiority eval gate, and the split design behind it

- Status: Accepted
- Date: 2026-09-26
- Phase: 8

## Context

SPEC.md Section 9.4 specifies a gate on the lower bound of a paired bootstrap
difference, with a margin per metric, and Section 8.2 specifies four splits: train,
validation, test_id and test_ood. The two decisions are one decision. A gate is a
statement about data, and which data it read is most of what the statement means.

The failure this guards against is specific. A prompt edit meant to cut cost drops
top-1 accuracy by half a point on eight tasks. Is that a regression? Without a
gate, whoever wrote it decides, and they decide it is noise. With a badly built
gate, it either blocks every change or passes every change, and in both cases
people stop reading it.

## Decision

**Non-inferiority, not superiority.** The gate asks whether a candidate has been
shown to be *worse*, not whether it has been shown to be better. Requiring an
improvement would block every change made for a reason other than accuracy, which
is most changes.

**The test is on the lower bound of a paired difference, not the point estimate.**
A candidate two points behind with an interval from minus ten to plus six has not
been shown to be worse. One two points behind with an interval from minus three to
minus one has. The point estimate cannot distinguish those, and they are entirely
different situations.

**Paired, and paired by task.** The configurations ran on the same incidents and
some incidents are harder than others, so comparing two independent intervals
throws the pairing away and reports a difference far less certain than the data is.
Pairing is by bundle id rather than by position, because two runs can cover
different tasks while a split fills up, and positional pairing would compare one
configuration's third incident to another's third incident with nothing in the
output saying so.

**Each metric gets its own margin, and each margin is a judgement written down.**
Nobody can derive these from data; they say how much regression is worth accepting
for a gain elsewhere. Writing them in `config/eval_gate.yaml` means the trade is
argued once in a pull request rather than re-litigated per release. The values and
their reasoning:

- top-1 accuracy, 3 points: on a thirty task split that is one task, so the gate
  cannot be more sensitive than the library is large.
- top-3 accuracy, 5 points: looser on purpose. Top-3 measures whether the system
  looked in the right place, which should be stable, so a drop is a more serious
  signal and a wider margin still catches it.
- abstention, 2 points: tighter than accuracy. Inventing a culprit on a healthy
  system is what teaches an operator to ignore the tool, and it is not something to
  trade for cost.
- evidence validity, 0: a citation that does not resolve is a fabrication, and the
  entire claim of this project is that it does not ship those.

**Direction is declared per metric and asserted.** `higher_is_better` decides which
end of the interval the gate reads. Getting it backwards would pass exactly the
regressions the gate exists to catch, silently, which is the worst available
failure.

**INCONCLUSIVE is a failure, not a pass.** Below `minimum_tasks` the gate refuses
to conclude and exits non-zero. A gate that passed for lack of evidence would
produce a signed statement that nothing was checked, which is worse than having no
gate. This is not hypothetical today: the validation split has eight recorded
bundles and the minimum is ten, so the gate currently returns INCONCLUSIVE, which
is the correct answer.

**Cost is checked relatively, with an absolute floor.** A candidate must not raise
median cost per investigation by more than 20% without an explicit flag and an
explanation. Median rather than mean, so one runaway investigation does not move
the number. The floor exists because going from a hundredth of a cent to two
hundredths is a 100% increase that nobody cares about.

**The splits are the other half of the gate.** A family in `test_ood` appears in no
tunable split, and `firebreak.evals.splits` enforces it. Two rules follow from
recording a library over days rather than at once:

- **The split assignment is recorded, not derived.** It was derived from each
  family's sorted ids, so removing twelve scenarios shifted every later one.
  Nothing leaked, because both destinations happened to be tunable, and the same
  mechanism would as happily have moved an analysed scenario into `test_id`, which
  would void the only thing making its numbers worth anything.
- **Recording order is tunable splits first**, which is the reverse of the obvious
  order. A held out recording made before the thresholds are re-tuned scores zero
  and measures nothing, and the first real recording proved it: triage ranked the
  true culprit first and then abstained, because the threshold was tuned on
  fixtures whose anomaly magnitudes are several times larger.

**A partly recorded split produces a real number that is not the split's result.**
Every report says which split, whether the data was recorded or synthetic, how much
of the split is covered, and whether the figure is quotable. A number from a
tunable split says how well the system fits data it was developed against, and the
report says that on its face rather than in a footnote.

## Alternatives considered

**A fixed threshold: fail if top-1 drops at all.** Rejected. On eight tasks a
single flipped case is 12 points, so this blocks noise and passes nothing; on a
large library it blocks a rounding error. It also ignores the interval, which is
the only thing that says whether a drop is real.

**A superiority gate.** Rejected: it blocks every change whose purpose is cost,
latency or clarity, which is most of them.

**One margin for every metric.** Simpler to explain and wrong in both directions
at once, because a point of evidence validity and a point of top-3 accuracy are not
comparable quantities.

**Derive splits from a hash every run.** What the first version did. Stable against
reordering and not against removal, and the removal case is the one that happened.

**Let the gate read written reports rather than run both sides.** Attractive for
CI, and the reason `eval compare` exists. Rejected for the gate itself: it needs
both sides graded on the same tasks in the same run, and two reports written at
different times may cover different tasks with nothing saying so.

## Consequences

- Running the graph through the gate found two defects in the gate itself. It
  reported FAIL_CALIBRATION for a candidate whose calibration had improved, and its
  mean absolute confidence gap could not distinguish a system 99% confident and
  half right from one 50% confident and half right. The first became an
  underpowered rule returning INCONCLUSIVE; the second became squared error.
- The gate's own inputs were transposed by its first caller, because it took four
  positional lists. It now takes a `Side` pair object. A transposed comparison
  produces a verdict about nothing while looking entirely normal.
- Reports now carry per-task scores, which Section 9.6 asked for and they did not
  have, so a comparison can be made against a stored baseline and in CI.
- The gate is currently INCONCLUSIVE on validation and will stay so until the
  library has ten recorded bundles in that split. This is reported, not worked
  around.

## Sources

- SPEC.md Sections 8.2, 8.4, 9.3, 9.4, 9.6, 17.
- `config/eval_gate.yaml` for every margin and its reasoning,
  `src/firebreak/evals/gate.py`, `compare.py`, `splits.py`.
- `scenarios/splits.yaml` for the recorded assignment,
  `tests/contract/test_split_stability.py` for the two invariants.
- `.github/workflows/eval-gate.yml` for the two halves of the CI gate.
- ADR-0003 for the bundle format the splits are assigned over.
