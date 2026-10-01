"""Baseline B1: one agent, all tools, one strong model, no gates, no ranking.

SPEC.md Section 9.5. The question it answers is whether the harness is worth
its complexity, and it can only answer that if it is a fair implementation of
the obvious alternative rather than a strawman. So B1 gets:

- every tool Firebreak has, with the same caps and the same evidence recording
- the strong tier on every call, which is the most any configuration spends
  per call
- the same budget limits, so a comparison is at equal cost ceilings
- the same windows, chosen from the manifest the same way

and it does not get:

- the dependency graph ranking, or any tool that reads the graph, which is what
  makes A2 and B1 different questions
- the exit gate, the critic, the hypothesis board, or the specialist briefs

**Why no graph tools rather than just no ranking.** SPEC.md Section 9.5 says "no
graph ranking" for B1 and "no ranking, no dependency tools" for A2. Leaving the
dependency tools in while removing the ranking would make B1 a weaker A2 rather
than a single-agent baseline, and the two rows would no longer answer different
questions. The tool list is the difference, stated once, here.

**One loop, not a graph.** The model is shown the tools it has, the transcript
of what it has asked so far, and asked for the next call. That is the shape of
the agent most teams would write first, and the point of the comparison.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from firebreak.agent.budget import BudgetLimits, BudgetState, StopReason
from firebreak.agent.gates import GateOutcome, run_exit_gate
from firebreak.agent.llm import LlmClient, LlmError, Tier, configured_client
from firebreak.agent.reexecute import re_execute
from firebreak.agent.state import Claim, Confidence, Notebook, Report
from firebreak.backends.bundle_duckdb import BundleBackend
from firebreak.lab.bundle import BundleReader
from firebreak.tools.base import ToolContext, ToolError, ToolRegistry
from firebreak.tools.registry import GRAPH_TOOLS, build_registry
from firebreak.triage.pipeline import choose_windows
from firebreak.triage.thresholds import load_thresholds

# Every tool except the ones that read the dependency graph. The list lives in
# `firebreak.tools.registry` next to the specialist allowlists, because ablation
# A2 removes the same tools and two copies is how B1 and A2 would quietly become
# the same configuration.

# How much of the transcript the model sees. A single agent with no notebook has
# nothing but its transcript, so trimming it is what makes long runs possible at
# all, and the cap is generous because this baseline is meant to be fair.
MAX_TRANSCRIPT_ENTRIES = 40


class NextCall(BaseModel):
    """What the agent wants to do next."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Empty means the agent is finished and wants to write its report. A closed
    # vocabulary would be better, but the tool set is configuration, so the name
    # is validated against the registry instead and a bad one is an observation
    # rather than a crash.
    tool: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=400)


class ReactReport(BaseModel):
    """The report the single agent writes, with no gate behind it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root_cause_service: str | None
    confidence: Confidence
    claims: tuple[Claim, ...] = ()


@dataclass
class ReactResult:
    """What one B1 or B2 run produced."""

    report: Report
    evidence: dict[str, Any]
    budget: BudgetState
    stopped_because: StopReason
    steps: int
    notes: list[str] = field(default_factory=list)
    wall_clock_seconds: float = 0.0
    # Set for B2 and None for B1, which is the whole difference between them.
    gate: GateOutcome | None = None


def notebook_from(report: Report) -> Notebook:
    """The notebook B2's gate needs, built from what B1 actually produced.

    Delegates to `Notebook.from_report`, which the deterministic floor needs for
    the same reason. Kept as a name here because B2's behaviour is what it
    documents, and because a second implementation is how the two would drift.
    """
    return Notebook.from_report(report)


def react_tools(registry: ToolRegistry) -> tuple[str, ...]:
    """Every tool this baseline may call.

    Raises rather than silently skipping an unknown graph tool name: if a graph
    tool is renamed, this baseline would quietly gain it, and the B1 and A2 rows
    would stop answering different questions.
    """
    unknown = [name for name in GRAPH_TOOLS if name not in registry]
    if unknown:
        raise ToolError(
            f"B1 excludes graph tools {', '.join(unknown)}, which the registry does "
            f"not have; it has {', '.join(registry.names())}"
        )
    return tuple(name for name in registry.names() if name not in GRAPH_TOOLS)


def investigate_react(
    bundle_dir: Path,
    llm: LlmClient | None = None,
    limits: BudgetLimits | None = None,
    verify: bool = False,
    gate: bool = False,
) -> ReactResult:
    """Run B1 over one recorded bundle, or B2 when `gate` is set.

    One function for both, because B2 is defined as B1 plus the exit gate and
    writing it twice would let the two drift into being different agents. The
    gate runs inside the backend context, since check 2 re-runs the cited
    evidence and needs the backend still open.
    """
    started = time.monotonic()
    client = llm or configured_client(react_stub_handlers())
    budget = BudgetState(limits=limits or BudgetLimits())
    notes: list[str] = []

    reader = BundleReader(bundle_dir, verify=verify)
    baseline, incident, anchored = choose_windows(reader.manifest)

    with BundleBackend(reader) as backend:
        tools = ToolContext(backend=backend)
        full = build_registry()
        registry = full.subset(react_tools(full))
        transcript: list[dict[str, Any]] = []
        steps = 0
        stop: StopReason | None = None

        while stop is None:
            payload = {
                "incident_id": reader.manifest.bundle_id,
                "window": {
                    "start": incident.start.isoformat(),
                    "end": incident.end.isoformat(),
                },
                "baseline": {
                    "start": baseline.start.isoformat(),
                    "end": baseline.end.isoformat(),
                },
                "window_anchored_on_alert": anchored,
                "tools": registry.describe(),
                "transcript": transcript[-MAX_TRANSCRIPT_ENTRIES:],
                "step": steps,
            }
            try:
                decision, completion = client.complete(
                    "react_step", payload, NextCall, tier=Tier.STRONG
                )
            except LlmError as error:
                notes.append(f"the model could not choose a next step: {error}")
                stop = StopReason.COMPLETE
                break
            budget.note_tokens(completion.tokens_in, completion.tokens_out, completion.usd)

            if not decision.tool:
                stop = StopReason.COMPLETE
                break

            observation = _call(registry, tools, budget, decision)
            transcript.append(observation)
            steps += 1
            budget.end_round(len(tools.evidence))
            budget.elapsed_seconds = time.monotonic() - started
            stop = stop or budget.stop_reason()

        report = _write_report(client, reader.manifest.bundle_id, transcript, budget, stop, notes)
        evidence = {
            evidence_id: record
            for evidence_id in tools.evidence.ids()
            if (record := tools.evidence.get(evidence_id)) is not None
        }

        outcome: GateOutcome | None = None
        if gate:
            rerun = re_execute(
                registry, ToolContext(backend=backend), evidence, report.cited_evidence
            )
            notes.extend(rerun.notes)
            outcome = run_exit_gate(
                report,
                notebook_from(report),
                evidence,
                rerun.records,
                min_support=load_thresholds().abstention.minimum_hypothesis_support,
            )
            report = outcome.report
            if outcome.notice is not None:
                report = report.model_copy(update={"notes": (*report.notes, outcome.notice)})

    return ReactResult(
        report=report,
        evidence=evidence,
        budget=budget,
        stopped_because=stop,
        steps=steps,
        notes=notes,
        wall_clock_seconds=time.monotonic() - started,
        gate=outcome,
    )


def _call(
    registry: ToolRegistry,
    tools: ToolContext,
    budget: BudgetState,
    decision: NextCall,
) -> dict[str, Any]:
    """Run one tool call and return what the agent gets to see.

    A refused or failing call is an observation rather than an exception. A
    single agent that crashed on its first bad argument would be a strawman, and
    the failure is information: it is how the agent learns the tool's shape.
    """
    fingerprint = f"{decision.tool}:{sorted(decision.arguments.items())}"
    if budget.is_repetitive(fingerprint):
        return {
            "tool": decision.tool,
            "error": "this exact call has already been made; the answer will not change",
        }
    budget.note_tool_call(fingerprint)
    try:
        result = registry.call(decision.tool, tools, decision.arguments)
    except ToolError as error:
        return {"tool": decision.tool, "error": str(error)}
    return {
        "tool": decision.tool,
        "summary": result.summary,
        "evidence_id": result.evidence_id,
        "data": result.data,
    }


def _write_report(
    client: LlmClient,
    bundle_id: str,
    transcript: list[dict[str, Any]],
    budget: BudgetState,
    stop: StopReason,
    notes: list[str],
) -> Report:
    """Ask for the report, and publish whatever comes back.

    No gate, by definition: B1 is the configuration that shows what the gate is
    worth, so anything that checked the claims here would be measuring B2.
    """
    payload = {
        "incident_id": bundle_id,
        "transcript": transcript[-MAX_TRANSCRIPT_ENTRIES:],
        "stopped_because": stop.value,
    }
    try:
        written, completion = client.complete(
            "react_report", payload, ReactReport, tier=Tier.STRONG
        )
    except LlmError as error:
        notes.append(f"the model could not write a report: {error}")
        return Report(
            incident_id=bundle_id,
            root_cause_service=None,
            stopped_because=stop,
            notes=("The model could not produce a report.",),
        )
    budget.note_tokens(completion.tokens_in, completion.tokens_out, completion.usd)
    confidence = written.confidence
    if stop.lowers_confidence:
        confidence = Confidence.LOW
    return Report(
        incident_id=bundle_id,
        root_cause_service=written.root_cause_service,
        confidence=confidence,
        claims=written.claims,
        stopped_because=stop,
    )


def react_stub_handlers() -> dict[str, Any]:
    """Deterministic stand-ins for the two questions B1 asks a model.

    Not mocks. Each reads the payload it is given and answers from it, the same
    way `firebreak.agent.llm` describes: that is what makes a stub run a real
    test of the loop, the caps and the budget rather than a test of a fixture.

    The plan encoded here is the obvious one, which is the point of a baseline:
    find the anomalous services, then look at the logs and traces of the loudest
    one, then report it. It is deliberately not the graph walk Firebreak does,
    because B1 exists to show what that walk is worth.
    """

    def step(payload: dict[str, Any]) -> dict[str, Any]:
        transcript = payload.get("transcript") or []
        window = payload["window"]
        baseline = payload["baseline"]
        called = [entry.get("tool") for entry in transcript]

        if "list_anomalies" not in called:
            return {
                "tool": "list_anomalies",
                "arguments": {"window": window, "baseline": baseline},
                "reason": "find out which services are unusual at all",
            }

        service = _loudest(transcript)
        if service is None:
            # Nothing stood out. A single agent has no ranking to fall back on,
            # so it stops rather than picking a service at random.
            return {"tool": "", "reason": "no service looked unusual"}

        if "search_logs" not in called:
            return {
                "tool": "search_logs",
                "arguments": {"window": window, "services": [service]},
                "reason": f"read {service}'s errors",
            }
        if "find_traces" not in called:
            return {
                "tool": "find_traces",
                "arguments": {"window": window, "services": [service], "status": "error"},
                "reason": f"see whether {service}'s failures show up in traces",
            }
        return {"tool": "", "reason": "enough gathered to report"}

    def report(payload: dict[str, Any]) -> dict[str, Any]:
        transcript = payload.get("transcript") or []
        service = _loudest(transcript)
        cited = [
            entry["evidence_id"] for entry in transcript if entry.get("evidence_id") is not None
        ]
        if service is None:
            return {
                "root_cause_service": None,
                "confidence": "low",
                "claims": [],
            }
        claims = [
            {
                "text": f"{service} is the root cause.",
                "claim_type": "mechanism",
                "evidence_ids": cited,
            }
        ]
        return {
            "root_cause_service": service,
            "confidence": "medium" if len(cited) > 1 else "low",
            "claims": claims,
        }

    return {"react_step": step, "react_report": report}


def _loudest(transcript: list[dict[str, Any]]) -> str | None:
    """The worst scoring service the anomaly call returned, if any.

    Reads the tool's own `data`, so a change to what `list_anomalies` reports
    changes what this stub concludes, rather than the two drifting apart.
    """
    for entry in transcript:
        if entry.get("tool") != "list_anomalies":
            continue
        anomalies = (entry.get("data") or {}).get("anomalies") or []
        if anomalies:
            worst = max(anomalies, key=lambda row: row.get("score", 0.0))
            return str(worst["service"])
    return None
