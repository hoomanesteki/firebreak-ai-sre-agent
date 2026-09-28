# ADR-0014: Two telemetry pipelines, and a Console that computes nothing

- Status: Accepted
- Date: 2026-09-27
- Phase: 11

## Context

SPEC.md Section 14 asks for a record on "separate telemetry pipelines for evidence and for
agent behavior". Firebreak reads one system's telemetry and emits its own, and both are
OpenTelemetry. That similarity is the hazard: the two are the same shape and mean entirely
different things.

Section 6.13 asks for a Console over six pages, and Phase 11's reviewer focus names two
things: no prompt content in spans by default, and clarity for a non-engineer.

## Decision

**Two pipelines, separated in both directions, because the confusion is symmetric.**

An agent span in the incident's pipeline would look like evidence. Firebreak's own spans
describe what Firebreak did: which node ran, which tool it called, how many tokens it spent.
Dropped into the target system's traces, those become telemetry about the system under
investigation, and the next investigation would read them as signal. A system that can see
its own activity as evidence will eventually explain an incident by pointing at itself.

Incident evidence in an agent span is worse, and it is the reviewer focus. Traces are
shipped to a collector, often a shared one, retained longer than anything else, and read by
people who were never given access to the incident. Firebreak reads customer identifiers,
internal hostnames and whatever an attacker put in a product name, and puts some of it in a
prompt. A span carrying the prompt carries all of it, outward, by default.

**So spans carry shapes and never content.** Which node, which tool, which tier, how many
tokens, how many rows, whether it succeeded. A prompt becomes a twelve character hash, which
answers the only question a trace is ever asked about a prompt, which is whether two calls
sent the same thing. The prompt's version stamp is recorded too, because tying a run to a
prompt file carries no content at all.

**`include_content` exists, is off, and announces itself.** A developer debugging one run
locally needs the prompts. The switch lives on one options object rather than at each call
site, so there is one place to look when asking whether a deployment ships prompts, and its
value is written onto every root span. A trace carrying content says so rather than leaving a
reader to notice.

**An error records the exception type and not its message.** A tool's exception message can
quote the data that caused it. That is content arriving through the one path that looks like
an error path rather than a content path, which is exactly the kind of gap that survives a
review of the content paths.

**The conventional attribute names come from the installed package.** `gen_ai.*` names are
imported from `opentelemetry.semconv`, not typed as literals, so a backend that understands
the convention charts token usage without being told about Firebreak. Names this project adds
are prefixed `firebreak.` and declared in one place, because an attribute name typed twice is
a dashboard that silently shows nothing. Declaring them in one place is not enough on its own:
one name held two vocabularies here until a test was written for it, described under
Consequences.

**One investigation is one trace, enforced by a context manager.** The root span wraps the
whole run, so node, tool and model spans are children by construction rather than by every
call site remembering to set a parent.

**The Console reads files and computes nothing.** Every page renders what another component
wrote: eval reports, the audit chain, the feedback log, a stored view of each investigation.
This is a constraint rather than a simplification. A Console that computed accuracy would be
a second definition of accuracy, and two definitions of a number disagree eventually. The
one this project would notice last is the one on the page a reader trusts most.

**Every page handles empty data as a first-class state.** Eight bundles of a hundred and
fourteen, no credentials, no approvals, no feedback: this repository *is* the empty case.
Each page says what is missing and names the command that would fill it, through one shared
type so no page improvises wording that blames the reader. A fresh clone and the first day of
a real deployment are the two times somebody opens a Console for the first time, and a blank
table is useless at both.

**Every claim links to its evidence, and the link opens the record rather than a rendering.**
The query, the window, the backend fingerprint, the row hash, and the tool and arguments
needed to re-run it. The project's whole claim is that its reports cite re-runnable evidence,
and the Report page is where a reader checks that. An invented evidence id returns 404 rather
than an empty panel: a citation nobody can open is the failure the exit gate exists to
prevent, and it deserves a status code.

**The Console has no approve button, and says so on the page.** Giving it one would mean the
Console could call the approval service, which puts the decision and the credential back in
one process. The paragraph explaining that renders whether or not the queue has anything in
it, because the absence of a button is most worth explaining to somebody looking for one.

**The showcase is built from scenario specs rather than recorded bundles.** A bundle averages
7.6 MB and the library is git-ignored, so a demo keyed to real recordings runs on one machine.
In CI that leaves failing or skipping, and a skip that reports success is worse than a
failure. Ten specs rebuilt deterministically instead, with a test asserting the rebuild is
byte identical, because the cassette keys hash the windows and evidence ids the bundle
produces.

**Live streaming is not built, and the page says why.** Section 6.13 asks for a streaming
timeline. Against a frozen bundle an investigation finishes in under a second, so a progress
bar would be theatre. Streaming is worth building when a run takes long enough to watch,
which means live mode with a real model.

## Alternatives considered

**One pipeline with an attribute distinguishing agent spans from evidence.** Simpler to
deploy and it fails in the direction that matters: a missing attribute is indistinguishable
from evidence, and the failure is silent. A separate pipeline fails by being absent, which is
noticed.

**Redact prompts rather than omit them.** Rejected. Redaction needs to know what is sensitive
in free text written by services under attack, which is the problem nobody has solved. A hash
answers the question traces are actually asked and needs no such judgement.

**Ship prompts and rely on collector-side filtering.** Rejected: it makes the default unsafe
and the safety somebody else's configuration file.

**Let the Console compute its own metrics from the bundles.** Attractive, because it would
show a number for a split with no eval report. Rejected on the second-definition argument
above.

**HTMX for the streaming timeline.** SPEC.md Section 6.13 names it and there is nothing to
stream yet. Adding the dependency for a feature that would be theatre is worse than recording
the gap.

**Vendor Cytoscape.js and Chart.js now.** Section 6.13 names both. Deferred: there is one
graph worth drawing and no calibration curve with enough data to plot. Committing two vendored
libraries and their licences for empty charts is weight without value, and the phase report
records it as not done rather than leaving a reader to discover it.

## Consequences

- `firebreak.node` carried both the graph node and, on model spans, the name of the prompt
  sent. The four analyst nodes share the `specialist` prompt, so grouping by node produced a
  `specialist` row naming no node in the graph and sitting beside the four real ones. The
  prompt name now has `firebreak.model.purpose` and the node a call belongs to is read from
  its parent span. Found while counting spans for the phase report, which is the argument for
  writing the numbers down: eight tests covered the content rule and none covered whether an
  attribute meant one thing.
- Wiring the tool span found a small lie: the first version recorded `len(result.data)` as a
  row count, which counts keys in a summary dict. It would have appeared in a dashboard as a
  row count and been wrong every time. It now reads `row_count` off the evidence record.
- The offline demo exposed a real interaction between two correct behaviours. When a cassette
  is missing, the graph's deterministic floor catches the model failure first and publishes
  B0's triage, so the demo saw a report rather than an error and blamed reproduction for a
  missing recording. It now recognises the floor and names the actual cause.
- The leakage scanner refused `firebreak.demo` for importing scenario specs. The import is
  legitimate, since a bundle must be built from something, so the package joined the
  allowlist with the condition it rests on written beside it and three tests keeping it true.
- The fonts SPEC.md Section 6.13 names are not vendored. The stack falls back to system faces
  rather than fetching from a CDN, because a web request would break the offline demo, which
  is the one thing this Console has to work for.
- The Console's server entry point lives in `scripts/` because it prints, and ruff forbids
  `print` under `src/`. That rule was right: library code that prints is a library that
  cannot be embedded.

## Sources

- SPEC.md Sections 6.12, 6.13, 11, 14, 17.
- `src/firebreak/telemetry/spans.py`, `src/firebreak/web/`, `src/firebreak/demo/showcase.py`.
- `tests/unit/test_telemetry_spans.py` for the no-content rule,
  `tests/integration/test_console.py` for seeded and empty pages and the evidence links,
  `tests/integration/test_offline_demo.py` for the replay.
- ADR-0009 for the floor the demo interacts with, ADR-0010 for the gate the Report page shows.
- [R17] on GenAI agent spans, [R22] for the threat model the content rule belongs to.
