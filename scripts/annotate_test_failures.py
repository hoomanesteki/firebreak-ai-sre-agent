"""Turn a JUnit report into GitHub annotations, so a red build says which test failed.

**The gap this closes.** Job logs need a token this environment does not have, so
`make ci-status` can name the failing *step* and never the failing *test*. That has cost two
diagnoses: a red Test step means reproducing the whole suite locally and hoping the machine
differs in the way that matters. It did not, twice, because the failing tests were ones that only
run where a service container exists.

Annotations are readable without a token. `::error::` lines written to stdout by a workflow step
become annotations on the run, so a failing test name survives into the API that anonymous
callers can read.

Run as an `if: failure()` step. It never fails the build itself: a reporting tool that can turn a
red build into a differently-red build for its own reasons is worse than no reporting.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from xml.etree import ElementTree


def escape(text: str) -> str:
    """GitHub's annotation format, which is line oriented and uses percent escapes."""
    return (
        text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A").replace("::", "%3A%3A")
    )


def failures(path: Path) -> list[tuple[str, str, str]]:
    """Each failing or erroring test as (file, name, message)."""
    try:
        root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError) as error:
        print(f"::warning::could not read {path}: {error}")
        return []
    found = []
    for case in root.iter("testcase"):
        for kind in ("failure", "error"):
            element = case.find(kind)
            if element is None:
                continue
            name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
            # The message attribute is the assertion's own summary; the body is the traceback.
            # The summary first, because an annotation is read at a glance.
            message = element.get("message") or ""
            body = (element.text or "").strip()
            detail = message if message else body
            # The last few traceback lines carry the assertion that actually failed.
            if body and body != message:
                tail = "\n".join(body.splitlines()[-12:])
                detail = f"{detail}\n{tail}" if detail else tail
            found.append((case.get("file") or "", name, detail))
            break
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit", type=Path)
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="annotations to emit. GitHub renders at most a few dozen per run, and a suite that "
        "failed fifty tests has one cause rather than fifty.",
    )
    arguments = parser.parse_args()

    if not arguments.junit.is_file():
        # Not an error. The step runs on failure, and a failure before pytest wrote a report is
        # exactly the case where there is nothing to annotate.
        print(f"::warning::no test report at {arguments.junit}, so nothing to annotate")
        return 0

    found = failures(arguments.junit)
    if not found:
        print("::warning::the test report lists no failures, so the step failed for another reason")
        print("  a coverage floor, a collection error, or a crash after the report was written")
        return 0

    print(f"{len(found)} failing test(s):")
    for file, name, detail in found[: arguments.limit]:
        location = f"file={file}," if file else ""
        print(f"::error {location}title={escape(name)}::{escape(detail[:2000])}")
        print(f"  {name}")
    if len(found) > arguments.limit:
        print(f"  and {len(found) - arguments.limit} more, not annotated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
