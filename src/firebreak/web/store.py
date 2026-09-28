"""Writing an investigation where the Console can read it.

The Console reads files and computes nothing, so something has to write the files. This is
that, and it is deliberately the only place that turns an `InvestigationResult` into what a
page renders.

**Why a stored view rather than the Console re-running the investigation.** Two reasons.
Re-running on every page load would make a page view cost an investigation, and more
importantly the Console would then show a fresh result rather than the one that was
published. A report is a statement that was made at a time; a page that quietly recomputed
it would show a different statement under the same heading.

**The evidence is stored with the report.** SPEC.md Section 6.13's Report page links every
claim to the query, window and rows behind it, and Section 6.10 has the approval service
show raw evidence rather than a summary. Both need the records themselves, so they are
written alongside rather than fetched from a backend the Console would have to hold open.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
CONSOLE_DIR = REPO_ROOT / "reports" / "console"


def as_console_payload(result: Any, *, replayed: bool = False) -> dict[str, Any]:
    """One investigation, in the shape the Console's templates read.

    Takes the result structurally rather than by type, so this module needs no import of
    the agent package. That keeps the web layer out of the agent's import graph, which the
    leakage rules care about: `firebreak.web` is an agent package as far as
    `config/leakage.yaml` is concerned.

    `replayed` drops the wall clock, and the reason is the same one that makes the
    Evaluation page print "not measured" rather than a cost of zero. A replay's elapsed
    time measures how fast this machine read cassettes off disk, not how long an
    investigation takes. Storing it would put a number on the Investigation page that says
    nothing about the system and reads exactly like one that does. It also kept the
    committed showcase reports permanently dirty, since the one field that varied run to
    run was the one nobody could use.
    """
    report = result.report
    state = result.state
    notebook = state.notebook
    return {
        "bundle_id": report.incident_id,
        "root_cause_service": report.root_cause_service,
        "confidence": report.confidence.value if report.confidence else None,
        "stopped_because": result.stopped_because.value,
        "notes": list(report.notes),
        "notes_run": list(result.notes),
        "rounds": result.rounds,
        "tool_calls": state.budget.tool_calls,
        "wall_clock_seconds": None if replayed else result.wall_clock_seconds,
        "replayed": replayed,
        "claims": [
            {
                "text": claim.text,
                "claim_type": claim.claim_type.value,
                "evidence_ids": list(claim.evidence_ids),
                "numbers": [
                    {
                        "value": number.value,
                        "unit": number.unit,
                        "evidence_id": number.evidence_id,
                        "field": number.field,
                    }
                    for number in claim.numbers
                ],
                "at": claim.at.isoformat() if claim.at else None,
            }
            for claim in report.claims
        ],
        "hypotheses": [
            {
                "id": hypothesis.id,
                "service": hypothesis.service,
                "statement": hypothesis.statement,
                "support": hypothesis.support,
                "contested": hypothesis.is_contested,
            }
            for hypothesis in notebook.ranked()
        ],
        "gate": result.gate.as_dict() if result.gate else None,
        # Keyed by evidence id, because that is what a claim cites and what the Console's
        # evidence endpoint is asked for.
        "evidence": {
            evidence_id: json.loads(record.model_dump_json())
            for evidence_id, record in state.evidence.items()
        },
    }


def write_console_report(
    result: Any, directory: Path | None = None, *, replayed: bool = False
) -> Path:
    """Store one investigation for the Console.

    Overwrites. An investigation of the same incident is the same incident, and keeping
    every run would make the Console show a history nobody asked for while hiding which
    one is current. The eval reports are where a history of runs belongs.
    """
    root = directory or CONSOLE_DIR
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{result.report.incident_id}.json"
    path.write_text(
        json.dumps(as_console_payload(result, replayed=replayed), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path
