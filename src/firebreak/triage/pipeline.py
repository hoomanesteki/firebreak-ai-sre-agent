"""Deterministic triage: bundle in, ranked candidates out, no model involved.

SPEC.md principle H11 puts grounding before generative reasoning. Everything
here runs before the first token is spent, so the agent starts from a short
list of named suspects with the evidence already attached, rather than
searching the system blind and paying for the search in context.

Being model-free is also what makes this measurable. The B0 baseline in
SPEC.md Section 17 is exactly this pipeline with no agent at all, and any
later claim that the agent helps has to beat it.

**What this is allowed to know.** The bundle manifest and nothing else. Not
the scenario, not the fault class, not the target service, and in particular
not the fault timing: a `ScenarioSpec` carries `warmup_seconds`, which would
hand this code the exact moment the incident began. A real investigation
starts from an alert or from a complaint, so this one starts from
`alert_fired_at` when there is an alert and from a fixed split of the
recorded window when there is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from firebreak.backends.bundle_duckdb import BundleBackend
from firebreak.graph.ranking import Candidate, edges_from_topology, rank_candidates
from firebreak.lab.bundle import BundleManifest, BundleReader
from firebreak.tools.evidence import TimeRange
from firebreak.triage.anomaly import AnomalyScore, rank_anomalies
from firebreak.triage.scoring import onset_seconds, score_services
from firebreak.triage.thresholds import Thresholds, WindowThresholds, load_thresholds

# The defaults below are duplicated in `config/thresholds.yaml`, which is
# where an operator changes them. A contract test asserts the two agree.
#
# Where to cut the recorded window when no alert says where the incident
# began. A recording is warmup, then fault, then cooldown, and the first
# third is the part most likely to be entirely warmup. Getting this slightly
# wrong is realistic rather than fatal: a baseline containing a little of
# the incident makes the incident look less unusual, which costs sensitivity
# and never invents a signal that is not there.
BASELINE_FRACTION = 1.0 / 3.0

# A gap between the end of the baseline and the start of the incident, so a
# slow onset does not contaminate the baseline it is being compared against.
GUARD_SECONDS = 30.0

# The range a derived confidence may occupy. It never reaches either end:
# deterministic triage reasons from two signals and has no business claiming
# certainty, and a floor above zero records that it did name a suspect.
#
# Kept narrow deliberately. A wide range would invite reading these as
# probabilities, and they are an ordering of confidence rather than a
# calibrated forecast until the calibration report says otherwise.
CONFIDENCE_FLOOR = 0.35
CONFIDENCE_CEILING = 0.85

# How many suspects to carry forward. SPEC.md Section 6.5 wants a short
# ranked list, and a list long enough to always contain the answer is not a
# ranking, it is the service inventory.
TOP_CANDIDATES = 5


@dataclass(frozen=True)
class TriageResult:
    """What deterministic triage concluded, before any model was asked."""

    bundle_id: str
    candidates: list[Candidate]
    anomalies: list[AnomalyScore]
    onsets: dict[str, float]
    baseline: TimeRange
    incident: TimeRange
    anchored_on_alert: bool
    top_anomaly: float
    abstention_threshold: float

    @property
    def says_nothing_is_wrong(self) -> bool:
        """True when nothing in the system was unusual enough to report.

        A ranking cannot express this on its own. PageRank returns an order
        whatever it is given, so a perfectly healthy system still yields a
        confident looking first place, and presenting that as a culprit is
        the worst failure this project can have: it is how an operator
        learns to ignore the tool.

        The threshold is tuned on the validation split and recorded in
        `config/thresholds.yaml` with the measurement behind it.
        """
        return self.top_anomaly < self.abstention_threshold

    @property
    def confidence(self) -> float | None:
        """How much to believe the leading suspect, from 0 to 1.

        Calibration is a headline metric (SPEC.md Section 9.3), and a
        configuration that states no confidence cannot be calibrated at all,
        so B0 needs one or the whole calibration column is empty for the
        baseline every other configuration is compared against.

        Derived from the one thing that actually distinguishes a trustworthy
        ranking from a lucky one: the margin between first and second place. A
        first place that beat second tenfold is a different finding from one
        that won by a rounding error, and nothing else available here carries
        that information.

        The margin is mapped through a ratio rather than a difference, because
        PageRank scores are shares of a fixed total: subtracting them makes
        the number depend on how many services are in the graph, while their
        ratio does not.

        None when abstaining. A system that has declined to name anybody has
        no claim to attach a confidence to, and reporting a low confidence
        instead would put it on the calibration curve as a near miss when it
        made no prediction at all.
        """
        if self.says_nothing_is_wrong or not self.candidates:
            return None
        top = self.candidates[0]
        if len(self.candidates) < 2:
            # One candidate and nothing to compare it against. Not treated as
            # certainty: an uncontested answer is uninformative rather than
            # convincing.
            return CONFIDENCE_FLOOR
        runner_up = self.candidates[1]
        if runner_up.graph_score <= 0.0:
            return CONFIDENCE_CEILING
        ratio = top.graph_score / runner_up.graph_score
        # A ratio of 1 means a tie and maps to the floor; the ceiling is
        # approached asymptotically, so the pipeline never claims certainty.
        scaled = 1.0 - 1.0 / max(ratio, 1.0)
        spread = CONFIDENCE_CEILING - CONFIDENCE_FLOOR
        return round(CONFIDENCE_FLOOR + spread * scaled, 4)

    @property
    def top_service(self) -> str | None:
        """The leading suspect, or None when the system abstains.

        Deliberately None rather than the first candidate when abstaining,
        so that a caller which ignores `says_nothing_is_wrong` still cannot
        accidentally report a culprit the pipeline does not believe in.
        """
        if self.says_nothing_is_wrong or not self.candidates:
            return None
        return self.candidates[0].service

    def rank_of(self, service: str) -> int | None:
        """Where a named service placed, one-based, or None if unranked."""
        for position, candidate in enumerate(self.candidates, start=1):
            if candidate.service == service:
                return position
        return None


def choose_windows(
    manifest: BundleManifest, windows: WindowThresholds | None = None
) -> tuple[TimeRange, TimeRange, bool]:
    """Split a recording into a baseline and an incident window.

    Returns the two windows and whether an alert anchored them, because a
    report should be able to say which it was: "the alert fired at 14:02" is
    a different kind of statement from "the second half of the recording".

    When an alert fired, the incident runs from the alert to the end of the
    recording and the baseline is everything before it, less a guard. When
    no alert fired there is nothing to anchor on, so the window is split by
    `BASELINE_FRACTION`.
    """
    baseline_fraction = windows.baseline_fraction if windows else BASELINE_FRACTION
    guard_seconds = windows.guard_seconds if windows else GUARD_SECONDS

    window = manifest.window
    total = window.duration_seconds

    anchor = None
    if manifest.alert_fired_at is not None:
        candidate = manifest.alert_fired_at
        # An alert at the very start leaves no baseline to compare against,
        # in which case the fixed split is the more useful answer.
        if window.start + timedelta(seconds=guard_seconds * 2) < candidate < window.end:
            anchor = candidate

    if anchor is None:
        anchor = window.start + timedelta(seconds=total * baseline_fraction)
        anchored = False
    else:
        anchored = True

    baseline_end = max(anchor - timedelta(seconds=guard_seconds), window.start)
    return (
        TimeRange(start=window.start, end=baseline_end),
        TimeRange(start=anchor, end=window.end),
        anchored,
    )


def triage_bundle(
    bundle_dir: Path,
    top_candidates: int = TOP_CANDIDATES,
    verify: bool = True,
    thresholds: Thresholds | None = None,
    **ranking_options: float | bool,
) -> TriageResult:
    """Run the whole deterministic pipeline over one recorded bundle.

    `ranking_options` override the tuned values from `config/thresholds.yaml`
    one at a time, which is how the ablation harness sweeps parameters
    without keeping a second copy of this function that can drift away from
    the one that ships.
    """
    tuned = thresholds or load_thresholds()
    options: dict[str, float | bool] = {
        **tuned.ranking.as_ranking_options(),
        **ranking_options,
    }
    reader = BundleReader(bundle_dir, verify=verify)
    baseline, incident, anchored = choose_windows(reader.manifest, tuned.windows)

    # The backend reuses the reader rather than opening the bundle a second
    # time, so the checksums are verified once per triage rather than twice.
    with BundleBackend(reader) as backend:
        scores = score_services(backend, baseline, incident)
        onsets = onset_seconds(
            backend, baseline, incident, threshold=tuned.ranking.onset_threshold_z
        )

    edges = edges_from_topology(reader.topology())
    candidates = rank_candidates(edges, scores, onsets=onsets, **options)  # type: ignore[arg-type]

    ranked = rank_anomalies(scores)
    return TriageResult(
        bundle_id=reader.manifest.bundle_id,
        candidates=candidates[:top_candidates],
        anomalies=ranked[:top_candidates],
        onsets=onsets,
        baseline=baseline,
        incident=incident,
        anchored_on_alert=anchored,
        # The worst score anywhere, not the worst among the few carried
        # forward, since the question abstention answers is whether anything
        # at all was unusual.
        top_anomaly=max((s.score for s in ranked), default=0.0),
        abstention_threshold=tuned.abstention.minimum_top_anomaly_z,
    )
