"""The investigation's nodes, each doing one job.

SPEC.md Section 6.6 lists them. The ones here are the v1 set: entry gate,
triage, commander, four specialists, hypothesis board, critic, reporter and
exit gate. Remediation and approval arrive in Phase 9.

**Code where code will do.** The entry gate, the hypothesis board and the exit
gate are deterministic, and they are the nodes that decide whether an
investigation starts, what it believes, and what it is allowed to say. Putting
any of those behind a model would mean asking a model to enforce a rule, and a
model enforces a rule right up until the moment it does not.

**A specialist sees a brief, not the state.** Every specialist is handed a
`Brief` and its own tool subset. There is no argument through which the whole
investigation could reach it, which is the structural version of [R9]'s advice
rather than an instruction in a prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from opentelemetry.trace import Span
from pydantic import BaseModel, ConfigDict, Field

from firebreak.agent.budget import StopReason
from firebreak.agent.llm import LlmClient, Tier
from firebreak.agent.state import (
    Brief,
    Claim,
    Confidence,
    Critique,
    Finding,
    Hypothesis,
    InvestigationState,
    Notebook,
    Report,
    Status,
)
from firebreak.telemetry.spans import (
    SpanRecorder,
    record_tool_result,
    record_usage,
)
from firebreak.tools.base import ToolContext, ToolError, ToolRegistry
from firebreak.tools.registry import GRAPH_TOOLS, SPECIALIST_TOOLS

# How many candidates from triage become hypotheses. SPEC.md Section 6.5 hands
# the agent a short ranked list; turning all of it into hypotheses would spend
# the budget confirming that the fifth candidate is innocent.
MAX_HYPOTHESES = 3

# What a hypothesis needs before the investigation may finish on it: more
# evidence for than against, and no unresolved objection. SPEC.md Section 6.6.
MIN_SUPPORT_TO_CONCLUDE = 2


class CommanderPlan(BaseModel):
    """What the commander decided to test this round."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hypotheses: tuple[Hypothesis, ...] = ()
    assignments: tuple[tuple[str, str, str], ...] = ()
    """Each entry is (specialist, hypothesis id, the question to answer)."""


class SpecialistAnswer(BaseModel):
    """What one specialist reports back."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(max_length=600)
    supports: bool
    confidence: Confidence


class CriticVerdict(BaseModel):
    """The critic's attempt to refute the leading hypothesis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    objection: str = Field(default="", max_length=600)
    requested_checks: tuple[str, ...] = ()
    convinced: bool = True


class ReporterOutput(BaseModel):
    """The report, as claims that cite evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root_cause_service: str | None
    confidence: Confidence
    claims: tuple[Claim, ...] = ()


@dataclass
class NodeContext:
    """What every node is given.

    The tool registry and the backend live here rather than on the state,
    because they are machinery rather than knowledge and putting them on the
    state would mean serialising a database connection into a checkpoint.
    """

    llm: LlmClient
    registry: ToolRegistry
    tools: ToolContext
    notes: list[str] = field(default_factory=list)
    # Ablation A2, SPEC.md Section 9.5: FB without the knowledge graph means no
    # ranking and no dependency tools. Removing only the ranking would leave a
    # weaker A2 that still reads the graph, and the row would stop answering the
    # question it exists for.
    allow_graph_tools: bool = True
    # Ablation A5, SPEC.md Section 9.5: FB without incident memory. A switch here
    # rather than an empty memory file, so the ablation is a property of the run and
    # two configurations can be compared on one machine without moving data about.
    allow_memory: bool = True
    # One recorder per investigation, so every span in a trace shares the same decision
    # about whether content is included. A per-node recorder would let one node ship
    # prompts while the rest did not, which is the worst version of a privacy setting.
    spans: SpanRecorder = field(default_factory=SpanRecorder)
    # The root span, so `_finish` can record the gate result on the trace rather than
    # only in the report. None when tracing is off.
    root_span: Span | None = None


def entry_gate(state: InvestigationState) -> InvestigationState:
    """Decide whether this is worth investigating at all.

    SPEC.md Section 6.8. Deterministic, and first, because every check here is
    cheaper than the investigation it prevents. A malformed incident that
    reached the commander would spend a strong-tier call discovering it has no
    window to look at.
    """
    problems = []
    if state.window_start is None or state.window_end is None:
        problems.append("the incident has no window")
    if state.baseline_start is None or state.baseline_end is None:
        problems.append("the incident has no baseline to compare against")
    if state.window_start and state.window_end and state.window_end <= state.window_start:
        problems.append("the incident window ends before it starts")

    if problems:
        return state.model_copy(
            update={
                "status": Status.FAILED,
                "report": Report(
                    incident_id=state.incident_id,
                    root_cause_service=None,
                    notes=(f"Not investigated: {'; '.join(problems)}.",),
                ),
            }
        )
    return state.model_copy(update={"status": Status.TRIAGED})


def seed_hypotheses(state: InvestigationState) -> InvestigationState:
    """Turn triage's ranked candidates into hypotheses to test.

    Code rather than a model call. The candidates are already ranked by
    `firebreak.graph.ranking`, and asking a model to restate a ranked list as
    prose would spend a strong-tier call to lose information.

    An abstaining triage seeds nothing, and the graph short-circuits to a report
    that says so. Investigating a system that showed no anomaly would spend the
    whole budget confirming that nothing happened.
    """
    triage = state.triage
    if triage is None or triage.says_nothing_is_wrong or not triage.candidates:
        return state.model_copy(update={"status": Status.ABSTAINED})

    hypotheses = tuple(
        Hypothesis(
            id=f"h{index}",
            service=candidate.service,
            statement=(
                f"{candidate.service} is the root cause: it ranked {index} of "
                f"{len(triage.candidates)} on the dependency graph with a worst metric "
                f"anomaly of {candidate.anomaly_score:.1f} robust deviations."
            ),
        )
        for index, candidate in enumerate(triage.candidates[:MAX_HYPOTHESES], start=1)
    )
    return state.model_copy(
        update={
            "notebook": Notebook(hypotheses=hypotheses),
            "status": Status.INVESTIGATING,
        }
    )


# How many memory-derived hypotheses may be added. Capped below MAX_HYPOTHESES on
# purpose: memory is advisory, and a board where past incidents outnumber this
# incident's own evidence would be a board that investigates the last outage.
MAX_MEMORY_HYPOTHESES = 2


def consult_memory(state: InvestigationState, context: NodeContext) -> InvestigationState:
    """Add past incidents with similar symptoms as hypotheses to test.

    SPEC.md Section 10.3, and ablation A5 switches it off. Memory entries arrive as
    hypotheses with no supporting evidence, which is what makes them advisory in
    practice rather than only in wording: the exit gate's abstention check counts
    support, so a memory-derived hypothesis cannot carry a report on its own. It has
    to be confirmed by a specialist against this incident's own data first.

    A service already on the board is not added again. Adding it would give the same
    service two hypotheses and let a memory entry inflate its apparent support by
    existing.

    The retrieval goes through the registry like any other tool call, so it is
    recorded as evidence and the exit gate can re-run it. A memory lookup that left no
    record would be a prior nobody could trace afterwards.
    """
    if not context.allow_memory:
        return state
    triage = state.triage
    if triage is None or not triage.candidates:
        return state

    symptoms = "; ".join(
        f"{candidate.service} anomaly {candidate.anomaly_score:.1f}"
        for candidate in triage.candidates[:MAX_HYPOTHESES]
    )
    services = tuple(candidate.service for candidate in triage.candidates[:MAX_HYPOTHESES])
    try:
        result = context.registry.call(
            "similar_incidents",
            context.tools,
            {"symptoms": symptoms, "services": list(services)},
        )
    except ToolError as error:
        # A memory lookup that cannot answer is a fact about the memory, not a reason
        # to stop investigating.
        context.notes.append(f"incident memory could not be consulted: {error}")
        return state

    known = {hypothesis.service for hypothesis in state.notebook.hypotheses}
    added: list[Hypothesis] = []
    for entry in result.data.get("hypotheses", [])[:MAX_MEMORY_HYPOTHESES]:
        service = str(entry.get("service", ""))
        if not service or service in known:
            continue
        known.add(service)
        added.append(
            Hypothesis(
                id=f"m{len(added) + 1}",
                service=service,
                statement=str(entry.get("statement", "")),
            )
        )
    if not added:
        return state
    context.notes.append(f"incident memory added {len(added)} hypothesis(es) to test")
    return state.model_copy(
        update={
            "notebook": Notebook(hypotheses=(*state.notebook.hypotheses, *added)),
        }
    )


def commander(state: InvestigationState, context: NodeContext) -> CommanderPlan:
    """Decide which specialists test which hypothesis this round.

    The one place a strong-tier model earns its cost in v1: choosing where to
    spend the next four tool calls. Everything downstream of it is either a
    small model answering a narrow question or code.
    """
    leader = state.notebook.leader
    payload = {
        "incident_id": state.incident_id,
        "hypotheses": [
            {"id": h.id, "service": h.service, "statement": h.statement, "support": h.support}
            for h in state.notebook.ranked()
        ],
        "unresolved_objections": [c.objection for c in state.unresolved_critiques],
        "round": state.budget.rounds,
        "leader": leader.id if leader else None,
    }
    with context.spans.model_call("commander", Tier.STRONG.value) as span:
        plan, completion = context.llm.complete(
            "commander", payload, CommanderPlan, tier=Tier.STRONG
        )
        record_usage(
            span,
            completion.tokens_in,
            completion.tokens_out,
            completion.usd,
            escalated=completion.escalated,
            repaired=completion.repaired,
        )
    state.budget.note_tokens(completion.tokens_in, completion.tokens_out, completion.usd)
    return plan


def run_specialist(
    state: InvestigationState,
    context: NodeContext,
    specialist: str,
    hypothesis: Hypothesis,
    question: str,
) -> Finding | None:
    """One specialist answers one question about one hypothesis.

    Returns None when it could gather no evidence at all. A finding with no
    evidence cannot be constructed, and inventing one would put an uncited
    opinion into the notebook where the reporter would treat it as a fact.
    """
    allowed = tools_for(specialist, allow_graph=context.allow_graph_tools)
    brief = state.brief_for(specialist, hypothesis, question, allowed)
    gathered = _gather(state, context, brief)
    if not gathered:
        context.notes.append(f"{specialist} gathered no evidence on {hypothesis.id}")
        return None

    payload = {
        "specialist": specialist,
        "question": question,
        "hypothesis": hypothesis.statement,
        "service": hypothesis.service,
        "evidence": [{"id": evidence_id, "summary": summary} for evidence_id, summary in gathered],
    }
    with context.spans.model_call("specialist", Tier.SMALL.value) as span:
        answer, completion = context.llm.complete(
            "specialist", payload, SpecialistAnswer, tier=Tier.SMALL
        )
        record_usage(
            span,
            completion.tokens_in,
            completion.tokens_out,
            completion.usd,
            escalated=completion.escalated,
            repaired=completion.repaired,
        )
    state.budget.note_tokens(completion.tokens_in, completion.tokens_out, completion.usd)
    return Finding(
        specialist=specialist,
        hypothesis_id=hypothesis.id,
        summary=answer.summary,
        supports=answer.supports,
        confidence=answer.confidence,
        evidence_ids=tuple(evidence_id for evidence_id, _ in gathered),
    )


def tools_for(specialist: str, allow_graph: bool = True) -> tuple[str, ...]:
    """What one specialist may call, after any ablation.

    Declared here rather than filtered at each call site, so A2 removes the
    dependency tools from every specialist that has them in one place. The change
    analyst is the only one that does today, and the one that would be forgotten.
    """
    allowed = SPECIALIST_TOOLS.get(specialist, ())
    if allow_graph:
        return allowed
    return tuple(name for name in allowed if name not in GRAPH_TOOLS)


def _gather(state: InvestigationState, context: NodeContext, brief: Brief) -> list[tuple[str, str]]:
    """Run this specialist's tools over its brief, returning evidence and summaries.

    Every call goes through the registry, so the caps and the argument
    validation are unavoidable, and every repeat is counted against the budget
    per SPEC.md Section 6.6.
    """
    window = {
        "start": brief.window_start.isoformat(),
        "end": brief.window_end.isoformat(),
    }
    baseline = {
        "start": brief.baseline_start.isoformat(),
        "end": brief.baseline_end.isoformat(),
    }
    plans: dict[str, list[tuple[str, dict[str, Any]]]] = {
        "metrics_analyst": [
            ("list_anomalies", {"window": window, "baseline": baseline, "minimum_score": 0.0}),
        ],
        "logs_analyst": [
            ("top_error_signatures", {"window": window, "service": brief.hypothesis.service}),
        ],
        "traces_analyst": [
            # `services`, plural and a tuple: `find_traces` takes a set of
            # services, not one. The first run of the graph on a real bundle
            # passed `service` and the tool refused every call, so the traces
            # analyst gathered nothing at all and did so quietly, because a tool
            # that cannot answer is treated as a fact about the incident rather
            # than a crash. A wrong argument name is not a fact about the
            # incident, which is why `test_agent_graph.py` now asserts that every
            # specialist's plan validates against its tools' own input models.
            ("find_traces", {"window": window, "services": [brief.hypothesis.service]}),
        ],
        "change_analyst": [
            ("recent_changes", {"window": {"start": baseline["start"], "end": window["end"]}}),
            ("blast_radius", {"service": brief.hypothesis.service}),
        ],
    }

    gathered: list[tuple[str, str]] = []
    for tool_name, arguments in plans.get(brief.specialist, []):
        if tool_name in GRAPH_TOOLS and tool_name not in brief.allowed_tools:
            # Ablation A2 removed it. Skipped rather than raising, because the
            # plan is shared by every configuration and a missing tool here is a
            # deliberate absence rather than a bug in the plan.
            context.notes.append(f"{brief.specialist} has no {tool_name} in this configuration")
            continue
        if tool_name not in brief.allowed_tools:
            # A specialist asking for a tool outside its allowlist is a bug in
            # this plan, not something to route around silently.
            raise ToolError(
                f"{brief.specialist} may not call {tool_name}; it has "
                f"{', '.join(brief.allowed_tools)}"
            )
        fingerprint = f"{tool_name}:{sorted(arguments.items())!r}"
        if state.budget.is_repetitive(fingerprint):
            context.notes.append(f"{brief.specialist} stopped repeating {tool_name}")
            continue
        state.budget.note_tool_call(fingerprint)
        try:
            with context.spans.tool_call(tool_name, arguments) as span:
                result = context.registry.call(tool_name, context.tools, arguments)
                # The row count comes from the evidence record, not from the result's
                # `data`. `len(result.data)` counts keys in a summary dict, which looked
                # like a row count in a dashboard and was not one.
                record = (
                    context.tools.evidence.get(result.evidence_id) if result.evidence_id else None
                )
                record_tool_result(
                    span,
                    record.row_count if record else 0,
                    result.evidence_id,
                    result.truncated,
                )
        except ToolError as error:
            # A tool that cannot answer is a fact about the incident, not a
            # crash. A bundle with no change log is a legitimate recording.
            context.notes.append(f"{tool_name} could not answer: {error}")
            continue
        if result.evidence_id:
            gathered.append((result.evidence_id, result.summary))
    return gathered


def hypothesis_board(state: InvestigationState, findings: list[Finding]) -> InvestigationState:
    """Fold this round's findings into the notebook.

    Code, deliberately. This is where an investigation decides what it
    believes, and a model asked to weigh evidence it produced tends to agree
    with itself. Counting is not clever and it cannot flatter anybody.
    """
    updated = []
    for hypothesis in state.notebook.hypotheses:
        supporting = list(hypothesis.supporting_evidence)
        contradicting = list(hypothesis.contradicting_evidence)
        for finding in findings:
            if finding.hypothesis_id != hypothesis.id:
                continue
            target = supporting if finding.supports else contradicting
            for evidence_id in finding.evidence_ids:
                if evidence_id not in target:
                    target.append(evidence_id)
        updated.append(
            hypothesis.model_copy(
                update={
                    "supporting_evidence": tuple(supporting),
                    "contradicting_evidence": tuple(contradicting),
                }
            )
        )

    return state.model_copy(
        update={
            "notebook": state.notebook.model_copy(update={"hypotheses": tuple(updated)}),
            "findings": state.findings + tuple(findings),
        }
    )


def critic(state: InvestigationState, context: NodeContext) -> Critique | None:
    """Try to refute the leading hypothesis.

    A strong-tier call with a different prompt, and where configured a
    different model family, because a model reviewing its own reasoning agrees
    with it [R8]. Returns None when it cannot find an objection, which is the
    only way an investigation is allowed to finish.
    """
    leader = state.notebook.leader
    if leader is None:
        return None

    payload = {
        "hypothesis": leader.statement,
        "service": leader.service,
        "supporting_evidence": list(leader.supporting_evidence),
        "contradicting_evidence": list(leader.contradicting_evidence),
        "alternatives": [
            {"id": h.id, "service": h.service, "support": h.support}
            for h in state.notebook.ranked()
            if h.id != leader.id
        ],
        "findings": [
            {"specialist": f.specialist, "summary": f.summary, "supports": f.supports}
            for f in state.findings
            if f.hypothesis_id == leader.id
        ],
    }
    verdict, completion = context.llm.complete("critic", payload, CriticVerdict, tier=Tier.STRONG)
    state.budget.note_tokens(completion.tokens_in, completion.tokens_out, completion.usd)
    if verdict.convinced:
        return None
    return Critique(
        hypothesis_id=leader.id,
        objection=verdict.objection,
        requested_checks=verdict.requested_checks,
        evidence_ids=leader.contradicting_evidence,
    )


def reporter(state: InvestigationState, context: NodeContext) -> Report:
    """Write the report from the notebook, as claims that cite evidence.

    Given the notebook and nothing else. A reporter with access to the raw tool
    output would be able to write a sentence no finding supports, and the exit
    gate would then have to catch it; not offering the opportunity is cheaper.
    """
    leader = state.notebook.leader
    payload = {
        "incident_id": state.incident_id,
        "leader": None
        if leader is None
        else {
            "service": leader.service,
            "statement": leader.statement,
            "supporting_evidence": list(leader.supporting_evidence),
            "contradicting_evidence": list(leader.contradicting_evidence),
        },
        "findings": [
            {
                "specialist": f.specialist,
                "summary": f.summary,
                "supports": f.supports,
                "evidence_ids": list(f.evidence_ids),
            }
            for f in state.findings
        ],
        "unresolved_objections": [c.objection for c in state.unresolved_critiques],
        "stopped_because": state.stopped_because.value if state.stopped_because else None,
    }
    written, completion = context.llm.complete(
        "reporter", payload, ReporterOutput, tier=Tier.STRONG
    )
    state.budget.note_tokens(completion.tokens_in, completion.tokens_out, completion.usd)

    confidence = written.confidence
    if state.stopped_because is not None and state.stopped_because.lowers_confidence:
        # SPEC.md Section 6.6: a report produced under a budget stop or a stall
        # says so and lowers its confidence. Enforced here rather than asked of
        # the model, because it is a rule.
        confidence = Confidence.LOW
    return Report(
        incident_id=state.incident_id,
        root_cause_service=written.root_cause_service,
        confidence=confidence,
        claims=written.claims,
        stopped_because=state.stopped_because,
    )


def should_continue(state: InvestigationState) -> StopReason | None:
    """Whether the loop runs another round.

    SPEC.md Section 6.6's three stopping rules, in one place so the graph's
    edge condition and the report's explanation cannot disagree about why a run
    ended.
    """
    spent = state.budget.stop_reason()
    if spent is not None:
        return spent
    leader = state.notebook.leader
    if leader is None:
        return StopReason.COMPLETE
    if leader.support >= MIN_SUPPORT_TO_CONCLUDE and not state.unresolved_critiques:
        return StopReason.COMPLETE
    return None
