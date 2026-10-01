# Runbook

Seven procedures, from SPEC.md Section 16. Each one is written to be followed by somebody
who did not build this, which means every step says how to tell it worked.

Where a procedure depends on something this repository does not have, it says so at the top
rather than failing halfway through.

## Contents

1. [Record a new scenario](#1-record-a-new-scenario)
2. [Add a tool](#2-add-a-tool)
3. [Add a remediation](#3-add-a-remediation)
4. [Retune thresholds](#4-retune-thresholds)
5. [Rotate secrets](#5-rotate-secrets)
6. [Investigate a failed eval](#6-investigate-a-failed-eval)
7. [Restore from backup](#7-restore-from-backup)

---

## 1. Record a new scenario

**Needs:** Docker with about 6 GB of memory, mains power, and roughly 40 minutes per
scenario.

**Why mains power is in the requirements.** Six of the first fourteen recordings were
corrupted by a laptop sleeping mid-run. `time.monotonic` stops while `datetime.now` jumps, so
the bundle looks complete and has a hole in the middle of it. The recorder now checks duration
and sample continuity and refuses such a bundle, but the cheaper fix is a cable. On macOS,
`caffeinate -dims` for the duration.

### Steps

1. Write the spec, or generate it. Specs live in `scenarios/specs/` and are generated from the
   flag inventory:

   ```bash
   make lab-flags      # writes reports/lab/flag_inventory.json from the pinned demo
   ```

   A flag that does not exist in the pinned release cannot be a scenario. The inventory lists
   what is actually there, and `scripts/generate_scenarios.py` reads it rather than a list
   somebody typed.

2. Choose the split before recording, not after. Splitting is by spec, so a spec that has been
   recorded and then moved between splits has leaked. `scenarios/splits.yaml` holds the
   assignment and `make lab-library` checks the whole library is sound.

3. Start the stack and check it is fit to record from:

   ```bash
   make live
   make lab-verify
   ```

   `lab-verify` checks every backend answers, the service graph connector is producing
   metrics, and the clock skew between containers is small enough for a window to mean
   something. Do not skip it: a recording made against a stack that was still warming up looks
   like a recording of a healthy system.

4. Record:

   ```bash
   make lab-record SPEC=<scenario-id> RUN=run1
   ```

   For the whole library, resumably:

   ```bash
   caffeinate -dims make lab-record-library
   ```

   It decides what to skip from the bundles on disk rather than from its own progress file,
   because a progress file can be stale and a bundle either exists or does not. Stopping it and
   starting it again continues.

5. Check the bundle:

   ```bash
   make lab-bundles
   ```

   This verifies every bundle against its manifest: row counts, checksums, and the continuity
   check that catches a sleep gap.

### How to tell it worked

- `bundles/<opaque-id>/manifest.json` exists and `make lab-bundles` passes.
- The bundle's directory name is a hash, not the scenario name. That is deliberate; a directory
  named for its fault would state the answer.
- `reports/lab/recording_progress.json` lists the scenario. Note that this file can overstate:
  it currently lists three scenarios whose bundles were later deleted. The bundles are the
  truth.

### If it failed

`RecordingDriftError` means the run took more than 1.5 times its planned duration, which
almost always means the machine slept. `BundleContinuityError` means a gap between samples
larger than four times the scrape interval. Both refuse the bundle rather than storing it.
Delete nothing by hand; re-record.

---

## 2. Add a tool

**Needs:** nothing beyond a clone.

A tool is the only way evidence enters the system, so a new one is a new way for the system to
be wrong. The checklist is longer than the code.

### Steps

1. Define the arguments as a Pydantic model with `extra="forbid"`. The model is the validation;
   there is no second check downstream.

2. Write the handler in `src/firebreak/tools/`. It builds its own query from validated
   arguments. **A model never supplies a query string**, which is what makes the query
   injection-proof and the evidence id meaningful.

3. Register it with a cap. Every tool caps how many rows it can return, because an uncapped
   tool is a tool that can fill a context window with one call.

4. Write the description the model reads. It says what the tool answers and what it does not,
   in the terms the model must choose between. Vague descriptions are the most common cause of
   a specialist calling the wrong tool.

5. Add it to the registry list its callers read, `src/firebreak/tools/registry.py`. There is one
   list and both the graph and the ReAct baseline read it, so they cannot drift apart. This was
   a real bug once: a tool sat in one list and not the other and an ablation silently tested
   nothing.

6. Write the parity test. It runs the tool against a live backend and against a recorded bundle
   over the same window and asserts the evidence records match. A tool that answers differently
   in replay makes every recorded evaluation meaningless.

7. Check the evidence id is deterministic. Run the tool twice on the same bundle and window;
   the ids must be identical. The exit gate's re-execution check depends on this.

### How to tell it worked

```bash
make verify
```

The leakage test will fail if the tool reaches anything it should not. The parity test will
fail if live and replay disagree. If both pass, run one investigation and look at the
Investigation page: the tool should appear with a row count, and the Report page's citations
should open.

---

## 3. Add a remediation

**Needs:** the live stack for the recovery check. The proposal path works offline.

### Steps

1. Add the action to the approval service's allowlist. **Firebreak cannot execute anything**;
   it writes a proposal. The approval service holds the only credentials that can act, and an
   action it does not know about is refused. That refusal is the design, not a gap.

2. Give the action a recovery check: the metric that must return to baseline, and the window to
   check it over. An action with no recovery check cannot be verified to have worked, and
   "applied successfully" is not the same claim as "fixed it".

3. Add the payload to the injection suite. A remediation is the highest-value target for a
   prompt injection through a log line, so a new action needs a red-team case asserting it is
   not proposed from attacker-controlled text.

4. Write the reversal. Every action states how to undo it, and the audit log records the
   reversal alongside the action.

### How to tell it worked

```bash
uv run pytest tests/security/ -q       # privilege separation and injection
uv run pytest tests/unit/test_remediation.py tests/unit/test_recovery.py -q
```

The privilege separation test walks the import graph and fails if any agent module can reach
the credential-holding module. If you added the action in the wrong package, that is the test
that will say so.

Then, on the live stack, propose it and approve it and confirm the audit chain verifies:
recovery is recorded as a separate entry from application, because the two can disagree.

---

## 4. Retune thresholds

**Needs:** recordings in train and validation. **Do not retune against test.**

Thresholds live in `config/thresholds.yaml` and are read, not compiled in. The ones that
matter:

| Key | File | Decides |
|---|---|---|
| `abstention.minimum_top_anomaly_z` | `config/thresholds.yaml` | Whether triage says an incident is happening at all |
| `abstention.minimum_hypothesis_support` | `config/thresholds.yaml` | How much net support a report needs before it may name a service |
| `ranking.*` | `config/thresholds.yaml` | How the random walk scores and orders services |
| `windows.baseline_fraction` | `config/thresholds.yaml` | Where the baseline ends and the incident begins when no alert says |
| `escalate_after_schema_failures` and the specialist confidence floor | `config/models.yaml` | When a small model escalates to a strong one |

Read the comments in those files before changing a value. Each one records what was measured,
what the alternatives cost, and whether the number was tuned or is a labelled default. Two are
currently marked as tuned on synthetic fixtures and needing a re-tune once recordings exist,
and one is marked measured wrong with the reason it has not been changed.

### Steps

1. Measure before changing anything:

   ```bash
   make eval CONFIG=b0 SPLIT=validation
   ```

2. Change the value. Change one.

3. Measure again and compare, paired by incident:

   ```bash
   make eval-compare BASE=<before-report> HEAD=<after-report>
   ```

   The comparison is a paired bootstrap over the same incidents. A difference whose interval
   includes zero is not a difference.

4. Check both directions. A threshold that catches more faults almost always names more
   healthy services, and the abstention grader is where that shows up. A change that improves
   root-cause accuracy and worsens abstention is usually a bad trade, because naming a service
   on a healthy system is the worst failure this design admits.

### A worked example, and why it was refused

On the eight recordings available, the abstention threshold could be lowered to roughly double
sensitivity at no measured cost in specificity. That change was not made, for three reasons
worth repeating because they generalise:

- There is exactly one no-fault recording. "No measured cost in specificity" over one negative
  example is not a measurement.
- Three of the positives sit exactly at the score cap, so the apparent headroom is an artefact
  of clipping rather than signal.
- The two faults it would still miss are latency faults scoring *below* the healthy recording.
  No threshold on a top anomaly score can separate those, so the change buys less than it
  appears to and costs an unknown amount.

### How to tell it worked

The comparison report's interval excludes zero in the direction you wanted, and the abstention
grader did not get worse. If validation improved and you cannot say what it cost, you have not
finished.

---

## 5. Rotate secrets

**Needs:** access to wherever the secrets live. This repository holds none.

### What secrets exist

| Secret | Read by | Set in |
|---|---|---|
| Model API key | The agent, for model calls | `LLM_API_KEY` |
| Model endpoint | The agent | `LLM_BASE_URL` |
| Neo4j password | The graph loader and tools | `FIREBREAK_NEO4J_PASSWORD` |
| Remediation credentials | The approval service only | Its own environment, never the agent's |

The last row is the one that matters. The approval service's credentials must not be readable
by the agent process, and a test asserts the agent's packages cannot import the module that
holds them. Putting them in a shared `.env` defeats the entire design.

### Steps

1. Issue the new secret. Do not revoke the old one yet.
2. Update the environment. `.env` locally, the secret store in a real deployment. `.env` is
   git-ignored and `gitleaks` runs on every commit; both are checks, not guarantees.
3. Restart the affected process. The service refuses to start without its required settings
   rather than starting degraded, so a missing value fails immediately and visibly.
4. Confirm the new secret works, then revoke the old one.

### How to tell it worked

For the model, run `make demo-offline` with credentials set: it will exercise the real loop
instead of replaying. If the key is wrong you get an `LlmError`, and the deterministic floor
publishes triage's answer labelled as having had no AI analysis. That label is the signal.

For Neo4j, `make graph-check`.

### If a secret was committed

Rotate first, then clean history. In that order: a secret in a public repository is compromised
the moment it is pushed, and rewriting history does not un-compromise it.

---

## 6. Investigate a failed eval

**Needs:** the reports, which are committed.

A failed eval is a red CI gate or a number that moved the wrong way. Work from the report, not
from a re-run: a re-run tells you what happens now and the question is what happened then.

### Steps

1. Read the report's caveats first. `reports/eval/<config>/<split>/` holds a JSON report per
   run, and `quotable_as_a_result` plus `caveats` usually answer the question before any
   analysis. Most surprising numbers in this project turned out to be a fixture run or a partly
   recorded split rather than a regression.

2. Check the report is the one you think it is. Reports are ordered by their own
   `generated_at`, not by modification time, because git rewrites modification time on every
   checkout. This was a real defect: a branch switch could promote a fixture run reporting
   1.000 accuracy over the recorded run reporting 0.143.

3. Check the report is not stale:

   ```bash
   make site-stats
   ```

   It prints any report whose recorded scenario count no longer matches the bundles on disk. A
   report describing deleted bundles is not evidence about anything.

4. Find which tasks failed. The report's `per_task` map holds the per-incident scores. Compare
   against the previous report for the same configuration and split, paired:

   ```bash
   make eval-compare BASE=<older> HEAD=<newer>
   ```

5. Read the transcript for one failure. The stored view is in `reports/console/` and the
   Console renders it:

   ```bash
   make console
   ```

   Look at the Investigation page for what the agent did and the Report page for what it
   claimed. Reading transcripts is how the most important finding in this project surfaced: the
   reports were passing the gate with claims that were true, cited, and said nothing.

6. Reproduce one task on its own before changing code.

### How to tell it worked

You can name the incident, the node, and the reason. "Accuracy fell" is not a diagnosis; "the
change analyst returned no evidence on three incidents because the window excluded the
deploy" is.

---

## 7. Restore from backup

**Needs:** the release archive, or the ability to re-record.

### What needs backing up

| Thing | Recoverable how |
|---|---|
| Scenario specs | In git. Nothing to do. |
| Eval reports | In git. Nothing to do. |
| Replay cassettes | In git. Nothing to do. |
| **Recorded bundles** | **Not in git.** Release archive, or re-record. |
| **Ground-truth labels** | Kept separately. See below. |
| Neo4j graph | Rebuilt from a bundle with `make graph-load`. Nothing to back up. |
| Audit log | Append-only file. Back up with the deployment. |

Bundles average 7.6 MB and the full library is roughly 870 MB, which is why `bundles/` is
git-ignored and the library ships as a release asset with checksums.

### Steps

1. Restore the bundles beside the repository as `bundles/`.

2. Verify them before trusting them:

   ```bash
   make lab-bundles
   ```

   This checks every manifest, row count and checksum. A bundle that fails is a bundle to
   delete and re-record, not one to repair.

3. Restore the labels to wherever your deployment keeps them. They are outside `bundles/` on
   purpose, in a package only the eval code may import, and a test walks the import graph to
   prove the agent has no route to them. Restoring them into the repository would break that
   property, so restore them where the eval harness reads them and nowhere else.

4. Rebuild anything derived:

   ```bash
   make graph-load
   make demo-offline
   ```

### How to tell it worked

`make verify` passes, `make lab-bundles` passes, and an eval over a restored split produces the
same numbers as the report that was generated from those bundles. If the numbers differ, the
bundles are not the ones the report was computed from, and the report is stale rather than the
code being wrong.

### If the bundles are gone and there is no archive

Re-record. It is roughly 36 hours of wall clock for the full library and there is no
compressing it: each scenario's baseline, incident and recovery have to actually happen. Record
in split order, since a partly recorded library is still useful if the recorded part is a whole
split rather than a scattering.

## Uncertain remediation execution

Keep the audit JSON Lines file and its adjacent `.sqlite3` reservation database together.
An `execution_uncertain` outcome means an earlier attempt may have changed the target.
Inspect the target and audit before making another proposal. Do not delete the reservation
or automatically retry: absence of a success record is not proof that nothing happened.
A failed executor is also reserved because it may have changed the target before raising.
