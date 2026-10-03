"""Feedback on a published report, and turning a failure into a test case.

SPEC.md Sections 10.1 and 10.2. A reviewer says whether the root cause was right,
what the true cause was if they know, which claims were wrong, and anything else.
`feedback promote` then turns a reviewed incident into an eval task.

**Why "partly" is one of the three answers.** Correct and incorrect are not enough
for this system. The most common real outcome is that the named service is in the
call path of the true cause: naming the frontend when payment was broken is wrong,
and it is not as wrong as naming the recommendation service. Collapsing it into
"incorrect" would throw away the distinction the ranking metrics exist to measure,
and collapsing it into "correct" would flatter the system.

**Why feedback records which claims were wrong.** SPEC.md Section 10.2 sources eval
tasks from real failures [R6], and a failure is only useful as a test if you know
what failed. "The report was wrong" produces a task nobody can grade; "claim 3
quoted an error rate the evidence did not support" produces one that fails today and
passes when the bug is fixed.

**Promotion is deliberately narrow.** It writes a label and says what else must
happen. It does not snapshot a bundle, because the bundle recorder is a live-stack
operation that takes eighteen minutes and needs the demo running, and a function that
silently did nothing when the stack was absent would be worse than one that says what
it did not do.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parents[3]
FEEDBACK_PATH = REPO_ROOT / "reports" / "feedback" / "feedback.jsonl"


class FeedbackError(Exception):
    """Feedback could not be recorded or read."""


class Verdict(StrEnum):
    """How right the report was.

    Three values, and the middle one is the point. See the module docstring.
    """

    CORRECT = "correct"
    PARTLY = "partly"
    INCORRECT = "incorrect"

    @property
    def confirms_the_root_cause(self) -> bool:
        """Whether this verdict confirms the named service.

        Only `correct` does. `partly` is a useful answer and not a confirmation, and
        memory admits confirmed incidents only, so treating it as one would put a
        service that was in the call path into memory as the cause.
        """
        return self is Verdict.CORRECT


class Feedback(BaseModel):
    """One reviewer's answer about one report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str
    reviewer: str = Field(min_length=1)
    verdict: Verdict
    # What the reviewer says the cause actually was. Required when the verdict is not
    # `correct`, because a report marked wrong with no correction is a complaint
    # rather than feedback, and it cannot become a test case.
    true_root_cause: str | None = None
    # Which claims did not hold, by their index in the published report. Indices
    # rather than text, so a reviewer marking claim 3 and a developer reading claim 3
    # are looking at the same sentence.
    incorrect_claims: tuple[int, ...] = ()
    note: str = Field(default="", max_length=2000)
    submitted_at: datetime

    def model_post_init(self, _context: object) -> None:
        if self.verdict is not Verdict.CORRECT and not self.true_root_cause:
            raise ValueError(
                f"{self.incident_id} was marked {self.verdict.value} with no true root "
                "cause; a report marked wrong with no correction cannot become a test "
                "case, which is the point of collecting this"
            )

    @property
    def promotable(self) -> bool:
        """Whether this feedback can become an eval task.

        Any verdict can, including `correct`: a report that was right is a task the
        system should keep getting right, and a regression suite made only of failures
        would not notice one.
        """
        return bool(self.true_root_cause) or self.verdict is Verdict.CORRECT


class FeedbackStore:
    """Feedback on disk, one record per line."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or FEEDBACK_PATH

    @property
    def path(self) -> Path:
        return self._path

    def all(self) -> list[Feedback]:
        if not self._path.is_file():
            return []
        found = []
        for number, line in enumerate(self._path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                found.append(Feedback.model_validate_json(line))
            except ValueError as error:
                raise FeedbackError(
                    f"{self._path.name} line {number} is not feedback: {error}"
                ) from error
        return found

    def record(self, feedback: Feedback) -> Feedback:
        """Append one answer.

        Appends rather than replaces, even for the same incident. Two reviewers
        disagreeing about a report is a fact worth keeping, and the second answer
        overwriting the first would hide exactly the cases where the system's output
        was ambiguous.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(feedback.model_dump_json() + "\n")
        return feedback

    def for_incident(self, incident_id: str) -> list[Feedback]:
        return [item for item in self.all() if item.incident_id == incident_id]

    def agreement(self, incident_id: str) -> bool | None:
        """Whether every reviewer of this incident agreed, or None if fewer than two.

        Used before promotion: a contested incident should not become a labelled test
        case on one reviewer's word, because the label would then encode a
        disagreement as a fact.
        """
        answers = self.for_incident(incident_id)
        if len(answers) < 2:
            return None
        verdicts = {item.verdict for item in answers}
        causes = {item.true_root_cause for item in answers}
        return len(verdicts) == 1 and len(causes) == 1


class PromotionResult(BaseModel):
    """What promotion did, and what it could not do.

    The unfinished steps are returned rather than logged, because a promotion that
    wrote a label and did not snapshot a bundle has produced a task nothing can run,
    and the caller has to know that rather than discover it at the next eval.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str
    label_path: str
    split: str
    remaining_steps: tuple[str, ...] = ()

    @property
    def runnable(self) -> bool:
        return not self.remaining_steps


def promote(
    feedback: Feedback,
    split: str,
    labels_root: Path,
    bundle_exists: bool,
    scenario_id: str | None = None,
    now: datetime | None = None,
    *,
    fault_class: str = "unknown",
    confirmed_no_fault: bool = False,
) -> PromotionResult:
    """Turn reviewed feedback into a labelled eval task.

    `split` is the caller's decision and is recorded, not derived. A promoted incident
    is new data, and which split it joins determines whether it can ever be used to
    tune anything. Deriving it from a hash here would put a real incident into a
    held-out split by accident, and the split assignment file exists precisely because
    that kind of accident is unrecoverable.

    `bundle_exists` is passed in rather than checked, because this function writes a
    label and knows nothing about the recorder. When it is false the label is still
    written and the missing snapshot is returned as a remaining step: a half-promoted
    task that says so is more useful than a refusal, since the label is the part a
    person cannot reconstruct later.
    """
    # Defence in depth, and unreachable for a validly constructed `Feedback`:
    # `model_post_init` already refuses a non-correct verdict with no cause, and it
    # runs even for `model_construct`. Kept because this function writes a label that
    # becomes ground truth, and a label with no target is the one mistake here that
    # would quietly corrupt an eval rather than fail.
    if not feedback.promotable:
        raise FeedbackError(
            f"{feedback.incident_id} cannot be promoted: it is marked "
            f"{feedback.verdict.value} with no true root cause, so there is nothing to "
            "label it with"
        )

    if split not in {"train", "validation", "test_id", "test_ood"}:
        raise FeedbackError("choose a declared evaluation split")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", feedback.incident_id):
        raise FeedbackError("incident id must be a safe file name")
    if feedback.true_root_cause is None and not confirmed_no_fault:
        raise FeedbackError(
            "confirm the root cause or explicitly confirm no fault before promotion"
        )
    if confirmed_no_fault and feedback.true_root_cause is not None:
        raise FeedbackError("no-fault confirmation contradicts the named root cause")
    target = feedback.true_root_cause
    label = {
        "bundle_id": feedback.incident_id,
        "run_id": "feedback",
        "fault_class": "none" if confirmed_no_fault else fault_class,
        "canary": "fbcanary-" + secrets.token_hex(16),
        "scenario_id": scenario_id or f"promoted-{feedback.incident_id}",
        "split": split,
        "target_service": target,
        "notes": json.dumps(
            {
                "source": "feedback",
                "reviewer": feedback.reviewer,
                "verdict": feedback.verdict.value,
                "promoted_at": (now or datetime.now(UTC)).isoformat(),
            }
        ),
    }
    path = labels_root / "promoted" / f"{feedback.incident_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(label, indent=2) + "\n")

    remaining = []
    if not bundle_exists:
        remaining.append(
            f"snapshot the bundle for {feedback.incident_id}: the label is written and "
            "nothing can run against it until the telemetry for that window is "
            "recorded"
        )
    if feedback.verdict is not Verdict.CORRECT and not feedback.incorrect_claims:
        remaining.append(
            "record which claims were wrong: a task that fails without saying which "
            "claim failed cannot show when it has been fixed"
        )
    return PromotionResult(
        incident_id=feedback.incident_id,
        label_path=str(path),
        split=split,
        remaining_steps=tuple(remaining),
    )
