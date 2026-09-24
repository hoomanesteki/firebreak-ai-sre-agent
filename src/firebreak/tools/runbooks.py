"""`runbook_search`: find the written procedure that covers a symptom.

SPEC.md Section 6.3 asks for hybrid retrieval over `knowledge/runbooks/`,
returning relevant sections with citations.

**Retrieval is lexical and structured, not dense, and that is a decision.**
Hybrid retrieval normally means BM25 plus embeddings. Embeddings are not used
here for the same reason Drain was rejected in ADR-0006: an evidence id is a
hash over a query and its result, and the exit gate re-runs a citation and
requires it to match. An embedding model is a second thing that has to
produce identical vectors on every machine and after every version bump, and
when it does not, the failure is a citation that silently stops matching.

What replaces the dense half is structure the knowledge files already carry.
A runbook declares which services it covers and which symptoms it addresses,
and both are far stronger signals than sentence similarity: an operator
asking about payment errors wants the runbook that says it covers payment,
not the one whose prose happens to sit nearby in embedding space. Those
declared fields are what the scoring leans on, with term overlap deciding
between runbooks that both qualify.

If dense retrieval is added later it needs a pinned model, cached vectors
committed to the repository, and a test that the same query returns the same
ids on a different machine. That is a real piece of work, not a swap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from firebreak.graph.knowledge import Knowledge, RunbookSpec, load_knowledge
from firebreak.tools.base import ToolContext, ToolError, ToolResult, ToolSpec
from firebreak.tools.evidence import NO_WINDOW, EvidenceKind, Fact, build_record

MAX_SECTIONS = 5
MAX_QUERY_CHARS = 200

# A section's text is quoted back so a citation includes what was read. Capped
# because the point of the tool is to save context, and a tool that returns
# four full runbooks has spent the budget it was meant to protect.
MAX_SECTION_CHARS = 700

# A runbook that names the service in its `covers` list is about that
# service. Nothing in the prose competes with that, so the multiplier is
# large enough to dominate term overlap rather than merely nudge it.
COVERS_SERVICE_BOOST = 4.0

# A declared symptom matching the query is the next strongest signal: it is
# the runbook author saying "this is the situation I wrote this for".
SYMPTOM_MATCH_BOOST = 2.5

# BM25's term frequency saturation constant. The point of it is that the
# tenth occurrence of a word says almost nothing the second did not, so
# score approaches a ceiling of 1 per term instead of growing without bound.
#
# A logarithmic dampener was tried first and is not enough: at log1p, a
# section repeating one query word forty times scored 3.71 and a section
# covering all four query words once scored 2.77, so padding beat relevance.
# Saturation fixes that because no single term can ever contribute more than
# 1, which makes covering four terms strictly better than hammering one.
BM25_K1 = 1.2

# Words too common to carry meaning in an operational question. Deliberately
# short: a longer stop list starts removing words like "down" and "failed"
# that are the entire question.
STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "and",
        "or",
        "with",
        "what",
        "why",
        "how",
        "do",
        "does",
        "did",
        "it",
        "this",
        "that",
        "i",
        "we",
    }
)

_WORD = re.compile(r"[a-z0-9]+")
_HEADING = re.compile(r"^##\s+(?P<heading>.+)$", re.MULTILINE)


class RunbookSearchInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    service: str | None = None
    limit: int = Field(default=MAX_SECTIONS, ge=1, le=MAX_SECTIONS)


@dataclass(frozen=True)
class Section:
    """One `##` section of one runbook."""

    runbook_id: str
    runbook_title: str
    path: str
    heading: str
    text: str


def tokenise(text: str) -> list[str]:
    """Lowercase word tokens, minus stopwords.

    Hyphens split, so `product-catalog` matches a query saying "product
    catalog". Service names are hyphenated and operators are not consistent
    about typing them that way.
    """
    return [word for word in _WORD.findall(text.lower()) if word not in STOPWORDS]


def split_sections(runbook: RunbookSpec) -> list[Section]:
    """Break a runbook body into its `##` sections.

    Sections rather than whole runbooks, because SPEC.md Section 6.3 asks
    for sections and because the useful answer to "what do I check first" is
    the First checks heading, not eight hundred words containing it.

    Text before the first heading is kept as a preamble, so a runbook
    written without headings still returns something rather than nothing.
    """
    body = runbook.body.strip()
    if not body:
        return []

    matches = list(_HEADING.finditer(body))
    sections: list[Section] = []

    if not matches or matches[0].start() > 0:
        preamble = body[: matches[0].start()] if matches else body
        if preamble.strip():
            sections.append(
                Section(
                    runbook_id=runbook.id,
                    runbook_title=runbook.title,
                    path=runbook.path,
                    heading=runbook.title,
                    text=preamble.strip(),
                )
            )

    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        text = body[start:end].strip()
        if text:
            sections.append(
                Section(
                    runbook_id=runbook.id,
                    runbook_title=runbook.title,
                    path=runbook.path,
                    heading=match.group("heading").strip(),
                    text=text,
                )
            )
    return sections


def score_section(
    section: Section,
    runbook: RunbookSpec,
    query_terms: list[str],
    service: str | None,
) -> float:
    """How well one section answers the query.

    Term frequency with BM25 saturation rather than a raw count, so a
    section repeating one word forty times cannot outrank one that covers
    every term in the question once. Each term contributes at most 1 no
    matter how often it appears, which makes breadth of coverage beat
    repetition by construction rather than by hoping the dampener is steep
    enough.

    BM25's document length normalisation is left out. Runbook sections here
    are all within a factor of a few of each other, and a normalisation
    tuned against nothing would be false precision.
    """
    haystack = tokenise(f"{section.heading} {section.text}")
    if not haystack:
        return 0.0
    counts: dict[str, int] = {}
    for word in haystack:
        counts[word] = counts.get(word, 0) + 1

    score = sum(counts[term] / (counts[term] + BM25_K1) for term in query_terms if term in counts)

    # The declared fields, which are the author's own statement of what this
    # covers, and worth more than anything the prose happens to contain.
    if service and service in runbook.covers:
        score += COVERS_SERVICE_BOOST
    symptom_terms = tokenise(" ".join(runbook.symptoms))
    matched_symptoms = sum(1 for term in query_terms if term in symptom_terms)
    if matched_symptoms:
        score += SYMPTOM_MATCH_BOOST * (matched_symptoms / (matched_symptoms + BM25_K1))

    # The title names the subject, so a hit there is worth more than one
    # buried in a paragraph.
    title_terms = tokenise(f"{runbook.title} {section.heading}")
    score += sum(0.75 for term in query_terms if term in title_terms)
    return score


def search_runbooks(
    knowledge: Knowledge,
    query: str,
    service: str | None = None,
    limit: int = MAX_SECTIONS,
) -> list[tuple[Section, float]]:
    """Rank runbook sections against a query, best first.

    Ties break on runbook id then heading, so the same question always
    returns the same sections in the same order. Without that a citation
    would not be re-runnable, which is the whole point.
    """
    query_terms = tokenise(query)
    if not query_terms:
        return []

    scored: list[tuple[Section, float]] = []
    for runbook in knowledge.runbooks:
        for section in split_sections(runbook):
            score = score_section(section, runbook, query_terms, service)
            if score > 0.0:
                scored.append((section, round(score, 6)))

    scored.sort(key=lambda pair: (-pair[1], pair[0].runbook_id, pair[0].heading))
    return scored[:limit]


def runbook_search(context: ToolContext, arguments: RunbookSearchInput) -> ToolResult:
    """Find written procedures covering a symptom, with citations."""
    try:
        knowledge = load_knowledge()
    except Exception as error:
        # A knowledge directory that does not load is a deployment problem,
        # not something an investigation can work around, and it must not
        # look like "no runbook covers this".
        raise ToolError(f"the knowledge files could not be read: {error}") from error

    if arguments.service and arguments.service not in knowledge.service_names:
        raise ToolError(
            f"{arguments.service!r} is not a service in the knowledge graph; "
            f"known services are {', '.join(sorted(knowledge.service_names))}"
        )

    matches = search_runbooks(knowledge, arguments.query, arguments.service, arguments.limit)

    rows = [
        {
            "runbook_id": section.runbook_id,
            "title": section.runbook_title,
            "path": section.path,
            "heading": section.heading,
            "score": score,
            "text": section.text[:MAX_SECTION_CHARS],
        }
        for section, score in matches
    ]
    facts = [Fact(field="sections_found", value=float(len(rows)), unit="count")]
    if rows:
        facts.append(Fact(field="top_score", value=float(matches[0][1]), unit="score"))

    record = context.record(
        build_record(
            kind=EvidenceKind.RUNBOOK,
            query="runbook_search",
            parameters={"query": arguments.query, "service": arguments.service},
            # Runbooks are not time series. A written procedure has no
            # window, and inventing one would make the evidence id depend on
            # when the question was asked rather than on what was asked.
            window=NO_WINDOW,
            fingerprint=context.backend.fingerprint(),
            rows=rows,
            facts=facts,
        )
    )

    if rows:
        named = ", ".join(f"{row['runbook_id']}#{row['heading']}" for row in rows[:3])
        summary = f"{len(rows)} runbook sections matched: {named}"
    else:
        summary = (
            f"no runbook section matches {arguments.query!r}"
            + (f" for {arguments.service}" if arguments.service else "")
            + "; there may be no written procedure for this, which is itself worth reporting"
        )

    return ToolResult(
        tool="runbook_search",
        summary=summary[:600],
        evidence_id=record.id,
        data={"sections": rows},
    )


RUNBOOK_SEARCH = ToolSpec(
    name="runbook_search",
    description=(
        "Search the written runbooks for procedures covering a symptom, returning "
        "the matching sections with their runbook id and path so a report can cite "
        "them. Pass a service to prefer runbooks that declare they cover it. Use "
        "this once a candidate service is named, to find what an operator is "
        "supposed to check or do about this class of problem, and to name the "
        "owning team's own documented procedure rather than inventing advice. It "
        "searches hand written documentation only: it knows nothing about the "
        "current state of the system, so it can neither confirm nor rule out a "
        "hypothesis, and a runbook describing a failure mode is not evidence that "
        "this failure mode is occurring. An empty result usually means no procedure "
        "has been written for this, which is worth reporting rather than working "
        "around."
    ),
    input_model=RunbookSearchInput,
    handler=runbook_search,
)

RUNBOOK_TOOLS: tuple[ToolSpec, ...] = (RUNBOOK_SEARCH,)
