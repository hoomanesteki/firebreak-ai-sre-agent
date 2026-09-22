"""Scenario specifications: the recipe for one reproducible incident.

A specification is the input to a recording. It names the fault to inject,
the load to inject it under, and how long each stage lasts.

**A specification is ground truth.** `target_service` and `fault_class` are
the answers the agent is being asked to find. Specifications live under
`scenarios/`, which SPEC.md Section 8.4 keeps away from the agent exactly
like `labels/`. Nothing under `src/firebreak/agent/`, `tools/`, or `graph/`
may import this module, and a test enforces that.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_LOAD_USERS = 500
MIN_LOAD_USERS = 1


class FaultClass(StrEnum):
    """What kind of failure the injected fault produces.

    These are the classes a grader scores `fault_class` accuracy against, so
    adding one changes what the eval measures. Each maps to flags that exist
    at the pinned demo tag; see `docs/target-system.md`.
    """

    ERROR_INJECTION = "error_injection"
    UNREACHABLE_DEPENDENCY = "unreachable_dependency"
    LATENCY = "latency"
    CPU_SATURATION = "cpu_saturation"
    GC_PRESSURE = "gc_pressure"
    MEMORY_LEAK = "memory_leak"
    QUEUE_LAG = "queue_lag"
    LOCK_CONTENTION = "lock_contention"
    HEALTH_CHECK_FAILURE = "health_check_failure"
    CACHE_FAILURE = "cache_failure"
    NONE = "none"
    MULTIPLE = "multiple"


class Split(StrEnum):
    """Which held out set a scenario belongs to.

    Anything tuned uses train and validation only. `test_ood` holds entire
    fault families back, which is what separates generalising from
    memorising (SPEC.md Section 8.2).
    """

    TRAIN = "train"
    VALIDATION = "validation"
    TEST_ID = "test_id"
    TEST_OOD = "test_ood"


class FaultKind(StrEnum):
    """How a fault is applied."""

    FLAG = "flag"
    NONE = "none"


class Fault(BaseModel):
    """One fault to inject."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: FaultKind = FaultKind.FLAG
    flag: str | None = None
    variant: str | None = None

    @model_validator(mode="after")
    def _flag_faults_name_a_flag(self) -> Self:
        if self.kind is FaultKind.FLAG and not (self.flag and self.variant):
            raise ValueError("a flag fault needs both flag and variant")
        if self.kind is FaultKind.NONE and (self.flag or self.variant):
            raise ValueError("a fault of kind none must not name a flag")
        return self


class Load(BaseModel):
    """The traffic level the fault is injected under.

    Part of a scenario's identity: the same fault at 5 users and at 50 looks
    different, so a recording that cannot set this cannot be reproduced.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    users: int = Field(ge=MIN_LOAD_USERS, le=MAX_LOAD_USERS)
    spawn_rate: float = Field(default=5.0, gt=0)


class DistractorKind(StrEnum):
    """Events that look like a cause but are not."""

    DEPLOY_EVENT = "deploy_event"
    HARMLESS_FLAG = "harmless_flag"


class Distractor(BaseModel):
    """An unrelated event placed near the incident.

    This is what stops a system from scoring well by always blaming the most
    recent change. The offset is relative to fault onset and is usually
    negative, putting the distractor just before the real cause.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: DistractorKind
    service: str
    offset_seconds: int
    flag: str | None = None
    variant: str | None = None

    @model_validator(mode="after")
    def _harmless_flag_names_a_flag(self) -> Self:
        if self.kind is DistractorKind.HARMLESS_FLAG and not (self.flag and self.variant):
            raise ValueError("a harmless_flag distractor needs both flag and variant")
        return self


class Timing(BaseModel):
    """How long each stage of a recording lasts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    warmup_seconds: int = Field(default=300, ge=60)
    fault_seconds: int = Field(default=600, ge=60)
    cooldown_seconds: int = Field(default=180, ge=0)

    @property
    def total_seconds(self) -> int:
        """Wall clock a single recording takes, excluding stack startup."""
        return self.warmup_seconds + self.fault_seconds + self.cooldown_seconds


class ScenarioSpec(BaseModel):
    """One reproducible incident.

    Carries ground truth. Never hand this, or anything derived from its
    `target_service` or `fault_class`, to the agent under test.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    family: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    fault: Fault
    target_service: str | None
    fault_class: FaultClass
    load: Load
    timing: Timing = Timing()
    distractors: tuple[Distractor, ...] = ()
    expected_alert: str | None = None
    split: Split
    notes: str | None = None

    @model_validator(mode="after")
    def _ground_truth_is_consistent(self) -> Self:
        """A no fault scenario has no culprit, and every other one does.

        Getting this wrong produces a scenario whose label cannot be scored,
        which is only discovered after the recording has been made.
        """
        if self.fault_class is FaultClass.NONE:
            if self.target_service is not None:
                raise ValueError("a no fault scenario must not name a target service")
            if self.fault.kind is not FaultKind.NONE:
                raise ValueError("a no fault scenario must not inject a flag fault")
        elif self.target_service is None:
            raise ValueError(f"{self.fault_class} needs a target service")
        return self

    @property
    def second_faults(self) -> tuple[Distractor, ...]:
        """Distractors that change a flag rather than only logging an event."""
        return tuple(d for d in self.distractors if d.kind is DistractorKind.HARMLESS_FLAG)


class ScenarioError(Exception):
    """A specification could not be loaded or does not match the demo."""


def load_spec(path: Path) -> ScenarioSpec:
    """Read and validate one specification file."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ScenarioError(f"{path} is not valid YAML: {error}") from error
    except OSError as error:
        raise ScenarioError(f"cannot read {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ScenarioError(f"{path} does not contain a mapping")
    try:
        spec = ScenarioSpec.model_validate(raw)
    except ValueError as error:
        raise ScenarioError(f"{path} is not a valid scenario: {error}") from error
    if spec.id != path.stem:
        raise ScenarioError(f"{path} declares id {spec.id!r}, which does not match its filename")
    return spec


def load_library(specs_dir: Path) -> dict[str, ScenarioSpec]:
    """Load every specification in a directory, keyed by id."""
    if not specs_dir.is_dir():
        raise ScenarioError(f"no scenario directory at {specs_dir}")
    library: dict[str, ScenarioSpec] = {}
    for path in sorted(specs_dir.glob("*.yaml")):
        spec = load_spec(path)
        if spec.id in library:
            raise ScenarioError(f"duplicate scenario id {spec.id!r}")
        library[spec.id] = spec
    if not library:
        raise ScenarioError(f"no scenario specifications found in {specs_dir}")
    return library


def check_against_inventory(
    library: dict[str, ScenarioSpec], inventory: dict[str, Any]
) -> list[str]:
    """Check every flag and variant a library names exists in the demo.

    A typo in a flag name produces a recording of nothing, discovered hours
    later. The inventory is generated from the pinned demo's own flag file by
    `firebreak lab flags inventory`.
    """
    flags = inventory.get("flags")
    if not isinstance(flags, dict):
        raise ScenarioError("inventory has no 'flags' object")

    problems: list[str] = []
    for spec in library.values():
        named = [(spec.fault.flag, spec.fault.variant)]
        named += [(d.flag, d.variant) for d in spec.second_faults]
        for flag, variant in named:
            if flag is None:
                continue
            if flag not in flags:
                problems.append(f"{spec.id}: flag {flag!r} does not exist in the demo")
                continue
            offered = flags[flag].get("variants", [])
            if variant not in offered:
                problems.append(
                    f"{spec.id}: flag {flag!r} has no variant {variant!r}; "
                    f"it offers {', '.join(offered)}"
                )
    return problems
