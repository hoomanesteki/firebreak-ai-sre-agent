"""Metric tools: what the numbers say, and which of them are unusual.

Three tools with deliberately non-overlapping jobs, because fourteen tools
with fuzzy boundaries is how an agent ends up calling four of them to answer
one question (SPEC.md principle H5).

- `list_anomalies` answers "where should I look", across every service at
  once, with no argument beyond a window. It is the first call of almost
  every investigation.
- `compare_windows` answers "did this service change", for one service and
  one metric, with a baseline to compare against.
- `query_metric` answers "what exactly did this do", returning the samples
  themselves.

There is no free-form query tool. A caller names a metric from the declared
vocabulary, and anything else is refused. SPEC.md Section 11 lists free-form
query execution as ASI05, and the practical reason is the same as the
security one: a query that can be anything cannot be reviewed, cached, or
re-run by a verifier.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from firebreak.backends.base import MAX_ROWS, MetricPoint
from firebreak.signals import MetricName, metric_names
from firebreak.tools.base import ToolContext, ToolResult, ToolSpec, summarise_rows
from firebreak.tools.evidence import (
    EvidenceKind,
    Fact,
    TimeRange,
    build_record,
)
from firebreak.triage.anomaly import Direction, rank_anomalies, score_series
from firebreak.triage.scoring import (
    ANOMALY_DIRECTIONS,
    MAX_SAMPLES_PER_SERIES,
    error_only,
    score_services,
)

TOP_ANOMALIES = 8


class MetricWindow(BaseModel):
    """A window, as a tool takes it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: str
    end: str

    def to_range(self) -> TimeRange:
        return TimeRange.model_validate({"start": self.start, "end": self.end})


class ListAnomaliesInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window: MetricWindow
    baseline: MetricWindow
    services: tuple[str, ...] = ()
    minimum_score: float = Field(default=3.0, ge=0.0)


class QueryMetricInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: MetricName
    window: MetricWindow
    services: tuple[str, ...] = ()
    limit: int = Field(default=MAX_ROWS, ge=1, le=MAX_ROWS)


class CompareWindowsInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: MetricName
    service: str
    baseline: MetricWindow
    incident: MetricWindow


def _fetch(
    context: ToolContext,
    metric: MetricName,
    window: TimeRange,
    services: tuple[str, ...],
    limit: int,
) -> list[MetricPoint]:
    points = context.backend.query_metrics(metric, window, services=services, limit=limit)
    return error_only(points) if metric is MetricName.SPAN_CALLS_TOTAL else points


def list_anomalies(context: ToolContext, arguments: ListAnomaliesInput) -> ToolResult:
    """Score every service against its baseline and rank the unusual ones."""
    incident = arguments.window.to_range()
    baseline = arguments.baseline.to_range()

    # The same scorer the triage pipeline runs, so what the agent is told is
    # anomalous and what the ranking treats as anomalous cannot diverge.
    scores = score_services(context.backend, baseline, incident, arguments.services)

    ranked = rank_anomalies(scores, minimum_score=arguments.minimum_score)[:TOP_ANOMALIES]
    rows = [
        {
            "service": score.subject,
            "metric": score.metric,
            "score": score.score,
            "baseline": round(score.baseline_median, 4),
            "incident": round(score.incident_median, 4),
        }
        for score in ranked
    ]
    facts = [
        Fact(field="anomaly_score", value=score.score, unit="z", subject=score.subject)
        for score in ranked
    ]

    record = context.record(
        build_record(
            kind=EvidenceKind.METRIC,
            query="list_anomalies",
            parameters={
                "baseline": baseline.canonical(),
                "services": list(arguments.services),
                "minimum_score": arguments.minimum_score,
            },
            window=incident,
            fingerprint=context.backend.fingerprint(),
            rows=rows,
            facts=facts,
        )
    )
    summary = (
        f"{len(rows)} service and metric pairs above z={arguments.minimum_score}: "
        f"{summarise_rows(rows)}"
        if rows
        else f"nothing above z={arguments.minimum_score} in this window"
    )
    return ToolResult(
        tool="list_anomalies",
        summary=summary[:600],
        evidence_id=record.id,
        data={"anomalies": rows},
    )


def query_metric(context: ToolContext, arguments: QueryMetricInput) -> ToolResult:
    """Return the samples of one named metric."""
    window = arguments.window.to_range()
    points = _fetch(context, arguments.metric, window, arguments.services, arguments.limit)
    rows = [
        {
            "timestamp": point.timestamp,
            "service": point.service_name,
            "value": round(point.value, 4),
            **(
                {"status_code": point.labels["status_code"]}
                if "status_code" in point.labels
                else {}
            ),
        }
        for point in points
    ]
    values = [point.value for point in points]
    facts = []
    if values:
        facts = [
            Fact(field="max", value=round(max(values), 4), unit="value"),
            Fact(field="min", value=round(min(values), 4), unit="value"),
            Fact(field="samples", value=float(len(values)), unit="count"),
        ]

    record = context.record(
        build_record(
            kind=EvidenceKind.METRIC,
            query=str(arguments.metric),
            parameters={"services": list(arguments.services), "limit": arguments.limit},
            window=window,
            fingerprint=context.backend.fingerprint(),
            rows=rows,
            facts=facts,
        )
    )
    summary = f"{len(rows)} samples of {arguments.metric}" + (
        f", peak {max(values):.4g}" if values else ", none found"
    )
    return ToolResult(
        tool="query_metric",
        summary=summary[:600],
        evidence_id=record.id,
        data={"samples": rows[:20], "sample_count": len(rows)},
        truncated=record.truncated,
    )


def compare_windows(context: ToolContext, arguments: CompareWindowsInput) -> ToolResult:
    """Compare one service's metric before and during, with an effect size."""
    baseline = arguments.baseline.to_range()
    incident = arguments.incident.to_range()
    services = (arguments.service,)

    base_points = _fetch(context, arguments.metric, baseline, services, MAX_SAMPLES_PER_SERIES)
    inc_points = _fetch(context, arguments.metric, incident, services, MAX_SAMPLES_PER_SERIES)
    score = score_series(
        subject=arguments.service,
        metric=str(arguments.metric),
        baseline=[p.value for p in base_points],
        incident=[p.value for p in inc_points],
        direction=ANOMALY_DIRECTIONS.get(arguments.metric, Direction.EITHER),
    )

    rows = [
        {
            "service": arguments.service,
            "metric": str(arguments.metric),
            "baseline_median": round(score.baseline_median, 4),
            "incident_median": round(score.incident_median, 4),
            "score": score.score,
            "trustworthy": score.is_trustworthy,
        }
    ]
    facts = [
        Fact(
            field="baseline_median",
            value=round(score.baseline_median, 4),
            unit="value",
            subject=arguments.service,
        ),
        Fact(
            field="incident_median",
            value=round(score.incident_median, 4),
            unit="value",
            subject=arguments.service,
        ),
        Fact(field="anomaly_score", value=score.score, unit="z", subject=arguments.service),
    ]

    record = context.record(
        build_record(
            kind=EvidenceKind.METRIC,
            query=f"compare_windows:{arguments.metric}",
            parameters={"service": arguments.service, "baseline": baseline.canonical()},
            window=incident,
            fingerprint=context.backend.fingerprint(),
            rows=rows,
            facts=facts,
        )
    )
    verdict = "significant" if score.score >= 3.0 else "not significant"
    caveat = "" if score.is_trustworthy else ", too few baseline samples to trust"
    summary = (
        f"{arguments.service} {arguments.metric}: "
        f"{score.baseline_median:.4g} to {score.incident_median:.4g}, "
        f"z={score.score:.2f} ({verdict}{caveat})"
    )
    return ToolResult(
        tool="compare_windows",
        summary=summary[:600],
        evidence_id=record.id,
        data={"comparison": rows[0]},
    )


LIST_ANOMALIES = ToolSpec(
    name="list_anomalies",
    description=(
        "Rank every service by how far its metrics depart from a baseline window, "
        "using a median and MAD z-score that extreme values cannot inflate. Use this "
        "first, before forming any hypothesis: it answers where to look across the "
        "whole system in one call, without needing a service name. Covers error rates, "
        "latency percentiles, dependency edge failures and container resources, so it "
        "finds resource faults that show no errors at all. Do not use it to confirm a "
        "hypothesis about one service, which is what compare_windows is for, and do "
        "not use it to read individual samples, which is query_metric."
    ),
    input_model=ListAnomaliesInput,
    handler=list_anomalies,
)

QUERY_METRIC = ToolSpec(
    name="query_metric",
    description=(
        "Return the individual samples of one named metric for a window, optionally "
        "filtered to some services. Use this when a claim needs the actual numbers, "
        f"for example to state a peak value. The metric must be one of: "
        f"{', '.join(metric_names())}. There is no free-form query language on purpose: "
        "a query that can be anything cannot be re-run by a verifier. Prefer "
        "compare_windows when the question is whether something changed, since this "
        "returns samples without a baseline to judge them against."
    ),
    input_model=QueryMetricInput,
    handler=query_metric,
)

COMPARE_WINDOWS = ToolSpec(
    name="compare_windows",
    description=(
        "Compare one service's metric between a baseline window and the incident "
        "window, returning both medians and a robust z-score. Use this to test a "
        "specific hypothesis, for example whether checkout latency actually rose. The "
        "score says how far the change is beyond normal variation, and the result says "
        "when there was too little baseline data to judge, which is a real answer and "
        "not a failure. Use list_anomalies instead when you do not yet know which "
        "service to ask about."
    ),
    input_model=CompareWindowsInput,
    handler=compare_windows,
)

METRIC_TOOLS = (LIST_ANOMALIES, QUERY_METRIC, COMPARE_WINDOWS)
