"""Commit message check for Firebreak.

Enforces SPEC.md Section 18.3 and 19.4: Conventional Commits subjects, a
72 character subject limit, no em or en dashes, and no AI attribution.

Git calls this with the path to the commit message file.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "repo_hygiene.yaml"
SUBJECT_PATTERN = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[a-z0-9-]+)\))?(?P<bang>!)?: (?P<subject>.+)$"
)


@dataclass(frozen=True)
class CommitRules:
    """The subset of the hygiene configuration this hook needs."""

    banned_characters: tuple[str, ...]
    ai_attribution_patterns: tuple[str, ...]
    commit_types: tuple[str, ...]
    commit_scopes: tuple[str, ...]
    subject_max_length: int


def _string_tuple(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    """Read a list of strings from the configuration, defaulting to empty."""
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list in {CONFIG_PATH}")
    return tuple(str(item) for item in value)


def load_rules(path: Path = CONFIG_PATH) -> CommitRules:
    """Load the commit rules from the hygiene configuration."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"hygiene config must be a mapping: {path}")
    return CommitRules(
        banned_characters=_string_tuple(raw, "banned_characters"),
        ai_attribution_patterns=_string_tuple(raw, "ai_attribution_patterns"),
        commit_types=_string_tuple(raw, "commit_types"),
        commit_scopes=_string_tuple(raw, "commit_scopes"),
        subject_max_length=int(raw.get("commit_subject_max_length", 72)),
    )


def strip_comments(message: str) -> str:
    """Drop git comment lines and the verbose diff section."""
    lines: list[str] = []
    for line in message.splitlines():
        if line.startswith("# ------------------------ >8"):
            break
        if line.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines).strip("\n")


def check_message(message: str, rules: CommitRules) -> list[str]:
    """Return a list of problems with the commit message. Empty means valid."""
    problems: list[str] = []
    body = strip_comments(message)
    if not body.strip():
        return ["commit message is empty"]

    lines = body.splitlines()
    subject_line = lines[0]

    if subject_line.startswith(("Merge ", "Revert ", "fixup! ", "squash! ")):
        return []

    for char in rules.banned_characters:
        if char in body:
            problems.append(f"contains {char!r} (U+{ord(char):04X}); use plain punctuation")

    for pattern in rules.ai_attribution_patterns:
        if re.search(pattern, body, re.IGNORECASE):
            problems.append(f"AI attribution matched {pattern!r}; commits are the owner's only")

    match = SUBJECT_PATTERN.match(subject_line)
    if match is None:
        problems.append(
            "subject must read 'type(scope): summary', for example "
            "'feat(gates): re-run cited queries before publishing a report'"
        )
        return problems

    commit_type = match.group("type")
    if commit_type not in rules.commit_types:
        problems.append(f"type {commit_type!r} is not one of {', '.join(rules.commit_types)}")

    scope = match.group("scope")
    if scope is not None and rules.commit_scopes and scope not in rules.commit_scopes:
        problems.append(f"scope {scope!r} is not one of {', '.join(rules.commit_scopes)}")

    if len(subject_line) > rules.subject_max_length:
        problems.append(
            f"subject is {len(subject_line)} characters; limit is {rules.subject_max_length}"
        )

    subject = match.group("subject")
    if subject.endswith("."):
        problems.append("subject must not end with a period")
    if subject[:1].isupper():
        problems.append("subject must start lower case and use the imperative mood")

    if len(lines) > 1 and lines[1].strip():
        problems.append("leave a blank line between the subject and the body")

    return problems


def main(argv: list[str] | None = None) -> int:
    """Entry point. Git passes the commit message file path."""
    parser = argparse.ArgumentParser(description="Firebreak commit message check")
    parser.add_argument("message_file", help="path to the commit message file")
    args = parser.parse_args(argv)

    message = Path(args.message_file).read_text(encoding="utf-8")
    problems = check_message(message, load_rules())
    if not problems:
        return 0

    print("commit message rejected (SPEC.md Section 18.3):", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
