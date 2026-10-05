"""Every diagram must be syntax a renderer will accept.

**The defect this closes.** `test_explainer_site.py` asserted that each page contained a
```{mermaid} block, and every page did, so the suite was green while the diagrams were broken.
Two faults had shipped:

1. Labels carried `<br/>` and `<small>` tags. Quarto puts the block through pandoc, which escapes
   them, so the renderer received `&lt;br/&gt;` as literal text rather than markup.
2. `metrics.qmd` had `style N stroke-dasharray: 4 3`. A `style` value cannot contain spaces, so
   that line is a parse error, and a parse error replaces the whole diagram with an error box.

Neither produces a build failure. The page renders, the block is present, and the picture is
missing or wrong. Only a human looking at the page would notice, which is exactly the kind of
defect a test should carry instead.

**What this checks is syntax, not beauty.** It cannot tell a clear diagram from a confusing one.
It can tell a diagram that renders from one that does not, which is the part that was failing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPLAINER = REPO_ROOT / "explainer"
README = REPO_ROOT / "README.md"

# A fenced mermaid block in a Quarto page or in Markdown.
QUARTO_BLOCK = re.compile(r"```\{mermaid\}\n(.*?)```", re.S)
MARKDOWN_BLOCK = re.compile(r"```mermaid\n(.*?)```", re.S)

# Diagram types this project uses. A typo in the first word means the renderer has no idea what
# it is looking at, and the message it shows is not helpful.
KNOWN_TYPES = ("flowchart", "graph", "sequenceDiagram", "stateDiagram", "erDiagram")


def quarto_diagrams() -> list[tuple[str, str]]:
    found = []
    for page in sorted(EXPLAINER.glob("*.qmd")):
        for body in QUARTO_BLOCK.findall(page.read_text(encoding="utf-8")):
            found.append((page.name, body))
    return found


def readme_diagrams() -> list[tuple[str, str]]:
    return [
        ("README.md", body) for body in MARKDOWN_BLOCK.findall(README.read_text(encoding="utf-8"))
    ]


ALL_DIAGRAMS = quarto_diagrams() + readme_diagrams()
QUARTO_ONLY = quarto_diagrams()


class TestThereAreDiagramsToCheck:
    def test_the_pages_carry_diagrams(self) -> None:
        """A regex that matched nothing would make every test below pass while checking
        nothing."""
        assert len(QUARTO_ONLY) >= 8, f"only found {len(QUARTO_ONLY)} diagrams in the explainer"

    def test_the_readme_carries_diagrams(self) -> None:
        assert readme_diagrams(), "the README has no mermaid blocks"


class TestNoDiagramContainsHtml:
    """Quarto escapes HTML inside a fenced block, so a tag in a label arrives at the renderer as
    entity text. The label then reads literally as `&lt;br/&gt;` or the parse fails outright."""

    @pytest.mark.parametrize(
        ("where", "body"), ALL_DIAGRAMS, ids=[f"{w}-{i}" for i, (w, _) in enumerate(ALL_DIAGRAMS)]
    )
    def test_no_html_tag_in_a_label(self, where: str, body: str) -> None:
        for tag in ("<br>", "<br/>", "<br />", "<small>", "</small>", "<b>", "<i>", "<span"):
            assert tag not in body, f"{where} has {tag} in a diagram label"

    @pytest.mark.parametrize(
        ("where", "body"), ALL_DIAGRAMS, ids=[f"{w}-{i}" for i, (w, _) in enumerate(ALL_DIAGRAMS)]
    )
    def test_no_html_entity_in_a_label(self, where: str, body: str) -> None:
        """An entity in the source means somebody escaped by hand, which double-escapes."""
        for entity in ("&lt;", "&gt;", "&amp;#"):
            assert entity not in body, f"{where} has {entity} in a diagram"


class TestStyleDirectivesParse:
    """Mermaid's `style` takes comma separated `key:value` pairs. A space inside a value ends the
    declaration early and the whole diagram fails to parse."""

    STYLE = re.compile(r"^\s*style\s+(\S+)\s+(.+)$", re.M)

    @pytest.mark.parametrize(
        ("where", "body"), ALL_DIAGRAMS, ids=[f"{w}-{i}" for i, (w, _) in enumerate(ALL_DIAGRAMS)]
    )
    def test_every_style_value_is_well_formed(self, where: str, body: str) -> None:
        for node, declarations in self.STYLE.findall(body):
            for pair in declarations.split(","):
                pair = pair.strip()
                assert ":" in pair, f"{where}: style {node} has `{pair}` with no colon"
                key, _, value = pair.partition(":")
                assert key.strip(), f"{where}: style {node} has an empty key"
                assert value.strip(), f"{where}: style {node} has an empty value for {key}"
                assert " " not in value.strip(), (
                    f"{where}: style {node} value `{value.strip()}` contains a space, "
                    "which ends the declaration early and breaks the whole diagram"
                )


class TestEveryDiagramDeclaresItsType:
    @pytest.mark.parametrize(
        ("where", "body"), ALL_DIAGRAMS, ids=[f"{w}-{i}" for i, (w, _) in enumerate(ALL_DIAGRAMS)]
    )
    def test_the_first_line_names_a_known_diagram_type(self, where: str, body: str) -> None:
        first = next((line.strip() for line in body.splitlines() if line.strip()), "")
        assert first.startswith(KNOWN_TYPES), f"{where} opens with {first!r}"

    @pytest.mark.parametrize(
        ("where", "body"), ALL_DIAGRAMS, ids=[f"{w}-{i}" for i, (w, _) in enumerate(ALL_DIAGRAMS)]
    )
    def test_a_flowchart_declares_a_direction(self, where: str, body: str) -> None:
        """`flowchart` with no direction is valid but renders top-down regardless of intent, so
        the omission is usually a mistake rather than a choice."""
        first = next((line.strip() for line in body.splitlines() if line.strip()), "")
        if not first.startswith("flowchart"):
            return
        assert first.split()[1:], f"{where}: flowchart with no direction"
        assert first.split()[1] in ("TB", "TD", "BT", "LR", "RL"), f"{where}: {first!r}"


class TestBracketsBalance:
    """An unclosed label swallows the rest of the diagram."""

    @pytest.mark.parametrize(
        ("where", "body"), ALL_DIAGRAMS, ids=[f"{w}-{i}" for i, (w, _) in enumerate(ALL_DIAGRAMS)]
    )
    def test_square_brackets_balance(self, where: str, body: str) -> None:
        assert body.count("[") == body.count("]"), f"{where} has unbalanced square brackets"

    @pytest.mark.parametrize(
        ("where", "body"), ALL_DIAGRAMS, ids=[f"{w}-{i}" for i, (w, _) in enumerate(ALL_DIAGRAMS)]
    )
    def test_braces_balance(self, where: str, body: str) -> None:
        assert body.count("{") == body.count("}"), f"{where} has unbalanced braces"

    @pytest.mark.parametrize(
        ("where", "body"), ALL_DIAGRAMS, ids=[f"{w}-{i}" for i, (w, _) in enumerate(ALL_DIAGRAMS)]
    )
    def test_quotes_pair_up(self, where: str, body: str) -> None:
        assert body.count('"') % 2 == 0, f"{where} has an odd number of quotes"


class TestTheRenderedOutputIsClean:
    """The checks above read the source. This one reads what the renderer was actually handed,
    because the escaping that caused the original fault happened between the two."""

    def test_no_rendered_page_feeds_entities_to_the_renderer(self) -> None:
        built = EXPLAINER / "_site"
        if not (built / "index.html").is_file():
            pytest.skip("explainer not rendered; run make site")
        for page in sorted(built.glob("*.html")):
            html = page.read_text(encoding="utf-8", errors="replace")
            for block in re.findall(r'<pre class="mermaid[^"]*">(.*?)</pre>', html, re.S):
                for entity in ("&lt;br", "&lt;small", "&lt;span"):
                    assert entity not in block, f"{page.name} feeds {entity} to the renderer"

    def test_every_rendered_page_still_has_its_diagrams(self) -> None:
        built = EXPLAINER / "_site"
        if not (built / "index.html").is_file():
            pytest.skip("explainer not rendered; run make site")
        for page in sorted(built.glob("*.html")):
            source = EXPLAINER / f"{page.stem}.qmd"
            if not source.is_file():
                continue
            expected = len(QUARTO_BLOCK.findall(source.read_text(encoding="utf-8")))
            actual = page.read_text(encoding="utf-8", errors="replace").count('class="mermaid')
            assert actual == expected, f"{page.name}: {expected} diagrams in source, {actual} built"
