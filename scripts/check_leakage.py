"""Prove the agent cannot reach ground truth.

SPEC.md Section 8.4. Leakage is the most consequential defect this project
can have and the least visible: nothing crashes, the numbers just stop
meaning anything. So it is checked mechanically, in CI, on every commit.

Three checks, each closing a different route:

1. Imports. Walks the import graph with the AST rather than grepping, so an
   alias, a nested import inside a function, or a `from x import y as z`
   cannot slip past.
2. Path literals. An import rule is useless if a module simply opens
   `labels/whatever.json` by hand, so string literals are checked too.
3. Bundle contents. Everything the agent can actually read is scanned for
   the fields that hold the answer.

Run with no arguments to check the whole repository.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "leakage.yaml"
SOURCE_ROOT = REPO_ROOT / "src"


class LeakageConfigError(Exception):
    """The leakage rules could not be loaded."""


@dataclass(frozen=True)
class Violation:
    """One route by which ground truth could reach the agent."""

    rule: str
    location: str
    message: str
    blocker: bool = True


@dataclass(frozen=True)
class Rules:
    """The leakage rules from config/leakage.yaml."""

    ground_truth_modules: tuple[str, ...]
    allowed_importers: tuple[str, ...]
    agent_packages: tuple[str, ...]
    ground_truth_paths: tuple[str, ...]
    forbidden_fields: tuple[str, ...]


def load_rules(path: Path = CONFIG_PATH) -> Rules:
    """Load and validate the leakage rules."""
    if not path.exists():
        raise LeakageConfigError(f"missing leakage config: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise LeakageConfigError(f"leakage config must be a mapping: {path}")
    required = (
        "ground_truth_modules",
        "allowed_importers",
        "agent_packages",
        "ground_truth_paths",
        "forbidden_fields",
    )
    missing = [key for key in required if key not in raw]
    if missing:
        raise LeakageConfigError(f"leakage config missing keys: {', '.join(missing)}")
    return Rules(
        ground_truth_modules=tuple(str(v) for v in raw["ground_truth_modules"]),
        allowed_importers=tuple(str(v) for v in raw["allowed_importers"]),
        agent_packages=tuple(str(v) for v in raw["agent_packages"]),
        ground_truth_paths=tuple(str(v) for v in raw["ground_truth_paths"]),
        forbidden_fields=tuple(str(v) for v in raw["forbidden_fields"]),
    )


def module_name_for(path: Path, source_root: Path) -> str:
    """Turn a source file path into its dotted module name."""
    relative = path.relative_to(source_root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imported_modules(tree: ast.AST) -> set[str]:
    """Every module a file imports, including inside functions and classes.

    `from a.b import c` is recorded as both `a.b` and `a.b.c`, because the
    thing being imported may itself be the module that holds the answer.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Relative imports stay inside their own package, which is
                # already covered by that package's own rule.
                continue
            if node.module is None:
                continue
            found.add(node.module)
            for alias in node.names:
                found.add(f"{node.module}.{alias.name}")
    return found


def string_literals(tree: ast.AST) -> list[str]:
    """Every string constant in a file."""
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def matches_prefix(name: str, prefixes: tuple[str, ...]) -> bool:
    """True when a dotted name equals or sits under one of the prefixes."""
    return any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)


def check_imports(rules: Rules, source_root: Path = SOURCE_ROOT) -> list[Violation]:
    """Fail when a module that must not see ground truth imports it."""
    violations: list[Violation] = []
    for path in sorted(source_root.rglob("*.py")):
        module = module_name_for(path, source_root)
        if matches_prefix(module, rules.ground_truth_modules):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as error:
            violations.append(
                Violation("unparseable", str(path), f"cannot parse: {error}", blocker=False)
            )
            continue

        if matches_prefix(module, rules.allowed_importers):
            continue

        # `from a.b import c` yields both `a.b` and `a.b.c`, so violations are
        # collected against the ground truth module that matched rather than
        # against each imported name. One bad import is one violation.
        offending = {
            ground_truth
            for imported in imported_modules(tree)
            for ground_truth in rules.ground_truth_modules
            if matches_prefix(imported, (ground_truth,))
        }
        for ground_truth in sorted(offending):
            is_agent = matches_prefix(module, rules.agent_packages)
            where = "the agent under test" if is_agent else "a module with no reason to"
            violations.append(
                Violation(
                    "ground-truth-import",
                    module,
                    f"imports {ground_truth}, which is ground truth, from {where}",
                )
            )
    return violations


def check_path_literals(rules: Rules, source_root: Path = SOURCE_ROOT) -> list[Violation]:
    """Fail when a module names a ground truth directory in a string.

    An import rule does not stop somebody opening the path directly.
    """
    violations: list[Violation] = []
    for path in sorted(source_root.rglob("*.py")):
        module = module_name_for(path, source_root)
        if matches_prefix(module, rules.allowed_importers):
            continue
        if matches_prefix(module, rules.ground_truth_modules):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for literal in string_literals(tree):
            for directory in rules.ground_truth_paths:
                if literal == directory or literal.startswith(directory + "/"):
                    violations.append(
                        Violation(
                            "ground-truth-path",
                            module,
                            f"names the ground truth path {literal!r} in a string literal",
                        )
                    )
    return violations


def check_bundles(rules: Rules, bundles_root: Path) -> list[Violation]:
    """Fail when anything the agent can read carries the answer.

    Covers manifests and change records. A change log that shows the fault
    flag being flipped is the classic accidental leak: it looks like honest
    telemetry and it gives the game away.
    """
    violations: list[Violation] = []
    if not bundles_root.is_dir():
        return violations

    for manifest_path in sorted(bundles_root.glob("*/*/manifest.json")):
        bundle_dir = manifest_path.parent
        label = f"{bundle_dir.parent.name}/{bundle_dir.name}"
        for json_path in sorted(bundle_dir.glob("*.json")):
            try:
                text = json_path.read_text(encoding="utf-8")
            except OSError as error:
                violations.append(
                    Violation("unreadable-bundle", label, f"cannot read {json_path.name}: {error}")
                )
                continue
            for field in rules.forbidden_fields:
                if f'"{field}"' in text:
                    violations.append(
                        Violation(
                            "ground-truth-in-bundle",
                            f"{label}/{json_path.name}",
                            f"contains the field {field!r}, which is ground truth",
                        )
                    )
    return violations


def check_flag_names_in_changes(bundles_root: Path, flag_names: set[str]) -> list[Violation]:
    """Fail when a change record names a feature flag.

    Leakage control 2 in SPEC.md Section 8.4. The recorder strips these, so
    finding one means the sanitiser was bypassed or regressed.
    """
    violations: list[Violation] = []
    if not bundles_root.is_dir() or not flag_names:
        return violations
    for changes_path in sorted(bundles_root.glob("*/*/changes.json")):
        bundle_dir = changes_path.parent
        label = f"{bundle_dir.parent.name}/{bundle_dir.name}"
        text = changes_path.read_text(encoding="utf-8").lower()
        named = sorted(flag for flag in flag_names if flag.lower() in text)
        if named:
            violations.append(
                Violation(
                    "flag-in-change-log",
                    f"{label}/changes.json",
                    f"names feature flags: {', '.join(named)}",
                )
            )
    return violations


def known_flag_names(inventory_path: Path) -> set[str]:
    """Flag names from the generated inventory, if it exists yet."""
    if not inventory_path.is_file():
        return set()
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    flags = inventory.get("flags")
    return set(flags) if isinstance(flags, dict) else set()


def collect_violations(rules: Rules, root: Path = REPO_ROOT) -> list[Violation]:
    """Run every leakage check."""
    source_root = root / "src"
    bundles_root = root / "bundles"
    inventory = root / "reports" / "lab" / "flag_inventory.json"

    violations = check_imports(rules, source_root)
    violations += check_path_literals(rules, source_root)
    violations += check_bundles(rules, bundles_root)
    violations += check_flag_names_in_changes(bundles_root, known_flag_names(inventory))
    return violations


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 when no route to ground truth exists."""
    parser = argparse.ArgumentParser(description="Firebreak ground truth leakage checks")
    parser.add_argument("--root", default=None, help="repository root; default is this one")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else REPO_ROOT

    try:
        rules = load_rules()
    except LeakageConfigError as error:
        print(f"leakage config error: {error}", file=sys.stderr)
        return 2

    violations = collect_violations(rules, root)
    if not violations:
        print("leakage: no route from ground truth to the agent")
        return 0

    print(f"leakage: {len(violations)} violation(s)", file=sys.stderr)
    for violation in violations:
        severity = "BLOCKER" if violation.blocker else "warning"
        print(
            f"  [{severity}] {violation.rule} {violation.location}: {violation.message}",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
