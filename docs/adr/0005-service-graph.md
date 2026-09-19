# ADR-0005: Service dependency edges from the service_graph connector

- Status: Accepted
- Date: 2026-09-19
- Phase: 1

## Context

The knowledge graph needs `CALLS` edges between services, with per edge
request rate, error rate, and latency for the incident window. Those edges
are what lets Firebreak rank a downstream service as the likely cause when
an upstream service is the one alerting.

The edges have to reflect what actually happened during the window, not a
diagram somebody drew. That means deriving them from telemetry.

## Decision

Run the OpenTelemetry Collector's `service_graph` connector, configured in
`ops/otelcol-config-extras.yml`. It pairs client and server spans and emits
`traces_service_graph_request_total`,
`traces_service_graph_request_failed_total`, and client and server duration
histograms per pair of services. Those metrics land in Prometheus with
everything else, so an edge is queryable over the same time range as the RED
metrics beside it.

Two settings differ from the connector defaults, both for the same reason:
the faults Firebreak injects are exactly the conditions that break span
pairing. The store TTL goes from 2 seconds to 10, and `max_items` from 1000
to 10000, so a 10 second latency fault does not cause the edges to vanish at
the moment they matter most. `metrics_flush_interval` is set to 15 seconds so
edges and RED metrics land in the same time buckets.

## Alternatives considered

**Building edges directly from sampled traces.** This is the fallback, and it
stays available because the bundle format stores spans anyway. It is more
work per query, it only sees sampled traffic, and the rates it produces are
estimates. It has one real advantage: it needs no collector change, so if the
connector proves unreliable it can be swapped in without touching the stack.

**A static topology file.** Cheap and wrong. It cannot show that an edge was
failing during the window, and it goes stale silently.

**Grafana Tempo's service graphs.** Equivalent output from a component the
demo does not ship. Adding Tempo to the stack for this alone is not worth the
memory on a laptop.

## Consequences

- The connector is marked **alpha** for traces to metrics in the contrib
  distribution. It ships in the contrib image the demo already runs
  (`0.159.0`), so it costs nothing to add, but alpha means the config surface
  can change. The trace based fallback keeps that from being a dead end.
- The connector type is `service_graph`. The older `servicegraph` spelling is
  deprecated. SPEC.md Section 6.1 uses the old name; this ADR is the
  correction.
- The collector merges config files but **replaces** arrays rather than
  appending to them, so `ops/otelcol-config-extras.yml` repeats the upstream
  pipeline entries. The comment inside upstream's own extras file lists a
  shorter set of metrics receivers than the observability profile actually
  runs with: copying it would silently drop `postgresql` and `kafkametrics`,
  and the Kafka scenario family depends on `kafkametrics`. A test asserts
  that Firebreak's overlay drops nothing the pinned files declare.
- Unpaired spans are counted by the connector itself
  (`traces_service_graph_unpaired_spans_total`), so the recorder can record
  how complete the edges were for a window rather than assuming they were
  complete.

## Sources

- Service Graph Connector README, contrib distribution, including the alpha
  stability level and the full configuration surface.
  https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/connector/servicegraphconnector/README.md
- `vendor/otel-demo/src/otel-collector/otelcol-config-extras.yml`, upstream's
  own note that arrays are replaced rather than appended.
- `docs/target-system.md` for the pipeline contents this overlay repeats.
- SPEC.md Sections 6.1, 6.4, and 14.
