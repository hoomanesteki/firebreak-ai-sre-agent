"""The Console: six pages over the artefacts Firebreak already produces.

SPEC.md Section 6.13. FastAPI and Jinja2, no build step, no front-end framework.

**Every page reads from disk and computes nothing.** Reports live in `reports/`, bundles
in `bundles/`, the audit chain in a JSON Lines file, feedback in another. The Console is a
view, and that is a deliberate constraint rather than a simplification: a page that
computed its own accuracy would be a second definition of accuracy, and the two would
disagree eventually. Where a number is shown here it came from a file something else wrote.

**Empty data is the normal state and every page is built for it.** SPEC.md Section 17
Phase 11 asks for every page to work with seeded and empty data, and this repository is
the empty case: eight bundles of a hundred and fourteen, no credentials, no feedback, no
approvals. A page that rendered a blank table would be useless on a fresh clone, so each
one says what is missing and what would fill it. That is also the more useful behaviour on
a real deployment on day one.

**The Report page is the one that matters, and the reason is the whole project's claim.**
Every claim links to the evidence it cites, and the evidence view shows the query, the
window, the backend fingerprint and the rows. A report whose claims cannot be opened is a
report that asks to be believed.

**Clarity for a non-engineer is this phase's other reviewer focus.** Each page opens with
one sentence saying what it is for, every number carries its units, and anything that
cannot be shown says so in words rather than as an empty region.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

REPORTS_DIR = REPO_ROOT / "reports"
BUNDLES_DIR = REPO_ROOT / "bundles"
CASSETTES_DIR = REPO_ROOT / "recordings" / "cassettes"


@dataclass(frozen=True)
class Missing:
    """Why a page has nothing to show, and what would change that.

    A type rather than a string, because every page needs the same two sentences and a
    page that improvised them would eventually improvise one that blamed the reader.
    """

    what: str
    why: str
    how: str

    def as_dict(self) -> dict[str, str]:
        return {"what": self.what, "why": self.why, "how": self.how}


def read_json(path: Path) -> dict[str, Any] | None:
    """Read one JSON file, or None when it is absent or unreadable.

    None rather than raising. A Console page whose file is missing should say so, and a
    stack trace in a browser is the least useful way to communicate that.
    """
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read one JSON Lines file, skipping what cannot be parsed."""
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            loaded = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            rows.append(loaded)
    return rows


def latest_eval_reports() -> dict[str, dict[str, Any]]:
    """The most recent eval report for each configuration and split.

    By modification time, not by filename. The filename carries a date and a commit, and
    two runs on the same day sort by commit hash, which has nothing to do with which came
    last.
    """
    found: dict[str, dict[str, Any]] = {}
    root = REPORTS_DIR / "eval"
    if not root.is_dir():
        return found
    for configuration in sorted(p for p in root.iterdir() if p.is_dir()):
        for split in sorted(p for p in configuration.iterdir() if p.is_dir()):
            files = sorted(split.glob("*.json"), key=lambda p: p.stat().st_mtime)
            if not files:
                continue
            payload = read_json(files[-1])
            if payload is not None:
                found[f"{configuration.name}/{split.name}"] = payload
    return found


def showcase_incidents() -> list[dict[str, Any]]:
    """The demo's incidents, from the cassette manifest.

    The Console's incident list in demo mode. Read from the manifest rather than by
    scanning bundles, because the manifest is committed and the bundles are not, so this
    is the list that exists on a fresh clone.
    """
    manifest = read_json(CASSETTES_DIR / "manifest.json")
    if manifest is None:
        return []
    incidents = manifest.get("incidents")
    if not isinstance(incidents, dict):
        return []
    rows = []
    for bundle_id, entry in sorted(incidents.items()):
        if not isinstance(entry, dict):
            continue
        rows.append(
            {
                "bundle_id": bundle_id,
                "scenario_id": entry.get("scenario_id", ""),
                "reason": entry.get("reason", ""),
                "named": entry.get("root_cause_service"),
                "abstained": bool(entry.get("abstained")),
                "claims": entry.get("claims", 0),
                "gate_passed": bool(entry.get("gate_passed")),
                "cassettes": len(entry.get("cassettes") or []),
            }
        )
    return rows


def recorded_incidents() -> list[dict[str, Any]]:
    """Bundles on disk, by their opaque ids.

    Ids only, and deliberately: a bundle directory named for its fault would state the
    answer, which is why they are opaque, and a Console that resolved them through the
    labels would put ground truth on a web page.
    """
    if not BUNDLES_DIR.is_dir():
        return []
    return [
        {"bundle_id": path.name}
        for path in sorted(BUNDLES_DIR.iterdir())
        if (path / "manifest.json").is_file()
    ]


def create_app() -> FastAPI:
    """Build the Console.

    A factory rather than a module-level app, so a test can build one against a temporary
    directory and so importing this module starts no server.
    """
    app = FastAPI(
        title="Firebreak Console",
        description=(
            "A view over what Firebreak produced. Every number here came from a file "
            "something else wrote."
        ),
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(request=request, name=name, context=context)

    @app.get("/", response_class=HTMLResponse)
    def incidents(request: Request) -> HTMLResponse:
        """Open and past investigations, plus the demo's scenario picker."""
        showcase = showcase_incidents()
        recorded = recorded_incidents()
        return page(
            request,
            "incidents.html",
            title="Incidents",
            lead=(
                "Every incident Firebreak can investigate. The showcase set runs offline "
                "from recorded model answers; the recorded set needs the library on this "
                "machine."
            ),
            showcase=showcase,
            recorded=recorded,
            missing=None
            if showcase or recorded
            else Missing(
                what="No incidents",
                why=(
                    "Nothing has been recorded and no replay cassettes are present, so "
                    "there is nothing to investigate."
                ),
                how="Run `make cassettes` for the offline showcase, or record the library.",
            ).as_dict(),
        )

    @app.get("/report/{bundle_id}", response_class=HTMLResponse)
    def report(request: Request, bundle_id: str) -> HTMLResponse:
        """One report, with every claim linked to the evidence it cites."""
        stored = read_json(REPORTS_DIR / "console" / f"{bundle_id}.json")
        return page(
            request,
            "report.html",
            title="Report",
            lead=(
                "What the investigation concluded, and what each sentence rests on. Every "
                "claim links to the evidence it cites, with the query and window that "
                "produced it."
            ),
            bundle_id=bundle_id,
            report=stored,
            missing=None
            if stored
            else Missing(
                what=f"No stored report for {bundle_id}",
                why=(
                    "A report is written when an investigation runs. This incident has "
                    "not been investigated, or its report was not saved for the Console."
                ),
                how="Run `make demo-offline`, which replays the showcase and writes reports.",
            ).as_dict(),
        )

    @app.get("/evidence/{bundle_id}/{evidence_id}", response_class=JSONResponse)
    def evidence(bundle_id: str, evidence_id: str) -> JSONResponse:
        """One evidence record: the query, the window, the fingerprint, the rows.

        The endpoint the Report page's claim links open. JSON rather than a page, because
        the point is to show exactly what was recorded rather than a rendering of it, and
        because a re-run has to be comparable with what is shown here.
        """
        stored = read_json(REPORTS_DIR / "console" / f"{bundle_id}.json")
        records = (stored or {}).get("evidence") or {}
        record = records.get(evidence_id) if isinstance(records, dict) else None
        if record is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": f"no evidence {evidence_id} recorded for {bundle_id}",
                    "why": (
                        "A claim citing evidence nobody can open is the failure the exit "
                        "gate exists to prevent, so this is worth a 404 rather than an "
                        "empty panel."
                    ),
                },
            )
        return JSONResponse(content=record)

    @app.get("/approvals", response_class=HTMLResponse)
    def approvals(request: Request) -> HTMLResponse:
        """Pending remediations with their raw evidence, and past decisions."""
        records = read_jsonl(REPORTS_DIR / "approvals" / "audit.jsonl")
        return page(
            request,
            "approvals.html",
            title="Approvals",
            lead=(
                "Proposed changes waiting for a person, and every decision already made. "
                "Firebreak cannot execute anything from this page: a separate service "
                "holds the only credentials that can."
            ),
            records=records,
            missing=None
            if records
            else Missing(
                what="No proposals",
                why=(
                    "Remediation execution is available in live mode only, and nothing has "
                    "proposed a change yet."
                ),
                how="make live, then run an investigation against the live stack.",
            ).as_dict(),
        )

    @app.get("/evaluation", response_class=HTMLResponse)
    def evaluation(request: Request) -> HTMLResponse:
        """Accuracy, calibration, cost, baselines and ablations, per split."""
        reports = latest_eval_reports()
        return page(
            request,
            "evaluation.html",
            title="Evaluation",
            lead=(
                "How well each configuration did, and on which data. A figure from a "
                "tunable split says how well the system fits data it was developed "
                "against; only the held-out splits support a claim about performance."
            ),
            reports=reports,
            missing=None
            if reports
            else Missing(
                what="No eval reports",
                why="No configuration has been run over a split yet.",
                how="Run `make eval CONFIG=fb-v1 SPLIT=validation`.",
            ).as_dict(),
        )

    @app.get("/feedback", response_class=HTMLResponse)
    def feedback(request: Request) -> HTMLResponse:
        """Reports awaiting review, and the feedback already given."""
        given = read_jsonl(REPORTS_DIR / "feedback" / "feedback.jsonl")
        awaiting = [
            incident
            for incident in showcase_incidents()
            if incident["bundle_id"] not in {row.get("incident_id") for row in given}
        ]
        return page(
            request,
            "feedback.html",
            title="Feedback",
            lead=(
                "Was the root cause right? Answering turns a wrong report into an "
                "evaluation task, which is how this system learns from its own failures."
            ),
            given=given,
            awaiting=awaiting,
            missing=None
            if given or awaiting
            else Missing(
                what="Nothing to review",
                why="No investigation has published a report yet.",
                how="Run `make demo-offline`.",
            ).as_dict(),
        )

    @app.get("/live/{bundle_id}", response_class=HTMLResponse)
    def live(request: Request, bundle_id: str) -> HTMLResponse:
        """The investigation view: what the agent did, step by step.

        Reads the stored timeline rather than streaming, and says so. SPEC.md Section 6.13
        asks for live streaming; streaming an investigation needs the investigation to be
        running, and in this repository investigations finish in under a second against a
        frozen bundle. A progress bar for a 900 millisecond operation would be theatre, so
        this shows the completed timeline and the phase report records that streaming is
        not built.
        """
        stored = read_json(REPORTS_DIR / "console" / f"{bundle_id}.json")
        return page(
            request,
            "live.html",
            title="Investigation",
            lead=(
                "Every node the agent ran, every tool it called, and what the budget was spent on."
            ),
            bundle_id=bundle_id,
            report=stored,
            missing=None
            if stored
            else Missing(
                what=f"No timeline for {bundle_id}",
                why="This incident has not been investigated on this machine.",
                how="Run `make demo-offline`.",
            ).as_dict(),
        )

    return app
