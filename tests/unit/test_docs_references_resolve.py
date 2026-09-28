"""Every command, path and config key the documentation names must exist.

**Why this is worth a test file.** Documentation is the only part of this project with no
compiler and no runtime, so it rots silently and a reader finds out by typing something that
fails. The runbook is the worst place for that: it is read by somebody who did not build this,
during a procedure they have not done before, and a step naming a file that was renamed is
indistinguishable from a step they got wrong.

This is the same class of check as `test_makefile_targets_resolve.py`, which exists because
`make console` pointed at a module that never existed for a whole phase while 34 tests said the
Console worked.

**Names only.** Nothing here runs a procedure or checks that the advice is good. A runbook can
be perfectly resolvable and still wrong; what it cannot be is full of paths that are not there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = (
    REPO_ROOT / "docs" / "runbook.md",
    REPO_ROOT / "docs" / "threat-model.md",
    REPO_ROOT / "README.md",
)

# `make <target>`, only where it is written as a command: inside backticks, or at the start of
# a line in a fenced block. Matching bare prose picked up "make it harder" and "make load
# levels hard" as targets, which is the reverse of useful: a test that cries wolf on English
# gets its assertions loosened until it checks nothing.
MAKE_PATTERN = re.compile(
    r"`make ([a-z][a-z0-9-]*)[^`]*`"  # `make demo-offline` or `make eval CONFIG=...`
    r"|^\s*(?:caffeinate [^\n]*)?make ([a-z][a-z0-9-]*)",  # a command line in a block
    re.MULTILINE,
)

# A repository path in backticks. Anchored on the directories this project has, so a phrase
# like `extra="forbid"` is not mistaken for a path.
PATH_PATTERN = re.compile(
    r"`((?:src|tests|scripts|scenarios|config|docs|reports|site|ops|recordings|bundles)"
    r"/[A-Za-z0-9_./*<>-]+)`"
)

# Paths that name something deliberately absent, with the reason. Each is a real statement in
# the documentation about something not in the repository, so requiring it to exist would
# make the test demand the opposite of what the text says.
ABSENT_BY_DESIGN = {
    # Bundles are git-ignored: 7.6 MB each, shipped as a release asset.
    "bundles/<opaque-id>/manifest.json",
    "bundles/",
}


def documented_make_targets() -> set[str]:
    found: set[str] = set()
    for path in DOCS:
        if not path.is_file():
            continue
        for groups in MAKE_PATTERN.findall(path.read_text(encoding="utf-8")):
            found |= {name for name in groups if name}
    return found


def documented_paths() -> set[str]:
    found: set[str] = set()
    for path in DOCS:
        if path.is_file():
            found |= set(PATH_PATTERN.findall(path.read_text(encoding="utf-8")))
    return found - ABSENT_BY_DESIGN


MAKEFILE_TEXT = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
REAL_TARGETS = set(re.compile(r"^([a-zA-Z][a-zA-Z0-9_-]*):", re.MULTILINE).findall(MAKEFILE_TEXT))

MAKE_TARGETS = sorted(documented_make_targets())
PATHS = sorted(documented_paths())


class TestEveryDocumentedCommandExists:
    def test_the_pattern_found_something(self) -> None:
        """A regex matching nothing makes every parametrised test below vacuous, which is the
        standard way a test built on parsing stops testing."""
        assert len(MAKE_TARGETS) >= 10, f"only found {MAKE_TARGETS}"

    @pytest.mark.parametrize("target", MAKE_TARGETS)
    def test_the_make_target_is_real(self, target: str) -> None:
        assert target in REAL_TARGETS, f"the docs tell a reader to run `make {target}`"


class TestEveryDocumentedPathExists:
    def test_the_pattern_found_something(self) -> None:
        assert len(PATHS) >= 10, f"only found {PATHS}"

    @pytest.mark.parametrize("path", PATHS)
    def test_the_path_is_on_disk(self, path: str) -> None:
        """Globs and placeholders are resolved loosely: what matters is that the directory
        leading to it exists, since a renamed directory is the failure this catches."""
        target = REPO_ROOT / path
        if target.exists():
            return
        if any(character in path for character in "*<>"):
            # Walk up past every placeholder segment. `reports/eval/<config>/<split>/` should
            # check `reports/eval`, not `reports/eval/<config>`, which never exists.
            concrete = Path(path)
            while concrete.parts and any(c in concrete.name for c in "*<>"):
                concrete = concrete.parent
            assert (REPO_ROOT / concrete).exists(), (
                f"the docs name {path} and {concrete} is missing"
            )
            return
        pytest.fail(f"the docs name {path} and it does not exist")


class TestTheRunbookNamesRealConfigKeys:
    """The retune procedure tells a reader which key to change. A key that moved is worse than
    no advice, because a reader will add it and wonder why nothing changed."""

    def test_the_threshold_keys_exist(self) -> None:
        loaded = yaml.safe_load((REPO_ROOT / "config" / "thresholds.yaml").read_text())
        runbook = (REPO_ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
        for dotted in re.findall(r"`((?:abstention|ranking|windows)\.[a-z_*]+)`", runbook):
            section, _, key = dotted.partition(".")
            assert section in loaded, f"the runbook names {dotted} and {section} is missing"
            if key == "*":
                continue
            assert key in loaded[section], f"the runbook names {dotted} and {key} is missing"

    def test_the_model_config_key_exists(self) -> None:
        text = (REPO_ROOT / "config" / "models.yaml").read_text(encoding="utf-8")
        assert "escalate_after_schema_failures" in text

    def test_the_worked_example_matches_the_config_it_describes(self) -> None:
        """The retune example quotes figures that also live in a comment in the config. Two
        copies of a number disagree eventually, and this is the pair a reader is most likely
        to act on."""
        config = " ".join((REPO_ROOT / "config" / "thresholds.yaml").read_text().split())
        runbook = " ".join((REPO_ROOT / "docs" / "runbook.md").read_text().lower().split())
        # Whitespace collapsed first. Line wrapping in prose is arbitrary, so a phrase that
        # happens to straddle a newline would fail an assertion about the wording rather than
        # about the meaning, and the fix for that failure is to weaken the assertion.
        for claim in ("double sensitivity", "latency faults", "one no-fault recording"):
            assert claim in runbook, f"the retune example no longer says {claim!r}"
        assert "13.4" in config, "the config no longer records the rejected retune value"
        assert "latency fault" in config, "the config no longer records the reason it stands"


class TestTheRunbookCoversWhatTheSpecAsksFor:
    def test_all_seven_procedures_are_present(self) -> None:
        """SPEC.md Section 16 lists them by name. A runbook missing one is a gap nothing else
        would report."""
        runbook = (REPO_ROOT / "docs" / "runbook.md").read_text(encoding="utf-8").lower()
        for procedure in (
            "record a new scenario",
            "add a tool",
            "add a remediation",
            "retune thresholds",
            "rotate secrets",
            "investigate a failed eval",
            "restore from backup",
        ):
            assert procedure in runbook, f"SPEC.md Section 16 asks for {procedure!r}"

    def test_every_procedure_says_how_to_tell_it_worked(self) -> None:
        """A procedure with no success condition is a list of commands. Seven procedures, so
        seven of these."""
        runbook = (REPO_ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
        assert runbook.count("### How to tell it worked") == 7

    def test_the_environment_variables_it_names_are_ones_the_code_reads(self) -> None:
        """The rotation procedure is useless if it names a variable nothing reads. This is the
        same contract that caught `LLM_BASE_URL` being documented and unread."""
        from firebreak.settings import Settings

        runbook = (REPO_ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
        named = set(re.findall(r"`((?:LLM_|FIREBREAK_)[A-Z0-9_]+)`", runbook))
        assert named, "the rotation procedure names no environment variable"
        readable = set()
        for name, field in Settings.model_fields.items():
            readable.add(f"FIREBREAK_{name.upper()}")
            alias = field.validation_alias
            for choice in getattr(alias, "choices", []) or ([alias] if alias else []):
                if isinstance(choice, str):
                    readable.add(choice)
        missing = named - readable
        assert not missing, f"the runbook names variables nothing reads: {sorted(missing)}"
