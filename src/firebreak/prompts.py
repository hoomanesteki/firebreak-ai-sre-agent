"""Versioned prompt files, with the hash that ties a report to what produced it.

SPEC.md Section 10.4: prompts are versioned files with ids and hashes.

**The hash is the load-bearing part.** A prompt is an input to every measurement this
project produces. A report quoting top-1 accuracy is quoting it for one particular set
of prompts, and if those prompts were string literals scattered through the code then
no number in any report could be reproduced six months later. The hash is what lets a
report say which prompts it used.

**The hash covers the body, not the file.** Editing a note in the frontmatter must not
change it, because a hash that moved for a comment would make every past report look
stale after a typo fix. What it covers is exactly the text a model sees.

**A new version is a new file.** Editing a shipped prompt in place would silently
change what every past report meant, which is the same reason bundles are immutable
and the split assignment is recorded rather than derived.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "prompts"

# The nodes that have a prompt. SPEC.md Section 10.4 names these four. Declared here
# so a node added later without a prompt fails a test rather than silently running on
# whatever a call site happened to pass.
PROMPT_NODES = ("commander", "specialist", "critic", "reporter")

HASH_LENGTH = 12
FRONTMATTER = "---"


class PromptError(Exception):
    """A prompt file is missing, malformed, or inconsistent with its own frontmatter."""


@dataclass(frozen=True)
class Prompt:
    """One versioned prompt."""

    id: str
    node: str
    version: int
    body: str
    # Set when this came out of `firebreak optimize`, naming the prompt it was derived
    # from. A candidate with no lineage is a prompt somebody wrote, and the difference
    # matters when reading a report that shipped one.
    optimized_from: str | None
    notes: str
    path: Path

    @property
    def body_hash(self) -> str:
        """A short hash of exactly the text the model sees."""
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()[:HASH_LENGTH]

    @property
    def stamp(self) -> str:
        """What a report records: the id and the hash of what was actually sent."""
        return f"{self.id}@{self.body_hash}"

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "node": self.node,
            "version": self.version,
            "body_hash": self.body_hash,
            "optimized_from": self.optimized_from,
            "path": str(self.path.relative_to(REPO_ROOT)),
        }


def parse_prompt(text: str, path: Path) -> Prompt:
    """Split frontmatter from body and check the two agree.

    The id encodes the node and the version, so a file whose id says `critic/v1` while
    its frontmatter says the reporter is a file somebody copied and half edited. That
    is worth failing on: it would otherwise ship a critic prompt to the reporter, and
    the only symptom would be a worse report.
    """
    if not text.startswith(FRONTMATTER):
        raise PromptError(f"{path.name} has no frontmatter block")
    parts = text.split(FRONTMATTER, 2)
    if len(parts) < 3:
        raise PromptError(f"{path.name} has an unterminated frontmatter block")
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as error:
        raise PromptError(f"{path.name} frontmatter is not valid YAML: {error}") from error
    if not isinstance(meta, dict):
        raise PromptError(f"{path.name} frontmatter is not a mapping")

    body = parts[2].strip()
    if not body:
        raise PromptError(f"{path.name} has frontmatter and no prompt")

    for field in ("id", "node", "version"):
        if field not in meta:
            raise PromptError(f"{path.name} frontmatter is missing {field!r}")

    prompt_id = str(meta["id"])
    node = str(meta["node"])
    version = int(meta["version"])
    if prompt_id != f"{node}/v{version}":
        raise PromptError(
            f"{path.name} says id {prompt_id!r} and node {node!r} version {version}, "
            f"which would be {node}/v{version}; a half-edited copy of another node's "
            "prompt would otherwise ship silently"
        )
    if node not in PROMPT_NODES:
        raise PromptError(
            f"{path.name} is for node {node!r}, which is not one of {', '.join(PROMPT_NODES)}"
        )

    return Prompt(
        id=prompt_id,
        node=node,
        version=version,
        body=body,
        optimized_from=(str(meta["optimized_from"]) if meta.get("optimized_from") else None),
        notes=str(meta.get("notes") or "").strip(),
        path=path,
    )


@lru_cache(maxsize=1)
def load_prompts(directory: Path | None = None) -> dict[str, Prompt]:
    """Every prompt, keyed by id.

    Cached, because prompts are read on every model call and do not change during a run.
    """
    root = directory or PROMPTS_DIR
    if not root.is_dir():
        raise PromptError(f"{root} does not exist, so there are no prompts to load")
    found: dict[str, Prompt] = {}
    for path in sorted(root.glob("*.v*.md")):
        prompt = parse_prompt(path.read_text(encoding="utf-8"), path)
        if prompt.id in found:
            raise PromptError(f"two files claim prompt id {prompt.id}: {path.name}")
        found[prompt.id] = prompt
    return found


def latest_for(node: str, directory: Path | None = None) -> Prompt:
    """The highest version for one node.

    Highest rather than a pointer file, so shipping a new version is adding a file and
    nothing else. A pointer would be a second place to forget to update, which is the
    shape of the defect this project keeps finding.
    """
    candidates = [prompt for prompt in load_prompts(directory).values() if prompt.node == node]
    if not candidates:
        raise PromptError(
            f"no prompt for node {node!r}; expected a file like {node}.v1.md in "
            f"{(directory or PROMPTS_DIR).name}/"
        )
    return max(candidates, key=lambda prompt: prompt.version)


def stamps(directory: Path | None = None) -> dict[str, str]:
    """The id and hash of the prompt in use for each node.

    What a report records. Without it a number cannot be tied to the prompts that
    produced it, and every comparison across time becomes a comparison of unknowns.
    """
    return {node: latest_for(node, directory).stamp for node in PROMPT_NODES}
