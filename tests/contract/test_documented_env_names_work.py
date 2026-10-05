"""The environment variables the documentation names must be the ones read.

A setting that is documented under one name and read under another does nothing,
and reports nothing, because the default is a valid value. SPEC.md Section 6.7
writes `LLM_BASE_URL` and `LLM_API_KEY` unprefixed, CLAUDE.md repeats them, and
`Settings` prefixed everything with FIREBREAK_, so an owner following the
documentation would have set two variables that had no effect and stayed in stub
mode.

This is a contract test rather than a unit test because the contract is between a
document and the code, and neither file can assert it alone.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from firebreak.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]

# The variables the documentation tells a reader to set, and the field each one
# must reach. Listed rather than parsed out of the prose, because a regular
# expression over documentation is a test of the regular expression.
DOCUMENTED = {
    "LLM_BASE_URL": "llm_base_url",
    "LLM_API_KEY": "llm_api_key",
}


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Neither spelling leaks in from the developer's own shell."""
    for name in DOCUMENTED:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"FIREBREAK_{name}", raising=False)


@pytest.mark.parametrize(("variable", "field"), sorted(DOCUMENTED.items()))
def test_the_documented_name_reaches_the_field(variable, field, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(variable, "https://gateway.example/v1")
    assert getattr(Settings(), field) == "https://gateway.example/v1"


@pytest.mark.parametrize(("variable", "field"), sorted(DOCUMENTED.items()))
def test_the_prefixed_name_still_works(variable, field, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Kept, so anybody who already set the prefixed form is not broken by the
    fix."""
    monkeypatch.setenv(f"FIREBREAK_{variable}", "https://gateway.example/v2")
    assert getattr(Settings(), field) == "https://gateway.example/v2"


def variables_the_code_tells_users_to_set() -> set[str]:
    """Every SCREAMING_SNAKE token the agent's own error messages name.

    Read out of the source rather than listed, because the list is what drifted:
    `llm.py` told a user to "set LLM_BASE_URL and LLM_API_KEY" while `Settings`
    read only the FIREBREAK_ prefixed forms, so following the instruction did
    nothing and the process stayed in stub mode.
    """
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "src" / "firebreak" / "agent").glob("*.py")
    )
    return set(re.findall(r"\b(LLM_[A-Z_]+)\b", text))


def test_the_error_message_names_variables_that_actually_work() -> None:
    """The contract this file exists for.

    Any variable an error message tells somebody to set has to be one the process
    reads. An instruction that does nothing is worse than no instruction: the
    reader follows it and concludes the system is broken elsewhere.
    """
    named = variables_the_code_tells_users_to_set()
    assert named, "llm.py names no environment variable, so this test is stale"
    assert named <= set(DOCUMENTED), (
        f"llm.py tells users to set {sorted(named - set(DOCUMENTED))}, which nothing "
        "in Settings reads under that name"
    )


def test_spec_names_the_gateway_variable() -> None:
    """SPEC.md Section 6.7's Tollgate paragraph is where the unprefixed spelling
    comes from, so the alias outlives its reason if that paragraph goes."""
    spec = (REPO_ROOT / "SPEC.md").read_text(encoding="utf-8")
    assert re.search(r"\bLLM_BASE_URL\b", spec)


def test_the_example_env_file_uses_names_that_work(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`.env.example` is what a new developer copies, so every commented setting
    in it has to reach a field."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for variable in sorted(set(re.findall(r"^#\s*((?:FIREBREAK_)?LLM_[A-Z_]+)=", text, re.M))):
        monkeypatch.setenv(variable, "https://from-example/v1")
        field = variable.removeprefix("FIREBREAK_").lower()
        assert getattr(Settings(), field) == "https://from-example/v1", (
            f".env.example names {variable}, which does not reach Settings.{field}"
        )
        monkeypatch.delenv(variable)


def test_the_documented_name_wins_when_both_are_set(monkeypatch) -> None:
    """An ambiguity has to resolve the same way every time, and it resolves to the
    spelling the documentation tells people to use."""
    monkeypatch.setenv("LLM_BASE_URL", "https://documented.example/v1")
    monkeypatch.setenv("FIREBREAK_LLM_BASE_URL", "https://prefixed.example/v1")
    assert Settings().llm_base_url == "https://documented.example/v1"
