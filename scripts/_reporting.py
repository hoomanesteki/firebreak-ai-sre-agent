"""Shared helpers for the scripts that write measurement reports.

Small on purpose. The three measurement scripts are separate programs that
answer separate questions, and merging them would make each harder to read.
What they do share is how they talk about where a report went.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def describe_path(path: Path, root: Path = REPO_ROOT) -> str:
    """A repository relative path when possible, absolute otherwise.

    `Path.relative_to` raises rather than falling back, so printing a
    report's location used to crash whenever the report was written
    anywhere outside the repository. That is not a hypothetical: a test
    directing output at a temporary directory hits it, and so does anyone
    running this with an output path of their own. Crashing after the work
    is done and the file is written, purely while describing it, is a
    particularly annoying way to lose a long measurement run.
    """
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
