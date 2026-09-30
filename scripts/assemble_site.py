"""Assemble the published site from its two halves.

There are two sites, and that is deliberate. They have different readers:

- **The explainer** (`explainer/`, Quarto) is five pages for somebody deciding whether this
  project is worth their attention. It leads with the problem and the trade-offs.
- **The reference** (`site/`, Jinja2) is seven pages for somebody checking whether its claims
  hold. It is dense with caveats by design, which is right for a reviewer and wrong for a first
  read.

Collapsing them would mean picking one reader and failing the other. So the explainer is
published at the root and the reference at `/reference/`, and each links to the other.

**Both refuse to publish an unsourced number, by different mechanisms.** The reference site's
templates read only `reports/site_stats.json` through one macro. The explainer reads
`explainer/_variables.yml`, generated from the same reports, which is why its pages contain no
executable code: a Quarto page can run Python, and making the render depend on a kernel would
mean CI discovering it was absent.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPLAINER_DIR = REPO_ROOT / "explainer"
EXPLAINER_OUTPUT = EXPLAINER_DIR / "_site"
REFERENCE_BUILDER = REPO_ROOT / "site" / "build.py"
DIST = REPO_ROOT / "site" / "dist"
VARIABLES = EXPLAINER_DIR / "_variables.yml"

# Where the reference site lands inside the published tree. Its internal links are relative,
# so it works from a subdirectory unchanged.
REFERENCE_SUBPATH = "reference"


class AssembleError(Exception):
    """A reason the site cannot be assembled. Fatal: half a site published is worse than none,
    because the missing half looks like a decision."""


def _shorten(path: Path) -> str:
    """A path relative to the repository when it is inside one, and absolute otherwise.

    `Path.relative_to` raises on a path outside the root, so using it directly meant that
    building an error message could itself fail with a different error. A helper that reports a
    missing file must not be the thing that crashes.
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def render_explainer() -> None:
    """Render the Quarto project.

    Quarto is a separate binary rather than a Python dependency, so its absence is a normal
    state on a machine that has not installed it and the message says what to do.
    """
    if shutil.which("quarto") is None:
        raise AssembleError(
            "quarto is not installed, so the explainer cannot be rendered. "
            "See https://quarto.org/docs/get-started/, or run `make site-reference` for the "
            "reference site alone."
        )
    if not VARIABLES.is_file():
        raise AssembleError(
            f"{_shorten(VARIABLES)} is missing; run scripts/build_site_stats.py. "
            "Every number the explainer shows comes from it, and Quarto would render the "
            "variable placeholders as literal text rather than failing."
        )
    completed = subprocess.run(
        ("quarto", "render"), cwd=EXPLAINER_DIR, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise AssembleError(f"quarto render failed:\n{completed.stdout}\n{completed.stderr}")
    if not (EXPLAINER_OUTPUT / "index.html").is_file():
        raise AssembleError(f"quarto reported success and wrote no {EXPLAINER_OUTPUT}/index.html")


def render_reference(target: Path) -> None:
    """Build the Jinja2 reference site into a subdirectory of the published tree."""
    completed = subprocess.run(
        (sys.executable, str(REFERENCE_BUILDER), "--output", str(target)),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(REPO_ROOT / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    if completed.returncode != 0:
        raise AssembleError(
            f"the reference site failed to build:\n{completed.stdout}\n{completed.stderr}"
        )


def assemble(dist: Path) -> dict[str, int]:
    """Put the explainer at the root and the reference beneath it."""
    render_explainer()
    if dist.exists():
        shutil.rmtree(dist)
    shutil.copytree(EXPLAINER_OUTPUT, dist)
    render_reference(dist / REFERENCE_SUBPATH)

    # GitHub Pages ignores paths beginning with an underscore without this, and Quarto writes
    # `site_libs/`, so its absence would silently strip every stylesheet and the bundled
    # diagram renderer.
    (dist / ".nojekyll").write_text("", encoding="utf-8")

    return {
        "explainer_pages": len(list(dist.glob("*.html"))),
        "reference_pages": len(list((dist / REFERENCE_SUBPATH).glob("*.html"))),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DIST)
    arguments = parser.parse_args()
    try:
        counts = assemble(arguments.output)
    except AssembleError as error:
        print(f"site: {error}", file=sys.stderr)
        return 1
    where = _shorten(arguments.output)
    print(
        f"site: {counts['explainer_pages']} explainer page(s) at {where}/ and "
        f"{counts['reference_pages']} reference page(s) at {where}/{REFERENCE_SUBPATH}/"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
