"""Every link inside the published site must resolve to a file that exists.

**Why only internal links.** External ones need the network, and a deploy that fails because
somebody else's server was down is a deploy blocked for the wrong reason. Outbound links are
checked by hand and the phase report says so. Internal links are free to check and are the ones
this repository can actually break, because there are now two sites assembled from two tools and
they link across the boundary between them.

**The failure this catches is silent.** A dead internal link renders as ordinary text a reader
clicks and nothing happens. Nothing in a build fails, no page is missing, and the only way to
find it is to click every link on every page.

Deliberately not a crawler. It reads the HTML, so it sees every link on every page including
ones no navigation path reaches.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

# `href` and `src`, since a stylesheet or a bundled script that does not exist breaks a page
# more thoroughly than a dead link does.
ATTRIBUTE = re.compile(r'(?:href|src)\s*=\s*"([^"]+)"', re.IGNORECASE)

# Schemes and shapes that are somebody else's problem.
EXTERNAL = ("http://", "https://", "mailto:", "data:", "javascript:", "tel:", "//")


@dataclass(frozen=True)
class DeadLink:
    page: Path
    target: str
    resolved: Path


def links_in(html: str) -> list[str]:
    found = []
    for raw in ATTRIBUTE.findall(html):
        target = raw.strip()
        if not target or target.startswith("#"):
            continue
        if target.lower().startswith(EXTERNAL):
            continue
        found.append(target)
    return found


def resolve(page: Path, target: str, root: Path) -> Path:
    """Where a link points, as a path on disk.

    Fragments and query strings are stripped first: `report.html#claims` is a link to
    `report.html`, and treating the whole string as a filename would report every anchor as
    broken.
    """
    parts = urlsplit(target)
    path = unquote(parts.path)
    if not path:
        return page
    base = root if path.startswith("/") else page.parent
    resolved = (base / path.lstrip("/")).resolve()
    # A directory link means its index, which is how both sites link to each other's roots.
    if resolved.is_dir():
        return resolved / "index.html"
    return resolved


def check(root: Path, allow_parent: bool = False) -> tuple[list[DeadLink], int, int, int]:
    if not root.is_dir():
        raise SystemExit(f"{root} is not a directory; build the site first")
    pages = sorted(root.rglob("*.html"))
    if not pages:
        raise SystemExit(f"{root} holds no HTML; the build produced nothing")
    dead: list[DeadLink] = []
    checked = 0
    escaped = 0
    resolved_root = root.resolve()
    for page in pages:
        for target in links_in(page.read_text(encoding="utf-8", errors="replace")):
            resolved = resolve(page, target, root)
            # A link that points above the directory being checked. The reference site is
            # published under `/reference/` and links up to the explainer at the root, so when it
            # is built on its own that link legitimately points outside what was built. Counted
            # and reported rather than ignored, because "not checked" and "fine" are different
            # answers and only the full assembled site can give the second.
            if allow_parent and not resolved.is_relative_to(resolved_root):
                escaped += 1
                continue
            checked += 1
            if not resolved.exists():
                dead.append(DeadLink(page.relative_to(root), target, resolved))
    return dead, len(pages), checked, escaped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="the built site directory")
    parser.add_argument(
        "--allow-parent",
        action="store_true",
        help="skip links pointing above the directory given, for checking one half of the "
        "assembled site. How many were skipped is reported.",
    )
    arguments = parser.parse_args()
    dead, pages, checked, escaped = check(arguments.root, arguments.allow_parent)
    if dead:
        print(f"{len(dead)} dead internal link(s):", file=sys.stderr)
        for link in dead:
            print(f"  {link.page} -> {link.target}", file=sys.stderr)
        return 1
    message = f"links: {checked} internal link(s) across {pages} page(s), all resolve"
    if escaped:
        message += (
            f"; {escaped} link(s) point above this directory and were not checked. "
            "Only the assembled site can check those, which `make site` builds."
        )
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
