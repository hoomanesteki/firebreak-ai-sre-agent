"""Load the tuned thresholds, so operators change numbers without editing code.

`config/thresholds.yaml` is where every tuned number lives, together with
the measurement that justified it. This module reads it.

The algorithm modules keep their own module-level constants as defaults, so
that `rank_candidates` can be imported and unit tested without a config file
existing at all. That is two places holding the same number, which is the
exact shape of the defect that cost Phase 3 four separate bugs, so a
contract test asserts the two agree and fails the build when they drift.
The duplication is deliberate and the test is the reason it is safe.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parents[3]
THRESHOLDS_PATH = REPO_ROOT / "config" / "thresholds.yaml"


class ThresholdError(Exception):
    """The thresholds file is missing, malformed, or internally inconsistent."""


class Measurement(BaseModel):
    """The measurement that justified a threshold, carried beside it.

    Declared rather than ignored. This block lives in `config/thresholds.yaml` under
    `abstention`, and the models used to allow unknown keys so that it could. That also meant a
    key nobody had implemented was accepted and dropped in silence, which matters here more than
    most places: the file's own comment says the abstention rule "needs a second signal, not a
    better number", so the obvious next edit is to add a signal that does not exist yet and
    believe it took effect.

    Every field is optional, because what justified a threshold differs between thresholds and
    a provenance block should never be the thing that stops the system loading.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    faulted_trials: int | None = None
    no_fault_trials: int | None = None
    quiet_max: float | None = None
    faulted_min: float | None = None
    sensitivity: float | None = None
    specificity: float | None = None
    rule: str | None = None


class AbstentionThresholds(BaseModel):
    """When the system is allowed to say nothing is wrong."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    minimum_top_anomaly_z: float = Field(ge=0.0)
    # The exit gate's check 6, rather than triage's. Both are abstention rules
    # and both belong in one place, because a system that abstains at triage on
    # one rule and at the gate on another has two answers to the same question.
    minimum_hypothesis_support: int = Field(ge=1)
    # Optional, because a threshold can be a labelled default with nothing measured yet, which
    # `minimum_hypothesis_support` currently is.
    measured: Measurement | None = None


class RankingThresholds(BaseModel):
    """Parameters for the candidate ranking, as measured."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    restart_alpha: float = Field(gt=0.0, le=1.0)
    restart_sharpness: float = Field(gt=0.0)
    use_endpoint_scaling: bool
    self_loop_ratio: float = Field(ge=0.0)
    backward_ratio: float = Field(ge=0.0)
    onset_bonus: float = Field(ge=1.0)
    onset_threshold_z: float = Field(gt=0.0)

    def as_ranking_options(self) -> dict[str, float | bool]:
        """The keyword arguments `rank_candidates` takes.

        Named here rather than at each call site so that adding a parameter
        is one change, not a search for every caller that forgot it.
        """
        return {
            "alpha": self.restart_alpha,
            "sharpness": self.restart_sharpness,
            "use_endpoint_scaling": self.use_endpoint_scaling,
            "self_loop_ratio": self.self_loop_ratio,
            "backward_ratio": self.backward_ratio,
            "onset_bonus": self.onset_bonus,
        }


class WindowThresholds(BaseModel):
    """How a recording is split when no alert says where to look."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    baseline_fraction: float = Field(gt=0.0, lt=1.0)
    guard_seconds: float = Field(ge=0.0)


class Thresholds(BaseModel):
    """Everything tuned, in one frozen object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    abstention: AbstentionThresholds
    ranking: RankingThresholds
    windows: WindowThresholds


@lru_cache(maxsize=1)
def load_thresholds(path: Path | None = None) -> Thresholds:
    """Read and validate the thresholds file.

    Cached, because this is read on every triage and the file does not
    change during a run. A test that needs a different file passes an
    explicit path, which gets its own cache entry.
    """
    resolved = path or THRESHOLDS_PATH
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except OSError as error:
        raise ThresholdError(f"cannot read {resolved}: {error}") from error
    if not isinstance(raw, dict):
        raise ThresholdError(f"{resolved} does not contain a mapping")
    try:
        return Thresholds.model_validate(raw)
    except ValueError as error:
        raise ThresholdError(f"{resolved} is not a valid thresholds file: {error}") from error
