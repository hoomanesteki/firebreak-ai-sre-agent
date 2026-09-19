"""Tests for the commit message hook (SPEC.md Sections 18.3 and 19.4)."""

from __future__ import annotations

import pytest
from check_commit_msg import check_message, load_rules

RULES = load_rules()


def problems(message: str) -> list[str]:
    return check_message(message, RULES)


def test_check_message_accepts_conventional_subject_with_scope():
    assert problems("feat(gates): re-run cited queries before publishing a report") == []


def test_check_message_accepts_subject_without_scope():
    assert problems("chore: pin the demo submodule") == []


def test_check_message_accepts_body_after_blank_line():
    message = (
        "fix(triage): use median and MAD for anomaly scores\n\n"
        "Sparse series made the mean unstable.\n"
    )
    assert problems(message) == []


def test_check_message_rejects_ai_coauthor_trailer():
    message = "feat(agent): add commander node\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
    found = problems(message)
    assert any("AI attribution" in problem for problem in found)


def test_check_message_rejects_generated_with_line():
    found = problems("docs(repo): add readme\n\nGenerated with an assistant\n")
    assert any("AI attribution" in problem for problem in found)


def test_check_message_rejects_em_dash():
    found = problems("feat(tools): add metrics tool \u2014 with caps")
    assert any("U+2014" in problem for problem in found)


def test_check_message_rejects_en_dash():
    found = problems("feat(tools): add metrics tool \u2013 with caps")
    assert any("U+2013" in problem for problem in found)


def test_check_message_rejects_non_conventional_subject():
    found = problems("update stuff")
    assert any("type(scope): summary" in problem for problem in found)


def test_check_message_rejects_unknown_type():
    found = problems("wip(agent): partial work")
    assert any("is not one of" in problem for problem in found)


def test_check_message_rejects_unknown_scope():
    found = problems("feat(quantum): add a node")
    assert any("scope 'quantum'" in problem for problem in found)


def test_check_message_rejects_subject_over_limit():
    subject = "feat(agent): " + "x" * 80
    found = problems(subject)
    assert any("limit is 72" in problem for problem in found)


def test_check_message_rejects_trailing_period():
    found = problems("feat(agent): add the commander node.")
    assert any("must not end with a period" in problem for problem in found)


def test_check_message_rejects_capitalised_subject():
    found = problems("feat(agent): Added agents")
    assert any("imperative mood" in problem for problem in found)


def test_check_message_rejects_missing_blank_line_before_body():
    found = problems("feat(agent): add node\nbody starts immediately")
    assert any("blank line" in problem for problem in found)


def test_check_message_rejects_empty_message():
    assert problems("\n# comment only\n") == ["commit message is empty"]


def test_check_message_allows_merge_commits():
    assert problems("Merge branch 'main' into feat/p01-scenario-lab") == []


@pytest.mark.parametrize("scope", ["gates", "evals", "triage", "ops"])
def test_check_message_accepts_every_spec_scope(scope: str):
    assert problems(f"feat({scope}): add something useful") == []
