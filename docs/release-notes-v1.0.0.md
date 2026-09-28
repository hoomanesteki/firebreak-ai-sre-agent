# Firebreak v1.0.0

Draft notes for the owner to review before tagging. **Not tagged by the builder**: a tag is a
public statement and the release archive it refers to is not something a build step should
decide to publish.

## What this is

A multi-agent AI SRE that investigates fault-injected incidents in the OpenTelemetry Demo and
writes reports whose every claim links to a query, a time window and a row hash you can re-run.
An exit gate removes any claim that fails to re-execute, and the system abstains rather than
guessing when the evidence will not carry an answer.

Also an evaluation harness for that system: 114 fault-injection scenarios across four splits,
seven graders, three baselines, six ablations, bootstrap intervals, and a non-inferiority gate.

## Read this before the feature list

**Nothing in this release has been measured.** The harness is built and tested; it has not been
run in a way that produces a result. Two things are missing:

- **No model has ever run.** There are no credentials configured, so every number involving a
  model is absent. The deterministic floor publishes triage's answer labelled as having had no
  AI analysis, which is what every report in this repository contains.
- **The incident library is 8 of 114 recorded, all in one tuning split.** Both held-out test
  splits are empty, so there is nothing to compute a result from.

Every eval report in `reports/eval/` is marked `quotable_as_a_result: false` with the reason
attached. The site and the README show "not measured" or "not a result" wherever a figure would
go, each citing the report it would have come from. That is deliberate and it is the most
important property of this release: two figures in this repository look quotable and are not, a
synthetic-fixture run at 1.000 root-cause accuracy and a partly recorded tuning split at 0.143,
and the build refuses to publish either.

**The offline demo's results are much better than the measured ones.** It names 8 of 8 faults
and abstains correctly on both no-fault incidents. Those are fixtures, replayed from a
deterministic stub rather than a model, and the same triage scores 1 of 7 on real recordings.
The demo prints what it is replaying before its first line. It demonstrates that the whole path
runs and reproduces; it is not evidence about quality.

## What works

- **`make demo-offline`** replays 10 incidents from 56 recorded answers with no model, no
  network and no Docker, rebuilding each bundle from its scenario spec and failing if any stops
  reproducing. Part of `make verify`, so it is checked on every commit.
- **The Console**, six pages over what Firebreak produced: incidents, one investigation step by
  step, a report with every claim linked to its evidence, the approval queue, evaluation and
  feedback. It reads files and computes nothing. Binds to loopback.
- **Deterministic triage** with robust anomaly scores and personalised PageRank over the service
  graph, which runs before any model and can end an investigation on its own.
- **14 typed tools** that build their own queries from validated arguments, cap their output,
  and record the query, window, backend fingerprint and row hash behind every answer.
- **The exit gate**, six checks that remove claims rather than annotating them, including
  re-running every citation.
- **Remediation as a proposal only.** A separate approval service holds the only credentials
  that can act, requires a person, and verifies afterwards that the metric recovered. A test
  walks the import graph and fails if any agent module can reach the credential holder.
- **Agent spans** carrying shapes and never content: a prompt becomes a twelve character hash
  and an error records its exception type, not its message.
- **Ground-truth leakage controls**, including an import-graph test and a canary in every label
  scanned against every prompt. It has caught two real routes.
- **14 architecture decision records**, each with the alternatives it rejected and the bugs the
  decision caused.

## What is not built

| Thing | Why |
|---|---|
| Agent metrics, a collector config, Grafana dashboards | Nothing runs a collector here. Shipping untested config that looks like a working observability stack is worse than a recorded gap. |
| Live streaming of an investigation | Against a frozen bundle a run finishes in under a second, so a progress bar would be theatre. |
| Vendored Chart.js and Cytoscape.js | One graph worth drawing and no calibration curve with enough data to plot. |
| Self-hosted fonts | A web request would break the offline demo, which is the one thing the Console must work for. The stack falls back to system faces. |
| A feedback form in the Console | The store and its rules exist; a form over them does not. |
| An OpenRCA comparison | The data terms are not stated on its repository page, and it needs hardware this project has not had. |
| A seventh gate check, for whether a claim is informative | Found late and left as a decision rather than made unilaterally. See below. |

## Known issues

**The exit gate cannot tell a useful claim from a useless one.** All 40 claims across the ten
showcase reports are true, cited, re-runnable, and say nothing: four of five describe the
investigation rather than the incident. Six checks ask whether a claim is supported and none
asks whether it is informative. The cause is the stub rather than the system, and it still means
the only end-to-end exercise of the gate that CI runs is one where the numbers check verifies
zero numbers.

**The abstention threshold is tuned on fixtures and known to be wrong on recordings.** It
abstains on five of the seven real faults available. A retune would more than double sensitivity
at no measured cost, and it was refused: one no-fault recording is not a measurement of
specificity, three positives sit at a score cap, and the two faults it would still miss are
latency faults scoring below the healthy recording. That needs a second signal, not a better
number. The shipped value is labelled as fixture-tuned everywhere it appears.

**The ranking blames the symptom.** It names the frontend for payment failures on the
recordings available, which is the thing personalised PageRank over the service graph was
supposed to prevent.

**An eval report can outlive the bundles it was computed from.** One in this repository does.
`build_site_stats.py` refuses to publish from a report whose recorded count no longer matches
the bundles present, and the Console shows when a report was generated, but nothing prevents a
stale report being read as current by a person.

**`reports/lab/recording_progress.json` overstates by three.** It lists 11 recordings and 8
exist; three were deleted for recording corruption and not un-marked. The recorder is unaffected
because it decides what to skip from the bundles on disk, not from that file.

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/). Nothing else for the offline path.
- Docker with about 6 GB of memory for the live stack.
- Any OpenAI-compatible endpoint, Ollama included, for model mode.
- Recording the library is roughly 36 hours of wall clock. Do it on mains power: six of the
  first fourteen recordings were corrupted by a laptop sleeping mid-run.

## Release assets

The recorded bundles are not in the repository. They average 7.6 MB, the full library is roughly
870 MB, and `bundles/` is git-ignored. A recorded library ships as a separate archive with
checksums; verify it with `make lab-bundles` before trusting it.

Ground-truth labels are kept outside the repository on purpose, in a package only the eval code
may import. Restoring them into the working tree would break the property a test exists to
prove.

## Credits and licence

Firebreak is MIT licensed.

The target system is the [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo),
Apache-2.0, used unmodified at a pinned release. Nothing inside `vendor/` is edited, so the pin
means what it says.

## Before tagging, the owner should decide

1. Whether to tag at all with no measured result, or wait for the library and credentials.
2. Whether the recorded library becomes a release asset, and where it is hosted.
3. `PYSEC-2026-2447` in `diskcache`, which has no fix available. It is carried explicitly in the
   threat model with the reason rather than suppressed, and shipping a release is the point at
   which that becomes a published decision.
4. Whether `reports/lab/recording_progress.json` is corrected or left as evidence of the
   corruption incident.
