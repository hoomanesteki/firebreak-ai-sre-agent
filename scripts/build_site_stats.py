"""Build `reports/site_stats.json`, the only source of numbers for the site and README.

SPEC.md Section 15: numbers on the site come only from this file, and a missing key fails the
build. Section 19.4 rule 5: the README's stats block must match what this produces.

**The single rule this script exists to enforce.** A number reaches the public surface only if
the report it came from says `quotable_as_a_result`. Every other number becomes a stated
absence with the reason attached. Today that means no figure on the front page is a result,
because all eight committed reports are not quotable: the library is 8 of 114 recorded and
there are no model credentials, so the held-out splits have nothing in them.

That is an uncomfortable front page and it is the correct one. A README claiming 0.143
root-cause accuracy would be quoting a partly recorded tuning split as a result, and one
claiming 1.000 would be quoting synthetic fixtures. Both numbers exist in `reports/eval/`
right now.

**Staleness is checked, not assumed.** A report is a snapshot. `reports/eval/b0/test_ood/`
holds one saying "1 of 30 scenarios recorded", which was true when written; that bundle was
later deleted for recording corruption and the count is now zero. A stale report satisfies
"every number traces to a report" by the letter, so this refuses to publish from one whose
recorded count no longer matches the bundles on disk.

**Ordering is by `generated_at`.** Not by modification time, which git rewrites on every
checkout, and not by filename, which carries a commit hash. See `firebreak.web.app`.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from firebreak.evals.report import git_commit  # noqa: E402
from firebreak.lab.bundle import derive_bundle_id  # noqa: E402
from firebreak.lab.scenario import load_library  # noqa: E402

EVAL_DIR = REPO_ROOT / "reports" / "eval"
BUNDLES_DIR = REPO_ROOT / "bundles"
SPECS_DIR = REPO_ROOT / "scenarios" / "specs"
OUTPUT = REPO_ROOT / "reports" / "site_stats.json"
CASSETTE_MANIFEST = REPO_ROOT / "recordings" / "cassettes" / "manifest.json"

# The run ids the recorder uses. A bundle directory is named by a hash of its scenario and
# run, so mapping bundles back to splits means deriving the ids rather than reading anything
# inside a bundle, which would be a route to the fault configuration.
RUN_IDS = ("run1", "run2")

# What a metric says when there is no number to show. Phrased as the condition that would
# produce it, because "TBD" on a released page tells a reader nothing about whether it is
# coming. SPEC.md Section 15 asks for a badge per mode; this is the text behind the badge.
NEEDS_LIBRARY = "not measured: the held-out splits have no recordings"
NEEDS_CREDENTIALS = "not measured: no model has run, so there is no model result to report"
NEEDS_BOTH = "not measured: needs the recorded library and model credentials"


class StatsError(Exception):
    """A reason the file cannot be built. Fatal, because a site built from a bad stats file
    publishes the bad numbers."""


@dataclass(frozen=True)
class Report:
    """One eval report, with where it came from."""

    configuration: str
    split: str
    path: Path
    payload: dict[str, Any]

    @property
    def source(self) -> str:
        """The path a reader can open, relative to the repository root."""
        return str(self.path.relative_to(REPO_ROOT))

    @property
    def quotable(self) -> bool:
        return bool(self.payload.get("quotable_as_a_result"))

    @property
    def data_source(self) -> str:
        return str(self.payload.get("data_source") or "unknown")

    @property
    def recorded_scenarios(self) -> int | None:
        value = self.payload.get("recorded_scenarios")
        return value if isinstance(value, int) else None

    @property
    def caveats(self) -> list[str]:
        values = self.payload.get("caveats")
        return [str(v) for v in values] if isinstance(values, list) else []

    def grader(self, name: str) -> dict[str, Any]:
        graders = (self.payload.get("overall") or {}).get("graders") or {}
        entry = graders.get(name)
        return entry if isinstance(entry, dict) else {}


@dataclass
class Metric:
    """One number for the site, or one stated absence.

    `value` is None whenever the number is not publishable, and `display` always carries
    something a page can render. Keeping both means a template cannot accidentally print a
    non-quotable number: there is nothing numeric in it to print.
    """

    key: str
    label: str
    value: float | None
    display: str
    mode: str
    source: str
    quotable: bool
    caveats: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "display": self.display,
            "mode": self.mode,
            "source": self.source,
            "quotable": self.quotable,
            "caveats": list(self.caveats),
        }


def newest_reports() -> dict[tuple[str, str], Report]:
    """The newest report per configuration and split, by its own `generated_at`."""
    found: dict[tuple[str, str], Report] = {}
    if not EVAL_DIR.is_dir():
        return found
    for configuration in sorted(p for p in EVAL_DIR.iterdir() if p.is_dir()):
        for split in sorted(p for p in configuration.iterdir() if p.is_dir()):
            candidates = []
            for path in sorted(split.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    raise StatsError(f"{path} cannot be read: {error}") from error
                if not isinstance(payload, dict):
                    raise StatsError(f"{path} is not a JSON object")
                stamp = payload.get("generated_at")
                candidates.append(((str(stamp or ""), path.name), payload, path))
            if not candidates:
                continue
            candidates.sort(key=lambda entry: entry[0])
            _, payload, path = candidates[-1]
            found[(configuration.name, split.name)] = Report(
                configuration=configuration.name, split=split.name, path=path, payload=payload
            )
    return found


def recorded_by_split() -> dict[str, int]:
    """How many scenarios of each split are recorded on disk right now.

    Derived from the specs, so no bundle is opened and no label is read. This is what a
    report's own `recorded_scenarios` is checked against.
    """
    if not SPECS_DIR.is_dir():
        return {}
    present = (
        {path.name for path in BUNDLES_DIR.iterdir() if (path / "manifest.json").is_file()}
        if BUNDLES_DIR.is_dir()
        else set()
    )
    counts: dict[str, int] = {}
    for spec in load_library(SPECS_DIR).values():
        counts.setdefault(spec.split.value, 0)
        if any(derive_bundle_id(spec.id, run) in present for run in RUN_IDS):
            counts[spec.split.value] += 1
    return counts


def stale_reason(report: Report, recorded: dict[str, int]) -> str | None:
    """Why this report no longer describes the repository, or None if it still does.

    Only checked for reports over recorded bundles. A fixture run describes fixtures, which
    are rebuilt from specs and cannot go stale.
    """
    if "recorded" not in report.data_source:
        return None
    claimed = report.recorded_scenarios
    if claimed is None:
        return None
    actual = recorded.get(report.split, 0)
    if claimed == actual:
        return None
    return (
        f"the report was computed over {claimed} recorded scenario(s) and {actual} "
        f"are on disk now, so it describes a state that no longer exists"
    )


# Splits whose figures can ever be a result. The others were available for tuning, so a
# number from them says how well the system fits data it was developed against.
RESULT_SPLITS = ("test_id", "test_ood")


def not_a_result_because(report: Report) -> str:
    """Why this report's numbers are not publishable, in the report's own terms.

    Derived from the report rather than from one blanket sentence, because a reader sees this
    string and not the caveats behind it. Saying "the held-out splits have no recordings"
    about a run over synthetic fixtures would be true of the repository and wrong about the
    report being cited, which is the kind of near-miss that survives review.
    """
    if "recorded" not in report.data_source:
        return "not a result: this report is over synthetic fixtures, not recorded incidents"
    claimed = report.recorded_scenarios
    total = report.payload.get("total_scenarios")
    if isinstance(claimed, int) and isinstance(total, int) and 0 < claimed < total:
        return f"not a result: only {claimed} of this split's {total} scenarios are recorded"
    if report.split not in RESULT_SPLITS:
        return f"not a result: {report.split} was available for tuning, so it measures fit"
    return "not a result: the report does not support a claim about performance"


def accuracy_metric(
    key: str, label: str, report: Report | None, grader: str, stale: str | None
) -> Metric:
    """One accuracy figure, published only if its report says it is quotable."""
    if report is None:
        return Metric(
            key=key,
            label=label,
            value=None,
            display=NEEDS_LIBRARY,
            mode="absent",
            source="",
            quotable=False,
            caveats=["No report exists for this configuration and split."],
        )
    if stale is not None:
        return Metric(
            key=key,
            label=label,
            value=None,
            display="not measured: the only report for this split is stale",
            mode="stale",
            source=report.source,
            quotable=False,
            caveats=[stale],
        )
    entry = report.grader(grader)
    accuracy = entry.get("accuracy")
    if not report.quotable or not isinstance(accuracy, int | float):
        return Metric(
            key=key,
            label=label,
            value=None,
            display=not_a_result_because(report),
            mode=report.data_source,
            source=report.source,
            quotable=False,
            caveats=report.caveats,
        )
    interval = entry.get("interval") or {}
    lower, upper = interval.get("lower"), interval.get("upper")
    display = f"{accuracy:.3f}"
    if isinstance(lower, int | float) and isinstance(upper, int | float):
        display = f"{accuracy:.3f} (95% CI {lower:.3f} to {upper:.3f})"
    return Metric(
        key=key,
        label=label,
        value=float(accuracy),
        display=display,
        mode=report.data_source,
        source=report.source,
        quotable=True,
        caveats=report.caveats,
    )


def cost_metric(report: Report | None, stale: str | None) -> Metric:
    """Cost per investigation, which is only a number once a model has run.

    Zero tokens means no model ran. Printing 0.000000 USD would say the system is free
    rather than unmeasured, which is the same mistake the Evaluation page avoids.
    """
    key, label = "cost_per_investigation_usd", "Cost per investigation"
    if report is None or stale is not None:
        return Metric(key, label, None, NEEDS_CREDENTIALS, "absent", "", False, [stale or ""])
    cost = (report.payload.get("overall") or {}).get("cost") or {}
    tokens = (cost.get("total_tokens_in") or 0) + (cost.get("total_tokens_out") or 0)
    if not tokens:
        return Metric(
            key,
            label,
            None,
            NEEDS_CREDENTIALS,
            report.data_source,
            report.source,
            False,
            ["A cost of zero tokens means no model ran, so there is no cost to report."],
        )
    median = cost.get("median_usd")
    if not isinstance(median, int | float) or not report.quotable:
        return Metric(
            key, label, None, NEEDS_BOTH, report.data_source, report.source, False, report.caveats
        )
    return Metric(
        key,
        label,
        float(median),
        f"{median:.4f} USD",
        report.data_source,
        report.source,
        True,
        report.caveats,
    )


def library_counts() -> dict[str, Any]:
    """The library's size, from the committed specs and nothing else.

    **No recording count.** How many bundles exist is a fact about one machine: they average
    7.6 MB, `bundles/` is git-ignored, and a reader who clones this has none. Publishing "8
    recorded" would describe this laptop while reading as a property of the artefact, and it
    would make the file unreproducible in CI, where the count is zero. The recordings are a
    release asset with checksums, so the README says that instead of a number.
    """
    if not SPECS_DIR.is_dir():
        raise StatsError(f"{SPECS_DIR} is missing, so the library cannot be counted")
    specs = load_library(SPECS_DIR)
    by_split: dict[str, int] = {}
    for spec in specs.values():
        by_split[spec.split.value] = by_split.get(spec.split.value, 0) + 1
    return {
        "specs": len(specs),
        "specs_by_split": dict(sorted(by_split.items())),
        "recordings_note": (
            "Recordings are not in this repository. Bundles average 7.6 MB and bundles/ is "
            "git-ignored; they ship as a release asset with checksums."
        ),
        "source": "scenarios/specs",
    }


def showcase_counts() -> dict[str, Any]:
    """The offline demo's size, from the committed cassette manifest."""
    if not CASSETTE_MANIFEST.is_file():
        raise StatsError(f"{CASSETTE_MANIFEST} is missing; run make cassettes")
    manifest = json.loads(CASSETTE_MANIFEST.read_text(encoding="utf-8"))
    incidents = manifest.get("incidents")
    if not isinstance(incidents, dict):
        raise StatsError("the cassette manifest has no incidents map")
    return {
        "incidents": len(incidents),
        "cassettes": sum(len(entry.get("cassettes") or []) for entry in incidents.values()),
        "recorded_from_mode": str(manifest.get("recorded_from_mode") or "unknown"),
        "source": "recordings/cassettes/manifest.json",
    }


def readme_rows(
    metrics: list[Metric], library: dict[str, Any], showcase: dict[str, Any]
) -> list[dict[str, str]]:
    """The README's results table, in the shape `scripts/check_repo_hygiene.py` renders.

    Counts first, because they are the numbers this repository can actually stand behind, and
    the accuracy rows below them say plainly that there is no result yet. A table that led
    with an unmeasured accuracy would read as a result withheld rather than as one not taken.
    """
    rows = [
        {
            "metric": "Scenario library",
            "value": (
                f"{library['specs']} scenario specs; recordings ship as a release asset, "
                "not in this repository"
            ),
            "source": library["source"],
        },
        {
            "metric": "Offline demo",
            "value": (
                f"{showcase['incidents']} incidents, {showcase['cassettes']} cassettes "
                f"recorded from mode {showcase['recorded_from_mode']}"
            ),
            "source": showcase["source"],
        },
    ]
    for metric in metrics:
        rows.append(
            {
                "metric": metric.label,
                "value": metric.display,
                "source": metric.source or "no report",
            }
        )
    return rows


def build() -> dict[str, Any]:
    reports = newest_reports()
    recorded = recorded_by_split()
    stale = {key: stale_reason(report, recorded) for key, report in reports.items()}

    def pick(configuration: str, split: str) -> tuple[Report | None, str | None]:
        report = reports.get((configuration, split))
        return report, stale.get((configuration, split))

    fb_test_id = pick("fb-v1", "test_id")
    fb_test_ood = pick("fb-v1", "test_ood")
    b0_test_id = pick("b0", "test_id")

    metrics = [
        accuracy_metric(
            "root_cause_accuracy_test_id",
            "Root cause accuracy, held-out ID test",
            fb_test_id[0],
            "root_cause",
            fb_test_id[1],
        ),
        accuracy_metric(
            "root_cause_accuracy_test_ood",
            "Root cause accuracy, held-out OOD test",
            fb_test_ood[0],
            "root_cause",
            fb_test_ood[1],
        ),
        accuracy_metric(
            "baseline_root_cause_accuracy_test_id",
            "B0 triage accuracy, held-out ID test",
            b0_test_id[0],
            "root_cause",
            b0_test_id[1],
        ),
        accuracy_metric(
            "abstention_accuracy_test_id",
            "Abstention accuracy, held-out ID test",
            fb_test_id[0],
            "abstention",
            fb_test_id[1],
        ),
        cost_metric(*fb_test_id),
    ]

    library = library_counts()
    showcase = showcase_counts()
    unmeasured = [metric.key for metric in metrics if not metric.quotable]
    stale_reports = {
        f"{configuration}/{split}": reason
        for (configuration, split), reason in stale.items()
        if reason
    }
    bundles_present = BUNDLES_DIR.is_dir() and any(
        (path / "manifest.json").is_file() for path in BUNDLES_DIR.iterdir()
    )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "commit": git_commit(),
        "library": library,
        "showcase": showcase,
        "metrics": {metric.key: metric.as_dict() for metric in metrics},
        "readme_rows": readme_rows(metrics, library, showcase),
        "unmeasured": unmeasured,
        # Everything derived from bundles on disk lives here and nowhere else. It differs
        # between machines by definition, so `--check` ignores this key and no page reads
        # it. Keeping it in the file at all is worth it because the staleness finding is
        # the only record that a committed report describes bundles that no longer exist.
        "local": {
            "bundles_present": bundles_present,
            "recorded_by_split": dict(sorted(recorded.items())),
            "stale_reports": stale_reports,
            "note": (
                "Derived from bundles on disk, so it describes the machine that ran the "
                "build. Not published on any page and not compared by --check. With no "
                "bundles present the staleness check cannot run and says nothing."
                if bundles_present
                else "No bundles on this machine, so staleness could not be checked."
            ),
        },
        "reports_considered": {
            f"{configuration}/{split}": report.source
            for (configuration, split), report in sorted(reports.items())
        },
        "honesty": (
            "A metric is published as a number only when its report says "
            "quotable_as_a_result. Everything else is a stated absence with its reason. "
            f"{len(unmeasured)} of {len(metrics)} metrics are unmeasured."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the output file differs from what would be written",
    )
    arguments = parser.parse_args()

    try:
        stats = build()
    except StatsError as error:
        print(f"site stats: {error}", file=sys.stderr)
        return 1

    rendered = json.dumps(stats, indent=2, sort_keys=True) + "\n"
    if arguments.check:
        if not arguments.output.is_file():
            print(f"site stats: {arguments.output} does not exist", file=sys.stderr)
            return 1
        current = json.loads(arguments.output.read_text(encoding="utf-8"))
        fresh = json.loads(rendered)
        # `local` is disk-derived and differs between machines; `generated_at` and
        # `commit` change on every run. Comparing them would make this check fail in CI,
        # where there are no bundles, and that failure would say the file is stale rather
        # than that the comparison was wrong.
        for key in ("generated_at", "commit", "local"):
            current.pop(key, None)
            fresh.pop(key, None)
        if current != fresh:
            print(
                "site stats: the committed file is stale; run scripts/build_site_stats.py",
                file=sys.stderr,
            )
            return 1
        print("site stats: up to date")
        return 0

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(rendered, encoding="utf-8")
    print(f"site stats: wrote {arguments.output.relative_to(REPO_ROOT)}")
    print(f"  {stats['honesty']}")
    local = stats["local"]
    if not local["bundles_present"]:
        print("  no bundles on this machine, so report staleness was not checked")
    for name, reason in sorted(local["stale_reports"].items()):
        print(f"  stale: {name}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
