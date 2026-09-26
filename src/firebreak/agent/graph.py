"""The investigation loop, and the stub that lets it run without a model.

SPEC.md Section 6.6 describes the graph LangGraph builds. This module runs the
same sequence directly rather than through `StateGraph`, and the reason is
worth stating because ADR-0001 chose LangGraph and this does not yet use it.

**Why the loop is plain Python in v1.** LangGraph earns its place through
durable checkpoints and interrupts, which is what SPEC.md Section 6.10's
approval step needs in Phase 9. None of that is exercised yet: v1 runs a fixed
sequence with one conditional edge, a shape that gains nothing from a graph
framework and loses the ability to be read top to bottom. Wiring it into
`StateGraph` before the interrupt exists would be adopting the cost of the
abstraction ahead of its benefit.

The node functions in `nodes.py` already have the signature LangGraph wants, so
Phase 9 wires them up rather than rewriting them. ADR-0008 records this.

**The stub is a real implementation, not a mock.** It reads each payload and
answers from what is in it, so an integration test in stub mode exercises the
graph's wiring, its budgets, its repetition limits and its exit gate for real.
It is also the deterministic floor SPEC.md Section 6.7 requires when every
model tier fails.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from firebreak.agent.budget import BudgetLimits, BudgetState, StopReason
from firebreak.agent.llm import LlmClient
from firebreak.agent.nodes import (
    NodeContext,
    commander,
    critic,
    entry_gate,
    exit_gate,
    hypothesis_board,
    reporter,
    run_specialist,
    seed_hypotheses,
    should_continue,
)
from firebreak.agent.state import (
    Alert,
    Claim,
    Finding,
    InvestigationState,
    Report,
    Status,
)
from firebreak.backends.bundle_duckdb import BundleBackend
from firebreak.lab.bundle import BundleReader
from firebreak.settings import LlmMode
from firebreak.tools.base import ToolContext
from firebreak.tools.registry import ALL_TOOLS, build_registry
from firebreak.triage.pipeline import triage_bundle

# Every specialist runs every round in v1. SPEC.md Section 6.6 fans them out in
# parallel; they are run in sequence here because each one's cost is a tool call
# against a local file, and concurrency would buy nothing while making the
# budget accounting racy.
SPECIALISTS = ("metrics_analyst", "logs_analyst", "traces_analyst", "change_analyst")


def stub_handlers() -> dict[str, Any]:
    """Deterministic answers, derived from each payload.

    Not canned responses. Each handler reads what it was given and answers from
    it, which is what makes a stub-mode run a real exercise of the graph.
    """

    def commander_plan(payload: dict[str, Any]) -> dict[str, Any]:
        """Assign every specialist to the leading hypothesis.

        A real commander would spread its specialists across hypotheses. The
        stub concentrates them, which is the behaviour that most exercises the
        repetition limit and the stall detector, and those are what a stub-mode
        integration test exists to cover.
        """
        hypotheses = payload.get("hypotheses") or []
        if not hypotheses:
            return {"hypotheses": [], "assignments": []}
        leader = hypotheses[0]
        return {
            "hypotheses": [],
            "assignments": [
                [specialist, leader["id"], f"Does the evidence implicate {leader['service']}?"]
                for specialist in SPECIALISTS
            ],
        }

    def specialist_answer(payload: dict[str, Any]) -> dict[str, Any]:
        """Support the hypothesis when the evidence mentions its service.

        A crude rule, and honest about being one: it reads the tool summaries
        it was handed and looks for the service under suspicion. That is enough
        to make the hypothesis board, the critic and the exit gate do real work
        on real evidence ids.
        """
        service = str(payload.get("service", ""))
        summaries = " ".join(str(e.get("summary", "")) for e in payload.get("evidence", []))
        mentioned = service and service in summaries
        verdict = "signal for" if mentioned else "nothing implicating"
        return {
            "summary": (
                f"{payload.get('specialist')} found {verdict} {service} in "
                f"{len(payload.get('evidence', []))} evidence record(s)."
            )[:600],
            "supports": bool(mentioned),
            "confidence": "medium" if mentioned else "low",
        }

    def critic_verdict(payload: dict[str, Any]) -> dict[str, Any]:
        """Object while a credible alternative still has support.

        The stub's objection is structural rather than reasoned: if another
        hypothesis has support at least as high as the leader's, the leader is
        not established. That is a real objection and it keeps the loop running
        exactly when it should.
        """
        alternatives = payload.get("alternatives") or []
        supporting = len(payload.get("supporting_evidence") or [])
        rival = max((int(a.get("support", 0)) for a in alternatives), default=0)
        if rival >= supporting and alternatives:
            return {
                "objection": (
                    f"An alternative still carries support {rival} against this "
                    f"hypothesis's {supporting}; the evidence does not separate them."
                ),
                "requested_checks": ["compare the two services over the same window"],
                "convinced": False,
            }
        return {"objection": "", "requested_checks": [], "convinced": True}

    def report(payload: dict[str, Any]) -> dict[str, Any]:
        """Write one claim per finding, each citing that finding's evidence.

        Every claim carries the ids it came from, so the exit gate has
        something real to check rather than prose invented to look cited.
        """
        leader = payload.get("leader")
        findings = payload.get("findings") or []
        claims = [
            {
                "text": str(f.get("summary", ""))[:600],
                "evidence_ids": list(f.get("evidence_ids") or []),
            }
            for f in findings
        ]
        if leader is None:
            return {
                "root_cause_service": None,
                "confidence": "low",
                "claims": [
                    {
                        "text": "No service could be established as the root cause.",
                        "evidence_ids": [],
                    }
                ],
            }
        supporting = len(leader.get("supporting_evidence") or [])
        unresolved = payload.get("unresolved_objections") or []
        claims.insert(
            0,
            {
                "text": (
                    f"{leader['service']} is the most likely root cause, with "
                    f"{supporting} piece(s) of supporting evidence."
                ),
                "evidence_ids": list(leader.get("supporting_evidence") or []),
            },
        )
        confidence = "low" if unresolved or supporting < 2 else "medium"
        return {
            "root_cause_service": leader["service"],
            "confidence": confidence,
            "claims": claims,
        }

    return {
        "commander": commander_plan,
        "specialist": specialist_answer,
        "critic": critic_verdict,
        "reporter": report,
    }


@dataclass
class InvestigationResult:
    """What one run of the graph produced."""

    state: InvestigationState
    report: Report
    stopped_because: StopReason
    rounds: int
    notes: list[str]
    wall_clock_seconds: float


def investigate(
    bundle_dir: Path,
    llm: LlmClient | None = None,
    limits: BudgetLimits | None = None,
    verify: bool = False,
) -> InvestigationResult:
    """Run one investigation over one recorded bundle.

    The same entry point for every mode. `stub` needs no configuration, which
    is why it is the default: an integration test, the offline demo and the
    deterministic floor all reach the graph the same way.
    """
    started = time.monotonic()
    client = llm or LlmClient(mode=LlmMode.STUB, stub_handlers=stub_handlers())

    triage = triage_bundle(bundle_dir, verify=verify)
    reader = BundleReader(bundle_dir, verify=False)

    state = InvestigationState(
        incident_id=triage.bundle_id,
        alert=Alert(fired_at=reader.manifest.alert_fired_at),
        window_start=triage.incident.start,
        window_end=triage.incident.end,
        baseline_start=triage.baseline.start,
        baseline_end=triage.baseline.end,
        triage=triage,
        budget=BudgetState(limits=limits or BudgetLimits()),
    )

    with BundleBackend(reader) as backend:
        tools = ToolContext(backend=backend)
        context = NodeContext(llm=client, registry=build_registry(ALL_TOOLS), tools=tools)

        state = entry_gate(state)
        if state.status is Status.FAILED:
            return _finish(state, state.report, StopReason.COMPLETE, started, context)

        state = seed_hypotheses(state)
        if state.status is Status.ABSTAINED:
            return _finish(state, _abstention_report(state), StopReason.COMPLETE, started, context)

        stop: StopReason | None = None
        while stop is None:
            plan = commander(state, context)
            findings: list[Finding] = []
            for specialist, hypothesis_id, question in plan.assignments:
                hypothesis = state.notebook.by_id(hypothesis_id)
                if hypothesis is None:
                    context.notes.append(f"commander named unknown hypothesis {hypothesis_id}")
                    continue
                finding = run_specialist(state, context, specialist, hypothesis, question)
                if finding is not None:
                    findings.append(finding)

            state = hypothesis_board(state, findings)
            state = state.model_copy(update={"evidence": _collected(context)})

            objection = critic(state, context)
            if objection is not None:
                state = state.model_copy(update={"critiques": (*state.critiques, objection)})

            state.budget.end_round(len(state.evidence))
            stop = should_continue(state)

        state = state.model_copy(update={"status": Status.REPORTING, "stopped_because": stop})
        written = reporter(state, context)
        final = exit_gate(state, written)
        return _finish(state, final, stop, started, context)


def _collected(context: NodeContext) -> dict[str, Any]:
    """Every evidence record gathered so far, keyed by id."""
    return {
        evidence_id: record
        for evidence_id in context.tools.evidence.ids()
        if (record := context.tools.evidence.get(evidence_id)) is not None
    }


def _abstention_report(state: InvestigationState) -> Report:
    """The report for an incident triage found nothing in.

    Written in code rather than by the reporter, because there is nothing to
    report and a model asked to write about nothing writes something.
    """
    triage = state.triage
    detail = ""
    if triage is not None:
        detail = (
            f" The largest anomaly anywhere was {triage.top_anomaly:.1f} robust "
            f"deviations, below the {triage.abstention_threshold:.1f} threshold."
        )
    return Report(
        incident_id=state.incident_id,
        root_cause_service=None,
        claims=(Claim(text=f"No service was unusual enough to investigate.{detail}"),),
    )


def _finish(
    state: InvestigationState,
    report: Report | None,
    stop: StopReason,
    started: float,
    context: NodeContext,
) -> InvestigationResult:
    final = report or Report(incident_id=state.incident_id, root_cause_service=None)
    status = Status.ABSTAINED if final.abstained else Status.COMPLETE
    return InvestigationResult(
        state=state.model_copy(update={"report": final, "status": status}),
        report=final,
        stopped_because=stop,
        rounds=state.budget.rounds,
        notes=list(context.notes),
        wall_clock_seconds=time.monotonic() - started,
    )
