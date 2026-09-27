"""The model-based judge for mechanism explanations, and its calibration.

SPEC.md Section 9.2. One grader cannot be code: whether an explanation is
consistent with the label and the evidence, without invented details. Everything
else in the grader table is an exact match or a re-run.

**Why a judge is dangerous and what is done about it.** [R23] measured position,
verbosity and self-enhancement bias in LLM judges. So:

- The rubric scores consistency and invention, and says explicitly that length and
  style are not evidence of quality. A judge that rewards long answers turns the
  reporter into a machine for writing long answers.
- The judge tier is a different family from `strong` where one is configured, so
  the model is not marking its own work.
- Where a comparison is pairwise the order is randomised, because position bias is
  real and a fixed order silently favours one side.
- **The judge is calibrated against sixty owner labels, and below Cohen's kappa
  0.4 the metric it produces is shown as "not trusted" rather than shown.** That
  threshold is the load-bearing part: an uncalibrated judge produces a number, and
  a number gets quoted.

**Why the blind sheet exists.** The owner's labels have to be collected without
the owner seeing the judge's verdict, or the calibration measures agreement with a
suggestion rather than independent judgement. `write_blind_sheet` emits the
reports in a shuffled order with no verdicts and no scenario names, and
`read_blind_sheet` reads the owner's answers back by opaque id.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

# Below this, the judged metric is reported as not trusted. SPEC.md Section 9.2.
# Landis and Koch call 0.4 the bottom of "moderate" agreement; below it two raters
# are closer to independent than to agreeing, and a metric built on that is noise
# with a decimal point.
MINIMUM_KAPPA = 0.4

# How many owner labels the calibration needs. SPEC.md Section 9.2 says sixty.
CALIBRATION_LABELS = 60


class Verdict(StrEnum):
    """What the judge can conclude about one mechanism explanation.

    Three values rather than a score out of ten. A judge asked for a number
    produces one with no calibration behind it, and the distinction that matters
    for this grader is threefold: it holds up, it does not, or it invented
    something. The third is not a worse version of the second: an explanation that
    is merely unsupported wastes an engineer's time, and one that invents a detail
    sends them somewhere specific and wrong.
    """

    CONSISTENT = "consistent"
    UNSUPPORTED = "unsupported"
    INVENTED = "invented"

    @property
    def acceptable(self) -> bool:
        return self is Verdict.CONSISTENT


class JudgeRubric(BaseModel):
    """The instructions the judge is held to, as data rather than as a prompt.

    Here rather than in a prompt file so the rubric can be asserted in a test and
    quoted in a report. A rubric that lives only inside a prompt is a rubric
    nobody can check was followed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    criteria: tuple[str, ...] = (
        "The explanation names a mechanism that the cited evidence supports.",
        "Every number in the explanation appears in the cited evidence.",
        "The explanation does not introduce a service, metric, or event that the "
        "evidence does not mention.",
        "The explanation is consistent with the recorded fault class.",
    )
    ignored: tuple[str, ...] = (
        "Length. A short explanation that is right scores the same as a long one.",
        "Style, tone, and confidence of phrasing.",
        "Whether the explanation reads as though written by a strong model.",
        "Agreement with your own preferred wording.",
    )

    def as_prompt_section(self) -> str:
        """The rubric as the judge is shown it.

        Built from the same data the tests assert on, so the rubric that is checked
        and the rubric that is sent cannot drift.
        """
        lines = ["Score the explanation against these criteria:"]
        lines += [f"{index}. {text}" for index, text in enumerate(self.criteria, start=1)]
        lines += ["", "Ignore all of the following. They are not evidence of quality:"]
        lines += [f"- {text}" for text in self.ignored]
        return "\n".join(lines)


class JudgedReport(BaseModel):
    """One judged explanation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Opaque, so a blind sheet carries no hint of which scenario this is.
    item_id: str
    verdict: Verdict
    reason: str = Field(default="", max_length=600)


@dataclass(frozen=True)
class Agreement:
    """How closely the judge and the owner agree, and whether to trust it."""

    pairs: int
    observed: float
    expected: float
    kappa: float

    @property
    def trusted(self) -> bool:
        """Whether the judged metric may be reported as a number at all."""
        return self.pairs >= CALIBRATION_LABELS and self.kappa >= MINIMUM_KAPPA

    @property
    def status(self) -> str:
        """What a report says next to the judged metric."""
        if self.pairs < CALIBRATION_LABELS:
            return (
                f"not trusted: calibrated on {self.pairs} of the {CALIBRATION_LABELS} "
                "owner labels SPEC.md Section 9.2 requires"
            )
        if self.kappa < MINIMUM_KAPPA:
            return f"not trusted: Cohen's kappa {self.kappa:.2f} is below {MINIMUM_KAPPA}"
        return f"trusted: Cohen's kappa {self.kappa:.2f} over {self.pairs} labels"

    def as_dict(self) -> dict[str, object]:
        return {
            "pairs": self.pairs,
            "observed_agreement": self.observed,
            "expected_agreement": self.expected,
            "kappa": self.kappa,
            "trusted": self.trusted,
            "status": self.status,
        }


def cohens_kappa(judge: list[Verdict], owner: list[Verdict]) -> Agreement:
    """Agreement between two raters, corrected for agreement by chance.

    Raw agreement is misleading here and the reason is structural: most
    explanations are consistent, so a judge that answered "consistent" every time
    would agree with the owner most of the time while carrying no information.
    Kappa subtracts exactly that.

    Pure Python, for the same reason `firebreak.evals.statistics` is: a number
    that decides whether a metric may be quoted should be readable by whoever
    doubts it.
    """
    if len(judge) != len(owner):
        raise ValueError(
            f"kappa needs one owner label per judge verdict, got {len(judge)} and {len(owner)}"
        )
    total = len(judge)
    if total == 0:
        return Agreement(pairs=0, observed=0.0, expected=0.0, kappa=0.0)

    observed = sum(1 for a, b in zip(judge, owner, strict=True) if a is b) / total

    expected = 0.0
    for verdict in Verdict:
        judge_share = sum(1 for v in judge if v is verdict) / total
        owner_share = sum(1 for v in owner if v is verdict) / total
        expected += judge_share * owner_share

    if expected >= 1.0:
        # Both raters used exactly one category for everything. Kappa is undefined
        # there, and reporting 1.0 would claim perfect agreement from a rater that
        # made no distinctions at all.
        return Agreement(pairs=total, observed=observed, expected=expected, kappa=0.0)

    kappa = (observed - expected) / (1.0 - expected)
    return Agreement(pairs=total, observed=observed, expected=expected, kappa=kappa)


@dataclass(frozen=True)
class BlindItem:
    """One report as the owner sees it: the text, and nothing else."""

    item_id: str
    explanation: str
    cited_evidence: tuple[str, ...]
    fault_class: str


def write_blind_sheet(
    items: list[BlindItem],
    path: Path,
    seed: int = 1,
) -> Path:
    """Write the owner's labelling sheet, shuffled and carrying no verdicts.

    Shuffled so the order carries no signal: reports arrive grouped by
    configuration, and an owner labelling them in that order would be labelling
    configurations rather than explanations.

    No judge verdict and no scenario name. Calibration measures whether the owner
    and the judge agree independently, and an owner shown the judge's answer is
    measuring their agreement with a suggestion.
    """
    shuffled = list(items)
    random.Random(seed).shuffle(shuffled)
    payload = {
        "instructions": (
            "For each item, answer consistent, unsupported, or invented. Consistent "
            "means the explanation is supported by the cited evidence and matches the "
            "fault class. Unsupported means it is not supported. Invented means it "
            "names a service, metric, or number the evidence does not contain. Ignore "
            "length, style, and how confident the writing sounds."
        ),
        "rubric": JudgeRubric().as_prompt_section(),
        "items": [
            {
                "item_id": item.item_id,
                "explanation": item.explanation,
                "cited_evidence": list(item.cited_evidence),
                "fault_class": item.fault_class,
                "your_verdict": "",
            }
            for item in shuffled
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def read_blind_sheet(path: Path) -> dict[str, Verdict]:
    """Read the owner's answers back, keyed by opaque id.

    An unanswered item is skipped rather than defaulted. Defaulting would let a
    half-finished sheet produce a kappa, and the kappa would be reported as a
    calibration of sixty labels.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    answers: dict[str, Verdict] = {}
    for item in raw.get("items") or []:
        given = str(item.get("your_verdict") or "").strip().lower()
        if not given:
            continue
        try:
            answers[str(item["item_id"])] = Verdict(given)
        except ValueError as error:
            raise ValueError(
                f"item {item.get('item_id')!r} has verdict {given!r}; expected one of "
                f"{', '.join(v.value for v in Verdict)}"
            ) from error
    return answers


def calibrate(judged: list[JudgedReport], owner: dict[str, Verdict]) -> Agreement:
    """Compare the judge against the owner on the items the owner answered.

    Only the overlap is compared, and the count comes back in the result, so a
    calibration on twelve labels cannot be reported as one on sixty.
    """
    shared = [report for report in judged if report.item_id in owner]
    return cohens_kappa(
        [report.verdict for report in shared],
        [owner[report.item_id] for report in shared],
    )
