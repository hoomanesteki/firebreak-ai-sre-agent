"""Build the static site into `site/dist/`.

SPEC.md Section 15. Jinja2, no build step beyond this script, deployed by `site.yml`.

**Numbers come only from `reports/site_stats.json`, and a missing key fails the build.**
That is the spec's rule and it is enforced here by `number()`, which raises rather than
rendering a blank or a zero. A site that silently omitted a metric would look like a site
whose author chose not to show it, and a site that rendered `None` would look broken; both
are worse than a build that stops.

**Nothing on this site is a result yet, and every page says so in its own words rather than
by omission.** All five metrics are unmeasured: the library is recorded for one split only,
there are no model credentials, and both held-out test splits are empty. The temptation this
script exists to resist is filling the result cards with the figures that do exist, which are
a fixture run at 1.000 and a partly recorded tuning split at 0.143.

**No stock images, no emoji** (Section 15). Diagrams are inline SVG written by hand rather
than a library, so the pages need no network and no CDN.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

SITE_DIR = Path(__file__).resolve().parent
REPO_ROOT = SITE_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

TEMPLATES_DIR = SITE_DIR / "templates"
DIST_DIR = SITE_DIR / "dist"
STATS_PATH = REPO_ROOT / "reports" / "site_stats.json"
ADR_DIR = REPO_ROOT / "docs" / "adr"

PAGES = (
    ("index.html", "home.html", "Firebreak"),
    ("how-it-works.html", "how_it_works.html", "How it works"),
    ("evaluation.html", "evaluation.html", "Evaluation"),
    ("self-improvement.html", "self_improvement.html", "Self-improvement"),
    ("security.html", "security.html", "Security"),
    ("decisions.html", "decisions.html", "Decisions"),
    ("run-it.html", "run_it.html", "Run it"),
)


class SiteError(Exception):
    """A reason the site cannot be built. Fatal, because a site built around a gap
    publishes the gap as a design choice."""


def load_stats() -> dict[str, Any]:
    if not STATS_PATH.is_file():
        raise SiteError(f"{STATS_PATH} is missing; run scripts/build_site_stats.py")
    payload = json.loads(STATS_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SiteError(f"{STATS_PATH} is not a JSON object")
    for key in ("metrics", "library", "showcase", "readme_rows", "commit", "generated_at"):
        if key not in payload:
            raise SiteError(f"{STATS_PATH} has no {key!r}; the site cannot be built without it")
    return payload


def metric_getter(stats: dict[str, Any]) -> Callable[[str], dict[str, Any]]:
    """A template helper that fails loudly on a key the stats file does not have.

    SPEC.md Section 15: a missing key fails the build. A template that rendered nothing for
    an unknown metric would produce a page missing a number with no sign anything went
    wrong, which is the failure this rule exists to prevent.
    """
    metrics = stats["metrics"]

    def number(key: str) -> dict[str, Any]:
        if key not in metrics:
            raise SiteError(
                f"the site asks for metric {key!r} and site_stats.json has no such key; "
                "add it to scripts/build_site_stats.py or stop citing it"
            )
        return dict(metrics[key])

    return number


def adr_index() -> list[dict[str, str]]:
    """Every ADR, from the files themselves rather than a hand-kept list."""
    rows = []
    for path in sorted(ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md")):
        first = path.read_text(encoding="utf-8").splitlines()[0]
        title = first.lstrip("# ").strip()
        number, _, rest = title.partition(":")
        rows.append(
            {
                "number": number.replace("ADR-", "").strip(),
                "title": rest.strip() or title,
                "href": f"https://github.com/hoomanesteki/firebreak-ai-sre-agent/blob/main/docs/adr/{path.name}",
            }
        )
    if not rows:
        raise SiteError(f"no ADRs found in {ADR_DIR}")
    return rows


def build(output: Path) -> list[Path]:
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    stats = load_stats()
    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
        # StrictUndefined rather than the default, so a template naming a variable that does
        # not exist fails the build instead of rendering an empty string. Same rule as
        # `number()`, applied to everything else a page reads.
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    context = {
        "stats": stats,
        "number": metric_getter(stats),
        "library": stats["library"],
        "showcase": stats["showcase"],
        "readme_rows": stats["readme_rows"],
        "adrs": adr_index(),
        "commit": stats["commit"],
        "generated_at": stats["generated_at"],
        "unmeasured_count": len(stats.get("unmeasured") or []),
        "metric_count": len(stats["metrics"]),
        "pages": [(name, title) for name, _, title in PAGES],
        "repository": "https://github.com/hoomanesteki/firebreak-ai-sre-agent",
    }

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    written = []
    for name, template_name, title in PAGES:
        template = environment.get_template(template_name)
        rendered = template.render(page_title=title, page_name=name, **context)
        path = output / name
        path.write_text(rendered, encoding="utf-8")
        written.append(path)
    (output / ".nojekyll").write_text("", encoding="utf-8")
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DIST_DIR)
    arguments = parser.parse_args()
    try:
        written = build(arguments.output)
    except SiteError as error:
        print(f"site: {error}", file=sys.stderr)
        return 1
    print(f"site: wrote {len(written)} page(s) to {arguments.output.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
