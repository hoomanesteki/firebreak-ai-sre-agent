"""Generate the scenario library from the pinned demo's flag inventory.

SPEC.md Section 6.2 wants about 120 recorded incidents built from the
feature flags the pinned OpenTelemetry Demo actually ships, split the way
Section 8.2 describes. Typing 120 YAML files by hand is how a flag name or a
split count quietly drifts from the source of truth, so this script builds
every `ScenarioSpec` in code, validates each flag and variant against
`reports/lab/flag_inventory.json`, and writes `scenarios/specs/*.yaml` plus
`scenarios/LIBRARY_CARD.md` from the same in-memory objects.

Running this script twice produces byte-identical output: there is no
randomness, no wall-clock timestamp, and no dependence on what is already on
disk. The existing `scenarios/specs/*.yaml` files are cleared before each
run so a scenario dropped from the plan below does not linger as a stale
file.

Flag to service mapping comes from the descriptions in the pinned demo's own
`vendor/otel-demo/src/flagd/demo.flagd.json` (for example `paymentUnreachable`
reads "Payment service is unavailable" and `failedReadinessProbe` reads
"readiness probe failure for cart service"), cross-checked against
`docs/target-system.md`. Fault classes and family membership follow the
table in this task's design, not the older cross-reference table in
`docs/target-system.md`, where the two disagree (`recommendationCacheFailure`
is `cache_failure` here, not `memory_leak`).
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from firebreak.lab.scenario import (
    Distractor,
    DistractorKind,
    Fault,
    FaultClass,
    FaultKind,
    Load,
    ScenarioSpec,
    Split,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FLAG_INVENTORY_PATH = REPO_ROOT / "reports" / "lab" / "flag_inventory.json"
SPECS_DIR = REPO_ROOT / "scenarios" / "specs"
LIBRARY_CARD_PATH = REPO_ROOT / "scenarios" / "LIBRARY_CARD.md"

# The traffic levels every family varies across, in users. SPEC.md's own
# scenario example uses `users` under `load`; these three give a light, a
# moderate, and a heavy run of each fault.
LOAD_LEVELS: tuple[int, ...] = (5, 20, 50)

# Families held out entirely as `test_ood`, per SPEC.md Section 8.2: no
# scenario from either family may appear in `train` or `validation`.
OOD_FAMILIES: frozenset[str] = frozenset({"resource", "contention"})

# Families in insertion order, so the library card and the summary line
# report in a fixed, readable sequence rather than dict iteration order.
FAMILY_ORDER: tuple[str, ...] = (
    "error-injection",
    "unreachable",
    "latency",
    "resource",
    "messaging",
    "contention",
    "health",
    "no-fault",
    "distractor",
    "double-fault",
)


class GeneratorError(Exception):
    """The plan below asked for a flag or variant the pinned demo lacks."""


def load_inventory(path: Path = FLAG_INVENTORY_PATH) -> dict[str, Any]:
    """Read the flag inventory, the only source of truth for flag names."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    flags = raw.get("flags")
    if not isinstance(flags, dict):
        raise GeneratorError(f"{path} has no 'flags' object")
    return dict(flags)


def require_variant(flags: dict[str, Any], flag: str, variant: str) -> None:
    """Fail fast when the plan names a flag or variant the demo does not offer.

    A typo here would otherwise surface only when `check_against_inventory`
    runs later, or worse, during a recording.
    """
    entry = flags.get(flag)
    if entry is None:
        raise GeneratorError(f"flag {flag!r} is not in the flag inventory")
    offered = entry.get("variants", [])
    if variant not in offered:
        raise GeneratorError(
            f"flag {flag!r} has no variant {variant!r}; it offers {', '.join(offered)}"
        )


def flag_slug(flag: str) -> str:
    """Convert a camelCase flag name to the kebab-case id segment for it."""
    chars: list[str] = []
    for index, char in enumerate(flag):
        if char.isupper() and index > 0:
            chars.append("-")
        chars.append(char.lower())
    return "".join(chars)


def variant_slug(variant: str) -> str:
    """Convert a flag variant to a kebab-safe id segment, for example 25% to 25pct."""
    return variant.replace("%", "pct").lower()


def make_fault(flag: str, variant: str) -> Fault:
    return Fault(kind=FaultKind.FLAG, flag=flag, variant=variant)


# --------------------------------------------------------------------------
# Family builders. Each returns specs with a placeholder split; the real
# split is assigned once, deterministically, after every family exists (see
# `assign_splits`), because it depends on sorting each family's full id list.
# --------------------------------------------------------------------------


def build_error_injection(flags: dict[str, Any]) -> list[ScenarioSpec]:
    """paymentFailure, cartFailure, adFailure, productCatalogFailure -> error_injection."""
    plan: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        ("paymentFailure", "payment", ("25%", "50%", "90%")),
        ("cartFailure", "cart", ("25%", "50%", "90%")),
        ("adFailure", "ad", ("on",)),
        ("productCatalogFailure", "product-catalog", ("on",)),
    )
    specs: list[ScenarioSpec] = []
    for flag, service, variants in plan:
        for variant in variants:
            require_variant(flags, flag, variant)
            for users in LOAD_LEVELS:
                specs.append(
                    ScenarioSpec(
                        id=f"{flag_slug(flag)}-{variant_slug(variant)}-{users}u",
                        family="error-injection",
                        fault=make_fault(flag, variant),
                        target_service=service,
                        fault_class=FaultClass.ERROR_INJECTION,
                        load=Load(users=users),
                        split=Split.TRAIN,
                    )
                )
    return specs


def build_unreachable(flags: dict[str, Any]) -> list[ScenarioSpec]:
    """paymentUnreachable -> unreachable_dependency."""
    flag, service, variant = "paymentUnreachable", "payment", "on"
    require_variant(flags, flag, variant)
    specs: list[ScenarioSpec] = []
    for spawn_rate in (5.0, 10.0):
        for users in LOAD_LEVELS:
            specs.append(
                ScenarioSpec(
                    id=(f"{flag_slug(flag)}-{variant_slug(variant)}-{users}u-sr{int(spawn_rate)}"),
                    family="unreachable",
                    fault=make_fault(flag, variant),
                    target_service=service,
                    fault_class=FaultClass.UNREACHABLE_DEPENDENCY,
                    load=Load(users=users, spawn_rate=spawn_rate),
                    split=Split.TRAIN,
                )
            )
    return specs


def build_latency(flags: dict[str, Any]) -> list[ScenarioSpec]:
    """imageSlowLoad, intlShippingSlowdown -> latency."""
    plan: tuple[tuple[str, str], ...] = (
        ("imageSlowLoad", "image-provider"),
        ("intlShippingSlowdown", "shipping"),
    )
    variants = ("5sec", "10sec")
    specs: list[ScenarioSpec] = []
    for flag, service in plan:
        for variant in variants:
            require_variant(flags, flag, variant)
            for users in LOAD_LEVELS:
                specs.append(
                    ScenarioSpec(
                        id=f"{flag_slug(flag)}-{variant_slug(variant)}-{users}u",
                        family="latency",
                        fault=make_fault(flag, variant),
                        target_service=service,
                        fault_class=FaultClass.LATENCY,
                        load=Load(users=users),
                        split=Split.TRAIN,
                    )
                )
    return specs


def build_resource(flags: dict[str, Any]) -> list[ScenarioSpec]:
    """adHighCpu, adManualGc, emailMemoryLeak, recommendationCacheFailure.

    Held out entirely as `test_ood` (SPEC.md Section 8.2): no resource
    scenario is ever assigned `train` or `validation`.
    """
    specs: list[ScenarioSpec] = []

    single_variant_plan: tuple[tuple[str, str, FaultClass], ...] = (
        ("adHighCpu", "ad", FaultClass.CPU_SATURATION),
        ("adManualGc", "ad", FaultClass.GC_PRESSURE),
        ("recommendationCacheFailure", "recommendation", FaultClass.CACHE_FAILURE),
    )
    for flag, service, fault_class in single_variant_plan:
        variant = "on"
        require_variant(flags, flag, variant)
        for users in LOAD_LEVELS:
            specs.append(
                ScenarioSpec(
                    id=f"{flag_slug(flag)}-{variant_slug(variant)}-{users}u",
                    family="resource",
                    fault=make_fault(flag, variant),
                    target_service=service,
                    fault_class=fault_class,
                    load=Load(users=users),
                    split=Split.TEST_OOD,
                )
            )

    flag, service = "emailMemoryLeak", "email"
    for variant in ("1x", "10x", "100x", "1000x", "10000x"):
        require_variant(flags, flag, variant)
        for users in LOAD_LEVELS:
            specs.append(
                ScenarioSpec(
                    id=f"{flag_slug(flag)}-{variant_slug(variant)}-{users}u",
                    family="resource",
                    fault=make_fault(flag, variant),
                    target_service=service,
                    fault_class=FaultClass.MEMORY_LEAK,
                    load=Load(users=users),
                    split=Split.TEST_OOD,
                )
            )
    return specs


def build_messaging(flags: dict[str, Any]) -> list[ScenarioSpec]:
    """kafkaQueueProblems -> queue_lag, on the checkout producer side."""
    flag, service, variant = "kafkaQueueProblems", "checkout", "on"
    require_variant(flags, flag, variant)
    specs: list[ScenarioSpec] = []
    for spawn_rate in (5.0, 10.0):
        for users in LOAD_LEVELS:
            specs.append(
                ScenarioSpec(
                    id=(f"{flag_slug(flag)}-{variant_slug(variant)}-{users}u-sr{int(spawn_rate)}"),
                    family="messaging",
                    fault=make_fault(flag, variant),
                    target_service=service,
                    fault_class=FaultClass.QUEUE_LAG,
                    load=Load(users=users, spawn_rate=spawn_rate),
                    split=Split.TRAIN,
                )
            )
    return specs


def build_contention(flags: dict[str, Any]) -> list[ScenarioSpec]:
    """productCatalogLockContention -> lock_contention.

    Held out entirely as `test_ood`, same as `resource`.
    """
    flag, service, variant = "productCatalogLockContention", "product-catalog", "on"
    require_variant(flags, flag, variant)
    specs: list[ScenarioSpec] = []
    for spawn_rate in (5.0, 10.0):
        for users in LOAD_LEVELS:
            specs.append(
                ScenarioSpec(
                    id=(f"{flag_slug(flag)}-{variant_slug(variant)}-{users}u-sr{int(spawn_rate)}"),
                    family="contention",
                    fault=make_fault(flag, variant),
                    target_service=service,
                    fault_class=FaultClass.LOCK_CONTENTION,
                    load=Load(users=users, spawn_rate=spawn_rate),
                    split=Split.TEST_OOD,
                )
            )
    return specs


def build_health(flags: dict[str, Any]) -> list[ScenarioSpec]:
    """failedReadinessProbe -> health_check_failure, on the cart service."""
    flag, service, variant = "failedReadinessProbe", "cart", "on"
    require_variant(flags, flag, variant)
    specs: list[ScenarioSpec] = []
    for spawn_rate in (5.0, 10.0):
        for users in LOAD_LEVELS:
            specs.append(
                ScenarioSpec(
                    id=(f"{flag_slug(flag)}-{variant_slug(variant)}-{users}u-sr{int(spawn_rate)}"),
                    family="health",
                    fault=make_fault(flag, variant),
                    target_service=service,
                    fault_class=FaultClass.HEALTH_CHECK_FAILURE,
                    load=Load(users=users, spawn_rate=spawn_rate),
                    split=Split.TRAIN,
                )
            )
    return specs


def build_no_fault(flags: dict[str, Any]) -> list[ScenarioSpec]:
    """loadGeneratorFloodHomepage: a demand spike, not a service defect.

    The point of this family is an alert with no service at fault, so the
    flood has to actually happen. It is applied as a `harmless_flag`
    distractor rather than as `fault.flag`, which is the honest description
    of it: a flag change that is not the fault. The recorder applies
    distractor flags and clears them, so the spike is real, while
    `fault_class` stays `none` and `target_service` stays null, and a system
    that blames a service for it is wrong.

    Naming it only in `notes`, as an earlier version of this generator did,
    produced specifications that described a spike the recorder never
    created: the scenario ran at ordinary load, nothing alerted, and the
    family tested nothing.
    """
    flood_flag = "loadGeneratorFloodHomepage"
    if flood_flag not in flags or "on" not in flags[flood_flag].get("variants", []):
        raise GeneratorError(f"{flood_flag!r} with variant 'on' is not in the flag inventory")

    specs: list[ScenarioSpec] = []
    for spawn_rate in (1.0, 2.0, 5.0, 10.0, 20.0):
        for users in LOAD_LEVELS:
            specs.append(
                ScenarioSpec(
                    id=f"no-fault-flood-homepage-sr{int(spawn_rate)}-{users}u",
                    family="no-fault",
                    fault=Fault(kind=FaultKind.NONE),
                    target_service=None,
                    fault_class=FaultClass.NONE,
                    load=Load(users=users, spawn_rate=spawn_rate),
                    distractors=(
                        Distractor(
                            kind=DistractorKind.HARMLESS_FLAG,
                            service="frontend",
                            offset_seconds=0,
                            flag=flood_flag,
                            variant="on",
                        ),
                    ),
                    notes=(
                        f"Demand spike only: {flood_flag}=on floods the frontend with "
                        "load generator traffic. No service fault is injected; the "
                        "correct answer is no fault."
                    ),
                    split=Split.TRAIN,
                )
            )
    return specs


def build_distractor(flags: dict[str, Any]) -> list[ScenarioSpec]:
    """An error-injection or latency base fault plus one unrelated deploy event."""
    plan: tuple[tuple[str, str, str, FaultClass, str, int], ...] = (
        ("paymentFailure", "payment", "75%", FaultClass.ERROR_INJECTION, "recommendation", -120),
        ("imageSlowLoad", "image-provider", "5sec", FaultClass.LATENCY, "frontend", -90),
        ("cartFailure", "cart", "10%", FaultClass.ERROR_INJECTION, "ad", -150),
    )
    specs: list[ScenarioSpec] = []
    for flag, service, variant, fault_class, distractor_service, offset in plan:
        require_variant(flags, flag, variant)
        for users in LOAD_LEVELS:
            specs.append(
                ScenarioSpec(
                    id=f"distractor-{flag_slug(flag)}-{variant_slug(variant)}-{users}u",
                    family="distractor",
                    fault=make_fault(flag, variant),
                    target_service=service,
                    fault_class=fault_class,
                    load=Load(users=users),
                    distractors=(
                        Distractor(
                            kind=DistractorKind.DEPLOY_EVENT,
                            service=distractor_service,
                            offset_seconds=offset,
                        ),
                    ),
                    split=Split.TRAIN,
                )
            )
    return specs


class DoubleFaultPair(NamedTuple):
    """A primary fault paired with a second, harmless-from-a-grading-view flag flip."""

    primary_flag: str
    primary_service: str
    primary_variant: str
    secondary_flag: str
    secondary_service: str
    secondary_variant: str


def build_double_fault(flags: dict[str, Any]) -> list[ScenarioSpec]:
    """Two faults in different services: the primary fault plus a harmless second flag.

    The primary fault is `fault`; the second is a `HARMLESS_FLAG` distractor
    at offset zero. `target_service` is the primary's service and
    `fault_class` is `multiple`, per the design.
    """
    plan: tuple[DoubleFaultPair, ...] = (
        DoubleFaultPair("paymentFailure", "payment", "50%", "cartFailure", "cart", "10%"),
        DoubleFaultPair("cartFailure", "cart", "50%", "adFailure", "ad", "on"),
        DoubleFaultPair("adFailure", "ad", "on", "productCatalogFailure", "product-catalog", "on"),
        DoubleFaultPair(
            "imageSlowLoad", "image-provider", "10sec", "intlShippingSlowdown", "shipping", "5sec"
        ),
    )
    specs: list[ScenarioSpec] = []
    for pair in plan:
        require_variant(flags, pair.primary_flag, pair.primary_variant)
        require_variant(flags, pair.secondary_flag, pair.secondary_variant)
        for users in LOAD_LEVELS:
            specs.append(
                ScenarioSpec(
                    id=(
                        f"double-fault-{flag_slug(pair.primary_flag)}"
                        f"-{flag_slug(pair.secondary_flag)}-{users}u"
                    ),
                    family="double-fault",
                    fault=make_fault(pair.primary_flag, pair.primary_variant),
                    target_service=pair.primary_service,
                    fault_class=FaultClass.MULTIPLE,
                    load=Load(users=users),
                    distractors=(
                        Distractor(
                            kind=DistractorKind.HARMLESS_FLAG,
                            service=pair.secondary_service,
                            offset_seconds=0,
                            flag=pair.secondary_flag,
                            variant=pair.secondary_variant,
                        ),
                    ),
                    split=Split.TRAIN,
                )
            )
    return specs


FAMILY_BUILDERS: tuple[Any, ...] = (
    build_error_injection,
    build_unreachable,
    build_latency,
    build_resource,
    build_messaging,
    build_contention,
    build_health,
    build_no_fault,
    build_distractor,
    build_double_fault,
)


def generate_library(flags: dict[str, Any]) -> list[ScenarioSpec]:
    """Build every scenario, then assign real splits deterministically."""
    specs: list[ScenarioSpec] = []
    for builder in FAMILY_BUILDERS:
        specs.extend(builder(flags))

    id_counts = Counter(spec.id for spec in specs)
    dupes = sorted(sid for sid, count in id_counts.items() if count > 1)
    if dupes:
        raise GeneratorError(f"duplicate scenario ids: {', '.join(dupes)}")

    return assign_splits(specs)


def assign_splits(specs: list[ScenarioSpec]) -> list[ScenarioSpec]:
    """Assign the real split per family, deterministically by sorted id.

    `resource` and `contention` are entirely `test_ood` already (assigned in
    their builders). Every other family splits by sorting its own ids and
    taking the first 50% as `train`, the next 20% as `validation`, and the
    rest as `test_id`, per SPEC.md Section 8.2. Sorting ids, rather than
    drawing at random, is what makes two runs of this script agree.
    """
    by_family: dict[str, list[ScenarioSpec]] = defaultdict(list)
    for spec in specs:
        by_family[spec.family].append(spec)

    result: list[ScenarioSpec] = []
    for family, group in by_family.items():
        if family in OOD_FAMILIES:
            result.extend(group)
            continue

        ordered_ids = sorted(spec.id for spec in group)
        train_count = int(len(ordered_ids) * 0.5)
        validation_count = int(len(ordered_ids) * 0.2)
        split_by_id: dict[str, Split] = {}
        for index, spec_id in enumerate(ordered_ids):
            if index < train_count:
                split_by_id[spec_id] = Split.TRAIN
            elif index < train_count + validation_count:
                split_by_id[spec_id] = Split.VALIDATION
            else:
                split_by_id[spec_id] = Split.TEST_ID

        for spec in group:
            result.append(spec.model_copy(update={"split": split_by_id[spec.id]}))

    return result


# --------------------------------------------------------------------------
# Writing YAML and the library card.
# --------------------------------------------------------------------------


def spec_to_yaml_data(spec: ScenarioSpec) -> dict[str, Any]:
    """Render one spec as a plain dict, field order matching SPEC.md's example."""
    return {
        "id": spec.id,
        "family": spec.family,
        "fault": {
            "kind": spec.fault.kind.value,
            "flag": spec.fault.flag,
            "variant": spec.fault.variant,
        },
        "target_service": spec.target_service,
        "fault_class": spec.fault_class.value,
        "load": {
            "users": spec.load.users,
            "spawn_rate": spec.load.spawn_rate,
        },
        "timing": {
            "warmup_seconds": spec.timing.warmup_seconds,
            "fault_seconds": spec.timing.fault_seconds,
            "cooldown_seconds": spec.timing.cooldown_seconds,
        },
        "distractors": [
            {
                "kind": d.kind.value,
                "service": d.service,
                "offset_seconds": d.offset_seconds,
                "flag": d.flag,
                "variant": d.variant,
            }
            for d in spec.distractors
        ],
        "expected_alert": spec.expected_alert,
        "split": spec.split.value,
        "notes": spec.notes,
    }


def write_specs(specs: list[ScenarioSpec], specs_dir: Path = SPECS_DIR) -> None:
    """Write one YAML file per spec, clearing stale files from a prior plan first."""
    specs_dir.mkdir(parents=True, exist_ok=True)
    for stale in specs_dir.glob("*.yaml"):
        stale.unlink()
    for spec in specs:
        data = spec_to_yaml_data(spec)
        text = yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)
        (specs_dir / f"{spec.id}.yaml").write_text(text, encoding="utf-8")


def describe_distractors(spec: ScenarioSpec) -> str:
    if not spec.distractors:
        return "none"
    parts: list[str] = []
    for d in spec.distractors:
        detail = f" ({d.flag}={d.variant})" if d.flag else ""
        parts.append(f"{d.kind.value}: {d.service}{detail} at {d.offset_seconds:+d}s")
    return "; ".join(parts)


def describe_load(spec: ScenarioSpec) -> str:
    return f"{spec.load.users} users, spawn {spec.load.spawn_rate:g}/s"


def render_library_card(specs: list[ScenarioSpec]) -> str:
    """Render scenarios/LIBRARY_CARD.md, grouped by family, with totals."""
    by_family: dict[str, list[ScenarioSpec]] = defaultdict(list)
    for spec in specs:
        by_family[spec.family].append(spec)
    for group in by_family.values():
        group.sort(key=lambda spec: spec.id)

    split_totals: dict[str, int] = defaultdict(int)
    for spec in specs:
        split_totals[spec.split.value] += 1

    lines: list[str] = []
    lines.append("# Scenario library card")
    lines.append("")
    lines.append(
        f"Generated by `scripts/generate_scenarios.py` from "
        f"`reports/lab/flag_inventory.json`. {len(specs)} scenarios in total, "
        f"grouped by family below. Run the script again after any change to "
        f"the plan; it clears and rewrites every file in `scenarios/specs/`."
    )
    lines.append("")

    lines.append("## Totals by family")
    lines.append("")
    lines.append("| Family | Count | Split held out |")
    lines.append("|---|---|---|")
    for family in FAMILY_ORDER:
        count = len(by_family.get(family, []))
        held_out = "test_ood only" if family in OOD_FAMILIES else "no"
        lines.append(f"| {family} | {count} | {held_out} |")
    lines.append(f"| total | {len(specs)} | |")
    lines.append("")

    lines.append("## Totals by split")
    lines.append("")
    lines.append("| Split | Count |")
    lines.append("|---|---|")
    for split in (Split.TRAIN, Split.VALIDATION, Split.TEST_ID, Split.TEST_OOD):
        lines.append(f"| {split.value} | {split_totals.get(split.value, 0)} |")
    lines.append(f"| total | {len(specs)} |")
    lines.append("")

    lines.append("## Flags not used")
    lines.append("")
    lines.append(
        "Three flags in the inventory are excluded from the library, for reasons recorded "
        "in `docs/target-system.md`:"
    )
    lines.append("")
    lines.append("- `aiSlowResponse`: needs the agent profile, not used in v1.")
    lines.append("- `aiRunawayAgent`: needs the agent profile, not used in v1.")
    lines.append(
        "- `emitRawPii`: not a failure scenario; never enabled, since it puts card "
        "numbers into telemetry."
    )
    lines.append("")

    for family in FAMILY_ORDER:
        group = by_family.get(family, [])
        if not group:
            continue
        lines.append(f"## {family} ({len(group)} scenarios)")
        lines.append("")
        lines.append(
            "| id | family | flag | variant | fault class | target service | load "
            "| distractors | split |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for spec in group:
            flag = spec.fault.flag or "none"
            variant = spec.fault.variant or "none"
            target = spec.target_service or "none"
            lines.append(
                f"| {spec.id} | {spec.family} | {flag} | {variant} | "
                f"{spec.fault_class.value} | {target} | {describe_load(spec)} | "
                f"{describe_distractors(spec)} | {spec.split.value} |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_library_card(specs: list[ScenarioSpec], path: Path = LIBRARY_CARD_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_library_card(specs), encoding="utf-8")


def main() -> int:
    flags = load_inventory(FLAG_INVENTORY_PATH)
    specs = generate_library(flags)
    # Paths are looked up here rather than taken as default arguments, so
    # the entry point can be pointed at a scratch directory and tested.
    write_specs(specs, SPECS_DIR)
    write_library_card(specs, LIBRARY_CARD_PATH)

    split_totals: dict[str, int] = defaultdict(int)
    family_totals: dict[str, int] = defaultdict(int)
    for spec in specs:
        split_totals[spec.split.value] += 1
        family_totals[spec.family] += 1

    split_summary = ", ".join(f"{s.value}={split_totals.get(s.value, 0)}" for s in Split)
    sys.stdout.write(
        f"generated {len(specs)} scenario specs across {len(family_totals)} families "
        f"({split_summary})\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
