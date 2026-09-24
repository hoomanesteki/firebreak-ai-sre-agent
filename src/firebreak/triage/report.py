"""Baseline B0: a written incident report with no model involved at all.

SPEC.md Section 9.5 defines B0 as deterministic triage with a template
report, and it answers the question that ought to be asked first: how far
does classic AIOps get before any of this needs a language model. Principle
H1 says to start with the simplest thing that works, and that only means
anything if the simplest thing is actually built and actually measured.

B0 has a second job. SPEC.md Section 7 calls it the deterministic floor: if
every model tier fails or the budget runs out before a report exists,
Firebreak publishes this instead, labelled as automated triage with no AI
analysis. An on call engineer gets something useful rather than an apology.

**Why it goes through the tool registry.** The scores could be computed
directly from the backend, and the triage pipeline does exactly that. But a
baseline whose claims cannot be checked the way the agent's claims are
checked is not a fair comparison: it would be measured by a different
standard, and any later "the agent beats B0" would partly be measuring which
of the two had to show its work. So every number quoted below arrives through
`ToolRegistry.call`, with the same caps, the same validation and the same
evidence records, and the exit gate can re-run a B0 citation exactly as it
re-runs the agent's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from firebreak.backends.bundle_duckdb import BundleBackend
from firebreak.graph.ranking import Candidate
from firebreak.lab.bundle import BundleReader
from firebreak.tools.base import ToolContext, ToolError, ToolRegistry, ToolResult
from firebreak.tools.evidence import EvidenceRecord
from firebreak.tools.registry import ALL_TOOLS, build_registry
from firebreak.triage.pipeline import TriageResult, triage_bundle

# The label SPEC.md Section 7 requires on the deterministic floor. An
# operator has to be able to tell at a glance that nothing reasoned about
# this, because the report reads like prose either way.
NO_AI_LABEL = "Automated triage only, no AI analysis."

# How many suspects the report names. Two is enough to show the runner up and
# how far behind it was, which is the part that tells a reader whether the
# ranking was confident or nearly a coin toss.
NAMED_CANDIDATES = 2


@dataclass(frozen=True)
class ReportSection:
    """One section of the report, with the evidence its claims rest on."""

    heading: str
    body: str
    evidence_ids: tuple[str, ...] = ()


@dataclass
class B0Report:
    """A deterministic incident report."""

    bundle_id: str
    sections: list[ReportSection] = field(default_factory=list)
    evidence: dict[str, EvidenceRecord] = field(default_factory=dict)
    triage: TriageResult | None = None
    tool_calls: int = 0

    @property
    def abstained(self) -> bool:
        return self.triage is not None and self.triage.says_nothing_is_wrong

    @property
    def named_service(self) -> str | None:
        """The service this report blames, or None when it blames nothing."""
        return self.triage.top_service if self.triage else None

    @property
    def cited_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for section in self.sections:
            for evidence_id in section.evidence_ids:
                if evidence_id not in seen:
                    seen.append(evidence_id)
        return tuple(seen)

    def to_markdown(self) -> str:
        """Render the report, with citations kept next to their claims."""
        lines = [f"# Incident {self.bundle_id}", "", f"_{NO_AI_LABEL}_", ""]
        for section in self.sections:
            lines.append(f"## {section.heading}")
            lines.append("")
            lines.append(section.body)
            if section.evidence_ids:
                lines.append("")
                lines.append(f"Evidence: {', '.join(section.evidence_ids)}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _describe_candidates(candidates: list[Candidate]) -> str:
    """Name the leading suspects and say how far apart they were.

    The gap is reported rather than just the order, because a first place
    that beat second place by a rounding error and one that beat it tenfold
    are different findings, and an operator deciding whether to trust this
    needs to know which they are looking at.
    """
    if not candidates:
        return "No service could be ranked, because no edge or metric was available."

    top = candidates[0]
    parts = [
        f"`{top.service}` ranks first, with a graph score of {top.graph_score:.4f} "
        f"and a worst metric anomaly of {top.anomaly_score:.1f} robust deviations."
    ]
    runner_up = candidates[1] if len(candidates) > 1 else None
    if runner_up is not None:
        if runner_up.graph_score > 0:
            ratio = top.graph_score / runner_up.graph_score
            margin = f"{ratio:.1f} times the score of"
        else:
            margin = "an unmeasurable margin over"
        parts.append(
            f"That is {margin} `{runner_up.service}`, which ranked second at "
            f"{runner_up.graph_score:.4f}."
        )
        # The interesting disagreement. A service the metrics shouted about
        # that the graph demoted is the shape of an upstream symptom, and
        # saying so is most of the value a dependency graph adds.
        louder = [
            candidate
            for candidate in candidates[1:NAMED_CANDIDATES]
            if candidate.anomaly_score > top.anomaly_score
        ]
        if louder:
            names = ", ".join(f"`{candidate.service}`" for candidate in louder)
            parts.append(
                f"{names} showed a larger metric change but ranks lower, which is what "
                "an upstream symptom looks like: it is unwell because something it "
                "depends on is unwell."
            )
    return " ".join(parts)


def _onset_sentence(triage: TriageResult) -> str:
    """Say what the onset times show, including when they show nothing.

    This section used to claim an origin unconditionally, and on a recording
    where every service crossed the threshold in the same sample it would
    name whichever sorted first and call it the likely origin. That is a
    sentence arguing against the report's own conclusion, produced by a tie.

    The incident window often opens after the fault has already taken hold,
    because without an alert it is a fixed split of the recording, so ties
    are common rather than exotic: onset ordering survived in about 70
    percent of trials at the measured split
    (`reports/triage/ranking_ablation.json`, baseline_fraction_sweep).
    """
    if not triage.onsets:
        return "No service crossed the onset threshold, so nothing can be ordered in time."

    ordered = sorted(triage.onsets.items(), key=lambda item: (item[1], item[0]))
    if len({at for _, at in ordered}) == 1:
        return (
            f"All {len(ordered)} affected services crossed the threshold in the same sample, "
            "so onset gives no ordering here. The incident was already under way when the "
            "comparison window opened, which is what happens when no alert marks the start."
        )

    first, at = ordered[0]
    sentence = f"`{first}` went abnormal first, {at:.0f} seconds into the incident window."
    if len(ordered) > 1:
        second, then = ordered[1]
        sentence += f" `{second}` followed at {then:.0f} seconds."
    sentence += (
        " Causes precede symptoms, so an earlier onset is weak evidence of origin. It is one "
        "signal among several and does not on its own outrank the dependency ranking above."
    )
    return sentence


def build_b0_report(bundle_dir: Path, verify: bool = True) -> B0Report:
    """Investigate one bundle deterministically and write it up.

    Every quoted number comes back through a tool call, so every sentence
    that cites an evidence id can be re-run and matched.
    """
    triage = triage_bundle(bundle_dir, verify=verify)
    reader = BundleReader(bundle_dir, verify=False)
    registry = build_registry(ALL_TOOLS)
    report = B0Report(bundle_id=triage.bundle_id, triage=triage)

    with BundleBackend(reader) as backend:
        context = ToolContext(backend=backend)

        anomalies = registry.call(
            "list_anomalies",
            context,
            {
                "window": {
                    "start": triage.incident.start.isoformat(),
                    "end": triage.incident.end.isoformat(),
                },
                "baseline": {
                    "start": triage.baseline.start.isoformat(),
                    "end": triage.baseline.end.isoformat(),
                },
                "minimum_score": 0.0,
            },
        )

        anchor = (
            "an alert that fired during the recording"
            if triage.anchored_on_alert
            else "a fixed split of the recorded window, because no alert fired"
        )
        report.sections.append(
            ReportSection(
                heading="What was looked at",
                body=(
                    f"The incident window was taken from {anchor}. "
                    f"It runs from {triage.incident.start.isoformat()} to "
                    f"{triage.incident.end.isoformat()}, compared against a baseline from "
                    f"{triage.baseline.start.isoformat()} to "
                    f"{triage.baseline.end.isoformat()}."
                ),
                evidence_ids=anomalies.cite(),
            )
        )

        if triage.says_nothing_is_wrong:
            report.sections.append(
                ReportSection(
                    heading="Finding",
                    body=(
                        "No service was unusual enough to report. The largest anomaly anywhere "
                        f"in the system was {triage.top_anomaly:.1f} robust deviations, below "
                        f"the {triage.abstention_threshold:.1f} threshold that separates a real "
                        "incident from ordinary variation. "
                        "No root cause is named, because naming one here would be a guess.\n\n"
                        "This is a deliberate abstention and not a failure to investigate. A "
                        "ranking always returns an order, so a healthy system still produces a "
                        "confident looking first place, and reporting that is how an operator "
                        "learns to ignore the tool."
                    ),
                    evidence_ids=anomalies.cite(),
                )
            )
            report.evidence = _collected(context)
            report.tool_calls = context.total_calls()
            return report

        report.sections.append(
            ReportSection(
                heading="What changed",
                body=anomalies.summary,
                evidence_ids=anomalies.cite(),
            )
        )
        report.sections.append(
            ReportSection(
                heading="Most likely cause",
                body=_describe_candidates(triage.candidates),
            )
        )
        report.sections.append(
            ReportSection(heading="Order of onset", body=_onset_sentence(triage))
        )

        named = triage.top_service
        if named is not None:
            blast = _safe_call(registry, context, "blast_radius", {"service": named})
            if blast is not None:
                report.sections.append(
                    ReportSection(
                        heading="Who is affected",
                        body=blast.summary,
                        evidence_ids=blast.cite(),
                    )
                )
            changes = _safe_call(
                registry,
                context,
                "recent_changes",
                {
                    "window": {
                        "start": triage.baseline.start.isoformat(),
                        "end": triage.incident.end.isoformat(),
                    }
                },
            )
            if changes is not None:
                report.sections.append(
                    ReportSection(
                        heading="Changes in the window",
                        body=(
                            f"{changes.summary}\n\n"
                            "A change near an incident is a coincidence until the same service "
                            "also shows an anomaly. Nothing here asserts that any of these "
                            "caused anything."
                        ),
                        evidence_ids=changes.cite(),
                    )
                )

        report.sections.append(
            ReportSection(
                heading="What this report is not",
                body=(
                    f"{NO_AI_LABEL} Anomaly scoring and a dependency walk ranked the services; "
                    "no log was read for meaning, no trace was followed, and no hypothesis was "
                    "tested against an alternative. Treat the named service as the place to "
                    "start looking, not as a conclusion."
                ),
            )
        )

        report.evidence = _collected(context)
        report.tool_calls = context.total_calls()

    return report


def _collected(context: ToolContext) -> dict[str, EvidenceRecord]:
    """Every record gathered, keyed by id, for a verifier to re-run."""
    return {
        evidence_id: record
        for evidence_id in context.evidence.ids()
        if (record := context.evidence.get(evidence_id)) is not None
    }


def _safe_call(
    registry: ToolRegistry, context: ToolContext, name: str, arguments: dict[str, object]
) -> ToolResult | None:
    """Run a supporting tool, tolerating a bundle that cannot answer it.

    The two calls this wraps are enrichment: blast radius and recent
    changes make the report more useful and neither is the finding. A bundle
    with an empty change log is a legitimate recording, and failing the whole
    report over it would lose the part that matters. The root cause section
    is not wrapped, because a report with no finding is not a report.
    """
    try:
        return registry.call(name, context, arguments)
    except ToolError:
        return None
