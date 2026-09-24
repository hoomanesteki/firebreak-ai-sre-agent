"""Run baseline B0 over every bundle and score it against the labels.

SPEC.md Section 17, Phase 4, requires B0 to run on all train and validation
bundles and write `reports/triage/b0_val.json`. B0 is deterministic triage
with a template report and no model at all, and it is the bar every agent
configuration has to beat.

**This script is allowed to read ground truth. Nothing it measures is.**
Scoring needs the answer, so it imports `firebreak_eval_labels`, which the
leakage check forbids anywhere under `firebreak.agent`, `firebreak.tools` or
`firebreak.graph`. The report is produced here, the investigation happens in
`firebreak.triage.report`, and the two never share a process boundary that
could leak the label backwards: `build_b0_report` is handed a bundle
directory and nothing else.

**Recorded bundles are used when they exist**, and synthetic fixtures when
they do not, and the report says which in its own fields. No incident
library has been recorded yet, so today this runs on fixtures and the number
is a design signal rather than a result. The moment bundles exist the same
command produces the real figure with no edit.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from _reporting import describe_path
from firebreak.lab.bundle import derive_bundle_id, iter_bundles
from firebreak.lab.scenario import Split, load_library
from firebreak.lab.synthetic import build_synthetic_bundle
from firebreak.triage.report import build_b0_report
from firebreak_eval_labels import IncidentLabel

REPO_ROOT = Path(__file__).resolve().parent.parent
SPECS_DIR = REPO_ROOT / "scenarios" / "specs"
BUNDLES_DIR = REPO_ROOT / "bundles"
LABELS_DIR = REPO_ROOT / "labels"
REPORT_PATH = REPO_ROOT / "reports" / "triage" / "b0_val.json"

SCORED_SPLITS = (Split.TRAIN, Split.VALIDATION)
SYNTHETIC_RUN = "b0"
SYNTHETIC_SEED = 1


@dataclass(frozen=True)
class Scored:
    """One bundle, what B0 said, and what was actually true."""

    bundle_id: str
    scenario_id: str
    family: str
    split: str
    truth: str | None
    named: str | None
    abstained: bool
    rank: int | None
    evidence_count: int
    tool_calls: int
    cited: int

    @property
    def correct(self) -> bool:
        """Did B0 give the right answer, including by staying silent.

        A no fault incident is answered correctly by naming nobody, and
        wrongly by naming anybody. Folding that into the same field as a
        root cause match is deliberate: a system that scores well only
        because the no fault cases are excluded is not the system that runs.
        """
        if self.truth is None:
            return self.abstained
        return self.named == self.truth


def _labels() -> dict[str, IncidentLabel]:
    """Every recorded label, keyed by the bundle id it belongs to."""
    if not LABELS_DIR.is_dir():
        return {}
    found: dict[str, IncidentLabel] = {}
    for path in sorted(LABELS_DIR.rglob("*.json")):
        label = IncidentLabel.model_validate_json(path.read_text(encoding="utf-8"))
        found[label.bundle_id] = label
    return found


def _score_one(
    bundle_dir: Path,
    scenario_id: str,
    family: str,
    split: str,
    truth: str | None,
    verify: bool,
) -> Scored:
    report = build_b0_report(bundle_dir, verify=verify)
    candidates = [c.service for c in report.triage.candidates] if report.triage else []
    rank = candidates.index(truth) + 1 if truth and truth in candidates else None
    return Scored(
        bundle_id=report.bundle_id,
        scenario_id=scenario_id,
        family=family,
        split=split,
        truth=truth,
        named=report.named_service,
        abstained=report.abstained,
        rank=rank,
        evidence_count=len(report.evidence),
        tool_calls=report.tool_calls,
        cited=len(report.cited_ids),
    )


def _summarise(scored: list[Scored]) -> dict[str, Any]:
    faulted = [s for s in scored if s.truth is not None]
    quiet = [s for s in scored if s.truth is None]
    return {
        "bundles": len(scored),
        "correct": sum(1 for s in scored if s.correct),
        "accuracy": round(sum(1 for s in scored if s.correct) / len(scored), 4) if scored else 0.0,
        "faulted": {
            "bundles": len(faulted),
            "top1": sum(1 for s in faulted if s.rank == 1),
            "top3": sum(1 for s in faulted if s.rank is not None and s.rank <= 3),
            # A named service that is not the culprit is the expensive
            # failure: it sends an on call engineer to the wrong place with
            # a report that looks confident.
            "named_wrong_service": sum(
                1 for s in faulted if s.named is not None and s.named != s.truth
            ),
            "abstained_on_a_real_incident": sum(1 for s in faulted if s.abstained),
        },
        "no_fault": {
            "bundles": len(quiet),
            "correctly_said_nothing": sum(1 for s in quiet if s.abstained),
            "invented_a_culprit": sum(1 for s in quiet if not s.abstained),
        },
        "cost": {
            "mean_tool_calls": round(sum(s.tool_calls for s in scored) / len(scored), 2)
            if scored
            else 0.0,
            "mean_evidence_records": round(sum(s.evidence_count for s in scored) / len(scored), 2)
            if scored
            else 0.0,
        },
    }


def _by_family(scored: list[Scored]) -> dict[str, Any]:
    families: dict[str, list[Scored]] = {}
    for entry in scored:
        families.setdefault(entry.family, []).append(entry)
    return {family: _summarise(group) for family, group in sorted(families.items())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="only the first N scenarios")
    arguments = parser.parse_args()

    specs = [s for s in load_library(SPECS_DIR).values() if s.split in SCORED_SPLITS]
    specs.sort(key=lambda s: s.id)
    if arguments.limit:
        specs = specs[: arguments.limit]

    labels = _labels()
    recorded = (
        {path.name: path for path in iter_bundles(BUNDLES_DIR)} if BUNDLES_DIR.is_dir() else {}
    )
    using_recorded = bool(recorded and labels)

    scored: list[Scored] = []
    if using_recorded:
        for bundle_id, bundle_dir in sorted(recorded.items()):
            label = labels.get(bundle_id)
            if label is None or label.split not in {s.value for s in SCORED_SPLITS}:
                continue
            spec = load_library(SPECS_DIR).get(label.scenario_id)
            scored.append(
                _score_one(
                    bundle_dir,
                    label.scenario_id,
                    spec.family if spec else "unknown",
                    label.split,
                    label.target_service,
                    verify=True,
                )
            )
    else:
        with tempfile.TemporaryDirectory(prefix="firebreak-b0-") as temporary:
            for spec in specs:
                bundle_dir = Path(temporary) / derive_bundle_id(spec.id, SYNTHETIC_RUN)
                build_synthetic_bundle(bundle_dir, spec, SYNTHETIC_RUN, seed=SYNTHETIC_SEED)
                scored.append(
                    _score_one(
                        bundle_dir,
                        spec.id,
                        spec.family,
                        spec.split.value,
                        spec.target_service,
                        verify=False,
                    )
                )

    report = {
        "what": "Baseline B0: deterministic triage with a template report, no model",
        "why": (
            "SPEC.md Section 9.5. B0 is the bar every agent configuration has to beat, "
            "and principle H1 says the simplest thing that works gets built and measured "
            "before anything agentic is added."
        ),
        "generated_at": datetime.now(UTC).isoformat(),
        "generated_by": "scripts/run_baseline_b0.py",
        "data_source": "recorded bundles" if using_recorded else "synthetic fixtures",
        "caveat": (
            "Recorded incidents."
            if using_recorded
            else (
                "Synthetic fixtures, because no incident library has been recorded yet. "
                "This is a design signal and must not be quoted as a result. Record the "
                "library and re-run to get the real figure."
            )
        ),
        "splits": [split.value for split in SCORED_SPLITS],
        "overall": _summarise(scored),
        "by_family": _by_family(scored),
        "by_split": {
            split.value: _summarise([s for s in scored if s.split == split.value])
            for split in SCORED_SPLITS
        },
        "bundles": [
            {
                "bundle_id": s.bundle_id,
                "family": s.family,
                "split": s.split,
                "named": s.named,
                "abstained": s.abstained,
                "rank_of_truth": s.rank,
                "correct": s.correct,
                "tool_calls": s.tool_calls,
                "evidence_records": s.evidence_count,
            }
            for s in scored
        ],
    }

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    overall = report["overall"]
    assert isinstance(overall, dict)
    print(f"source: {report['data_source']}")
    print(
        f"B0 over {overall['bundles']} bundles: "
        f"{overall['correct']} correct ({overall['accuracy']:.1%})"
    )
    print(
        f"  faulted   top1={overall['faulted']['top1']}/{overall['faulted']['bundles']} "
        f"top3={overall['faulted']['top3']} "
        f"wrong_service={overall['faulted']['named_wrong_service']} "
        f"missed={overall['faulted']['abstained_on_a_real_incident']}"
    )
    print(
        f"  no fault  silent={overall['no_fault']['correctly_said_nothing']}"
        f"/{overall['no_fault']['bundles']} "
        f"invented={overall['no_fault']['invented_a_culprit']}"
    )
    print(f"  cost      {overall['cost']['mean_tool_calls']} tool calls per investigation")
    print(f"\nwrote {describe_path(REPORT_PATH)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
