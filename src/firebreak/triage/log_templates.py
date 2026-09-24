"""A deterministic log template masker: the same line always yields the same template.

`mask_log_line` collapses the parts of a log line that vary between
otherwise identical occurrences, so `top_error_signatures`
(`firebreak.tools.logs`) can count failure modes instead of failure lines.
This is not Drain, and that is a decision rather than an oversight.

Drain, and its relatives, build an incrementally learned parse tree whose
clusters depend on the order lines arrive in and on parameters tuned per
corpus. Two runs over the same log file can produce different trees if the
lines are fed in a different order, which makes a result built from it
unreproducible. That breaks the one property evidence in this project
cannot do without: an evidence id is a hash of a query and its result
(`firebreak.tools.evidence`), so the same question asked twice must get the
same answer, and a learned clusterer that is order sensitive cannot
promise that.

A masker with a fixed, ordered list of substitutions carries no such state.
The same line produces the same template regardless of what came before it,
what order the bundle happened to store lines in, or which process asked
first. ADR-0006 is where the real tradeoff between the two gets decided,
against validation data collected in Phase 4, scoring this masker's cluster
purity and stability against Drain's on incidents with a known answer. This
module is the baseline that decision is judged against, not a standing
verdict that Drain is worse. If Drain wins on the validation split,
ADR-0006 says so and this module is replaced.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Every pattern below is a single character class under one quantifier, with
# no nested quantifiers and no alternation that overlaps itself. That is
# what keeps matching linear in the length of the input rather than
# exponential in the worst case: log bodies are attacker influenced text, so
# a pattern that can be made to backtrack badly is a denial of service
# waiting for the right line.
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_HEX_ID = re.compile(r"\b[0-9a-fA-F]{8,}\b")
_IPV4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
_ISO_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_DOUBLE_QUOTED = re.compile(r'"[^"]*"')
_SINGLE_QUOTED = re.compile(r"'[^']*'")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")

# Applied in this fixed order, because each step narrows what is left for
# the next one. A UUID is also 32 hex characters, so it must be masked
# before the hex step or the hex pattern would eat it and leave no dashes
# to tell a UUID from a container id. Quoted strings are masked before bare
# numbers so a quoted number, such as a stringified request id, becomes
# <str> rather than a series of <num> tokens with quote marks stranded
# between them. Whatever digits remain after every structured shape has
# been claimed are the plain numbers.
_STEPS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_UUID, "<uuid>"),
    (_HEX_ID, "<hex>"),
    (_IPV4, "<ip>"),
    (_ISO_TIMESTAMP, "<ts>"),
    (_DOUBLE_QUOTED, "<str>"),
    (_SINGLE_QUOTED, "<str>"),
    (_NUMBER, "<num>"),
)


def mask_log_line(body: str) -> str:
    """Replace the variable parts of one log line with stable placeholders.

    Pure: the output depends only on `body`. Nothing about call order, prior
    lines, or random state can change what one line masks to, which is the
    whole property this module exists to provide.
    """
    masked = body
    for pattern, placeholder in _STEPS:
        masked = pattern.sub(placeholder, masked)
    return masked


def cluster_templates(bodies: Iterable[str]) -> list[tuple[str, int]]:
    """Mask every body and count how many collapse to each template.

    Ranked by count descending, then by the template text itself, so two
    templates tied on count still come out in the same order every time
    regardless of which body happened to arrive first.
    """
    counts: dict[str, int] = {}
    for body in bodies:
        template = mask_log_line(body)
        counts[template] = counts.get(template, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))
