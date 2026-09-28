"""The ten showcase incidents the offline demo replays, and how to build them anywhere.

SPEC.md Section 17 Phase 11 asks for replay cassettes for ten showcase incidents and for
`make demo-offline` to be green.

**Why the showcase is built from scenario specs rather than from recorded bundles.** A
recorded bundle is 2.7 MB and the library is git-ignored, so a demo keyed to real
recordings runs on exactly one machine and fails in CI and on a fresh clone. That would
make the CI check either a failure or, worse, a skip that reports success having checked
nothing.

So the showcase is ten scenario specs, and the bundles are rebuilt from them with a fixed
run id and seed. `build_synthetic_bundle` is deterministic in its seed, so the same spec
produces byte-identical parquet every time and the cassette keys, which hash the windows
and the evidence ids, stay valid.

**What is honestly lost by this, stated rather than glossed.** These are fixtures, and
this project's largest recurring finding is that fixtures are a different scale from
reality rather than merely easier: B0 goes from perfect on fixtures to two of nine on
recordings. So the offline demo demonstrates that the whole path runs and reproduces. It
does not demonstrate how well the system does on real telemetry, and the demo's own output
says which of those it is showing.

**Chosen to cover the failure modes rather than to look good.** One from each family that
exists, including a no-fault incident where abstaining is the right answer and a distractor
where a harmless change sits next to a real fault. A showcase of ten wins would be a
brochure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from firebreak.lab.bundle import derive_bundle_id
from firebreak.lab.scenario import ScenarioSpec, load_library
from firebreak.lab.synthetic import build_synthetic_bundle

REPO_ROOT = Path(__file__).resolve().parents[3]
SPECS_DIR = REPO_ROOT / "scenarios" / "specs"

# The run id and seed the showcase is built with. Fixed, because the cassette keys hash
# the windows and evidence ids that come out of the bundle, so a different seed would
# invalidate every cassette silently: the graph would ask a question the recording does
# not answer, and the demo would report a missing cassette rather than a changed seed.
SHOWCASE_RUN_ID = "showcase"
SHOWCASE_SEED = 11

# Ten scenarios, chosen so the demo shows what the system does rather than what it does
# well. Each line says why it is here, because a showcase whose selection nobody can
# justify is a showcase somebody will quietly replace with easier cases.
SHOWCASE_SCENARIOS: tuple[tuple[str, str], ...] = (
    (
        "payment-failure-50pct-20u",
        "The clearest case: half of payment's calls fail and the errors are everywhere.",
    ),
    (
        "payment-failure-25pct-20u",
        "The same fault at half the rate, which is where a detector's threshold shows.",
    ),
    (
        "intl-shipping-slowdown-10sec-20u",
        "A latency fault, which on real recordings scores far lower than an error fault "
        "and is the case abstention currently gets wrong.",
    ),
    (
        "kafka-queue-problems-on-50u-sr5",
        "Queue lag, where the broken service and the symptomatic services differ.",
    ),
    (
        "failed-readiness-probe-on-50u-sr5",
        "A health check failure, where the culprit was not in the top three on the real recording.",
    ),
    (
        "payment-unreachable-on-50u-sr5",
        "A service that is gone rather than failing, which produces a different signal.",
    ),
    (
        "no-fault-flood-homepage-sr20-20u",
        "No fault at all. Abstaining is the right answer and naming anybody is the worst "
        "failure this system can have.",
    ),
    (
        "no-fault-flood-homepage-sr2-20u",
        "A quieter healthy system, so the demo shows abstention at two load levels rather "
        "than one.",
    ),
    (
        "distractor-intl-shipping-slowdown-5sec-50u",
        "A harmless change beside a real fault, which is the case correlation gets wrong.",
    ),
    (
        "double-fault-payment-failure-cart-failure-20u",
        "Two faults at once, where naming one is partly right and the report has to say which.",
    ),
)


class ShowcaseError(Exception):
    """A showcase scenario is missing from the library."""


@dataclass(frozen=True)
class ShowcaseIncident:
    """One showcase incident: its spec, its bundle id, and why it is in the set."""

    spec: ScenarioSpec
    bundle_id: str
    reason: str


def showcase(specs_dir: Path | None = None) -> list[ShowcaseIncident]:
    """The showcase set, refusing to quietly shrink.

    A scenario that has left the library is an error rather than a gap to skip past. The
    alternative is a demo that silently shows nine incidents, or three, and still reports
    success, which is the failure mode this project keeps finding in other people's
    checks.
    """
    library = load_library(specs_dir or SPECS_DIR)
    missing = [name for name, _ in SHOWCASE_SCENARIOS if name not in library]
    if missing:
        raise ShowcaseError(
            f"these showcase scenarios are not in the library: {missing}. Either restore "
            "them or choose replacements deliberately; a showcase that shrinks silently "
            "is a demo that shows less every time somebody edits the library."
        )
    return [
        ShowcaseIncident(
            spec=library[name],
            bundle_id=derive_bundle_id(name, SHOWCASE_RUN_ID),
            reason=reason,
        )
        for name, reason in SHOWCASE_SCENARIOS
    ]


def build_showcase(workspace: Path, specs_dir: Path | None = None) -> list[ShowcaseIncident]:
    """Build every showcase bundle into a workspace, deterministically.

    Rebuilds rather than caching. A cached bundle whose spec changed underneath it would
    replay against cassettes recorded for the old spec, and the only symptom would be a
    missing cassette with no hint that the spec had moved.
    """
    incidents = showcase(specs_dir)
    for incident in incidents:
        build_synthetic_bundle(
            workspace / incident.bundle_id,
            incident.spec,
            SHOWCASE_RUN_ID,
            seed=SHOWCASE_SEED,
        )
    return incidents
