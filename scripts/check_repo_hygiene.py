"""Repository hygiene checks for Firebreak.

Enforces SPEC.md Section 19.4: no em or en dashes in tracked text files, no
filler words in prose, no AI attribution in commit messages, only allowed
commit authors, and a README stats block that matches the generated report.

Run over the whole repository with no arguments, or over a list of files
(which is how pre-commit calls it).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "repo_hygiene.yaml"
STATS_PATH = REPO_ROOT / "reports" / "site_stats.json"
README_PATH = REPO_ROOT / "README.md"
STATS_START = "<!-- stats:start -->"
STATS_END = "<!-- stats:end -->"


class HygieneError(Exception):
    """Raised when the hygiene configuration cannot be loaded."""


@dataclass
class Finding:
    """One rule violation, with enough detail to fix it."""

    rule: str
    location: str
    message: str


@dataclass
class Config:
    """Hygiene rules loaded from config/repo_hygiene.yaml."""

    allowed_author_emails: list[str]
    banned_characters: list[str]
    excluded_paths: list[str]
    text_extensions: list[str]
    flagged_words: list[str]
    prose_globs: list[str]
    ai_attribution_patterns: list[str]
    commit_types: list[str]
    commit_scopes: list[str]
    commit_subject_max_length: int = 72
    _raw: dict[str, object] = field(default_factory=dict, repr=False)


def load_config(path: Path = CONFIG_PATH) -> Config:
    """Load and validate the hygiene configuration."""
    if not path.exists():
        raise HygieneError(f"missing hygiene config: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise HygieneError(f"hygiene config must be a mapping: {path}")
    required = (
        "allowed_author_emails",
        "banned_characters",
        "excluded_paths",
        "text_extensions",
        "flagged_words",
        "prose_globs",
        "ai_attribution_patterns",
        "commit_types",
        "commit_scopes",
    )
    missing = [key for key in required if key not in raw]
    if missing:
        raise HygieneError(f"hygiene config missing keys: {', '.join(missing)}")
    return Config(
        allowed_author_emails=list(raw["allowed_author_emails"]),
        banned_characters=list(raw["banned_characters"]),
        excluded_paths=list(raw["excluded_paths"]),
        text_extensions=list(raw["text_extensions"]),
        flagged_words=list(raw["flagged_words"]),
        prose_globs=list(raw["prose_globs"]),
        ai_attribution_patterns=list(raw["ai_attribution_patterns"]),
        commit_types=list(raw["commit_types"]),
        commit_scopes=list(raw["commit_scopes"]),
        commit_subject_max_length=int(raw.get("commit_subject_max_length", 72)),
        _raw=raw,
    )


def run_git(args: list[str], cwd: Path = REPO_ROOT) -> str:
    """Run a git command and return stdout, or an empty string on failure."""
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return ""
    return result.stdout


def list_tracked_files(cwd: Path = REPO_ROOT) -> list[str]:
    """Return every file git tracks, as repository-relative paths."""
    output = run_git(["ls-files"], cwd=cwd)
    return [line for line in output.splitlines() if line]


def is_excluded(rel_path: str, excluded: list[str]) -> bool:
    """True when a path sits under an excluded prefix."""
    return any(rel_path == prefix or rel_path.startswith(prefix) for prefix in excluded)


def is_text_file(rel_path: str, extensions: list[str]) -> bool:
    """True when the file extension is one we scan as prose or code."""
    return Path(rel_path).suffix in extensions


def check_banned_characters(
    files: list[str], config: Config, root: Path = REPO_ROOT
) -> list[Finding]:
    """Fail on em dashes and en dashes in tracked text files."""
    findings: list[Finding] = []
    for rel_path in files:
        if is_excluded(rel_path, config.excluded_paths):
            continue
        if not is_text_file(rel_path, config.text_extensions):
            continue
        path = root / rel_path
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for char in config.banned_characters:
                if char in line:
                    findings.append(
                        Finding(
                            rule="banned-character",
                            location=f"{rel_path}:{line_number}",
                            message=(
                                f"found {char!r} (U+{ord(char):04X}); "
                                "use a colon, comma, parentheses, or two sentences"
                            ),
                        )
                    )
    return findings


def matches_any_glob(rel_path: str, globs: list[str]) -> bool:
    """True when the path matches one of the prose globs.

    fnmatch treats `*` as matching separators too, so `site/templates/**`
    already covers nested files and no extra prefix rule is needed.
    """
    return any(fnmatch.fnmatch(rel_path, pattern) for pattern in globs)


def check_flagged_words(files: list[str], config: Config, root: Path = REPO_ROOT) -> list[Finding]:
    """Fail on filler and marketing words in prose files."""
    findings: list[Finding] = []
    patterns = {
        word: re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE) for word in config.flagged_words
    }
    for rel_path in files:
        if is_excluded(rel_path, config.excluded_paths):
            continue
        if not matches_any_glob(rel_path, config.prose_globs):
            continue
        path = root / rel_path
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for word, pattern in patterns.items():
                if pattern.search(line):
                    findings.append(
                        Finding(
                            rule="flagged-word",
                            location=f"{rel_path}:{line_number}",
                            message=f"flagged word {word!r}; write it plainly",
                        )
                    )
    return findings


def check_commit_messages(config: Config, base_ref: str) -> list[Finding]:
    """Fail on AI attribution in commit messages on this branch."""
    log = run_git(["log", f"{base_ref}..HEAD", "--format=%H%x00%B%x00"])
    if not log:
        return []
    findings: list[Finding] = []
    patterns = [re.compile(pattern, re.IGNORECASE) for pattern in config.ai_attribution_patterns]
    entries = [entry for entry in log.split("\x00\n") if entry.strip()]
    for entry in entries:
        sha, _, body = entry.partition("\x00")
        short_sha = sha.strip()[:12]
        for pattern in patterns:
            if pattern.search(body):
                findings.append(
                    Finding(
                        rule="ai-attribution",
                        location=f"commit {short_sha}",
                        message=f"commit message matches {pattern.pattern!r}",
                    )
                )
    return findings


def check_commit_authors(config: Config, base_ref: str) -> list[Finding]:
    """Fail when a commit on this branch has an unexpected author."""
    log = run_git(["log", f"{base_ref}..HEAD", "--format=%H %ae %ce"])
    if not log:
        return []
    findings: list[Finding] = []
    allowed = {email.lower() for email in config.allowed_author_emails}
    for line in log.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        sha, author_email, committer_email = parts
        for role, email in (("author", author_email), ("committer", committer_email)):
            if email.lower() not in allowed:
                findings.append(
                    Finding(
                        rule="commit-author",
                        location=f"commit {sha[:12]}",
                        message=f"{role} {email} is not in allowed_author_emails",
                    )
                )
    return findings


def render_stats_block(stats: dict[str, object]) -> str:
    """Render the README results table from reports/site_stats.json."""
    rows = stats.get("readme_rows")
    if not isinstance(rows, list):
        raise HygieneError("site_stats.json has no 'readme_rows' list")
    lines = ["| Metric | Value | Source |", "|---|---|---|"]
    for row in rows:
        if not isinstance(row, dict):
            raise HygieneError("each readme_rows entry must be a mapping")
        missing = [key for key in ("metric", "value", "source") if key not in row]
        if missing:
            raise HygieneError(f"readme_rows entry missing {', '.join(missing)}")
        lines.append(f"| {row['metric']} | {row['value']} | {row['source']} |")
    return "\n".join(lines)


def check_readme_stats(
    readme_path: Path = README_PATH, stats_path: Path = STATS_PATH
) -> list[Finding]:
    """Fail when the README stats block does not match the generated report."""
    if not stats_path.exists() or not readme_path.exists():
        return []
    readme = readme_path.read_text(encoding="utf-8")
    if STATS_START not in readme or STATS_END not in readme:
        return [
            Finding(
                rule="readme-stats",
                location="README.md",
                message=f"missing {STATS_START} and {STATS_END} markers",
            )
        ]
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    expected = render_stats_block(stats).strip()
    start = readme.index(STATS_START) + len(STATS_START)
    end = readme.index(STATS_END)
    actual = readme[start:end].strip()
    if actual != expected:
        return [
            Finding(
                rule="readme-stats",
                location="README.md",
                message="stats block is stale; run scripts/build_site_stats.py",
            )
        ]
    return []


def collect_findings(
    files: list[str],
    config: Config,
    base_ref: str,
    skip_git: bool,
    root: Path | None = None,
) -> list[Finding]:
    """Run every hygiene rule and gather the findings.

    The root is a parameter rather than a module constant read at import
    time, so the checks can be pointed at a scratch directory. A gatekeeper
    that is awkward to test is a gatekeeper that stops being tested.
    """
    base = root if root is not None else REPO_ROOT
    findings = check_banned_characters(files, config, root=base)
    findings += check_flagged_words(files, config, root=base)
    findings += check_readme_stats(base / "README.md", base / "reports" / "site_stats.json")
    if not skip_git:
        findings += check_commit_messages(config, base_ref)
        findings += check_commit_authors(config, base_ref)
    return findings


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 when the repository is clean."""
    parser = argparse.ArgumentParser(description="Firebreak repository hygiene checks")
    parser.add_argument("files", nargs="*", help="files to check; default is all tracked files")
    parser.add_argument("--base-ref", default="main", help="branch to compare commits against")
    parser.add_argument(
        "--root", default=None, help="repository root to check; default is this one"
    )
    parser.add_argument(
        "--skip-git",
        action="store_true",
        help="skip commit message and author checks",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except HygieneError as error:
        print(f"hygiene config error: {error}", file=sys.stderr)
        return 2

    root = Path(args.root) if args.root else REPO_ROOT
    files = args.files or list_tracked_files(cwd=root)
    findings = collect_findings(files, config, args.base_ref, args.skip_git, root=root)

    if not findings:
        print(f"repo hygiene: clean ({len(files)} files checked)")
        return 0

    print(f"repo hygiene: {len(findings)} finding(s)", file=sys.stderr)
    for finding in findings:
        print(f"  [{finding.rule}] {finding.location}: {finding.message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
