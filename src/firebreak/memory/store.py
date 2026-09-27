"""Incident memory: what past incidents taught, and the rule that keeps it honest.

SPEC.md Section 10.3. Confirmed incidents are stored with a summary, symptoms, root
cause, the evidence types that mattered, and the remediation. `similar_incidents`
retrieves the closest few so an investigation can start from what happened last time.

**The train-only rule is the whole design, not a precaution.** Memory is written by
the system itself, so it is the one place where a held-out answer can leak into a run
without anybody editing a split file. If a `test_ood` incident's root cause sits in
memory, then every later run on that incident is reading the answer, and its score
measures recall rather than investigation. Worse, nothing downstream could notice: the
report would cite real evidence and reach the right conclusion for the wrong reason.

So the split is stored on every entry, `MemoryStore.admit` refuses anything that is
not from `train`, and a contract test asserts the invariant over the file on disk.
Three layers for one rule, because this rule failing is unrecoverable: a leaked
number cannot be un-quoted.

**Memory is advisory and says so in its own type.** An entry is a `Hypothesis` to
test, never a `Finding` and never evidence. SPEC.md Section 10.3 and Section 11's
ASI06 both require it, and the reason is that a wrong memory entry is worse than no
memory: it is a confident prior pointing at the wrong service, arriving before any
evidence has been gathered.

**Retrieval is lexical, not embedded, and that is a decision rather than a shortcut.**
SPEC.md Section 10.3 says embedding similarity plus graph overlap. An embedding model
is a dependency, a download, and a source of nondeterminism in a harness whose
measurements depend on reproducibility. The symptom summaries here are short, share a
controlled vocabulary of service names and fault classes, and number in the dozens, so
token overlap weighted by rarity does the same job and can be read by whoever doubts
a retrieval. ADR-0013 records this and what would change it.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

REPO_ROOT = Path(__file__).resolve().parents[3]
MEMORY_PATH = REPO_ROOT / "knowledge" / "incident_memory.jsonl"

# The only split memory may hold. SPEC.md Section 10.3.
ALLOWED_SPLIT = "train"

# How many entries `similar_incidents` returns. SPEC.md Section 10.3 says three. Few
# enough that a wrong one is visibly a minority rather than a consensus.
TOP_MATCHES = 3

# Below this, a match is not worth showing. A memory entry sharing only the word
# "service" with the current incident is noise presented as a prior, and a prior is
# the most expensive kind of noise here.
MINIMUM_SIMILARITY = 0.10

# Words carrying no signal in a symptom summary. Deliberately short: a stopword list
# long enough to include "error" or "latency" would remove the vocabulary the
# retrieval runs on.
STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "with",
    }
)

TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]*")


class IncidentMemoryError(Exception):
    """An entry could not be admitted, or the store could not be read."""


class LeakageError(IncidentMemoryError):
    """An entry from a held-out split was offered to memory.

    Its own type because the response differs from every other rejection: a
    malformed entry is a bug to fix, and this is a measurement that would have been
    silently invalidated.
    """


class MemoryEntry(BaseModel):
    """One confirmed incident, as memory keeps it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str
    scenario_id: str
    # Stored on the entry rather than looked up at read time, so the invariant can be
    # checked against the file itself without needing the label set that produced it.
    split: str
    # What an investigation would recognise: the symptoms, in the vocabulary a
    # specialist would use. Not the conclusion, which is what `root_cause_service`
    # is for, because retrieving on the conclusion would match incidents by their
    # answers.
    symptom_summary: str = Field(min_length=10, max_length=600)
    root_cause_service: str
    fault_class: str
    # Which signals turned out to matter. The most useful thing memory carries: it
    # tells a commander where to spend its first tool calls.
    evidence_types: tuple[str, ...] = ()
    remediation_id: str | None = None
    services_involved: tuple[str, ...] = ()
    confirmed_by: str = Field(min_length=1)
    confirmed_at: datetime

    @model_validator(mode="after")
    def _only_train(self) -> MemoryEntry:
        """The invariant, on the type.

        The outermost of the three layers. A caller that bypassed `admit` and wrote
        an entry directly still cannot construct one from a held-out split.
        """
        if self.split != ALLOWED_SPLIT:
            raise ValueError(
                f"incident memory holds {ALLOWED_SPLIT} only, and {self.incident_id} is "
                f"from {self.split!r}; a held-out incident in memory makes every later "
                "score on it a measure of recall rather than of investigation"
            )
        return self

    def as_hypothesis(self, index: int) -> dict[str, str]:
        """The entry as something to test, never as something to believe.

        Returns the shape the commander is shown. Advisory wording is in the
        statement itself rather than left to a prompt, because a prompt that says
        "these are advisory" and a statement that reads like a finding will lose that
        argument.
        """
        return {
            "id": f"m{index}",
            "service": self.root_cause_service,
            "statement": (
                f"A past incident with similar symptoms was caused by "
                f"{self.root_cause_service} ({self.fault_class}). This is a lead to "
                f"test, not evidence: confirm it against this incident's own data."
            ),
            "evidence_types_that_mattered": ", ".join(self.evidence_types),
        }


@dataclass(frozen=True)
class Match:
    """One retrieved entry and why it was retrieved.

    `similarity` is a ranking score, not a probability and not bounded by one: the
    graph-overlap bonus multiplies it, so an entry on the same services can exceed
    1.0. Named `similarity` because that is what it ranks by; read as "more is
    closer" and nothing further. `shared_services` is included so a reader can see
    which half of the score came from where.
    """

    entry: MemoryEntry
    similarity: float
    shared_services: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "incident_id": self.entry.incident_id,
            "root_cause_service": self.entry.root_cause_service,
            "fault_class": self.entry.fault_class,
            "similarity": round(self.similarity, 4),
            "shared_services": list(self.shared_services),
            "evidence_types": list(self.entry.evidence_types),
        }


def tokenise(text: str) -> list[str]:
    return [token for token in TOKEN.findall(text.lower()) if token not in STOPWORDS]


class MemoryStore:
    """The entries on disk, and the only way to add one.

    JSON Lines for the same reason the audit log is: appending cannot corrupt what is
    already there, and a human can read a single entry without a tool.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or MEMORY_PATH

    @property
    def path(self) -> Path:
        return self._path

    def entries(self) -> list[MemoryEntry]:
        if not self._path.is_file():
            return []
        found = []
        for number, line in enumerate(self._path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                found.append(MemoryEntry.model_validate_json(line))
            except ValueError as error:
                raise IncidentMemoryError(
                    f"{self._path.name} line {number} is not a memory entry: {error}"
                ) from error
        return found

    def admit(self, entry: MemoryEntry) -> MemoryEntry:
        """Add an entry, refusing a held-out one and refusing a duplicate.

        The middle of the three layers. `MemoryEntry` already refuses a non-train
        split, and this raises `LeakageError` specifically so a caller can tell the
        difference between "that entry was malformed" and "that entry would have
        invalidated a measurement".
        """
        if entry.split != ALLOWED_SPLIT:
            raise LeakageError(
                f"refusing {entry.incident_id} from split {entry.split!r}: memory holds "
                f"{ALLOWED_SPLIT} only"
            )
        existing = self.entries()
        if any(held.incident_id == entry.incident_id for held in existing):
            # Not an error. Re-confirming an incident is a normal thing to do, and the
            # first confirmation is the one that counts.
            return next(held for held in existing if held.incident_id == entry.incident_id)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json() + "\n")
        return entry

    def held_out_entries(self) -> list[MemoryEntry]:
        """Any entry not from train, for the contract test to fail on.

        Reads the file without validating, because the point is to catch an entry that
        got in some other way. `entries()` would raise on the first one and the test
        could not report how many there were or which.
        """
        if not self._path.is_file():
            return []
        offenders = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            if raw.get("split") != ALLOWED_SPLIT:
                offenders.append(MemoryEntry.model_construct(**raw))
        return offenders

    def similar(
        self,
        symptom_summary: str,
        services: tuple[str, ...] = (),
        limit: int = TOP_MATCHES,
        minimum: float = MINIMUM_SIMILARITY,
    ) -> list[Match]:
        """The closest entries, by symptom overlap and shared services.

        Ordered by similarity, ties broken by incident id, so two identical runs
        retrieve the same entries in the same order. A retrieval that reshuffled
        would make pass^3 measure the shuffling, which is the same reason the
        candidate ranking breaks ties by name.
        """
        entries = self.entries()
        if not entries:
            return []

        wanted = Counter(tokenise(symptom_summary))
        if not wanted:
            return []
        rarity = _inverse_document_frequency(entries)

        matches = []
        for entry in entries:
            shared_services = tuple(
                sorted(set(services) & set(entry.services_involved)) if services else ()
            )
            score = _similarity(wanted, Counter(tokenise(entry.symptom_summary)), rarity)
            # Graph overlap, per SPEC.md Section 10.3. A bonus rather than a separate
            # ranking: two incidents on the same services are more likely related, and
            # two incidents with the same symptoms are more likely related still.
            if shared_services:
                score *= 1.0 + 0.25 * len(shared_services)
            if score >= minimum:
                matches.append(
                    Match(entry=entry, similarity=score, shared_services=shared_services)
                )

        matches.sort(key=lambda match: (-match.similarity, match.entry.incident_id))
        return matches[:limit]


def _inverse_document_frequency(entries: list[MemoryEntry]) -> dict[str, float]:
    """How rare each token is across memory.

    Without this, "service" and "latency" would dominate every match, because they
    appear in nearly every summary. Weighting by rarity is what makes a shared
    mention of "kafka" worth more than a shared mention of "error".
    """
    total = len(entries)
    seen: Counter[str] = Counter()
    for entry in entries:
        seen.update(set(tokenise(entry.symptom_summary)))
    return {token: math.log(1.0 + total / count) for token, count in seen.items()}


def _similarity(wanted: Counter[str], candidate: Counter[str], rarity: dict[str, float]) -> float:
    """Rarity-weighted overlap, normalised by the query's own weight.

    Normalised by the query rather than by both sides, so a long memory entry does
    not score higher simply for containing more words. The result is the share of the
    query's meaningful weight that the entry accounts for.
    """
    shared = set(wanted) & set(candidate)
    if not shared:
        return 0.0
    # A token absent from memory has no measured rarity; treat it as maximally rare,
    # since a word appearing in the query and in exactly one entry is the strongest
    # signal available.
    default = max(rarity.values(), default=1.0)
    overlap = sum(rarity.get(token, default) for token in shared)
    total = sum(rarity.get(token, default) for token in wanted)
    return overlap / total if total else 0.0


def entry_from_label(
    incident_id: str,
    scenario_id: str,
    split: str,
    target_service: str,
    fault_class: str,
    symptom_summary: str,
    confirmed_by: str,
    evidence_types: tuple[str, ...] = (),
    services_involved: tuple[str, ...] = (),
    remediation_id: str | None = None,
    confirmed_at: datetime | None = None,
) -> MemoryEntry:
    """Build an entry from a confirmed label.

    A named function rather than callers constructing the model, so the one place that
    turns ground truth into memory is the one place to look when asking how something
    got in there.
    """
    return MemoryEntry(
        incident_id=incident_id,
        scenario_id=scenario_id,
        split=split,
        symptom_summary=symptom_summary,
        root_cause_service=target_service,
        fault_class=fault_class,
        evidence_types=evidence_types,
        remediation_id=remediation_id,
        services_involved=services_involved,
        confirmed_by=confirmed_by,
        confirmed_at=confirmed_at or datetime.now(UTC),
    )
