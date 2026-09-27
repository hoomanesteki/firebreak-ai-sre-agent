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
from firebreak.agent.floor import FloorReason, build_floor_report
from firebreak.agent.gates import GateOutcome, run_exit_gate
from firebreak.agent.llm import LlmClient, LlmError, Tier
from firebreak.agent.nodes import (
    MIN_SUPPORT_TO_CONCLUDE,
    NodeContext,
    commander,
    consult_memory,
    critic,
    entry_gate,
    hypothesis_board,
    reporter,
    run_specialist,
    seed_hypotheses,
    should_continue,
)
from firebreak.agent.reexecute import re_execute
from firebreak.agent.state import (
    Alert,
    Finding,
    InvestigationState,
    Notebook,
    Report,
    Status,
)
from firebreak.backends.bundle_duckdb import BundleBackend
from firebreak.lab.bundle import BundleReader
from firebreak.settings import LlmMode
from firebreak.telemetry.spans import SpanRecorder, TracingOptions, record_gate
from firebreak.tools.base import ToolContext
from firebreak.tools.registry import ALL_TOOLS, build_registry
from firebreak.triage.pipeline import triage_bundle
from firebreak.triage.thresholds import ThresholdError, load_thresholds

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


@dataclass(frozen=True)
class AgentOptions:
    """Which parts of Firebreak are switched on.

    One object rather than a keyword per ablation. SPEC.md Section 9.5 has six
    ablations and three of them are switches on this graph, with two more coming
    in Phase 10, so the alternative is a function signature nobody reads to the
    end.

    Every ablation is a switch on the one implementation rather than a separate
    graph. Two graphs differing in a critic would drift in something else, and the
    comparison would then measure the drift.
    """

    # A1: FB without the critic. What independent critique is worth [R8].
    use_critic: bool = True
    # A2: FB without the knowledge graph. No dependency ranking and no dependency
    # tools, so the question it answers is the graph's whole contribution.
    use_graph: bool = True
    # A3 and A4: every call at one tier, so the cascade's cost and quality effect
    # can be read against the two extremes.
    tier_override: Tier | None = None
    # A5: FB without incident memory. What learning from past incidents is worth.
    use_memory: bool = True
    # A6: FB with prompts optimized by GEPA, per SPEC.md Section 9.5. Names the prompt
    # version set to run with, so the ablation is "these prompts against those" rather
    # than a boolean nobody can trace to a file. Empty means the latest of each, which
    # is what every other configuration runs.
    prompt_versions: str = ""

    @property
    def name(self) -> str:
        """A short label for a transcript or a report.

        Named from the switches rather than passed in, so a configuration cannot
        be labelled as something it is not.
        """
        if self.tier_override is not None:
            return f"all-{self.tier_override.value}"
        if not self.use_critic:
            return "no-critic"
        if not self.use_graph:
            return "no-graph"
        if not self.use_memory:
            return "no-memory"
        if self.prompt_versions:
            return f"prompts-{self.prompt_versions}"
        return "full"


@dataclass
class InvestigationResult:
    """What one run of the graph produced."""

    state: InvestigationState
    report: Report
    stopped_because: StopReason
    rounds: int
    notes: list[str]
    wall_clock_seconds: float
    # SPEC.md Section 6.9: the gate's results are kept, not just applied. A
    # report that lost four statements and a report that passed cleanly look the
    # same afterwards, and the difference is what an eval measures.
    gate: GateOutcome


def investigate(
    bundle_dir: Path,
    llm: LlmClient | None = None,
    limits: BudgetLimits | None = None,
    verify: bool = False,
    options: AgentOptions | None = None,
    tracing: TracingOptions | None = None,
) -> InvestigationResult:
    """Run one investigation over one recorded bundle.

    The same entry point for every mode. `stub` needs no configuration, which
    is why it is the default: an integration test, the offline demo and the
    deterministic floor all reach the graph the same way.
    """
    started = time.monotonic()
    chosen = options or AgentOptions()
    client = llm or LlmClient(mode=LlmMode.STUB, stub_handlers=stub_handlers())
    if chosen.tier_override is not None:
        # Set on the client the caller passed rather than on a copy of it. A copy
        # applied the ablation to an object the caller could not see, so a caller
        # inspecting its own client saw no override and would conclude the
        # ablation had not applied. A caller passing both a client and a tier
        # override is asking for exactly this.
        client.tier_override = chosen.tier_override

    triage = triage_bundle(bundle_dir, verify=verify, use_graph=chosen.use_graph)
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

    recorder = SpanRecorder(
        options=TracingOptions(
            enabled=tracing is None or tracing.enabled,
            include_content=tracing.include_content if tracing else False,
            configuration=chosen.name,
        )
    )
    with BundleBackend(reader) as backend, recorder.investigation(triage.bundle_id) as root:
        tools = ToolContext(backend=backend)
        context = NodeContext(
            llm=client,
            registry=build_registry(ALL_TOOLS),
            tools=tools,
            allow_graph_tools=chosen.use_graph,
            allow_memory=chosen.use_memory,
            spans=recorder,
        )
        context.root_span = root

        with recorder.node("entry_gate"):
            state = entry_gate(state)
        if state.status is Status.FAILED:
            return _finish(state, state.report, StopReason.COMPLETE, started, context)

        with recorder.node("seed_hypotheses"):
            state = seed_hypotheses(state)
        with recorder.node("consult_memory"):
            state = consult_memory(state, context)
        if state.status is Status.ABSTAINED:
            return _finish(state, _abstention_report(state), StopReason.COMPLETE, started, context)

        try:
            return _investigate_with_models(state, context, started, chosen.use_critic)
        except LlmError as error:
            # SPEC.md Section 6.7's deterministic floor. Every model failing is a
            # bad day, not a bug, and an on-call engineer still gets a ranked list
            # with re-runnable evidence. A stack trace here is how a tool stops
            # being opened.
            context.notes.append(f"falling back to deterministic triage: {error}")
            floor = build_floor_report(bundle_dir, FloorReason.MODELS_UNAVAILABLE, verify=False)
            failed = state.model_copy(update={"stopped_because": StopReason.COMPLETE})
            return _finish(
                failed,
                floor.report,
                StopReason.COMPLETE,
                started,
                context,
                evidence=dict(floor.b0.evidence),
                notebook=Notebook.from_report(floor.report),
            )


def _investigate_with_models(
    state: InvestigationState,
    context: NodeContext,
    started: float,
    use_critic: bool,
) -> InvestigationResult:
    """The part of an investigation that needs a model to answer.

    Split out so `investigate` has one place to catch a model failure and publish
    the deterministic floor instead. Inlined, the try block would have to wrap the
    backend context too, and the floor would then be built with nothing open to
    re-run its evidence against.
    """
    stop: StopReason | None = None
    while stop is None:
        # A span per node, which is the third of the three kinds SPEC.md Section 17
        # Phase 11 asks for. Wrapped here rather than inside each node function, so a
        # node added later is traced by being called rather than by remembering to.
        with context.spans.node("commander"):
            plan = commander(state, context)
        findings: list[Finding] = []
        for specialist, hypothesis_id, question in plan.assignments:
            hypothesis = state.notebook.by_id(hypothesis_id)
            if hypothesis is None:
                context.notes.append(f"commander named unknown hypothesis {hypothesis_id}")
                continue
            with context.spans.node(specialist):
                finding = run_specialist(state, context, specialist, hypothesis, question)
            if finding is not None:
                findings.append(finding)

        with context.spans.node("hypothesis_board"):
            state = hypothesis_board(state, findings)
        state = state.model_copy(update={"evidence": _collected(context)})

        # Ablation A1, SPEC.md Section 9.5: FB without the critic, to find out
        # what independent critique is worth [R8]. A switch rather than a separate
        # graph, so the two configurations cannot drift apart in anything but the
        # critic.
        if use_critic:
            with context.spans.node("critic"):
                objection = critic(state, context)
            if objection is not None:
                state = state.model_copy(update={"critiques": (*state.critiques, objection)})

        state.budget.end_round(len(state.evidence))
        stop = should_continue(state)

    state = state.model_copy(update={"status": Status.REPORTING, "stopped_because": stop})
    with context.spans.node("reporter"):
        written = reporter(state, context)
    return _finish(state, written, stop, started, context)


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

    The explanation is a note rather than a claim. It describes what the system
    did and why it stopped, the numbers come from the deterministic triage result
    rather than from a model, and there is no tool call to cite for them: triage
    runs before the registry exists. A claim would fail check 1 and be removed,
    which would leave an abstention with no stated reason.
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
        notes=(f"No service was unusual enough to investigate.{detail}",),
    )


def _finish(
    state: InvestigationState,
    report: Report | None,
    stop: StopReason,
    started: float,
    context: NodeContext,
    evidence: dict[str, Any] | None = None,
    notebook: Notebook | None = None,
) -> InvestigationResult:
    """Run the exit gate and return the published report.

    Every route out of `investigate` comes through here, which is what SPEC.md Section 17
    Phase 7's "the gate cannot be bypassed" means in code. An earlier version called the
    gate once, at the end of the main loop, so the two short-circuit paths returned
    reports nothing had checked.
    """
    written = report or Report(incident_id=state.incident_id, root_cause_service=None)
    # The floor passes B0's evidence store, because the floor's claims cite B0's records
    # and the graph gathered none. Defaulting to the graph's store would make the gate
    # strip every claim in a floor report for citing nothing it knows, which is the
    # opposite of what the floor is for.
    gathered = evidence if evidence is not None else _collected(context)

    # Re-run each cited record against the same backend, through a fresh tool context so
    # verification does not spend the investigation's per-tool caps.
    rerun = re_execute(
        context.registry,
        ToolContext(backend=context.tools.backend),
        gathered,
        written.cited_evidence,
    )
    context.notes.extend(rerun.notes)

    # The floor passes the notebook its own report implies, and passes it explicitly
    # rather than being detected here. The graph's notebook is not empty on that path:
    # `seed_hypotheses` filled it from triage with hypotheses no specialist ever
    # supported, so the gate's abstention check saw support of zero and stripped the named
    # service, which is the one thing the floor exists to deliver. Guessing from whether
    # the notebook looked empty would have got that wrong silently, which is why the
    # caller says.
    with context.spans.node("exit_gate"):
        outcome = run_exit_gate(
            written,
            notebook if notebook is not None else state.notebook,
            gathered,
            rerun.records,
            min_support=_min_support(context),
        )
    final = outcome.report
    if outcome.notice is not None:
        final = final.model_copy(update={"notes": (*final.notes, outcome.notice)})
    record_gate(context.root_span, outcome.passed, outcome.removed)
    status = Status.ABSTAINED if final.abstained else Status.COMPLETE
    return InvestigationResult(
        state=state.model_copy(update={"report": final, "status": status, "evidence": gathered}),
        report=final,
        stopped_because=stop,
        rounds=state.budget.rounds,
        notes=list(context.notes),
        wall_clock_seconds=time.monotonic() - started,
        gate=outcome,
    )


def _min_support(context: NodeContext) -> int:
    """The gate's abstention threshold, from `config/thresholds.yaml`.

    Falls back to the loop's completion rule when the file cannot be read, and
    says so in the notes. A gate that silently abstained on a different threshold
    than the one on disk would make every abstention unexplainable.
    """
    try:
        return load_thresholds().abstention.minimum_hypothesis_support
    except ThresholdError as error:
        context.notes.append(
            f"could not read the abstention threshold ({error}), using {MIN_SUPPORT_TO_CONCLUDE}"
        )
        return MIN_SUPPORT_TO_CONCLUDE
