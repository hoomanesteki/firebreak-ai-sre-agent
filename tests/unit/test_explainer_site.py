"""Tests for the Quarto explainer site.

**Two sites, two audiences, one rule.** The explainer is five pages for somebody deciding
whether this project deserves their attention; the reference site is seven pages for somebody
checking whether its claims hold. Both are bound by the same rule: a number reaches a page only
if a file under `reports/` produced it.

**The explainer enforces it differently, and the difference is the point.** The reference site's
templates call one macro that has no number to print when a figure is unpublishable. A Quarto
page has no macros, so the numbers arrive as variables generated into `explainer/_variables.yml`.
That file is the boundary, and these tests guard it.

**Why the pages contain no executable code.** The first draft read `site_stats.json` from a
`{python}` block, which worked locally and would have made the render depend on a Python kernel
being present in CI. That is the failure `make verify-clean` exists to catch, and the cheaper fix
was to stop needing the kernel.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPLAINER = REPO_ROOT / "explainer"
VARIABLES = EXPLAINER / "_variables.yml"
PAGES = sorted(EXPLAINER.glob("*.qmd"))
CONFIG = EXPLAINER / "_quarto.yml"

# `{{< var name >}}`, Quarto's substitution.
VARIABLE_USE = re.compile(r"\{\{<\s*var\s+([a-z_]+)\s*>\}\}")


def source(page: Path) -> str:
    return page.read_text(encoding="utf-8")


class TestThePagesExist:
    def test_there_are_five(self) -> None:
        assert len(PAGES) == 5, [p.name for p in PAGES]

    def test_every_page_the_navbar_names_exists(self) -> None:
        """A navbar entry pointing at a page nobody wrote renders as a link to a 404."""
        config = yaml.safe_load(source(CONFIG))
        navbar = config["website"]["navbar"]
        named = [
            entry["href"]
            for entry in navbar.get("left", [])
            if isinstance(entry, dict) and entry.get("href", "").endswith(".qmd")
        ]
        assert named, "the navbar names no pages"
        for href in named:
            assert (EXPLAINER / href).is_file(), f"the navbar names {href} and it is missing"

    @pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
    def test_every_page_has_a_title(self, page: Path) -> None:
        assert re.search(r"^title:", source(page), re.MULTILINE), page.name


class TestNoPageTypesANumber:
    """The rule, applied to the half of the site where it is easiest to break: prose."""

    def test_every_variable_a_page_uses_is_generated(self) -> None:
        """An unknown variable renders as literal `{{< var whatever >}}` text rather than
        failing, so a typo would publish a placeholder where a number belongs."""
        declared = set(yaml.safe_load(source(VARIABLES)))
        for page in PAGES:
            for name in VARIABLE_USE.findall(source(page)):
                assert name in declared, f"{page.name} uses {name}, which nothing generates"

    def test_the_variable_file_says_it_is_generated(self) -> None:
        assert "Do not edit" in source(VARIABLES)
        assert "build_site_stats" in source(VARIABLES)

    def test_the_figures_that_matter_come_from_variables(self) -> None:
        """The comparison this project turns on: the same triage names nearly every fault on
        fixtures and a fraction of them on recordings. Typed into prose, a number that important
        goes stale and then gets quoted."""
        declared = set(yaml.safe_load(source(VARIABLES)))
        for name in (
            "recorded_root_cause_correct",
            "recorded_root_cause_applicable",
            "recorded_abstention_correct",
            "recorded_abstention_trials",
        ):
            assert name in declared
        used = {name for page in PAGES for name in VARIABLE_USE.findall(source(page))}
        assert "recorded_root_cause_correct" in used

    def test_no_page_states_a_bare_accuracy(self) -> None:
        """The two figures in `reports/eval/` that look quotable and are not: a fixture run at
        1.000 and a partly recorded tuning split at 0.143."""
        for page in PAGES:
            body = source(page)
            for forbidden in ("1.000", "0.143", "0.111", "0.885"):
                assert forbidden not in body, f"{page.name} states {forbidden}"

    def test_the_variable_file_carries_no_local_bundle_count(self) -> None:
        """It describes whichever machine ran the build, and a reader of this site cloned
        nothing."""
        assert "recorded:" not in source(VARIABLES)


class TestNoPageRunsCode:
    def test_no_page_has_an_executable_block(self) -> None:
        """A `{python}` or `{r}` block makes the render depend on a kernel. The first draft had
        one, it worked locally, and CI would have been where that stopped being true."""
        for page in PAGES:
            body = source(page)
            for engine in ("```{python}", "```{r}", "```{julia}", "```{ojs}"):
                assert engine not in body, f"{page.name} runs {engine}"

    def test_diagrams_are_mermaid_rather_than_an_image(self) -> None:
        """Mermaid is bundled by Quarto into `site_libs`, so a diagram needs no network and no
        stock image. Section 15 forbids the latter outright."""
        with_diagrams = [p for p in PAGES if "```{mermaid}" in source(p)]
        assert len(with_diagrams) >= 4, "the pages lean on prose where a diagram would carry it"
        for page in PAGES:
            assert "![" not in source(page).replace("![](", ""), f"{page.name} embeds an image"


class TestTheHonestyIsOnThePage:
    def test_the_overview_says_performance_is_not_measured(self) -> None:
        """Asserted on substance rather than on a sentence.

        The first version matched the exact phrase "has never been measured", which made the
        wording unchangeable without a test failure, and the wording needed changing: it led the
        page with a red box about absence before a reader knew what the project was. What has to
        hold is that the page states the gap and names the gate, not that it does so in one
        particular set of words.
        """
        body = source(EXPLAINER / "index.qmd")
        assert "not measured" in body, "the overview does not say performance is unmeasured"
        assert "quotable_as_a_result" in body, "the overview does not name the gate"
        # Both inputs, because naming one and not the other implies the other is done.
        assert "credentials" in body
        assert "recordings for the held-out splits" in body

    def test_the_overview_does_not_open_with_the_disclosure(self) -> None:
        """A reader should know what the thing is before learning what it has not proved.

        The disclosure used to be the first element on the page, above the problem statement, in
        a red callout. It is the same information either way; placed first it reads as a warning
        about the project rather than a property of its evaluation harness.
        """
        body = source(EXPLAINER / "index.qmd")
        problem = body.index("## The problem")
        disclosure = body.index("## How well does it work")
        assert problem < disclosure, "the disclosure precedes the problem statement again"

    def test_the_measurement_page_separates_engineering_from_performance(self) -> None:
        """A passing test says the code does what a test says it should. Conflating that with
        finding root causes is the single easiest way for this site to mislead."""
        body = source(EXPLAINER / "metrics.qmd")
        assert "says nothing about whether" in body.lower() or "says nothing about" in body

    def test_the_demo_is_labelled_as_a_stub_replay(self) -> None:
        body = source(EXPLAINER / "index.qmd")
        assert "deterministic stub" in body

    def test_the_trade_offs_page_records_what_was_refused(self) -> None:
        """Refusing a change that looks good is harder than making one, and a site that only
        lists wins is a site nobody learns anything from."""
        body = source(EXPLAINER / "decisions.qmd")
        assert "refused" in body.lower()
        assert "measured worse" in body.lower() or "regression" in body.lower()

    def test_every_page_avoids_em_and_en_dashes(self) -> None:
        for page in PAGES:
            body = source(page)
            # Escapes, so this file does not itself contain what it forbids. The hygiene
            # check scans it too, now that the extension list covers the whole repository.
            assert "\u2014" not in body, f"{page.name} contains an em dash"
            assert "\u2013" not in body, f"{page.name} contains an en dash"

    def test_no_page_contains_an_emoji(self) -> None:
        emoji = re.compile("[\U0001f300-\U0001faff☀-➿]")
        for page in PAGES:
            found = emoji.findall(source(page))
            assert not found, f"{page.name} contains {found}"


class TestTheSiteIsSelfContained:
    def test_the_config_bundles_rather_than_fetches(self) -> None:
        """Quarto copies mermaid and bootstrap into `site_libs`. A page that fetched a font or a
        chart library could not be read offline, which is the one thing this project's own demo
        insists on."""
        config = source(CONFIG)
        assert "cdn" not in config.lower()
        assert "fonts.googleapis" not in config

    def test_the_theme_is_local(self) -> None:
        config = yaml.safe_load(source(CONFIG))
        theme = config["format"]["html"]["theme"]
        assert "custom.scss" in theme
        assert (EXPLAINER / "custom.scss").is_file()
