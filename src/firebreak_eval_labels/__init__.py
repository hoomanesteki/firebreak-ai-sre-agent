"""Ground truth for recorded incidents. Read by evaluation code only.

This is a separate top level package, not a module inside `firebreak`, for
one reason: it makes the rule mechanical. Any import of
`firebreak_eval_labels` from `firebreak.agent`, `firebreak.tools`, or
`firebreak.graph` is a single unambiguous line that a test can find and fail
on. A module tucked inside the main package would be one refactor away from
looking ordinary.

SPEC.md Section 8.4 is the contract. Leakage is the fastest way to produce
impressive numbers that mean nothing, and it usually arrives by accident:
somebody wants the target service for a log line, and six months later the
accuracy figure is fiction.

Each label carries a canary, a random string that appears nowhere else. After
an evaluation run, every prompt sent to a model is scanned for canaries. A
hit proves ground truth reached the agent, whatever the import graph says.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

CANARY_PREFIX = "fbcanary"
CANARY_BYTES = 16
LABELS_DIRNAME = "labels"


class LabelError(Exception):
    """A label is missing, malformed, or does not match its bundle."""


def new_canary() -> str:
    """A random marker that should never appear outside `labels/`."""
    return f"{CANARY_PREFIX}-{secrets.token_hex(CANARY_BYTES)}"


class DistractorLabel(BaseModel):
    """A distractor as it was actually applied, for grading precision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    service: str
    applied_at: datetime
    flag: str | None = None
    variant: str | None = None


class IncidentLabel(BaseModel):
    """The answer for one recorded incident.

    `target_service` and `fault_class` are what the graders score against.
    `fault_onset` is the moment flagd actually served the fault, not the
    moment it was written, which is why the recorder measures convergence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str
    run_id: str
    split: str
    target_service: str | None
    fault_class: str
    fault_flag: str | None = None
    fault_variant: str | None = None
    fault_onset: datetime | None = None
    fault_cleared: datetime | None = None
    distractors: tuple[DistractorLabel, ...] = ()
    alert_fired: bool = False
    alert_fired_at: datetime | None = None
    canary: str = Field(default_factory=new_canary, pattern=rf"^{CANARY_PREFIX}-[0-9a-f]{{32}}$")
    notes: str | None = None

    @property
    def is_no_fault(self) -> bool:
        """True for scenarios whose correct answer is that nothing broke."""
        return self.target_service is None


def label_path(labels_root: Path, scenario_id: str, run_id: str) -> Path:
    """Where one label lives. Outside the bundle, always."""
    return labels_root / scenario_id / f"{run_id}.json"


def write_label(labels_root: Path, label: IncidentLabel) -> Path:
    """Store a label next to its peers, never inside a bundle."""
    path = label_path(labels_root, label.scenario_id, label.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(label.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def read_label(labels_root: Path, scenario_id: str, run_id: str) -> IncidentLabel:
    """Read one label. Only evaluation code may call this."""
    path = label_path(labels_root, scenario_id, run_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise LabelError(f"no label at {path}") from error
    except json.JSONDecodeError as error:
        raise LabelError(f"{path} is not valid JSON: {error}") from error
    try:
        return IncidentLabel.model_validate(raw)
    except ValueError as error:
        raise LabelError(f"{path} is not a valid label: {error}") from error


def load_all(labels_root: Path) -> dict[tuple[str, str], IncidentLabel]:
    """Every label under a root, keyed by scenario and run."""
    labels: dict[tuple[str, str], IncidentLabel] = {}
    if not labels_root.is_dir():
        return labels
    for path in sorted(labels_root.glob("*/*.json")):
        label = read_label(labels_root, path.parent.name, path.stem)
        labels[(label.scenario_id, label.run_id)] = label
    return labels


def all_canaries(labels_root: Path) -> set[str]:
    """Every canary in the label store, for scanning captured prompts."""
    return {label.canary for label in load_all(labels_root).values()}


def find_canaries(text: str, canaries: set[str]) -> set[str]:
    """Canaries present in a blob of text.

    Used against captured prompts after an evaluation run. A non empty
    result means ground truth reached a model and the run is void.
    """
    return {canary for canary in canaries if canary in text}
