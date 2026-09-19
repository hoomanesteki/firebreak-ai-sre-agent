# Target system facts: OpenTelemetry Demo 3.1.0

Everything here was read from the pinned submodule at
`vendor/otel-demo` (tag `3.1.0`, commit `dedc0178`) or from that tag's
published files. Nothing here is assumed. Re-check this document whenever
the pin moves.

## Compose layout

The demo splits its stack across several Compose files. The application
services are in `compose.yaml`; the telemetry backends Firebreak reads are
in `compose.observability.yaml`. Running only `compose.yaml` gives a shop
with no Prometheus, Jaeger, Grafana, or OpenSearch.

| File | Contents |
|---|---|
| `compose.yaml` | Application services, flagd, flagd-ui, load generator, OTel Collector |
| `compose.observability.yaml` | Jaeger, Grafana, Prometheus, OpenSearch, OpAMP server, and collector overrides |
| `compose.full.yaml`, `compose.extras.yaml`, `compose.agent.yaml`, `compose.profiling.yaml`, `compose.tests.yaml` | Other profiles, not used by Firebreak |

## Services

Application: `ad`, `cart`, `checkout`, `currency`, `email`, `frontend`,
`frontend-proxy`, `image-provider`, `load-generator`, `payment`,
`product-catalog`, `quote`, `recommendation`, `shipping`.

Infrastructure: `flagd`, `flagd-ui`, `telemetry-docs`, `astronomy-db`,
`valkey-cart`, `otel-collector`.

Observability: `jaeger`, `grafana`, `prometheus`, `opensearch`,
`opamp-server`.

The demo at this tag has no `fraud-detection` or `accounting` service, so
the Kafka scenario family covers `checkout` and the Kafka broker rather than
a fraud detection consumer.

## Ports

| Variable | Value | Published to the host by the demo |
|---|---|---|
| `ENVOY_PORT` | 8080 | Yes, `8080:8080` |
| `PROMETHEUS_PORT` | 9090 | Yes, `9090:9090` |
| `JAEGER_UI_PORT` | 16686 | No, container port only |
| `OPENSEARCH_PORT` | 9200 | No, container port only |
| `GRAFANA_PORT` | 3000 | No, container port only |
| `FLAGD_PORT` | 8013 | No, container port only |
| `FLAGD_OFREP_PORT` | 8016 | No, container port only |
| `LOCUST_WEB_PORT` | 8089 | No, container port only |

Firebreak's overlay publishes Jaeger, OpenSearch, and the flagd OFREP port
to the host, because the tool layer queries them directly.

## Routes through the Envoy proxy on 8080

| Path | Target | Prefix rewritten |
|---|---|---|
| `/loadgen/` | Locust web on `LOCUST_WEB_PORT` | Yes, to `/` |
| `/flagservice/` | flagd on `FLAGD_PORT` (8013) | Yes, to `/` |
| `/feature` | flagd-ui | Yes, to `/` |
| `/jaeger/` | Jaeger UI | No |
| `/grafana/` | Grafana | No |
| `/` | frontend | No |

The flagd OFREP port (8016) is not routed through the proxy, which is why
the overlay publishes it directly.

## Feature flags

flagd reads `src/flagd/demo.flagd.json`, mounted into the container at
`/etc/flagd`. flagd-ui mounts the same directory at `/app/data` and writes
to it, so flagd picks up edits to that file while it runs.

Eighteen flags exist at this tag. All eleven that SPEC.md Section 6.2
assumed are present.

| Flag | Variants | Used by Firebreak as |
|---|---|---|
| `paymentFailure` | off, 10%, 25%, 50%, 75%, 90%, 100% | error_injection |
| `cartFailure` | off, 10%, 25%, 50%, 75%, 90%, 100% | error_injection |
| `adFailure` | off, on | error_injection |
| `productCatalogFailure` | off, on | error_injection |
| `paymentUnreachable` | off, on | unreachable_dependency |
| `imageSlowLoad` | off, 5sec, 10sec | latency |
| `intlShippingSlowdown` | off, 5sec, 10sec | latency |
| `adHighCpu` | off, on | cpu_saturation |
| `adManualGc` | off, on | gc_pressure |
| `emailMemoryLeak` | off, 1x, 10x, 100x, 1000x, 10000x | memory_leak |
| `recommendationCacheFailure` | off, on | memory_leak |
| `kafkaQueueProblems` | off, on | queue_lag |
| `productCatalogLockContention` | off, on | lock_contention, new at this tag |
| `failedReadinessProbe` | off, on | health_check_failure, new at this tag |
| `loadGeneratorFloodHomepage` | off, on | no_fault load spike, new at this tag |
| `aiSlowResponse` | off, 5sec, 10sec | needs the agent profile, not used in v1 |
| `aiRunawayAgent` | off, on | needs the agent profile, not used in v1 |
| `emitRawPii` | off, on | not a fault, never enabled |

`loadGeneratorFloodHomepage` gives the no-fault family a demand spike with
no service defect, which is a cleaner control than changing the load
generator settings mid-run.

## Load generator

Tag 3.1.0 reverted to Locust after 3.0.0 shipped a k6 generator that had a
dependency licensing problem. Locust exposes a web API, reachable from the
host at `http://localhost:8080/loadgen/`. Defaults: `LOCUST_USERS=5`,
`LOCUST_AUTOSTART=true`, HTTP to browser user weights 9 to 1.

## Collector configuration

The collector loads several config files in order and the last one wins:

```
--config=/etc/otelcol-config.yml
--config=/etc/otelcol-config-full.yml            (observability profile only)
--config=/etc/otelcol-config-observability.yml   (observability profile only)
--config=/etc/otelcol-config-extras.yml
```

`otelcol-config-extras.yml` is upstream's documented seam for forks, and its
path comes from the `OTEL_COLLECTOR_CONFIG_EXTRAS` variable, so Firebreak
points that variable at its own file rather than editing anything under
`vendor/`.

Upstream warns that the collector merges config files but replaces arrays
rather than appending to them. Any pipeline Firebreak touches must repeat
the upstream entries. At this tag they are:

- traces exporters: `otlp_grpc/jaeger`, `debug`, `span_metrics`
- metrics exporters: `otlp_http/prometheus`, `debug`
- logs exporters: `opensearch`, `debug`
- metrics receivers: `docker_stats`, `http_check/frontend-proxy`,
  `host_metrics`, `nginx`, `otlp`, `redis`, `span_metrics`

## Prometheus

Prometheus runs with `--web.enable-otlp-receiver`, so the collector pushes
metrics over OTLP rather than Prometheus scraping the services. The
consequence is that metric resolution follows the SDK export interval, not
the 60 second `scrape_interval` in the config file.

Retention is 7 days and `out_of_order_time_window` is 30 minutes. The
upstream config has no `rule_files` and no `alerting` block, so Firebreak
renders its own Prometheus config from the vendored one and adds both.

## Setting flags without touching vendored files

flagd-ui exposes a REST API through the proxy, which is how the scenario lab
changes flags:

| Call | Effect |
|---|---|
| `GET http://localhost:8080/feature/api/read` | Returns the whole flag configuration as JSON under a `flags` key |
| `POST http://localhost:8080/feature/api/write` | Replaces the whole configuration, sent as `{"data": {...}}` |

The write replaces everything, so the lab reads the current configuration,
changes one `defaultVariant`, and writes the result back. Nothing under
`vendor/` is edited and no volume is remapped.

`/read-file` and `/write-to-file` exist as compatibility aliases for the
same two handlers.

## Image versions at this tag

| Component | Image |
|---|---|
| Collector | `opentelemetry-collector-contrib:0.159.0` |
| Prometheus | `quay.io/prometheus/prometheus:v3.13.1` |
| Jaeger | `quay.io/jaegertracing/jaeger:2.19.0` |
| Grafana | `grafana/grafana:13.1.0` |
| flagd | `ghcr.io/open-feature/flagd:v0.16.0` |

Collector 0.159.0 is well past the rename of the service graph connector, so
the connector type is `service_graph`. The older `servicegraph` spelling is
deprecated. SPEC.md Section 6.1 uses the old name; ADR-0005 records the
correction.

## Things that would corrupt a recording

1. **The flagd-ui scheduler.** Tag 3.1.0 added a scheduler to flagd-ui that
   activates randomly picked flags for random durations, so a demo left
   running produces incidents on its own. It starts with `running: false`
   and has to be started from the `Scheduler` tab, so it is off unless
   somebody turns it on. There is no REST route for it, only a LiveView
   page, so the lab cannot check it programmatically. A recording made
   while it runs contains an unlabelled second fault, which is why the
   recorder's preconditions list it.
2. **Browser users.** `LOCUST_BROWSER_TRAFFIC_ENABLED=true` starts headless
   Chromium processes, which are memory hungry on a laptop.
3. **`emitRawPii`.** Never enabled. It puts card numbers into telemetry.
