"""Every path the Makefile names has to exist.

**The defect this generalises from.** `make console` ran `python -m firebreak.web.serve` for
the whole of Phase 11 and no such module existed. Thirty-four tests said the Console worked,
because they drove the application object rather than the command, so the failure was visible
only to somebody who typed the command.

The Makefile is the documented interface to this project: the README quotes it, the phase
reports quote it, and it is what a reader runs first. A target naming a file that is not there
is a broken interface no amount of testing behind it can detect. This file checks the names,
which is cheap, and says nothing about whether the targets do the right thing, which is the
job of the tests for each one.

**Not a substitute for running them.** `make live` needs Docker and `make eval` needs a
library; neither is run here. The point is narrower: nothing the Makefile points at should be
missing.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"

TEXT = MAKEFILE.read_text(encoding="utf-8")

# Any Python file the Makefile runs, not just those under scripts/. The site builder lives
# in site/, and a pattern anchored on one directory would have let that one through.
SCRIPT_PATTERN = re.compile(r"(?:scripts|site|ops|tools)/[A-Za-z0-9_/]+\.py")
MODULE_PATTERN = re.compile(r"python -m ([A-Za-z0-9_.]+)")
TARGET_PATTERN = re.compile(r"^([a-zA-Z][a-zA-Z0-9_-]*):.*?## ", re.MULTILINE)

SCRIPTS = sorted(set(SCRIPT_PATTERN.findall(TEXT)))
MODULES = sorted(set(MODULE_PATTERN.findall(TEXT)))
DOCUMENTED_TARGETS = sorted(set(TARGET_PATTERN.findall(TEXT)))


class TestEveryScriptTheMakefileRunsExists:
    def test_at_least_one_script_was_found(self) -> None:
        """A regex that matched nothing would make every test below pass while checking
        nothing, which is the failure mode of a test built on parsing."""
        assert len(SCRIPTS) >= 10, f"only found {SCRIPTS}, the pattern is probably wrong"

    @pytest.mark.parametrize("path", SCRIPTS)
    def test_the_script_is_on_disk(self, path: str) -> None:
        assert (REPO_ROOT / path).is_file(), f"the Makefile runs {path} and it is missing"


class TestEveryModuleTheMakefileRunsImports:
    def test_at_least_one_module_was_found(self) -> None:
        assert MODULES, "the python -m pattern matched nothing"

    @pytest.mark.parametrize("name", MODULES)
    def test_the_module_imports(self, name: str) -> None:
        """Imported rather than run. `python -m` on a module that cannot be imported is
        the exact failure `make console` had, and importing is enough to catch it while
        starting a server is not something a unit test should do."""
        assert importlib.import_module(name) is not None


class TestTheHelpTextCoversTheTargets:
    """`make help` lists targets by their `##` comment, so a target without one is
    invisible to a reader even though it works."""

    def test_every_documented_target_is_declared_phony(self) -> None:
        """A target whose name matches a real file silently stops running when that file
        exists. `make demo` beside a `demo/` directory is exactly that, and this project
        has a `src/firebreak/demo` package already."""
        phony = " ".join(
            line for line in TEXT.splitlines() if line.startswith(".PHONY") or line.startswith("  ")
        )
        block = TEXT.split(".PHONY:", 1)[1].split("\n\n", 1)[0] if ".PHONY:" in TEXT else phony
        declared = set(block.replace("\\", " ").split())
        missing = [name for name in DOCUMENTED_TARGETS if name not in declared]
        assert not missing, f"documented but not .PHONY: {missing}"

    def test_the_targets_this_project_documents_are_present(self) -> None:
        """The commands CLAUDE.md and the README name. A rename that missed one of these
        would break the documented interface without breaking a test."""
        for name in (
            "setup",
            "verify",
            "site",
            "site-stats",
            "demo",
            "demo-offline",
            "console",
            "eval",
            "live",
            "spec-check",
            "ci-status",
        ):
            assert name in DOCUMENTED_TARGETS, f"make {name} is documented elsewhere and missing"
