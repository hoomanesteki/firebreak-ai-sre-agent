"""Tests for the model-based judge and its calibration.

The judge is the only grader that is not code, so it is the only one that can be
wrong in a way no assertion catches. SPEC.md Section 9.2's controls exist because
[R23] measured position, verbosity and self-enhancement bias in LLM judges, and
the control that actually protects a number is the last one: below Cohen's kappa
0.4 the judged metric is shown as "not trusted" rather than shown.

Most of this file tests that threshold and the arithmetic behind it, because an
uncalibrated judge still produces a number and a number gets quoted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from firebreak.evals.judge import (
    CALIBRATION_LABELS,
    MINIMUM_KAPPA,
    Agreement,
    BlindItem,
    JudgedReport,
    JudgeRubric,
    Verdict,
    calibrate,
    cohens_kappa,
    read_blind_sheet,
    write_blind_sheet,
)


def judged(**verdicts: str) -> list[JudgedReport]:
    return [
        JudgedReport(item_id=item_id, verdict=Verdict(value)) for item_id, value in verdicts.items()
    ]


class TestTheRubricIgnoresLengthAndStyle:
    """A judge that rewards long answers turns the reporter into a machine for
    writing long answers."""

    def test_length_is_explicitly_ignored(self) -> None:
        rubric = JudgeRubric()
        assert any("Length" in text for text in rubric.ignored)

    def test_style_is_explicitly_ignored(self) -> None:
        rubric = JudgeRubric()
        assert any("Style" in text for text in rubric.ignored)

    def test_confident_phrasing_is_explicitly_ignored(self) -> None:
        """Verbosity and self-enhancement bias [R23]: a judge rewards writing that
        sounds like its own, and confident phrasing is how that shows up."""
        rubric = JudgeRubric()
        assert any("confidence of phrasing" in text.lower() for text in rubric.ignored)
        assert any("strong model" in text.lower() for text in rubric.ignored)

    def test_the_criteria_are_about_support_and_invention(self) -> None:
        rubric = JudgeRubric()
        joined = " ".join(rubric.criteria).lower()
        assert "evidence" in joined
        assert "does not" in joined

    def test_the_prompt_is_built_from_the_same_data_the_tests_assert_on(self) -> None:
        """A rubric that lives only inside a prompt is one nobody can check was
        followed."""
        rubric = JudgeRubric()
        prompt = rubric.as_prompt_section()
        for text in rubric.criteria + rubric.ignored:
            assert text in prompt


class TestInventedIsNotAWorseUnsupported:
    def test_only_consistent_is_acceptable(self) -> None:
        assert Verdict.CONSISTENT.acceptable
        assert not Verdict.UNSUPPORTED.acceptable
        assert not Verdict.INVENTED.acceptable

    def test_there_are_exactly_three_verdicts(self) -> None:
        """Three, not a score out of ten. A judge asked for a number produces one
        with no calibration behind it."""
        assert [v.value for v in Verdict] == ["consistent", "unsupported", "invented"]


class TestKappaCorrectsForChance:
    def test_perfect_agreement_on_a_mixed_set_is_one(self) -> None:
        both = [Verdict.CONSISTENT, Verdict.UNSUPPORTED, Verdict.INVENTED] * 4
        assert cohens_kappa(both, both).kappa == pytest.approx(1.0)

    def test_a_rater_that_says_one_thing_every_time_scores_zero(self) -> None:
        """The failure raw agreement hides.

        Most explanations are consistent, so a judge answering "consistent" every
        time agrees with the owner most of the time while carrying no information.
        """
        judge = [Verdict.CONSISTENT] * 10
        owner = [Verdict.CONSISTENT] * 9 + [Verdict.INVENTED]
        agreement = cohens_kappa(judge, owner)
        assert agreement.observed == pytest.approx(0.9)
        assert agreement.kappa == pytest.approx(0.0)

    def test_both_raters_using_one_category_is_not_perfect_agreement(self) -> None:
        """Kappa is undefined there, and 1.0 would claim perfect agreement from a
        rater that made no distinctions at all."""
        both = [Verdict.CONSISTENT] * 10
        agreement = cohens_kappa(both, both)
        assert agreement.observed == pytest.approx(1.0)
        assert agreement.kappa == pytest.approx(0.0)

    def test_total_disagreement_is_negative(self) -> None:
        judge = [Verdict.CONSISTENT] * 5 + [Verdict.INVENTED] * 5
        owner = [Verdict.INVENTED] * 5 + [Verdict.CONSISTENT] * 5
        assert cohens_kappa(judge, owner).kappa < 0.0

    def test_mismatched_lengths_are_refused(self) -> None:
        with pytest.raises(ValueError, match="one owner label per judge verdict"):
            cohens_kappa([Verdict.CONSISTENT], [])

    def test_no_labels_gives_zero_rather_than_dividing_by_zero(self) -> None:
        assert cohens_kappa([], []).kappa == 0.0


class TestWhenTheJudgedMetricMayBeQuoted:
    def test_good_agreement_on_enough_labels_is_trusted(self) -> None:
        agreement = Agreement(pairs=CALIBRATION_LABELS, observed=0.9, expected=0.5, kappa=0.8)
        assert agreement.trusted
        assert "trusted: Cohen's kappa 0.80" in agreement.status

    def test_low_kappa_is_not_trusted_however_many_labels(self) -> None:
        agreement = Agreement(pairs=1000, observed=0.9, expected=0.89, kappa=0.1)
        assert not agreement.trusted
        assert "below 0.4" in agreement.status

    def test_high_kappa_on_too_few_labels_is_not_trusted(self) -> None:
        """Twelve labels that agree perfectly are twelve labels, and SPEC.md
        Section 9.2 asks for sixty."""
        agreement = Agreement(pairs=12, observed=1.0, expected=0.4, kappa=1.0)
        assert not agreement.trusted
        assert "12 of the 60" in agreement.status

    def test_the_threshold_is_the_one_spec_names(self) -> None:
        assert MINIMUM_KAPPA == 0.4
        assert CALIBRATION_LABELS == 60

    def test_it_serialises_with_its_status(self) -> None:
        payload = Agreement(pairs=60, observed=0.9, expected=0.5, kappa=0.8).as_dict()
        assert payload["trusted"] is True
        assert "kappa" in payload
        assert isinstance(payload["status"], str)


class TestTheBlindSheet:
    def items(self) -> list[BlindItem]:
        return [
            BlindItem(
                item_id=f"item{index:02d}",
                explanation=f"explanation {index}",
                cited_evidence=(f"ev_metric_{index:012x}",),
                fault_class="error",
            )
            for index in range(10)
        ]

    def test_it_carries_no_verdict_for_the_owner_to_agree_with(self, tmp_path: Path) -> None:
        """Calibration measures independent judgement. An owner shown the judge's
        answer is measuring their agreement with a suggestion."""
        path = write_blind_sheet(self.items(), tmp_path / "sheet.json")
        raw = json.loads(path.read_text())
        for item in raw["items"]:
            assert item["your_verdict"] == ""
            assert "judge_verdict" not in item
            assert "verdict" not in set(item) - {"your_verdict"}

    def test_it_carries_no_scenario_name(self, tmp_path: Path) -> None:
        """A scenario name states the answer, which is the same leakage control
        that keeps bundle directories opaque."""
        path = write_blind_sheet(self.items(), tmp_path / "sheet.json")
        raw = json.loads(path.read_text())
        for item in raw["items"]:
            assert set(item) == {
                "item_id",
                "explanation",
                "cited_evidence",
                "fault_class",
                "your_verdict",
            }

    def test_it_shuffles_so_the_order_carries_no_signal(self, tmp_path: Path) -> None:
        """Reports arrive grouped by configuration, and an owner labelling them in
        that order would be labelling configurations."""
        path = write_blind_sheet(self.items(), tmp_path / "sheet.json", seed=7)
        raw = json.loads(path.read_text())
        order = [item["item_id"] for item in raw["items"]]
        assert order != sorted(order)
        assert sorted(order) == [f"item{index:02d}" for index in range(10)]

    def test_the_shuffle_is_reproducible(self, tmp_path: Path) -> None:
        first = write_blind_sheet(self.items(), tmp_path / "a.json", seed=3)
        second = write_blind_sheet(self.items(), tmp_path / "b.json", seed=3)
        assert json.loads(first.read_text()) == json.loads(second.read_text())

    def test_it_includes_the_rubric_the_judge_was_given(self, tmp_path: Path) -> None:
        """Two raters scoring against different rubrics measures the rubrics."""
        path = write_blind_sheet(self.items(), tmp_path / "sheet.json")
        raw = json.loads(path.read_text())
        assert raw["rubric"] == JudgeRubric().as_prompt_section()

    def test_reading_it_back_skips_unanswered_items(self, tmp_path: Path) -> None:
        """Defaulting would let a half-finished sheet produce a kappa, and the
        kappa would be reported as a calibration of sixty labels."""
        path = write_blind_sheet(self.items(), tmp_path / "sheet.json")
        raw = json.loads(path.read_text())
        raw["items"][0]["your_verdict"] = "consistent"
        raw["items"][1]["your_verdict"] = "invented"
        path.write_text(json.dumps(raw))
        answers = read_blind_sheet(path)
        assert len(answers) == 2

    def test_an_unknown_verdict_is_refused(self, tmp_path: Path) -> None:
        path = write_blind_sheet(self.items(), tmp_path / "sheet.json")
        raw = json.loads(path.read_text())
        raw["items"][0]["your_verdict"] = "probably fine"
        path.write_text(json.dumps(raw))
        with pytest.raises(ValueError, match="expected one of"):
            read_blind_sheet(path)

    def test_case_and_whitespace_are_tolerated(self, tmp_path: Path) -> None:
        """A human fills this in by hand."""
        path = write_blind_sheet(self.items(), tmp_path / "sheet.json")
        raw = json.loads(path.read_text())
        raw["items"][0]["your_verdict"] = "  Consistent "
        path.write_text(json.dumps(raw))
        assert list(read_blind_sheet(path).values()) == [Verdict.CONSISTENT]


class TestCalibratingAgainstTheSheet:
    def test_only_the_overlap_is_compared_and_counted(self) -> None:
        """A calibration on twelve labels must not be reportable as one on sixty."""
        verdicts = judged(a="consistent", b="invented", c="consistent")
        owner = {"a": Verdict.CONSISTENT, "b": Verdict.INVENTED}
        agreement = calibrate(verdicts, owner)
        assert agreement.pairs == 2
        assert not agreement.trusted

    def test_an_empty_sheet_produces_no_calibration(self) -> None:
        agreement = calibrate(judged(a="consistent"), {})
        assert agreement.pairs == 0
        assert not agreement.trusted
