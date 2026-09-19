"""Check that a running live stack is fit to record incidents from.

Phase 1's acceptance criteria are claims about a running system, so they are
checked by code and written to a report rather than asserted in prose. Each
check returns what it measured, not just a verdict, because a stack that is
technically up can still be too starved to record from: SPEC.md Section 23
anticipates exactly that.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from firebreak.lab.endpoints import DemoEndpoints

# A recording needs enough samples inside a rate window for the window to
# evaluate. Below this, alert rules written as rate(...[5m]) return nothing
# and a scenario silently records no alert.
MIN_SAMPLES_IN_RATE_WINDOW = 3
RATE_WINDOW_SECONDS = 300
SAMPLE_STEP_SECONDS = 15

EDGE_QUERY = "count(traces_service_graph_request_total)"
SPAN_METRIC_QUERY = "count(traces_span_metrics_calls_total)"
DENSITY_QUERY = 'traces_span_metrics_calls_total{service_name="frontend"}'


@dataclass
class Check:
    """One named check with whatever it measured."""

    name: str
    passed: bool
    detail: str
    measured: dict[str, Any] = field(default_factory=dict)


def _count_query(client: httpx.Client, endpoints: DemoEndpoints, query: str) -> int:
    response = client.get(f"{endpoints.prometheus}/api/v1/query", params={"query": query})
    response.raise_for_status()
    result = response.json().get("data", {}).get("result", [])
    if not result:
        return 0
    return int(float(result[0]["value"][1]))


def check_prometheus_rules(client: httpx.Client, endpoints: DemoEndpoints) -> Check:
    """Firebreak's alert rules must be loaded, or no scenario ever alerts."""
    response = client.get(f"{endpoints.prometheus}/api/v1/rules")
    response.raise_for_status()
    groups = response.json().get("data", {}).get("groups", [])
    names = sorted(rule["name"] for group in groups for rule in group.get("rules", []))
    return Check(
        name="prometheus_rules_loaded",
        passed="checkout_error_rate_high" in names,
        detail=f"{len(names)} rule(s) loaded",
        measured={"rules": names},
    )


def check_service_graph(client: httpx.Client, endpoints: DemoEndpoints) -> Check:
    """The knowledge graph's CALLS edges come from this connector."""
    edges = _count_query(client, endpoints, EDGE_QUERY)
    return Check(
        name="service_graph_edges",
        passed=edges > 0,
        detail=f"{edges} dependency edge series",
        measured={"edge_series": edges},
    )


def check_span_metrics(client: httpx.Client, endpoints: DemoEndpoints) -> Check:
    """RED metrics per service, which the alert rules are written against."""
    series = _count_query(client, endpoints, SPAN_METRIC_QUERY)
    return Check(
        name="span_metrics_present",
        passed=series > 0,
        detail=f"{series} span metric series",
        measured={"series": series},
    )


def check_sample_density(client: httpx.Client, endpoints: DemoEndpoints, now: float) -> Check:
    """Measure how many samples land inside one rate window.

    This is the check that catches a stack which is up but too starved to
    record from. Gaps in metric delivery leave rate(...[5m]) empty, so an
    alert never fires and the recording captures an incident with no alert.
    """
    response = client.get(
        f"{endpoints.prometheus}/api/v1/query_range",
        params={
            "query": DENSITY_QUERY,
            "start": str(int(now - RATE_WINDOW_SECONDS)),
            "end": str(int(now)),
            "step": str(SAMPLE_STEP_SECONDS),
        },
    )
    response.raise_for_status()
    result = response.json().get("data", {}).get("result", [])
    points = len(result[0]["values"]) if result else 0
    expected = RATE_WINDOW_SECONDS // SAMPLE_STEP_SECONDS
    return Check(
        name="sample_density",
        passed=points >= MIN_SAMPLES_IN_RATE_WINDOW,
        detail=f"{points} of {expected} possible points in a {RATE_WINDOW_SECONDS}s window",
        measured={"points": points, "possible": expected},
    )


def check_flagd(client: httpx.Client, endpoints: DemoEndpoints, flag: str) -> Check:
    """flagd must answer, since every fault is injected through it."""
    response = client.post(f"{endpoints.flagd_ofrep}/ofrep/v1/evaluate/flags/{flag}")
    response.raise_for_status()
    variant = response.json().get("variant")
    return Check(
        name="flagd_serving",
        passed=isinstance(variant, str),
        detail=f"{flag} served as {variant!r}",
        measured={"flag": flag, "variant": variant},
    )


def check_load_generator(client: httpx.Client, endpoints: DemoEndpoints) -> Check:
    """Without traffic there are no symptoms to record."""
    response = client.get(f"{endpoints.load_api}/stats/requests")
    response.raise_for_status()
    body = response.json()
    users = int(body.get("user_count", 0))
    return Check(
        name="load_generator_running",
        passed=users > 0,
        detail=f"{users} user(s), state {body.get('state')}",
        measured={"user_count": users, "state": body.get("state")},
    )


def check_alertmanager(client: httpx.Client, endpoints: DemoEndpoints) -> Check:
    """Alertmanager is the only route from a fired rule to Firebreak."""
    response = client.get(f"{endpoints.alertmanager}/api/v2/status")
    response.raise_for_status()
    cluster = response.json().get("cluster", {})
    return Check(
        name="alertmanager_ready",
        passed=response.status_code == 200,
        detail=f"cluster status {cluster.get('status')}",
        measured={"cluster_status": cluster.get("status")},
    )


def run_checks(
    client: httpx.Client,
    endpoints: DemoEndpoints,
    flag: str,
    now: float,
) -> list[Check]:
    """Run every check, turning a connection failure into a failed check.

    A check that cannot reach its service is a failure, not a crash, so the
    report shows which parts of the stack answered and which did not.
    """
    runners: list[tuple[str, Callable[[], Check]]] = [
        ("prometheus_rules_loaded", lambda: check_prometheus_rules(client, endpoints)),
        ("service_graph_edges", lambda: check_service_graph(client, endpoints)),
        ("span_metrics_present", lambda: check_span_metrics(client, endpoints)),
        ("sample_density", lambda: check_sample_density(client, endpoints, now)),
        ("flagd_serving", lambda: check_flagd(client, endpoints, flag)),
        ("load_generator_running", lambda: check_load_generator(client, endpoints)),
        ("alertmanager_ready", lambda: check_alertmanager(client, endpoints)),
    ]
    checks: list[Check] = []
    for name, runner in runners:
        try:
            checks.append(runner())
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as error:
            checks.append(Check(name=name, passed=False, detail=f"{type(error).__name__}: {error}"))
    return checks


def build_report(checks: list[Check], demo_tag: str, vendor_clean: bool) -> dict[str, Any]:
    """Assemble the report written to reports/lab/live_verification.json."""
    failed = [check.name for check in checks if not check.passed]
    return {
        "demo_tag": demo_tag,
        "checks_run": len(checks),
        "checks_passed": len(checks) - len(failed),
        "failed_checks": failed,
        "vendor_submodule_clean": vendor_clean,
        "ready_to_record": not failed and vendor_clean,
        "checks": [asdict(check) for check in checks],
    }
