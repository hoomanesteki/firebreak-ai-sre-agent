"""Synthetic incident bundle: fabricated data for tests only, never for a reported metric.

Nothing in this module has touched the live demo. It exists so Phase 3 tool
code and the eval harness can be built and exercised before a single real
incident has been recorded, and so tests do not need the full OpenTelemetry
Demo stack running to check that code against a bundle-shaped input. The
numbers are invented, the traces did not happen, and the moment this output
appears in a results table or a README metric, that number is fiction.

It still has to be a bundle in every way `bundle.py` checks: an explicit
pyarrow schema for each table, a manifest written last, and a change log
that has been through `sanitise_changes`, because a fixture that skips the
leakage control is a fixture that would hide a bug in it.
"""

from __future__ import annotations

import json
import random
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from firebreak.lab.bundle import BundleManifest, EdgeRecord, TimeWindow, derive_bundle_id
from firebreak.lab.bundle_writer import BundleWriter
from firebreak.lab.endpoints import DEMO_TAG
from firebreak.lab.scenario import DistractorKind, FaultClass, FaultKind, ScenarioSpec
from firebreak.signals import MetricName, StatusCode

SYNTHETIC_RECORDER_VERSION = "synthetic-fixture-0"

# Fixed rather than tied to wall clock time, so the same spec and seed
# produce byte-identical rows on every run.
WINDOW_ANCHOR = datetime(2025, 1, 1, tzinfo=UTC)

METRIC_STEP_SECONDS = 15
# Which container resource each resource fault class moves. A class that
# moved both would be indistinguishable from the others, and the fault class
# grader scores exactly that distinction.
RESOURCE_SIGNAL_BY_CLASS: dict[FaultClass, str] = {
    FaultClass.MEMORY_LEAK: "memory",
    FaultClass.CPU_SATURATION: "cpu",
    FaultClass.GC_PRESSURE: "cpu",
    FaultClass.CACHE_FAILURE: "latency",
}

# Container resources a healthy service reports, and how a leaking or
# saturated one departs from them.
BASELINE_MEMORY_BYTES = 120_000_000.0
MEMORY_LEAK_BYTES_PER_SECOND = 180_000.0
BASELINE_CPU = 0.18
SATURATED_CPU = 0.94

# Latency a healthy service and a degraded one report, in milliseconds.
BASELINE_LATENCY_MS: tuple[float, float] = (45.0, 180.0)
DEGRADED_LATENCY_MS: tuple[float, float] = (260.0, 1450.0)
# Cumulative fraction of samples at or under each bucket bound, plus +Inf.
# Real Prometheus histogram buckets are cumulative; a degraded profile moves
# mass out of the low buckets and into the tail rather than changing the
# total, which is what a real latency regression looks like.

TARGET_TRACE_COUNT = 70
BACKGROUND_TRACE_COUNT = 90
LOAD_SPIKE_MULTIPLIER = 1.6

# Weighted toward INFO, which is what a healthy service mostly logs.
LOG_SEVERITIES = ("DEBUG", "INFO", "INFO", "INFO", "WARN")

# A simplified slice of the OpenTelemetry Demo's real service dependency
# graph (docs/target-system.md lists the services; the edges below are the
# well known call directions between them). Not every real edge is here,
# but every edge here is real, which is enough structure to give a fault
# injected on any one service a chain of upstream callers to propagate into.
REAL_ADJACENCY: dict[str, tuple[str, ...]] = {
    "frontend": (
        "ad",
        "cart",
        "checkout",
        "currency",
        "product-catalog",
        "recommendation",
        "shipping",
        "image-provider",
    ),
    "checkout": (
        "cart",
        "currency",
        "email",
        "payment",
        "product-catalog",
        "shipping",
        "fraud-detection",
        "accounting",
    ),
    "recommendation": ("product-catalog",),
    "shipping": ("quote",),
}

CORE_SERVICES: tuple[str, ...] = ("frontend", "checkout")
FILL_PRIORITY: tuple[str, ...] = (
    "cart",
    "payment",
    "shipping",
    "product-catalog",
    "recommendation",
    "email",
    "currency",
    "ad",
    "quote",
    "image-provider",
    "fraud-detection",
    "accounting",
)
TARGET_SERVICE_COUNT = 8


def _select_services(target: str | None) -> list[str]:
    """Pick about eight services, always including the target and its real callers.

    A fixed cast of eight would be simpler, but a target service outside it
    would make the "shows a symptom on the target" requirement impossible to
    satisfy, so the target and whoever calls it are seated first and the
    rest of the cast fills in around them.
    """
    services = list(CORE_SERVICES)
    if target and target not in services:
        services.append(target)
    callers = [client for client, servers in REAL_ADJACENCY.items() if target in servers]
    for caller in callers:
        if caller not in services:
            services.append(caller)
    for candidate in FILL_PRIORITY:
        if len(services) >= TARGET_SERVICE_COUNT:
            break
        if candidate not in services:
            services.append(candidate)
    return services


def _select_edges(services: list[str], target: str | None) -> list[tuple[str, str]]:
    """Restrict the real adjacency to the selected cast, adding a fallback route.

    If the target has no real caller among the selected services (it is a
    leaf that nothing in `REAL_ADJACENCY` points at), frontend is wired to
    it directly, because a target nothing ever calls cannot appear in a
    trace at all.
    """
    allowed = set(services)
    edges = [
        (client, server)
        for client, servers in REAL_ADJACENCY.items()
        for server in servers
        if client in allowed and server in allowed
    ]
    if target and target != "frontend" and not any(server == target for _, server in edges):
        edges.append(("frontend", target))
    return edges


def _shortest_path(edges: list[tuple[str, str]], start: str, target: str) -> list[str]:
    """The shortest call chain from `start` to `target` over `edges`.

    This is the chain a symptom on the target propagates up through, so it
    doubles as the definition of "the target's callers" used everywhere
    else in this module.
    """
    if start == target:
        return [start]
    adjacency: dict[str, list[str]] = {}
    for client, server in edges:
        adjacency.setdefault(client, []).append(server)
    queue: deque[list[str]] = deque([[start]])
    seen = {start}
    while queue:
        path = queue.popleft()
        for nxt in adjacency.get(path[-1], ()):
            if nxt in seen:
                continue
            if nxt == target:
                return [*path, nxt]
            seen.add(nxt)
            queue.append([*path, nxt])
    return [start, target]


def _random_walk(
    rng: random.Random, edges: list[tuple[str, str]], start: str, depth: int
) -> list[str]:
    """A short, unweighted walk from `start`, for background traffic with no story to tell."""
    adjacency: dict[str, list[str]] = {}
    for client, server in edges:
        adjacency.setdefault(client, []).append(server)
    path = [start]
    node = start
    for _ in range(depth):
        options = adjacency.get(node, [])
        if not options:
            break
        node = rng.choice(options)
        path.append(node)
    return path


def _hex_id(rng: random.Random, nibbles: int) -> str:
    """A deterministic-given-the-rng hex identifier, OTel ID shaped."""
    return f"{rng.getrandbits(nibbles * 4):0{nibbles}x}"


def _build_trace(
    rng: random.Random,
    path: list[str],
    start_time: datetime,
    symptomatic: set[str],
    fault_active: bool,
) -> tuple[list[dict[str, Any]], list[tuple[str, str, bool]]]:
    """Build the spans for one trace walking `path`, root first.

    A span on a symptomatic service during the fault window runs slower and
    fails more often. A span whose immediate child just failed degrades too,
    at a smaller magnitude, which is what lets a caller show a symptom
    without ever touching the fault itself: cascading failure, not
    injection.
    """
    trace_id = _hex_id(rng, 32)
    spans: list[dict[str, Any]] = []
    edges_used: list[tuple[str, str, bool]] = []
    parent_span_id: str | None = None
    parent_errored = False
    current_time = start_time
    for index, service in enumerate(path):
        span_id = _hex_id(rng, 16)
        duration_ms = rng.uniform(15.0, 60.0)
        error_probability = 0.01
        if fault_active and service in symptomatic:
            duration_ms *= rng.uniform(3.0, 8.0)
            error_probability = 0.4
        elif fault_active and parent_errored:
            duration_ms *= rng.uniform(1.1, 1.6)
            error_probability = 0.15
        status_code = "STATUS_CODE_ERROR" if rng.random() < error_probability else "STATUS_CODE_OK"
        spans.append(
            {
                "trace_id": trace_id,
                "span_id": span_id,
                "parent_span_id": parent_span_id,
                "service_name": service,
                "span_name": f"{service}.handle",
                "span_kind": "SPAN_KIND_SERVER",
                "start_time": current_time,
                "duration_ms": round(duration_ms, 3),
                "status_code": status_code,
                "attributes_json": json.dumps({"rpc.service": service}, sort_keys=True),
            }
        )
        if index > 0:
            edges_used.append((path[index - 1], service, status_code == "STATUS_CODE_ERROR"))
        parent_span_id = span_id
        parent_errored = status_code == "STATUS_CODE_ERROR"
        current_time += timedelta(milliseconds=duration_ms * rng.uniform(0.05, 0.25))
    return spans, edges_used


def _generate_traces(
    rng: random.Random,
    edges: list[tuple[str, str]],
    window: TimeWindow,
    fault_start: datetime,
    fault_end: datetime,
    path_to_target: list[str],
    symptomatic: set[str],
    target: str | None,
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], dict[str, int]]]:
    """Target-focused traces plus background noise, and the edge call counts they exercised."""
    traces: list[dict[str, Any]] = []
    edge_stats: dict[tuple[str, str], dict[str, int]] = {}
    total_seconds = (window.end - window.start).total_seconds()
    fault_seconds = (fault_end - fault_start).total_seconds()

    def record_edges(used: list[tuple[str, str, bool]]) -> None:
        for client, server, errored in used:
            stats = edge_stats.setdefault((client, server), {"calls": 0, "errors": 0})
            stats["calls"] += 1
            if errored:
                stats["errors"] += 1

    if target:
        for _ in range(TARGET_TRACE_COUNT):
            # Concentrated in the fault window so there is a quiet baseline
            # on either side for a tool to compare against, with a fraction
            # spread across the whole window so the target is not silent
            # outside the incident.
            if rng.random() < 0.8:
                start_time = fault_start + timedelta(seconds=rng.uniform(0.0, fault_seconds))
            else:
                start_time = window.start + timedelta(seconds=rng.uniform(0.0, total_seconds))
            fault_active = fault_start <= start_time < fault_end
            spans, used = _build_trace(rng, path_to_target, start_time, symptomatic, fault_active)
            traces.extend(spans)
            record_edges(used)

    for _ in range(BACKGROUND_TRACE_COUNT):
        start_time = window.start + timedelta(seconds=rng.uniform(0.0, total_seconds))
        fault_active = fault_start <= start_time < fault_end
        path = _random_walk(rng, edges, "frontend", rng.randint(1, 3))
        spans, used = _build_trace(rng, path, start_time, symptomatic, fault_active)
        traces.extend(spans)
        record_edges(used)

    traces.sort(key=lambda row: row["start_time"])
    return traces, edge_stats


def _generate_logs(
    rng: random.Random,
    services: list[str],
    window: TimeWindow,
    fault_start: datetime,
    fault_end: datetime,
    symptomatic: set[str],
    traces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Baseline logs for every service, plus an error and warning burst on symptomatic ones."""
    trace_ids_by_service: dict[str, list[str]] = {}
    for span in traces:
        trace_ids_by_service.setdefault(span["service_name"], []).append(span["trace_id"])

    total_seconds = (window.end - window.start).total_seconds()
    fault_seconds = (fault_end - fault_start).total_seconds()
    logs: list[dict[str, Any]] = []

    for service in services:
        candidates = trace_ids_by_service.get(service, [])
        for _ in range(rng.randint(15, 30)):
            timestamp = window.start + timedelta(seconds=rng.uniform(0.0, total_seconds))
            trace_id = rng.choice(candidates) if candidates and rng.random() < 0.4 else None
            logs.append(
                {
                    "timestamp": timestamp,
                    "service_name": service,
                    "severity": rng.choice(LOG_SEVERITIES),
                    "body": f"{service} handled request",
                    "trace_id": trace_id,
                    "attributes_json": json.dumps({"service.name": service}, sort_keys=True),
                }
            )
        if service in symptomatic:
            for _ in range(rng.randint(20, 40)):
                timestamp = fault_start + timedelta(seconds=rng.uniform(0.0, fault_seconds))
                trace_id = rng.choice(candidates) if candidates and rng.random() < 0.6 else None
                severity = rng.choice(("ERROR", "ERROR", "WARN"))
                body = (
                    f"{service} request failed with an upstream error"
                    if severity == "ERROR"
                    else f"{service} request is slower than usual"
                )
                logs.append(
                    {
                        "timestamp": timestamp,
                        "service_name": service,
                        "severity": severity,
                        "body": body,
                        "trace_id": trace_id,
                        "attributes_json": json.dumps({"service.name": service}, sort_keys=True),
                    }
                )

    logs.sort(key=lambda row: row["timestamp"])
    return logs


def _latency_percentiles(rng: random.Random, degraded: bool) -> tuple[float, float]:
    """The p50 and p95 a service reports, healthy or degraded.

    Jittered per sample. Without it every degraded service reports the
    identical series and scores identically, which makes a ranking a set of
    ties and Phase 4's candidate ordering impossible to test against.
    """
    p50, p95 = DEGRADED_LATENCY_MS if degraded else BASELINE_LATENCY_MS
    return (
        round(p50 * rng.uniform(0.82, 1.18), 3),
        round(p95 * rng.uniform(0.80, 1.20), 3),
    )


def _metric_row(
    timestamp: datetime, metric_name: str, service: str, labels: dict[str, str], value: float
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "metric_name": metric_name,
        "service_name": service,
        "labels_json": json.dumps(labels, sort_keys=True),
        "value": float(value),
    }


def _generate_metrics(
    rng: random.Random,
    services: list[str],
    window: TimeWindow,
    fault_start: datetime,
    fault_end: datetime,
    symptomatic: set[str],
    load_spike: bool,
    edges: list[tuple[str, str]] | None = None,
    resource_target: str | None = None,
    resource_signal: str | None = None,
    raises_errors: bool = True,
) -> list[dict[str, Any]]:
    """Every 15 seconds: call volume split by status, and a duration histogram, per service.

    Metric names match what the demo's own span_metrics connector produces
    (confirmed in `ops/alert_rules.yml`, read from the pinned demo's Grafana
    dashboards), so a query written against a real bundle also runs against
    this one. A symptomatic service during the fault window gets a higher
    error share and a latency profile shifted into the tail; a no-fault
    scenario with an alert gets a volume bump instead, standing in for the
    load spike that family is built around.
    """
    rows: list[dict[str, Any]] = []
    tick = window.start
    while tick <= window.end:
        in_fault = fault_start <= tick < fault_end
        for service in services:
            degraded = in_fault and service in symptomatic
            baseline_calls = rng.uniform(8.0, 14.0)
            if load_spike and in_fault:
                baseline_calls *= LOAD_SPIKE_MULTIPLIER
            # A resource fault does not raise the error rate. A leaking
            # process serves correct responses right up until it dies,
            # which is exactly why SPEC.md Section 6.2 calls this family
            # slow onset and says it needs metric trends rather than
            # errors. A fixture that leaked errors too would make the
            # hardest family the easiest one.
            error_share = (
                rng.uniform(0.25, 0.45) if degraded and raises_errors else rng.uniform(0.0, 0.02)
            )
            error_calls = baseline_calls * error_share
            ok_calls = max(baseline_calls - error_calls, 0.0)
            rows.append(
                _metric_row(
                    tick,
                    MetricName.SPAN_CALLS_TOTAL,
                    service,
                    {"status_code": StatusCode.OK.value},
                    ok_calls,
                )
            )
            rows.append(
                _metric_row(
                    tick,
                    MetricName.SPAN_CALLS_TOTAL,
                    service,
                    {"status_code": StatusCode.ERROR.value},
                    error_calls,
                )
            )

            # Latency is emitted as the same derived percentiles the real
            # exporter produces. Writing raw histogram buckets here while
            # the exporter wrote percentiles is what made every latency
            # tool return nothing for half the bundles.
            p50, p95 = _latency_percentiles(rng, degraded)
            rows.append(_metric_row(tick, MetricName.SPAN_DURATION_P50_MS, service, {}, p50))
            rows.append(_metric_row(tick, MetricName.SPAN_DURATION_P95_MS, service, {}, p95))
        # Service graph edges. The knowledge graph's CALLS relationships
        # come from these, so a bundle without them cannot exercise the
        # dependency ranking that most of triage rests on.
        for client, server in edges or []:
            failing = server in symptomatic
            requests = rng.uniform(4.0, 12.0) * (1.6 if load_spike else 1.0)
            failed = requests * (rng.uniform(0.6, 0.95) if failing and in_fault else 0.0)
            labels = {"client": client, "server": server}
            rows.append(
                _metric_row(tick, MetricName.SERVICE_GRAPH_REQUESTS, client, labels, requests)
            )
            rows.append(_metric_row(tick, MetricName.SERVICE_GRAPH_FAILED, client, labels, failed))

        # Container resources. The resource family's symptom is here and
        # nowhere else: a memory leak shows no error rate at all until the
        # process dies.
        for service in services:
            affected = service == resource_target and in_fault
            # Which resource moves is what tells the resource classes apart.
            # A memory leak that also saturated the CPU would make
            # memory_leak and cpu_saturation indistinguishable, and the
            # fault class grader scores exactly that distinction.
            leaking = affected and resource_signal == "memory"
            saturating = affected and resource_signal == "cpu"
            elapsed = (tick - fault_start).total_seconds() if leaking else 0.0
            memory = BASELINE_MEMORY_BYTES + elapsed * MEMORY_LEAK_BYTES_PER_SECOND
            cpu = SATURATED_CPU if saturating else BASELINE_CPU
            rows.append(
                _metric_row(tick, MetricName.CONTAINER_MEMORY_BYTES, service, {}, round(memory, 1))
            )
            rows.append(
                _metric_row(
                    tick,
                    MetricName.CONTAINER_CPU_UTILISATION,
                    service,
                    {},
                    round(min(cpu + rng.uniform(-0.02, 0.02), 1.0), 4),
                )
            )

        tick += timedelta(seconds=METRIC_STEP_SECONDS)
    return rows


def _build_topology(
    services: list[str],
    edges: list[tuple[str, str]],
    edge_stats: dict[tuple[str, str], dict[str, int]],
    window: TimeWindow,
) -> dict[str, Any]:
    """The dependency graph, with per edge rates rather than raw counts.

    Built through EdgeRecord so this and the real exporter cannot drift.
    They did: this wrote call_count and error_count while the exporter wrote
    requests_per_second, and a topology tool reading one against the other
    reported every edge as carrying no traffic.
    """
    seconds = max((window.end - window.start).total_seconds(), 1.0)
    return {
        "window_start": window.start.isoformat(),
        "window_end": window.end.isoformat(),
        "services": sorted(services),
        "edges": [
            EdgeRecord(
                client=client,
                server=server,
                requests_per_second=edge_stats.get((client, server), {}).get("calls", 0) / seconds,
                failures_per_second=edge_stats.get((client, server), {}).get("errors", 0) / seconds,
            ).as_row()
            for client, server in edges
        ],
    }


def _build_alert(
    spec: ScenarioSpec,
    path_to_target: list[str],
    alert_fired: bool,
    alert_fired_at: datetime | None,
    window: TimeWindow,
) -> dict[str, Any]:
    """An Alertmanager-shaped webhook payload, the format `alert.json` records verbatim."""
    if not alert_fired:
        return {"status": "none", "alerts": []}
    alerting_service = path_to_target[1] if len(path_to_target) > 1 else path_to_target[0]
    alertname = spec.expected_alert or f"{alerting_service}_error_rate_high"
    starts_at = alert_fired_at or window.start
    return {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": alertname,
                    "service": alerting_service,
                    "severity": "critical",
                    "environment": "demo",
                },
                "annotations": {
                    "summary": f"{alertname} fired for {alerting_service}",
                },
                "startsAt": starts_at.isoformat(),
                "endsAt": None,
            }
        ],
    }


def _build_changes(
    rng: random.Random,
    spec: ScenarioSpec,
    services: list[str],
    onset_anchor: datetime,
    window: TimeWindow,
) -> list[dict[str, Any]]:
    """The raw change feed, before sanitising: distractors, the fault flag, and filler.

    Records use the canonical shape declared by `ChangeRecord` in
    `firebreak.lab.bundle`. This producer and the real recorder once
    disagreed on field names, and the replay backend silently returned no
    changes at all, so a distractor scenario would have shown an agent an
    empty change log rather than an error.

    The fault flag record is generated on purpose. A real recorder's change
    feed would capture it, which is exactly the event `sanitise_changes`
    exists to remove before a bundle is written, so leaving it out here
    would mean this fixture never exercises the control it is supposed to
    prove works.
    """
    records: list[dict[str, Any]] = []

    for distractor in spec.distractors:
        applied_at = onset_anchor + timedelta(seconds=distractor.offset_seconds)
        if distractor.kind is DistractorKind.DEPLOY_EVENT:
            records.append(
                {
                    "kind": "deploy",
                    "service": distractor.service,
                    "at": applied_at.isoformat(),
                    "detail": f"deployed a new build of {distractor.service}",
                }
            )
        else:
            records.append(
                {
                    "kind": "flag_change",
                    "service": distractor.service,
                    "at": applied_at.isoformat(),
                    "detail": f"{distractor.flag} set to {distractor.variant}",
                }
            )

    if spec.fault.kind is FaultKind.FLAG:
        records.append(
            {
                "kind": "flag_change",
                "service": spec.target_service,
                "at": onset_anchor.isoformat(),
                "detail": f"{spec.fault.flag} set to {spec.fault.variant}",
            }
        )

    benign_pool = [service for service in services if service != spec.target_service]
    if benign_pool:
        records.append(
            {
                "kind": "restart",
                "service": rng.choice(benign_pool),
                "at": (window.start + timedelta(seconds=rng.uniform(0.0, 60.0))).isoformat(),
                "detail": "scheduled container restart",
            }
        )
        records.append(
            {
                "kind": "config_change",
                "service": rng.choice(benign_pool),
                "at": (window.start + timedelta(seconds=rng.uniform(0.0, 90.0))).isoformat(),
                "detail": "updated rate limit configuration",
            }
        )

    return records


def build_synthetic_bundle(
    bundle_dir: Path, spec: ScenarioSpec, run_id: str, seed: int = 0
) -> BundleManifest:
    """Fabricate a bundle for `spec` that passes `verify_bundle`.

    Deterministic in `seed` via `random.Random`, never the global `random`
    module, so the same spec and seed produce byte-identical parquet on
    every call. When `spec.target_service` is set, the target and the
    services on its call path to `frontend` run hot during the fault window:
    higher error share, slower spans, error and warning logs. Everything
    else is quiet background traffic, so a detector has both a signal and a
    baseline to compare it against.
    """
    rng = random.Random(seed)

    services = _select_services(spec.target_service)
    edges = _select_edges(services, spec.target_service)

    window = TimeWindow(
        start=WINDOW_ANCHOR,
        end=WINDOW_ANCHOR + timedelta(seconds=spec.timing.total_seconds),
    )
    fault_start = window.start + timedelta(seconds=spec.timing.warmup_seconds)
    fault_end = fault_start + timedelta(seconds=spec.timing.fault_seconds)

    target = spec.target_service
    if target:
        path_to_target = _shortest_path(edges, "frontend", target)
        symptomatic = set(path_to_target)
    else:
        path_to_target = ["frontend"]
        symptomatic = set()

    fault_onset = None
    if spec.fault.kind is FaultKind.FLAG:
        fault_onset = fault_start + timedelta(seconds=rng.uniform(0.2, 1.2))
    onset_anchor = fault_onset or fault_start

    alert_fired = spec.expected_alert is not None
    alert_fired_at = None
    if alert_fired:
        candidate = onset_anchor + timedelta(seconds=rng.uniform(60.0, 150.0))
        alert_fired_at = min(candidate, window.end)

    load_spike = target is None and alert_fired

    traces, edge_stats = _generate_traces(
        rng, edges, window, fault_start, fault_end, path_to_target, symptomatic, target
    )
    logs = _generate_logs(rng, services, window, fault_start, fault_end, symptomatic, traces)
    # Resource faults leave their mark on container metrics rather than on
    # error rates, so the resource families need a target named here or
    # their whole signal is absent from the fixture.
    # Resource faults leave their mark on container metrics rather than on
    # error rates, and which metric moves is what tells the classes apart.
    resource_signal = RESOURCE_SIGNAL_BY_CLASS.get(spec.fault_class)
    resource_target = spec.target_service if resource_signal else None

    # A resource fault does not drag its whole call path down the way an
    # error injection does. SPEC.md Section 6.2 calls this family slow
    # onset and says it needs metric trends rather than errors, so the
    # latency symptom stays on the affected service and the container
    # trend is what identifies it. Spreading it up the path would bury
    # the culprit under its own callers.
    metric_symptomatic = (
        {spec.target_service} if resource_target and spec.target_service else symptomatic
    )
    metrics = _generate_metrics(
        rng,
        services,
        window,
        fault_start,
        fault_end,
        metric_symptomatic,
        load_spike,
        edges=edges,
        resource_target=resource_target,
        resource_signal=resource_signal,
        raises_errors=resource_target is None,
    )
    topology = _build_topology(services, edges, edge_stats, window)
    alert_payload = _build_alert(spec, path_to_target, alert_fired, alert_fired_at, window)
    raw_changes = _build_changes(rng, spec, services, onset_anchor, window)
    # Includes distractor flags, not just the primary fault. Computing
    # this here separately is how the flood flag survived into a no fault
    # bundle's change log.
    fault_flags = spec.fault_flag_names

    writer = BundleWriter(
        bundle_dir=bundle_dir,
        bundle_id=derive_bundle_id(spec.id, run_id),
        run_id=run_id,
        demo_tag=DEMO_TAG,
        recorder_version=SYNTHETIC_RECORDER_VERSION,
    )
    writer.write_alert(alert_payload)
    writer.write_changes(raw_changes, fault_flags)
    writer.write_topology(topology)
    writer.write_metrics(metrics)
    writer.write_traces(traces)
    writer.write_logs(logs)
    return writer.finalise(
        window=window,
        alert_fired=alert_fired,
        alert_fired_at=alert_fired_at,
        notes="synthetic fixture data, not a real recording",
    )
