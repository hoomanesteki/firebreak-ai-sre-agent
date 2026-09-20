"""Prove that each feature flag actually changes the demo's behaviour.

A flag that is defined but has no observable effect is a scenario that will
never produce a real incident, and a library built on it would report
accuracy on nothing. This runs once per flag against the live stack: sample
the signals, turn the flag on, hold, sample again, turn it off.

Results go to reports/lab/flag_smoke.json. SPEC.md Section 17, Phase 1.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from firebreak.lab.endpoints import DemoEndpoints
from firebreak.lab.flags import OFF_VARIANT, FlagController, FlagState

# Signals broad enough to catch any fault family: errors show up as error
# spans, latency and resource faults show up in the duration tail.
SIGNAL_QUERIES = {
    "error_span_rate_per_second": (
        'sum(rate(traces_span_metrics_calls_total{status_code="STATUS_CODE_ERROR"}[2m]))'
    ),
    "span_rate_per_second": "sum(rate(traces_span_metrics_calls_total[2m]))",
    "latency_p95_milliseconds": (
        "histogram_quantile(0.95, sum by (le) "
        "(rate(traces_span_metrics_duration_milliseconds_bucket[2m])))"
    ),
}

# Never turned on by a smoke run. emitRawPii puts card numbers into
# telemetry, and the two ai flags need a demo profile Firebreak does not run.
EXCLUDED_FLAGS = frozenset({"emitRawPii", "aiSlowResponse", "aiRunawayAgent"})

RELATIVE_CHANGE_THRESHOLD = 0.20


class PrometheusQueryError(Exception):
    """Prometheus did not answer with a usable result."""


@dataclass(frozen=True)
class SignalSample:
    """One reading of every smoke signal."""

    values: dict[str, float | None]


@dataclass
class FlagSmokeResult:
    """What turning one flag on did to the signals."""

    flag: str
    variant: str
    before: dict[str, float | None]
    after: dict[str, float | None]
    changed_signals: list[str]
    observed_effect: bool
    error: str | None = None


def query_instant(client: httpx.Client, endpoints: DemoEndpoints, query: str) -> float | None:
    """Run one instant PromQL query and return its single scalar value."""
    response = client.get(f"{endpoints.prometheus}/api/v1/query", params={"query": query})
    response.raise_for_status()
    body: Any = response.json()
    if not isinstance(body, dict) or body.get("status") != "success":
        raise PrometheusQueryError(f"query failed: {query}")
    result = body.get("data", {}).get("result", [])
    if not result:
        return None
    value = result[0].get("value")
    if not isinstance(value, list) or len(value) != 2:
        raise PrometheusQueryError(f"unexpected result shape for: {query}")
    try:
        return float(value[1])
    except (TypeError, ValueError):
        return None


def sample_signals(client: httpx.Client, endpoints: DemoEndpoints) -> SignalSample:
    """Read every smoke signal once."""
    return SignalSample(
        values={name: query_instant(client, endpoints, q) for name, q in SIGNAL_QUERIES.items()}
    )


def relative_change(before: float | None, after: float | None) -> float | None:
    """Relative change between two readings, or None when it cannot be computed.

    A missing reading means the query matched no series at all, which is not
    the same as a reading of zero. Appearing and disappearing are handled by
    `has_changed` rather than here, because there is no meaningful ratio
    between nothing and something.
    """
    if before is None or after is None:
        return None
    if before == 0:
        return None if after == 0 else float("inf")
    return (after - before) / abs(before)


def has_changed(
    before: float | None,
    after: float | None,
    threshold: float = RELATIVE_CHANGE_THRESHOLD,
) -> bool:
    """True when a signal moved enough to count as an observed effect.

    A series that did not exist before the fault and does after is the
    strongest possible evidence, not a missing measurement. Error rate
    queries match no series at all while a service is healthy, so treating
    that as uncomputable made this blind to exactly the error injection
    flags it most needs to confirm.

    The reverse, a signal that vanishes, is deliberately not counted. It is
    ambiguous: it can mean the fault stopped the traffic, or it can mean a
    batch of telemetry was dropped, which does happen on a loaded host. A
    false positive here would certify a flag as usable when it does nothing,
    which is worse than missing one, so only appearing counts.
    """
    if before is None:
        return after is not None and after != 0
    change = relative_change(before, after)
    if change is None:
        return False
    return abs(change) > threshold


def find_changed_signals(
    before: dict[str, float | None],
    after: dict[str, float | None],
    threshold: float = RELATIVE_CHANGE_THRESHOLD,
) -> list[str]:
    """Signals that moved by more than the threshold, appeared, or vanished."""
    names = set(before) | set(after)
    return sorted(
        name for name in names if has_changed(before.get(name), after.get(name), threshold)
    )


def pick_strongest_variant(state: FlagState) -> str | None:
    """Choose the variant most likely to show an effect.

    Percentage flags get the largest percentage, duration flags the longest
    duration, and multiplier flags the largest multiplier. Anything else
    falls back to the first variant that is not off.
    """
    candidates = [v for v in state.variants if v != OFF_VARIANT]
    if not candidates:
        return None

    def rank(variant: str) -> tuple[int, float]:
        digits = "".join(c for c in variant if c.isdigit())
        if not digits:
            return (0, 0.0)
        return (1, float(digits))

    return max(sorted(candidates), key=rank)


def smoke_one_flag(
    controller: FlagController,
    client: httpx.Client,
    endpoints: DemoEndpoints,
    state: FlagState,
    hold_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> FlagSmokeResult:
    """Turn one flag on, watch the signals, and turn it back off."""
    variant = pick_strongest_variant(state)
    if variant is None:
        return FlagSmokeResult(
            flag=state.name,
            variant="",
            before={},
            after={},
            changed_signals=[],
            observed_effect=False,
            error="flag offers no variant other than off",
        )

    before = sample_signals(client, endpoints).values
    try:
        controller.set_variant(state.name, variant)
        sleep(hold_seconds)
        after = sample_signals(client, endpoints).values
    finally:
        controller.set_variant(state.name, OFF_VARIANT)

    changed = find_changed_signals(before, after)
    return FlagSmokeResult(
        flag=state.name,
        variant=variant,
        before=before,
        after=after,
        changed_signals=changed,
        observed_effect=bool(changed),
    )


def build_report(
    results: list[FlagSmokeResult], demo_tag: str, hold_seconds: float
) -> dict[str, Any]:
    """Assemble the report written to reports/lab/flag_smoke.json."""
    return {
        "demo_tag": demo_tag,
        "hold_seconds": hold_seconds,
        "relative_change_threshold": RELATIVE_CHANGE_THRESHOLD,
        "signal_queries": SIGNAL_QUERIES,
        "excluded_flags": sorted(EXCLUDED_FLAGS),
        "flags_tested": len(results),
        "flags_with_observed_effect": sum(1 for r in results if r.observed_effect),
        "results": [asdict(r) for r in results],
    }
