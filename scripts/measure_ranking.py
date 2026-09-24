"""Measure how well candidate ranking finds the culprit, and write the report.

Two measurements, one harness, because they have to be comparable:

- **anomaly only**, services ranked by their worst metric score with no
  dependency graph at all. This is the number that justified building the
  graph in the first place, and Phase 3 recorded it without leaving behind
  anything that could reproduce it. A measurement nobody can re-run is an
  assertion, which is precisely what this project claims not to ship.
- **graph ranking**, the full deterministic pipeline, plus an ablation over
  the parameters so that each one earns its place with a number rather than
  with a citation.

Run it with:

    make measure-ranking

**This runs on synthetic fixtures**, not on recorded incidents, and the
report says so in its own fields. It is a design signal for choosing between
implementations. It is never a result, and nothing in `reports/` that came
from here may be quoted as one.

Tuning happens on train and validation only. `test_ood` holds entire fault
families back and is not read here, which is the point of holding it back.
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _reporting import describe_path
from firebreak.lab.bundle import derive_bundle_id
from firebreak.lab.scenario import ScenarioSpec, Split, load_library
from firebreak.lab.synthetic import build_synthetic_bundle
from firebreak.triage.anomaly import rank_anomalies
from firebreak.triage.pipeline import TriageResult, triage_bundle

REPO_ROOT = Path(__file__).resolve().parent.parent
SPECS_DIR = REPO_ROOT / "scenarios" / "specs"
REPORT_PATH = REPO_ROOT / "reports" / "triage" / "ranking_ablation.json"
BASELINE_PATH = REPO_ROOT / "reports" / "triage" / "anomaly_only_ranking.json"

# Several seeds per scenario, because one seed is an anecdote. The synthetic
# builder is deterministic in its seed, so this is a spread over plausible
# recordings of the same fault rather than over random noise.
SEEDS = (1, 2, 3)

# Splits that may be read while tuning. SPEC.md Section 9 holds `test_ood`
# back precisely so that a number from it means something later.
TUNING_SPLITS = (Split.TRAIN, Split.VALIDATION)

# Each entry is a named configuration passed straight to `rank_candidates`.
# Named rather than a grid sweep, because the question is not "which of two
# hundred settings wins on this fixture", which on synthetic data is
# overfitting with extra steps. The question is whether each mechanism earns
# its place.
#
# An ablation either removes something the defaults turn on or adds
# something they turn off. Both directions are needed: the self-loop and
# backward-edge mechanisms default to off precisely because this harness
# measured them as harmful, and an ablation that removed an already absent
# mechanism would silently be testing nothing.
ABLATIONS: dict[str, dict[str, Any]] = {
    "graph_default": {},
    # Removing what the defaults rely on.
    "no_endpoint_scaling": {"use_endpoint_scaling": False},
    "no_onset_bonus": {"onset_bonus": 1.0},
    # Adding back what the defaults switch off, which is how the decision to
    # switch them off stays falsifiable.
    "with_self_loops": {"self_loop_ratio": 0.5},
    "with_backward_edges": {"backward_ratio": 0.3},
    "eirwr_as_published": {"self_loop_ratio": 0.5, "backward_ratio": 0.3, "sharpness": 2.0},
    # The restart exponent, either side of the default.
    "sharper_restart": {"sharpness": 2.0},
    "sharpest_restart": {"sharpness": 3.0},
}

# Where to cut the recording when no alert says where the incident began.
# Swept separately from ABLATIONS because it is not a ranking parameter: it
# changes what data the ranking sees rather than what it does with it.
#
# It matters more than it looks. A split landing after the fault has already
# started puts fault data in the baseline, which makes the incident look
# less unusual, and leaves the onset outside the incident window entirely,
# which makes every service appear to go abnormal at the same instant.
BASELINE_FRACTIONS = (0.2, 0.25, 0.3333333333333333, 0.4)


@dataclass(frozen=True)
class WindowTrial:
    """One scenario at one baseline fraction, and what the split cost."""

    scenario_id: str
    target: str | None
    rank: int | None
    distinct_onsets: int
    services_with_onset: int


@dataclass(frozen=True)
class Trial:
    """One scenario at one seed, and where the culprit placed.

    `top_anomaly` is carried alongside the rank because a ranking on its own
    cannot say "nothing is wrong". PageRank always returns an order, so a
    quiet system produces a confident-looking first place just as a broken
    one does. Separating those two cases needs a magnitude, not an order.
    """

    scenario_id: str
    family: str
    target: str | None
    rank: int | None
    top_anomaly: float = 0.0

    @property
    def is_no_fault(self) -> bool:
        return self.target is None


def _top_anomaly(result: TriageResult) -> float:
    """The largest trustworthy anomaly score anywhere in this incident.

    The quantity an abstention threshold has to work on: how unusual the
    most unusual thing in the system was. A no-fault recording still has a
    busiest service, so the question is never "is anything the worst" but
    "is the worst bad enough to be worth waking someone for".
    """
    trustworthy = [s.score for s in result.anomalies if s.is_trustworthy]
    return max(trustworthy, default=0.0)


def _rank_in(ordered: list[str], target: str | None) -> int | None:
    """One-based position of the target, or None if it is not in the list.

    A no-fault scenario has no target. Its correct answer is that nothing is
    at fault, which this harness scores separately rather than folding into
    a rank that would be meaningless.
    """
    if target is None:
        return None
    return ordered.index(target) + 1 if target in ordered else None


def _summarise(trials: list[Trial]) -> dict[str, Any]:
    """Top-1, top-3 and the rank histogram for one group of trials."""
    ranked = [t for t in trials if t.target is not None]
    histogram = Counter(str(t.rank) if t.rank else "unranked" for t in ranked)
    return {
        "trials": len(ranked),
        "top1": sum(1 for t in ranked if t.rank == 1),
        "top3": sum(1 for t in ranked if t.rank is not None and t.rank <= 3),
        "mean_reciprocal_rank": round(sum(1.0 / t.rank for t in ranked if t.rank) / len(ranked), 4)
        if ranked
        else 0.0,
        "rank_distribution": dict(sorted(histogram.items())),
    }


def _by_family(trials: list[Trial]) -> dict[str, Any]:
    families: dict[str, list[Trial]] = {}
    for trial in trials:
        families.setdefault(trial.family, []).append(trial)
    return {family: _summarise(group) for family, group in sorted(families.items())}


def _run(specs: list[ScenarioSpec], workspace: Path) -> dict[str, list[Trial]]:
    """Build each bundle once, then rank it under every configuration.

    Building dominates the runtime, so the loop is bundle-outermost and
    configuration-innermost. Every configuration therefore sees byte
    identical inputs, which is what makes the comparison between them a
    comparison of the ranking rather than of the fixtures.
    """
    results: dict[str, list[Trial]] = {name: [] for name in ABLATIONS}
    results["anomaly_only"] = []

    for spec in specs:
        for seed in SEEDS:
            run_id = f"seed{seed}"
            bundle_dir = workspace / derive_bundle_id(spec.id, run_id)
            build_synthetic_bundle(bundle_dir, spec, run_id, seed=seed)

            for name, options in ABLATIONS.items():
                result = triage_bundle(bundle_dir, verify=False, **options)
                ordered = [c.service for c in result.candidates]
                results[name].append(
                    Trial(
                        spec.id,
                        spec.family,
                        spec.target_service,
                        _rank_in(ordered, spec.target_service),
                        _top_anomaly(result),
                    )
                )

            # The no-graph comparison, taken from the same triage run's own
            # anomaly scores so the two rankings cannot disagree about what
            # the metrics said.
            plain = triage_bundle(bundle_dir, verify=False)
            ordered_anomaly: list[str] = []
            for score in rank_anomalies(plain.anomalies):
                if score.subject not in ordered_anomaly:
                    ordered_anomaly.append(score.subject)
            results["anomaly_only"].append(
                Trial(
                    spec.id,
                    spec.family,
                    spec.target_service,
                    _rank_in(ordered_anomaly, spec.target_service),
                    _top_anomaly(plain),
                )
            )

    return results


def _abstention(trials: list[Trial]) -> dict[str, Any]:
    """Find the threshold that best tells a real incident from a quiet system.

    SPEC.md Section 6.2 includes a no-fault family for exactly this reason,
    and `firebreak.triage.anomaly` promises that thresholds are tuned on the
    validation split with a report rather than chosen by eye. This is that
    report.

    The score is Youden's J, sensitivity plus specificity minus one, which
    weighs a missed incident and a false alarm equally. That is a judgement
    and not a fact, and it is stated here so a reader who disagrees can pick
    a different point off the curve, which is why the whole curve is
    written out rather than only the winner.
    """
    faulted = sorted(t.top_anomaly for t in trials if not t.is_no_fault)
    quiet = sorted(t.top_anomaly for t in trials if t.is_no_fault)
    if not faulted or not quiet:
        return {"usable": False, "reason": "needs both faulted and no-fault trials"}

    candidates = sorted({round(v, 3) for v in faulted + quiet})
    # Typed, so that comparing thresholds later is a comparison of floats
    # rather than of objects mypy cannot order.
    curve: list[dict[str, float]] = []
    for threshold in candidates:
        # At or above the threshold is "something is wrong".
        detected = sum(1 for v in faulted if v >= threshold)
        false_alarms = sum(1 for v in quiet if v >= threshold)
        sensitivity = detected / len(faulted)
        specificity = 1.0 - false_alarms / len(quiet)
        curve.append(
            {
                "threshold": threshold,
                "sensitivity": round(sensitivity, 4),
                "specificity": round(specificity, 4),
                "youden_j": round(sensitivity + specificity - 1.0, 4),
            }
        )

    # Youden's J ties across an entire separating gap: every threshold
    # between the loudest quiet system and the quietest real incident scores
    # a perfect one. Breaking that tie on "lowest" would sit the threshold
    # exactly on the noisiest quiet recording observed, which is the most
    # fragile point in the range rather than the safest.
    #
    # The robust choice is the middle of the gap, and the middle is
    # geometric rather than arithmetic because these are z-scores, where the
    # meaningful distance between 8 and 64 is a ratio and not a difference.
    best_j = max(point["youden_j"] for point in curve)
    tied = [point for point in curve if point["youden_j"] == best_j]
    chosen: dict[str, Any]
    if faulted[0] > quiet[-1]:
        chosen_threshold = (
            round(math.sqrt(quiet[-1] * faulted[0]), 3) if quiet[-1] > 0 else faulted[0]
        )
        detected = sum(1 for v in faulted if v >= chosen_threshold)
        false_alarms = sum(1 for v in quiet if v >= chosen_threshold)
        chosen = {
            "threshold": chosen_threshold,
            "sensitivity": round(detected / len(faulted), 4),
            "specificity": round(1.0 - false_alarms / len(quiet), 4),
            "youden_j": round(detected / len(faulted) - false_alarms / len(quiet), 4),
            "rule": "geometric midpoint of a fully separating gap",
        }
    else:
        # Overlapping classes, so there is a genuine trade-off and no gap to
        # sit in the middle of. Take the highest of the tied-best thresholds,
        # which is the one that raises the fewest false alarms.
        best = max(tied, key=lambda point: point["threshold"])
        chosen = {**best, "rule": "best Youden J"}

    return {
        "usable": True,
        "faulted_trials": len(faulted),
        "no_fault_trials": len(quiet),
        "faulted_top_anomaly": {
            "min": round(faulted[0], 3),
            "median": round(faulted[len(faulted) // 2], 3),
            "max": round(faulted[-1], 3),
        },
        "no_fault_top_anomaly": {
            "min": round(quiet[0], 3),
            "median": round(quiet[len(quiet) // 2], 3),
            "max": round(quiet[-1], 3),
        },
        "separable": faulted[0] > quiet[-1],
        "gap": {"quiet_max": round(quiet[-1], 3), "faulted_min": round(faulted[0], 3)},
        "warning": (
            "A gap this wide is a property of synthetic fixtures, not of production "
            "telemetry. Treat the chosen threshold as a starting point to re-tune on "
            "recorded incidents, never as a validated operating point."
        ),
        "chosen": chosen,
        "curve": curve,
    }


def _sweep_windows(specs: list[ScenarioSpec], workspace: Path) -> dict[str, Any]:
    """Measure the window split, on accuracy and on whether onset survives.

    Onset ordering is the one signal in SPEC.md Section 6.5 that a ranking
    cannot recover by other means: it is what separates a cause from a
    symptom when both are equally unwell. A split that lands after the fault
    has begun destroys it silently, leaving every service tied at zero
    seconds, and the ablation over `onset_bonus` cannot show that because a
    bonus applied to everyone cancels out.
    """
    from firebreak.triage.thresholds import Thresholds, load_thresholds

    base = load_thresholds()
    results: dict[str, list[WindowTrial]] = {}

    for fraction in BASELINE_FRACTIONS:
        tuned = Thresholds.model_validate(
            {
                "abstention": base.abstention.model_dump(),
                "ranking": base.ranking.model_dump(),
                "windows": {
                    "baseline_fraction": fraction,
                    "guard_seconds": base.windows.guard_seconds,
                },
            }
        )
        trials: list[WindowTrial] = []
        for spec in specs:
            for seed in SEEDS:
                run_id = f"seed{seed}"
                bundle_dir = workspace / derive_bundle_id(spec.id, run_id)
                if not bundle_dir.exists():
                    build_synthetic_bundle(bundle_dir, spec, run_id, seed=seed)
                result = triage_bundle(bundle_dir, verify=False, thresholds=tuned)
                ordered = [c.service for c in result.candidates]
                trials.append(
                    WindowTrial(
                        scenario_id=spec.id,
                        target=spec.target_service,
                        rank=_rank_in(ordered, spec.target_service),
                        distinct_onsets=len(set(result.onsets.values())),
                        services_with_onset=len(result.onsets),
                    )
                )
        results[f"{fraction:.4f}"] = trials

    summary = {}
    for name, trials in results.items():
        ranked = [t for t in trials if t.target is not None]
        # A split that preserves onset ordering produces more than one
        # distinct onset time. All services tied at zero means the incident
        # window opened after everything had already gone wrong.
        informative = sum(1 for t in trials if t.distinct_onsets > 1)
        summary[name] = {
            "trials": len(ranked),
            "top1": sum(1 for t in ranked if t.rank == 1),
            "top3": sum(1 for t in ranked if t.rank is not None and t.rank <= 3),
            "trials_where_onset_ordering_survives": informative,
            "onset_informative_share": round(informative / len(trials), 4) if trials else 0.0,
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=0, help="only use the first N scenarios (for a quick check)"
    )
    arguments = parser.parse_args()

    specs = [s for s in load_library(SPECS_DIR).values() if s.split in TUNING_SPLITS]
    specs.sort(key=lambda s: s.id)
    if arguments.limit:
        specs = specs[: arguments.limit]

    with tempfile.TemporaryDirectory(prefix="firebreak-ranking-") as temporary:
        results = _run(specs, Path(temporary))
        window_sweep = _sweep_windows(specs, Path(temporary))

    caveat = {
        "data_source": (
            "synthetic fixtures (src/firebreak/lab/synthetic.py), NOT recorded incidents"
        ),
        "caveat": "Synthetic data. This is a design signal, never a reported result.",
        "splits": [split.value for split in TUNING_SPLITS],
        "seeds": list(SEEDS),
        "scenarios": len(specs),
        "generated_by": "scripts/measure_ranking.py",
    }

    report = {
        "what": "Culprit rank under each ranking configuration",
        "why": (
            "Every mechanism in firebreak.graph.ranking should earn its place with a "
            "measurement rather than with a citation. Each ablation removes exactly one."
        ),
        **caveat,
        "abstention": _abstention(results["graph_default"]),
        "baseline_fraction_sweep": window_sweep,
        "configurations": {
            name: {
                "options": ABLATIONS.get(name, {}),
                "overall": _summarise(trials),
                "by_family": _by_family(trials),
            }
            for name, trials in results.items()
        },
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    baseline = {
        "what": "Culprit rank from metric anomaly scoring alone, with no dependency graph",
        "why": (
            "Motivates the topology ranking in SPEC.md Section 6.4. If anomaly scoring alone "
            "found the culprit reliably, the graph would be unnecessary complexity."
        ),
        **caveat,
        "overall": _summarise(results["anomaly_only"]),
        "by_family": _by_family(results["anomaly_only"]),
    }
    BASELINE_PATH.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")

    for name in ("anomaly_only", *ABLATIONS):
        summary = _summarise(results[name])
        print(
            f"{name:22s} top1={summary['top1']:3d}/{summary['trials']:3d} "
            f"top3={summary['top3']:3d} mrr={summary['mean_reciprocal_rank']:.3f}"
        )
    print(f"\nwrote {describe_path(REPORT_PATH)}")
    print(f"wrote {describe_path(BASELINE_PATH)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
