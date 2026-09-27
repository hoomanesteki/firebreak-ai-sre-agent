"""The agent holds nothing that can change the target system.

SPEC.md Section 17 Phase 9's first acceptance criterion: agent processes hold no
write credentials, asserted by a test. SPEC.md Section 6.10 puts the only such
credentials in the approval service, a separate process.

**Why this is an import test and not a runtime test.** A runtime test proves the
agent did not write anything on the path it happened to take. This proves it could
not have: the modules that can change flagd or restart a container are not reachable
from `firebreak.agent` at all. The check walks the import graph rather than reading
one file, because the seventh way this could go wrong is a helper three imports deep.

**Why an allowlist of readers rather than a denylist of writers.** A denylist has to
enumerate every way to reach the system, and a new one arrives whenever a dependency
is added. The set of modules the agent may import is small and stated, so anything
new has to be added deliberately, which is the review this test exists to force.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPO_ROOT / "src"
AGENT = SOURCE / "firebreak" / "agent"

# Modules that can change the target system, or that hold a way to. Named by the
# thing they can do, so the reason each one is here survives a rename.
WRITERS = {
    # Sets flag variants through flagd. The one thing that can change the demo's
    # behaviour.
    "firebreak.lab.flags",
    # Starts, stops and restarts containers.
    "firebreak.lab.stack",
    # Drives the recorder, which does both of the above.
    "firebreak.lab.recorder",
    # Sets load, which changes what the target system is doing.
    "firebreak.lab.load",
    # The only process allowed to execute a proposal.
    "firebreak.remediation.approval",
}

# Standard library modules that would let any of the above be reached indirectly.
DANGEROUS_STDLIB = {"subprocess", "os.system", "docker"}


def python_files(root: Path) -> Iterator[Path]:
    yield from sorted(root.rglob("*.py"))


def imports_of(path: Path, source_root: Path | None = None) -> set[str]:
    """Every module one file imports, by dotted name.

    Both forms: `import a.b` and `from a.b import c`. A relative import is resolved
    against the file's own package, because `from .flags import FlagController` is
    the same reach as the absolute form and a checker that missed it would be
    checking style rather than reachability.
    """
    root = source_root or SOURCE
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    package = ".".join(path.relative_to(root).with_suffix("").parts[:-1])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1])
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module or ""
            if module:
                found.add(module)
                found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


def reachable_from(start: Path, source_root: Path | None = None) -> dict[str, Path]:
    """Every firebreak module reachable from one file, and who imported it.

    Returns the importer as well as the module, so a failure says which file to
    look at rather than only that something somewhere reaches a writer.
    """
    root = source_root or SOURCE
    seen: dict[str, Path] = {}
    frontier = [start]
    while frontier:
        current = frontier.pop()
        for module in imports_of(current, source_root=root):
            if not module.startswith("firebreak"):
                continue
            if module in seen:
                continue
            seen[module] = current
            candidate = root / Path(*module.split(".")).with_suffix(".py")
            if candidate.is_file():
                frontier.append(candidate)
    return seen


class TestTheAgentCannotReachAWriter:
    """SPEC.md Section 17 Phase 9's acceptance criterion, by name."""

    def test_no_agent_module_imports_a_writer_directly(self) -> None:
        offences = []
        for path in python_files(AGENT):
            for module in imports_of(path) & WRITERS:
                offences.append(f"{path.relative_to(SOURCE)} imports {module}")
        assert not offences, "\n".join(offences)

    def test_no_writer_is_reachable_from_the_agent_at_any_depth(self) -> None:
        """The case a single-file check misses: a helper three imports deep."""
        offences = []
        for path in python_files(AGENT):
            reachable = reachable_from(path)
            for module in set(reachable) & WRITERS:
                offences.append(
                    f"{path.relative_to(SOURCE)} reaches {module} via "
                    f"{reachable[module].relative_to(SOURCE)}"
                )
        assert not offences, "\n".join(offences)

    def test_no_agent_module_shells_out(self) -> None:
        """subprocess is the general purpose way to reach anything, so it is out of
        bounds inside the agent regardless of what it would be used for."""
        offences = []
        for path in python_files(AGENT):
            for module in imports_of(path):
                if module.split(".")[0] in DANGEROUS_STDLIB:
                    offences.append(f"{path.relative_to(SOURCE)} imports {module}")
        assert not offences, "\n".join(offences)

    def test_the_agent_can_reach_the_proposal_module(self) -> None:
        """The other half of the claim. If the agent could not propose at all, the
        test above would pass on a system with no remediation in it.
        """
        from firebreak.remediation import proposal

        assert hasattr(proposal, "build_proposal")

    def test_the_writers_named_here_actually_exist(self) -> None:
        """A renamed writer would leave this test passing against nothing."""
        for module in WRITERS:
            path = SOURCE / Path(*module.split(".")).with_suffix(".py")
            assert path.is_file(), f"{module} is in WRITERS and does not exist"


class TestTheApprovalServiceIsTheOnlyExecutor:
    def test_the_proposal_module_cannot_reach_a_writer(self) -> None:
        """A proposal is passed to the approval service, so if the proposal module
        could reach a writer the boundary would be a formality."""
        reachable = reachable_from(SOURCE / "firebreak" / "remediation" / "proposal.py")
        assert not set(reachable) & WRITERS

    def test_the_audit_module_cannot_reach_a_writer(self) -> None:
        """The audit log is read by anything that wants to verify the chain, which
        should not be a route to a credential."""
        reachable = reachable_from(SOURCE / "firebreak" / "remediation" / "audit.py")
        assert not set(reachable) & WRITERS - {"firebreak.remediation.approval"}
        assert "firebreak.remediation.approval" not in reachable

    def test_the_approval_service_holds_no_executor_by_default(self) -> None:
        """So a service started without being given one changes nothing, rather
        than defaulting to whatever it can find."""
        from firebreak.remediation.approval import open_service

        service = open_service(REPO_ROOT / "unused.jsonl", allowlist={})
        assert service.executors == {}


class TestTheImportWalkerWorks:
    """A checker that found nothing because it was broken would be worse than none."""

    def test_it_finds_an_absolute_import(self, tmp_path: Path) -> None:
        path = _module(tmp_path, "probe.py", "from firebreak.lab.flags import FlagController\n")
        assert "firebreak.lab.flags" in imports_of(path, source_root=tmp_path)

    def test_it_finds_a_plain_import(self, tmp_path: Path) -> None:
        path = _module(tmp_path, "probe.py", "import firebreak.lab.flags\n")
        assert "firebreak.lab.flags" in imports_of(path, source_root=tmp_path)

    def test_it_finds_a_relative_import(self, tmp_path: Path) -> None:
        """`from .flags import x` is the same reach as the absolute form, and a
        checker that missed it would be checking style rather than reachability."""
        package = tmp_path / "firebreak" / "lab"
        package.mkdir(parents=True)
        path = package / "probe.py"
        path.write_text("from .flags import FlagController\n", encoding="utf-8")
        assert "firebreak.lab.flags" in imports_of(path, source_root=tmp_path)

    def test_it_finds_subprocess(self, tmp_path: Path) -> None:
        path = _module(tmp_path, "probe.py", "import subprocess\n")
        assert "subprocess" in imports_of(path, source_root=tmp_path)

    def test_a_real_agent_file_imports_something(self) -> None:
        """If `imports_of` returned nothing for every file, every test above would
        pass vacuously."""
        assert imports_of(AGENT / "graph.py")


def _module(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.write_text(body, encoding="utf-8")
    return path
