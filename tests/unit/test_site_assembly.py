"""Tests for assembling the published site and for its link checker.

The site is now two sites: a Quarto explainer at the root and the Jinja2 reference site at
`/reference/`. That boundary is the new thing that can break, and it breaks quietly: a dead link
across it renders as text a reader clicks with no effect, and nothing in a build fails.

**Why the link checker only looks inward.** External links need the network, and a deploy that
fails because somebody else's server was down is blocked for the wrong reason. Internal links are
free to check and are the ones this repository can actually break.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


links = _load("check_site_links_under_test", REPO_ROOT / "scripts" / "check_site_links.py")
assembly = _load("assemble_site_under_test", REPO_ROOT / "scripts" / "assemble_site.py")


def page(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"<html><body>{body}</body></html>", encoding="utf-8")
    return path


class TestTheLinkCheckerFindsWhatItShould:
    def test_a_dead_link_is_reported(self, tmp_path: Path) -> None:
        page(tmp_path, "index.html", '<a href="missing.html">gone</a>')
        dead, pages, checked, _ = links.check(tmp_path)
        assert [(d.page.name, d.target) for d in dead] == [("index.html", "missing.html")]
        assert pages == 1
        assert checked == 1

    def test_a_live_link_is_not_reported(self, tmp_path: Path) -> None:
        page(tmp_path, "index.html", '<a href="other.html">there</a>')
        page(tmp_path, "other.html", "ok")
        dead, _, _, _ = links.check(tmp_path)
        assert dead == []

    def test_it_crosses_into_a_subdirectory(self, tmp_path: Path) -> None:
        """The boundary the two sites link across, which is the reason this exists."""
        page(tmp_path, "index.html", '<a href="reference/index.html">reference</a>')
        page(tmp_path / "reference", "index.html", "ok")
        dead, _, _, _ = links.check(tmp_path)
        assert dead == []

    def test_it_climbs_back_out_of_a_subdirectory(self, tmp_path: Path) -> None:
        page(tmp_path, "index.html", "root")
        page(tmp_path / "reference", "index.html", '<a href="../index.html">up</a>')
        dead, _, _, _ = links.check(tmp_path)
        assert dead == []

    def test_a_directory_link_means_its_index(self, tmp_path: Path) -> None:
        page(tmp_path, "index.html", '<a href="reference/">reference</a>')
        page(tmp_path / "reference", "index.html", "ok")
        dead, _, _, _ = links.check(tmp_path)
        assert dead == []

    def test_a_stylesheet_that_does_not_exist_is_reported(self, tmp_path: Path) -> None:
        """A missing stylesheet breaks a page more thoroughly than a dead link does, so `src`
        and `href` are both checked."""
        page(tmp_path, "index.html", '<link href="site_libs/theme.css" rel="stylesheet">')
        dead, _, _, _ = links.check(tmp_path)
        assert len(dead) == 1

    def test_a_fragment_is_a_link_to_the_page_it_is_on(self, tmp_path: Path) -> None:
        """Treating the whole string as a filename would report every anchor as broken, and the
        fix for that failure would be to stop checking anchors at all."""
        page(tmp_path, "index.html", '<a href="other.html#claims">claims</a><a href="#top">top</a>')
        page(tmp_path, "other.html", "ok")
        dead, _, checked, _ = links.check(tmp_path)
        assert dead == []
        # The same-page fragment is skipped rather than counted.
        assert checked == 1

    def test_external_links_are_left_alone(self, tmp_path: Path) -> None:
        page(
            tmp_path,
            "index.html",
            '<a href="https://example.invalid/x">x</a>'
            '<a href="mailto:a@b.c">mail</a>'
            '<a href="//cdn.example/x.js">protocol relative</a>',
        )
        dead, _, checked, _ = links.check(tmp_path)
        assert dead == []
        assert checked == 0

    def test_a_root_relative_link_resolves_against_the_site_root(self, tmp_path: Path) -> None:
        page(tmp_path / "reference", "index.html", '<a href="/index.html">home</a>')
        page(tmp_path, "index.html", "ok")
        dead, _, _, _ = links.check(tmp_path)
        assert dead == []

    def test_an_empty_directory_is_refused(self, tmp_path: Path) -> None:
        """A build that produced nothing must not pass a link check by having no links."""
        with pytest.raises(SystemExit, match="no HTML"):
            links.check(tmp_path)

    def test_a_missing_directory_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit, match="not a directory"):
            links.check(tmp_path / "absent")

    def test_main_returns_one_on_a_dead_link(self, tmp_path: Path) -> None:
        page(tmp_path, "index.html", '<a href="missing.html">gone</a>')
        sys.argv = ["check_site_links.py", str(tmp_path)]
        assert links.main() == 1

    def test_main_returns_zero_when_everything_resolves(self, tmp_path: Path, capsys: Any) -> None:
        page(tmp_path, "index.html", '<a href="other.html">there</a>')
        page(tmp_path, "other.html", "ok")
        sys.argv = ["check_site_links.py", str(tmp_path)]
        assert links.main() == 0
        assert "all resolve" in capsys.readouterr().out

    def test_a_link_above_the_root_is_counted_rather_than_ignored(self, tmp_path: Path) -> None:
        """The reference site is published under `/reference/` and links up to the explainer, so
        when it is built alone that link legitimately points outside what was built. Reporting it
        as fine would be wrong, and reporting it as dead would make the check useless, so it is
        counted and named."""
        page(tmp_path / "reference", "index.html", '<a href="../index.html">up</a>')
        dead, _, checked, escaped = links.check(tmp_path / "reference", allow_parent=True)
        assert dead == []
        assert escaped == 1
        assert checked == 0

    def test_without_the_flag_an_escaping_link_is_dead(self, tmp_path: Path) -> None:
        """The default stays strict. The assembled site has no reason to link above its root, so
        one that does is a bug there."""
        page(tmp_path / "reference", "index.html", '<a href="../index.html">up</a>')
        dead, _, _, escaped = links.check(tmp_path / "reference")
        assert len(dead) == 1
        assert escaped == 0


class TestTheAssemblyRefusesToPublishHalfASite:
    def test_a_missing_variable_file_is_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Quarto renders an unknown variable as literal text rather than failing, so a missing
        variable file would publish `{{< var specs >}}` where a number belongs. That looks like a
        bug in the site rather than a missing measurement, which is the worse of the two.

        **Quarto is pretended present rather than required.** `render_explainer` checks for the
        binary first, so on a machine without it this reached the wrong branch and asserted the
        wrong message. It passed here, where Quarto is installed, and failed in CI's verify job,
        which does not install it: only the site job does. A test whose outcome depends on which
        binaries a machine happens to have is a test that holds on one machine.
        """
        monkeypatch.setattr(assembly.shutil, "which", lambda _: "/usr/local/bin/quarto")
        monkeypatch.setattr(assembly, "VARIABLES", tmp_path / "absent.yml")
        with pytest.raises(assembly.AssembleError, match="build_site_stats"):
            assembly.render_explainer()

    def test_a_missing_quarto_says_what_to_do(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Quarto is a separate binary, so its absence is a normal state on a fresh machine and
        the message names the fallback rather than only the problem."""
        monkeypatch.setattr(assembly.shutil, "which", lambda _: None)
        with pytest.raises(assembly.AssembleError, match="make site-reference"):
            assembly.render_explainer()

    def test_the_reference_subpath_is_declared_once(self) -> None:
        """The explainer's navbar links to it and the reference site's own nav climbs out of it.
        Two places agreeing on a path is the shape of every vocabulary defect in this project, so
        the value is named rather than spelled twice in code."""
        assert assembly.REFERENCE_SUBPATH == "reference"
        navbar = (REPO_ROOT / "explainer" / "_quarto.yml").read_text(encoding="utf-8")
        assert f"{assembly.REFERENCE_SUBPATH}/index.html" in navbar

    def test_no_test_here_depends_on_quarto_being_installed(self) -> None:
        """The lesson from the CI failure, pinned. Every test that calls the real renderer must
        first say whether the binary exists, rather than asking the machine. `make site` is where
        the real binary is needed, and CI's site job is the only place that installs it."""
        body = Path(__file__).read_text(encoding="utf-8")
        assert body.count("assembly.render_explainer()") <= body.count('"which"')

    def test_the_reference_site_links_back_out(self) -> None:
        base = (REPO_ROOT / "site" / "templates" / "base.html").read_text(encoding="utf-8")
        assert "../index.html" in base, "the reference site has no way back to the explainer"


class TestTheAssembledSiteIfItExists:
    """Asserted against a real build when one is present. Skipped rather than built here: the
    build needs Quarto and takes seconds, and the workflow runs it."""

    @pytest.fixture
    def dist(self) -> Path:
        built = REPO_ROOT / "site" / "dist"
        if not (built / "index.html").is_file():
            pytest.skip("site not built; run make site")
        return built

    def test_both_halves_are_present(self, dist: Path) -> None:
        assert (dist / "index.html").is_file()
        assert (dist / "reference" / "index.html").is_file()

    def test_nojekyll_is_written(self, dist: Path) -> None:
        """Quarto writes `site_libs/`, and without this GitHub Pages strips every path beginning
        with an underscore, which would silently remove the stylesheet and the diagram renderer."""
        assert (dist / ".nojekyll").is_file()

    def test_every_internal_link_resolves(self, dist: Path) -> None:
        dead, _, checked, _ = links.check(dist)
        assert dead == [], [(d.page.as_posix(), d.target) for d in dead]
        assert checked > 50

    def test_no_variable_was_left_unsubstituted(self, dist: Path) -> None:
        for html in dist.glob("*.html"):
            assert "{{< var" not in html.read_text(encoding="utf-8", errors="replace"), html.name
