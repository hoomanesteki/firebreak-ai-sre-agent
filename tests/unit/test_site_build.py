"""Tests for the public site.

**The failure this file exists to catch is not a crash.** A page that renders 1.000 root-cause
accuracy would build cleanly, deploy cleanly, and be wrong in the one place it matters most.
Both figures that could do that are committed in this repository: a synthetic-fixture run
reporting 1.000 and a partly recorded tuning split reporting 0.143. So the assertions here are
mostly about what must not appear in the rendered HTML.

SPEC.md Section 15 gives two rules this checks directly: numbers come only from
`reports/site_stats.json`, and a missing key fails the build. The second is why
`StrictUndefined` and `number()` exist, and both are tested rather than assumed, because a
template engine that silently renders an empty string for an unknown variable is the default.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD = REPO_ROOT / "site" / "build.py"
STATS_PATH = REPO_ROOT / "reports" / "site_stats.json"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("site_build_under_test", BUILD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


site = _load()


@pytest.fixture(scope="module")
def pages(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Every page, rendered once. Built into a temporary directory rather than over
    `site/dist`, so running the tests never changes what a developer has on disk."""
    output = tmp_path_factory.mktemp("site")
    written = site.build(output)
    return {path.name: path.read_text(encoding="utf-8") for path in written}


@pytest.fixture(scope="module")
def stats() -> dict[str, Any]:
    return json.loads(STATS_PATH.read_text(encoding="utf-8"))


class TestEveryPageBuilds:
    def test_all_seven_pages_are_written(self, pages: dict[str, str]) -> None:
        assert set(pages) == {name for name, _, _ in site.PAGES}

    def test_every_page_has_a_title_and_a_lead(self, pages: dict[str, str]) -> None:
        """The reviewer focus for this phase is first impression. A page opening with a table
        and no sentence fails that before anything else."""
        for name, html in pages.items():
            assert "<title>" in html, name
            assert 'class="lead"' in html, f"{name} opens with no sentence saying what it is"

    def test_every_page_links_to_every_other(self, pages: dict[str, str]) -> None:
        for name, html in pages.items():
            for other, _, _ in site.PAGES:
                assert f'href="{other}"' in html, f"{name} does not link to {other}"

    def test_nojekyll_is_written(self, pages: dict[str, str], tmp_path: Path) -> None:
        """Without it GitHub Pages ignores files beginning with an underscore. Nothing here
        starts with one today, which is exactly why its absence would be found late."""
        output = tmp_path / "again"
        site.build(output)
        assert (output / ".nojekyll").is_file()


class TestNoUnquotableNumberReachesAPage:
    """The assertion that matters. These are not hypothetical values: both are in
    `reports/eval/` right now."""

    FORBIDDEN = ("1.000", "0.143", "0.111", "88.5%", "0.885")

    def test_no_page_prints_a_figure_from_a_non_quotable_report(
        self, pages: dict[str, str]
    ) -> None:
        for name, html in pages.items():
            for value in self.FORBIDDEN:
                assert value not in html, f"{name} publishes {value}, which is not a result"

    def test_every_unmeasured_metric_renders_its_reason(
        self, pages: dict[str, str], stats: dict[str, Any]
    ) -> None:
        home = pages["index.html"]
        evaluation = pages["evaluation.html"]
        for key in stats["unmeasured"]:
            display = stats["metrics"][key]["display"]
            assert display in home or display in evaluation, (
                f"{key} is unmeasured and no page says why"
            )

    def test_a_metric_with_no_value_renders_no_digits_as_its_value(
        self, pages: dict[str, str]
    ) -> None:
        """A card whose value is absent must not contain a number at all, since a reader
        skimming cards reads the large text and not the badge under it."""
        for name, html in pages.items():
            for card in re.findall(r'class="value absent">(.*?)</div>', html, re.S):
                assert not re.search(r"\d+\.\d+", card), f"{name} shows a number in an absent card"

    def test_the_pages_say_plainly_that_nothing_is_a_result(self, pages: dict[str, str]) -> None:
        """Substance, not wording. The first version matched one sentence, which froze the
        phrasing; what matters is that both pages carry the reason a figure is absent and name
        the gate that withheld it."""
        for name in ("index.html", "evaluation.html"):
            html = pages[name]
            assert "not yet quotable" in html or "nothing quotable" in html, (
                f"{name} does not say a figure is unavailable"
            )
            assert "reports/eval/" in html, f"{name} does not say where the figures live"

    def test_the_demo_figures_are_labelled_as_stub_replays(self, pages: dict[str, str]) -> None:
        """The demo's per-incident results are far better than the measured ones because they
        run on fixtures, so a page mentioning the demo has to say what it replayed."""
        for name in ("index.html", "run-it.html"):
            assert "stub" in pages[name], f"{name} describes the demo without naming its mode"


class TestThePublicSiteCannotPublishAReviewersAnswer:
    """The Console and the site are not the same audience, and only one of them is public.

    A feedback record holds `true_root_cause`: a human's statement of what actually broke. For
    an incident in a held-out split that is the label. The Console may show it, because it binds
    loopback and a second reviewer checking agreement needs to see it. The site must not, because
    the site is on the internet.

    Nothing connects them today: the site reads `site_stats.json` and the ADR files, and neither
    touches feedback. This is the test that keeps it that way, because the connection would be one
    convenient line and the consequence would be publishing answers to a benchmark.
    """

    def test_no_page_contains_a_true_root_cause_field(self, pages: dict[str, str]) -> None:
        for name, html in pages.items():
            assert "true_root_cause" not in html, f"{name} renders a reviewer's answer"

    def test_the_stats_file_carries_no_feedback(self, stats: dict[str, Any]) -> None:
        assert "feedback" not in json.dumps(stats)

    def test_the_builder_never_reads_the_feedback_store(self) -> None:
        """Asserted on the source, because the risk is a future edit rather than today's
        behaviour."""
        builder = (REPO_ROOT / "scripts" / "build_site_stats.py").read_text(encoding="utf-8")
        assert "feedback" not in builder
        assert "memory" not in builder

    def test_no_template_reads_a_feedback_record(self) -> None:
        for template in (REPO_ROOT / "site" / "templates").glob("*.html"):
            body = template.read_text(encoding="utf-8")
            assert "true_root_cause" not in body, template.name
            assert "verdict" not in body, template.name


class TestAMissingKeyFailsTheBuild:
    """SPEC.md Section 15, by name. A site that quietly omitted a metric would look like one
    whose author chose not to show it."""

    def test_an_unknown_metric_raises(self) -> None:
        number = site.metric_getter({"metrics": {}})
        with pytest.raises(site.SiteError, match="no such key"):
            number("root_cause_accuracy_test_id")

    @pytest.mark.parametrize(
        "key", ["metrics", "library", "showcase", "readme_rows", "commit", "generated_at"]
    )
    def test_a_missing_top_level_key_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
    ) -> None:
        payload = {
            "metrics": {},
            "library": {},
            "showcase": {},
            "readme_rows": [],
            "commit": "x",
            "generated_at": "y",
        }
        payload.pop(key)
        path = tmp_path / "stats.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(site, "STATS_PATH", path)
        with pytest.raises(site.SiteError, match=key):
            site.load_stats()

    def test_a_missing_stats_file_names_the_command_that_writes_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(site, "STATS_PATH", tmp_path / "absent.json")
        with pytest.raises(site.SiteError, match="build_site_stats"):
            site.load_stats()

    def test_an_undefined_template_variable_fails_rather_than_rendering_empty(
        self, tmp_path: Path
    ) -> None:
        """`StrictUndefined`, asserted rather than assumed. Jinja2's default is to render an
        empty string, which would turn a typo into a silently incomplete page."""
        from jinja2 import Environment, StrictUndefined, UndefinedError

        environment = Environment(autoescape=True, undefined=StrictUndefined)
        with pytest.raises(UndefinedError):
            environment.from_string("{{ nope }}").render()

    def test_the_real_environment_uses_strict_undefined(self) -> None:
        """The test above proves Jinja2 behaves that way; this proves the site asks for it."""
        assert "StrictUndefined" in BUILD.read_text(encoding="utf-8")


class TestTheSiteNeedsNoNetwork:
    def test_no_page_loads_an_external_script_or_stylesheet(self, pages: dict[str, str]) -> None:
        """A page that fetches a font or a chart library cannot be read offline, and this
        project's own demo is the offline case."""
        for name, html in pages.items():
            assert "<script" not in html, f"{name} loads a script"
            assert 'rel="stylesheet"' not in html, f"{name} loads an external stylesheet"
            assert "cdn" not in html.lower(), f"{name} references a CDN"
            assert "fonts.googleapis" not in html, f"{name} fetches a font"

    def test_no_page_embeds_an_image(self, pages: dict[str, str]) -> None:
        """Section 15 says no stock images. Diagrams are inline SVG, which needs no request."""
        for name, html in pages.items():
            assert "<img" not in html, f"{name} embeds an image"


class TestHygieneRulesHoldInTheRenderedHtml:
    """The hygiene script checks tracked files. These pages are generated, so they are not
    tracked and nothing else would look at them."""

    def test_no_em_or_en_dash(self, pages: dict[str, str]) -> None:
        # Written as escapes so this file does not itself contain what it forbids, which
        # ruff's ambiguous-character rule catches and the hygiene script would too.
        for name, html in pages.items():
            assert "\u2014" not in html, f"{name} contains an em dash"
            assert "\u2013" not in html, f"{name} contains an en dash"

    def test_no_emoji(self, pages: dict[str, str]) -> None:
        emoji = re.compile("[\U0001f300-\U0001faff☀-➿]")
        for name, html in pages.items():
            found = emoji.findall(html)
            assert not found, f"{name} contains {found}"


class TestTheDecisionsPageComesFromTheFiles:
    def test_every_adr_on_disk_is_listed(self, pages: dict[str, str]) -> None:
        """Generated from the directory rather than a hand-kept table, so an ADR cannot be
        written and left off the site."""
        on_disk = sorted((REPO_ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md"))
        html = pages["decisions.html"]
        for path in on_disk:
            assert path.name in html, f"{path.name} is not on the Decisions page"

    def test_an_empty_adr_directory_fails_the_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(site, "ADR_DIR", tmp_path)
        with pytest.raises(site.SiteError, match="no ADRs"):
            site.adr_index()
