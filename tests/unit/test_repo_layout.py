"""Tests for the repository layout itself.

Two classes of mistake are invisible until much later and expensive then.

A gitignore rule can swallow a directory the spec requires. The standard
Python template ignores `lib/` unanchored and `/site` for mkdocs, and both
collide with SPEC.md Section 7.1: the vendored Chart.js and Cytoscape.js in
Section 6.13 live under a `lib` directory, and `site/` is the committed
source of the project website. `git add` reports nothing wrong when a path
is ignored, so the offline demo would simply be missing its assets for
anyone who cloned the repository.

The pinned demo version is declared in three places that can drift apart:
the submodule pin, the constant the reports are labelled with, and the image
tag the Makefile composes with. Two of them disagreeing means a recording
labelled 3.1.0 that was produced by something else.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from firebreak.lab.endpoints import DEMO_TAG

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories SPEC.md Section 7.1 lists as part of the repository.
REQUIRED_PATHS = [
    "site/build.py",
    "site/templates/index.html",
    "site/static/lib/cytoscape.js",
    "src/firebreak/web/static/vendor/chart.js",
    "src/firebreak/web/templates/report.html",
    "knowledge/runbooks/payment.md",
    "knowledge/services.yaml",
    "prompts/critic.md",
    "scenarios/specs/payment-failure.yaml",
    "scenarios/injectors/log_injector.py",
    "bundles/scenario/run/manifest.json",
    "labels/scenario/run.json",
    "recordings/cassette.json",
    "reports/eval/fb/test-id/result.json",
    "ops/grafana/dashboard.json",
    "config/models.yaml",
]

# Things that must stay out of the repository.
IGNORED_PATHS = [
    "build/artifact",
    "dist/wheel.whl",
    ".venv/bin/python",
    ".env",
    "htmlcov/index.html",
    "reports/eval/fb/test-id/transcripts/trial1.json",
]


def is_ignored(rel_path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", rel_path],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


@pytest.mark.parametrize("rel_path", REQUIRED_PATHS)
def test_required_path_is_not_ignored(rel_path: str):
    assert not is_ignored(rel_path), (
        f"{rel_path} is gitignored; committing it would silently do nothing"
    )


@pytest.mark.parametrize("rel_path", IGNORED_PATHS)
def test_generated_path_stays_ignored(rel_path: str):
    assert is_ignored(rel_path), f"{rel_path} should not be committed"


def submodule_tag() -> str:
    result = subprocess.run(
        ["git", "describe", "--tags", "--exact-match"],
        cwd=REPO_ROOT / "vendor" / "otel-demo",
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def makefile_demo_tag() -> str:
    text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    match = re.search(r"^DEMO_TAG\s*:=\s*(\S+)\s*$", text, re.MULTILINE)
    assert match is not None, "Makefile has no DEMO_TAG"
    return match.group(1)


def test_demo_tag_constant_matches_the_submodule_pin():
    assert submodule_tag() == DEMO_TAG


def test_makefile_image_tag_matches_the_submodule_pin():
    """The images and the code must come from the same release.

    The demo's own .env pins images to latest, so the Makefile overrides
    DEMO_VERSION. If that override drifts from the submodule, the stack runs
    one release while the lab reads flag names from another.
    """
    assert makefile_demo_tag() == submodule_tag()


def test_makefile_composes_the_full_profile():
    """compose.full.yaml carries Kafka, without which the collector errors."""
    text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "compose.full.yaml" in text


def test_makefile_points_the_collector_at_firebreaks_extras_file():
    """Left unset, the collector loads the empty vendored file instead."""
    text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "OTEL_COLLECTOR_CONFIG_EXTRAS=../../ops/otelcol-config-extras.yml" in text


def test_vendor_submodule_is_not_modified():
    """Editing vendored files is a blocker, so it is checked rather than assumed."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT / "vendor" / "otel-demo",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.strip() == "", f"vendor/otel-demo is dirty:\n{result.stdout}"
