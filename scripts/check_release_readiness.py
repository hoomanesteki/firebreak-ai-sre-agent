"""Refuse release approval without complete held-out measurements of one commit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from firebreak.evals.report import git_commit
from firebreak.evals.runner import CONFIGURATIONS, split_coverage
from firebreak.lab.scenario import Split

ROOT = Path(__file__).resolve().parents[1]


def check_reports(
    root: Path,
    configurations: tuple[str, ...],
    sizes: dict[str, int],
    commit: str,
) -> list[str]:
    """Require the latest report for each configuration and split to be usable."""
    problems = []
    for configuration in configurations:
        for split, size in sizes.items():
            reports = []
            for path in (root / configuration / split).glob("*.json"):
                try:
                    payload = json.loads(path.read_text())
                except (OSError, ValueError):
                    problems.append(f"{path}: unreadable report")
                    continue
                if isinstance(payload, dict):
                    reports.append(payload)
            label = f"{configuration}/{split}"
            if not reports:
                problems.append(f"{label}: no evaluation report")
                continue
            report = max(reports, key=lambda item: str(item.get("generated_at", "")))
            required = {
                "configuration": configuration,
                "split": split,
                "commit": commit,
                "quotable_as_a_result": True,
                "data_source": "recorded bundles",
                "trials_per_task": 3,
                "total_scenarios": size,
                "recorded_scenarios": size,
                "evaluated_scenarios": size,
            }
            wrong = [key for key, value in required.items() if report.get(key) != value]
            if wrong:
                problems.append(f"{label}: unmet {', '.join(wrong)}")
            if configuration != "b0":
                modes = report.get("model_modes") or []
                if (
                    not modes
                    or not set(modes) <= {"api", "local"}
                    or not report.get("model_ids")
                    or not report.get("model_calls")
                    or report.get("fallback_trials") != 0
                ):
                    problems.append(f"{label}: real model provenance is incomplete")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", default=None, help="frozen candidate commit; defaults to HEAD")
    arguments = parser.parse_args()
    problems = []
    sizes = {}
    for split in (Split.TEST_ID, Split.TEST_OOD):
        recorded, total = split_coverage(split)
        sizes[split.value] = total
        if recorded != total:
            problems.append(f"{split.value}: {recorded}/{total} scenarios recorded")
    problems.extend(
        check_reports(
            ROOT / "reports" / "eval",
            tuple(CONFIGURATIONS),
            sizes,
            arguments.commit or git_commit(),
        )
    )
    for problem in problems:
        print(problem)
    print("Release validation blocked." if problems else "Release measurements verified.")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
