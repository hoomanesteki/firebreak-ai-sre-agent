# ADR-0003: Incident bundles as Parquet files, queried with DuckDB

- Status: Accepted
- Date: 2026-09-22
- Phase: 2

## Context

Every accuracy number this project reports is measured against recorded
incidents. That puts three requirements on how a recording is stored, and
they are not the requirements a normal telemetry store is built for.

It has to be **frozen**. A measurement taken against data that can change is
not a measurement. Re-running an eval six months from now must query exactly
the bytes the first run queried.

It has to be **cheap to open**. The eval runs three trials across a hundred
or more incidents, and CI runs a subset on every pull request. Standing up a
Prometheus and a Jaeger per incident is not workable.

It has to be **separable from its answer**. The recording and the ground
truth cannot live in the same place, or the first careless change puts the
answer in front of the agent.

## Decision

A bundle is a directory of flat files: three Parquet tables for metrics,
traces and logs, three JSON files for the alert, the change log and the
topology, and a manifest.

The manifest carries a SHA-256 for every file and is written last.
`verify_bundle` re-checksums everything and additionally refuses a bundle
that contains a file the manifest does not list. `BundleReader` will only
open names the manifest lists.

Ground truth lives under `labels/`, in a separate top level package
`firebreak_eval_labels`, never inside the bundle.

Queries at read time go through DuckDB, which reads Parquet directly with no
server, no import step and no schema migration.

## Alternatives considered

**Keeping the live stack and replaying into it.** The most faithful option,
and it was rejected on cost. Prometheus, Jaeger and OpenSearch is several
gigabytes of memory to answer one query about a twenty minute window, and
recordings would then depend on those services still existing and still
behaving the same way in a year.

**JSON or newline delimited JSON.** Simple, diffable, and far too slow and
large. A single recording is tens of thousands of metric samples, and the
tool layer filters them by service and time on every call.

**SQLite.** A real option. Parquet won on two points: it is columnar, which
suits filtering a long thin metrics table by service and time, and it is a
format many tools read, so a bundle stays useful outside this project.
SQLite would have given transactions, which a frozen artefact does not need.

**One Parquet file per metric.** Rejected. Seven files per bundle that all
have to be kept in step, when one long table with a `metric_name` column
answers the same questions in SQL.

**Putting the label inside the bundle.** Convenient, and precisely the
mistake this design exists to prevent. Every leak starts as a convenience.

## Consequences

- A bundle is verifiable but not repairable. Editing any file invalidates
  the manifest, which is the intent: the fix is to re-record, not to patch.
- `pyarrow` and `duckdb` become dependencies of the eval path. Both are
  self contained wheels with no server.
- Parquet schemas are declared explicitly rather than inferred, so a
  recording made a year apart from another still has the same columns and
  types.
- The reader refusing unlisted names means a label dropped into a bundle
  directory by mistake is unreadable through the normal path and fails
  verification loudly. That is leakage control 3 in SPEC.md Section 8.4.
- Bundles are large. SPEC.md Section 7.1 keeps a showcase subset in the
  repository and publishes the full library as a release asset, which the
  checksums in each manifest make verifiable after download.
- Nothing about this format depends on the OpenTelemetry Demo. A bundle
  recorded from a different system with the same schemas would replay.

## Sources

- SPEC.md Sections 6.2, 7.1, 8.4 and 12.
- `src/firebreak/lab/bundle.py` for the manifest and verification rules.
- `src/firebreak_eval_labels/__init__.py` for the label store and canaries.
