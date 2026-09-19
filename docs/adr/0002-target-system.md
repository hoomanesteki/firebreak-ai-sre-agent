# ADR-0002: OpenTelemetry Demo 3.1.0 as the target system

- Status: Accepted
- Date: 2026-09-19
- Phase: 1

## Context

Firebreak's accuracy claims only mean something if the incidents behind them
have true labels. Real incidents do not come labelled, so the system under
investigation has to be one where faults can be injected on purpose, held
for a known window, and switched off again.

The requirements were: a realistic multi-service application, first class
OpenTelemetry instrumentation across metrics, logs, and traces, a built in
way to inject faults, a permissive licence, and something a reader can start
on a laptop.

## Decision

Use the OpenTelemetry Demo, the Astronomy Shop, pinned as a Git submodule at
tag `3.1.0`, commit `dedc0178918e260823323b8d95005a8cb924b007`.

Nothing under `vendor/` is edited. The demo is extended through the seams it
documents: the `OTEL_COLLECTOR_CONFIG_EXTRAS` variable for collector
configuration, Compose overlay files for services and ports, and flagd-ui's
REST API for flag changes.

## Alternatives considered

**Tag 3.0.0.** Two months older, so more settled. Rejected on two counts.
Its load generator is k6, which 3.1.0 reverted to Locust over a dependency
licensing problem; inheriting a known licensing problem into a public
portfolio repository is not worth two months of soak time. Locust also
exposes a web API that lets the scenario lab set an exact user count, which
is part of a scenario's identity.

**Tag 2.2.0 or older.** Further from what a reader would run today, and the
flag set is smaller.

**Writing a purpose built microservice application.** Full control, and a
month of work building a shop instead of building an AI SRE. It would also
be a system nobody else can check the results against.

**Using the OpenRCA dataset as the only source of incidents.** It has real
world style telemetry and labels, but it is a fixed dataset: no live mode, no
remediation, no recovery verification, and the data terms are not stated on
its repository page. SPEC.md Section 8.3 keeps it as optional external
validation in Phase 12 rather than the primary source.

## Consequences

- The demo's telemetry backends are in `compose.observability.yaml`, not in
  `compose.yaml`. Running only the base file gives a shop with nothing to
  investigate. Both files plus Firebreak's overlay are required.
- Jaeger, OpenSearch, and the flagd OFREP port are not published to the host
  by the demo. The overlay publishes them, bound to the loopback address.
- All eleven flags SPEC.md Section 6.2 assumed exist at this tag. Four more
  are useful and were not in the spec: `productCatalogLockContention`,
  `failedReadinessProbe`, `loadGeneratorFloodHomepage`, and a pair of AI
  service flags that need a demo profile Firebreak does not run. The no fault
  family uses `loadGeneratorFloodHomepage` for a demand spike with no service
  defect, which is a cleaner control than changing load settings mid run.
- Kafka, fraud detection, and accounting arrive with `compose.full.yaml`,
  which the demo's own Makefile includes by default. Leaving it out is not a
  smaller working stack: the observability profile's collector config
  scrapes Kafka whether or not the broker is running, so the collector logs
  a failure every 10 seconds. The first live run of this stack found exactly
  that, which is why the composition now matches upstream's.
- Tag 3.1.0 added a scheduler to flagd-ui that activates random flags on its
  own. It defaults to off and has no REST route, so the recorder lists it as
  a manual precondition rather than checking it.
- Moving the pin is a deliberate change: service names, flag names, metric
  names, and collector pipeline contents are all read from the pinned files
  and all of them have changed between releases before.

## Sources

- OpenTelemetry Demo release 3.1.0 notes, including the load generator
  revert and the new flags.
  https://github.com/open-telemetry/opentelemetry-demo/releases/tag/3.1.0
- The pinned files themselves, read at `vendor/otel-demo` and summarised in
  `docs/target-system.md`.
- SPEC.md Sections 3.1, 6.1, 6.2, and 8.3.
