"""Tests for firebreak.tools.runbooks (runbook_search).

Built on hand written runbooks rather than on `knowledge/`, so that editing a
real runbook does not change what these assert, and so each scoring rule can
be provoked on its own.
"""

from __future__ import annotations

from typing import Any

import pytest

from firebreak.graph.knowledge import Knowledge
from firebreak.tools.base import ToolContext, ToolError
from firebreak.tools.registry import ALL_TOOLS, COMMANDER_TOOLS, SPECIALIST_TOOLS, build_registry
from firebreak.tools.runbooks import (
    COVERS_SERVICE_BOOST,
    MAX_SECTION_CHARS,
    RunbookSearchInput,
    runbook_search,
    score_section,
    search_runbooks,
    split_sections,
    tokenise,
)

CART_BODY = """## Symptoms

Customers see errors adding items to the basket.

## First checks

Look at the cart service error rate, then at the datastore it writes to.

## Remediation

Restart the cart service only after ruling out its datastore.
"""

PAYMENT_BODY = """## Symptoms

Checkout completes but the charge never lands.

## First checks

Compare the payment error rate against its baseline.
"""


def _knowledge(**overrides: Any) -> Knowledge:
    payload: dict[str, Any] = {
        "services": [
            {
                "name": "cart",
                "language": "dotnet",
                "tier": "core",
                "description": "Holds baskets.",
                "owning_team": "Storefront",
                "container": "cart",
            },
            {
                "name": "payment",
                "language": "go",
                "tier": "core",
                "description": "Takes money.",
                "owning_team": "Storefront",
                "container": "payment",
            },
        ],
        "teams": [
            {
                "name": "Storefront",
                "slug": "storefront",
                "description": "Owns the shop.",
                "escalation": "#storefront-oncall",
                "services": ["cart", "payment"],
            }
        ],
        "remediations": [
            {
                "id": "page-owning-team",
                "kind": "page_owning_team",
                "title": "Page the owning team",
                "description": "Fallback.",
                "blast_radius": "One pager.",
                "reversible": True,
                "requires_approval": False,
            }
        ],
        "runbooks": [
            {
                "id": "cart-errors",
                "title": "Cart errors",
                "covers": ["cart"],
                "symptoms": ["errors adding to basket"],
                "updated": "2026-09-24",
                "path": "knowledge/runbooks/cart-errors.md",
                "body": CART_BODY,
            },
            {
                "id": "payment-failures",
                "title": "Payment failures",
                "covers": ["payment"],
                "symptoms": ["charge never lands"],
                "updated": "2026-09-24",
                "path": "knowledge/runbooks/payment-failures.md",
                "body": PAYMENT_BODY,
            },
        ],
    }
    payload.update(overrides)
    return Knowledge.model_validate(payload)


class TestTokenising:
    def test_hyphens_split_so_a_service_name_matches_either_spelling(self) -> None:
        """Operators type `product catalog` as often as `product-catalog`."""
        assert tokenise("product-catalog") == ["product", "catalog"]

    def test_stopwords_go(self) -> None:
        assert tokenise("what is the error") == ["error"]

    def test_the_stop_list_keeps_words_that_are_the_question(self) -> None:
        """A longer stop list would remove `down` and `failed`."""
        assert tokenise("service is down and failed") == ["service", "down", "failed"]


class TestSectioning:
    def test_a_runbook_splits_on_its_headings(self) -> None:
        runbook = _knowledge().runbooks[0]
        assert [s.heading for s in split_sections(runbook)] == [
            "Symptoms",
            "First checks",
            "Remediation",
        ]

    def test_text_before_the_first_heading_is_kept(self) -> None:
        """A runbook written without headings must still return something."""
        facts = _knowledge()
        runbook = facts.runbooks[0].model_copy(update={"body": "Just a paragraph."})
        sections = split_sections(runbook)
        assert len(sections) == 1
        assert sections[0].text == "Just a paragraph."

    def test_an_empty_body_yields_nothing(self) -> None:
        runbook = _knowledge().runbooks[0].model_copy(update={"body": "   "})
        assert split_sections(runbook) == []

    def test_an_empty_section_is_skipped(self) -> None:
        runbook = (
            _knowledge()
            .runbooks[0]
            .model_copy(update={"body": "## Empty\n\n## Real\n\nSomething.\n"})
        )
        assert [s.heading for s in split_sections(runbook)] == ["Real"]


class TestScoring:
    def test_the_declared_service_dominates_term_overlap(self) -> None:
        """A runbook saying it covers the service beats one that merely mentions it.

        This is the structural signal standing in for the dense half of
        hybrid retrieval, so it has to actually dominate.
        """
        facts = _knowledge()
        cart = facts.runbooks[0]
        section = split_sections(cart)[0]
        with_service = score_section(section, cart, tokenise("errors"), "cart")
        without = score_section(section, cart, tokenise("errors"), None)
        assert with_service - without == pytest.approx(COVERS_SERVICE_BOOST)

    def test_a_matching_declared_symptom_raises_the_score(self) -> None:
        facts = _knowledge()
        cart = facts.runbooks[0]
        section = split_sections(cart)[1]
        assert score_section(section, cart, tokenise("basket"), None) > score_section(
            section, cart, tokenise("unrelated"), None
        )

    def test_repeating_one_word_does_not_beat_covering_every_word(self) -> None:
        """The part of BM25 that matters at this scale.

        Both sections come from the same runbook, so the title and symptom
        boosts are identical and only the term overlap differs. Comparing
        across two runbooks would be comparing their metadata as well.
        """
        runbook = _knowledge().runbooks[0].model_copy(update={"symptoms": (), "title": "H"})
        repeated = runbook.model_copy(update={"body": "## H\n\n" + "cart " * 40})
        broad = runbook.model_copy(update={"body": "## H\n\ncart datastore error rate"})
        terms = tokenise("cart datastore error rate")
        assert score_section(split_sections(broad)[0], broad, terms, None) > score_section(
            split_sections(repeated)[0], repeated, terms, None
        )


class TestSearch:
    def test_it_finds_the_runbook_for_the_named_service(self) -> None:
        results = search_runbooks(_knowledge(), "errors", service="payment")
        assert results[0][0].runbook_id == "payment-failures"

    def test_a_query_matching_nothing_returns_nothing(self) -> None:
        assert search_runbooks(_knowledge(), "kubernetes etcd quorum") == []

    def test_a_query_of_only_stopwords_returns_nothing(self) -> None:
        """Rather than everything, which is what an empty term list would match."""
        assert search_runbooks(_knowledge(), "what is the") == []

    def test_results_are_ordered_and_limited(self) -> None:
        results = search_runbooks(_knowledge(), "cart error datastore", limit=2)
        assert len(results) == 2
        assert results[0][1] >= results[1][1]

    def test_the_same_query_always_returns_the_same_order(self) -> None:
        """A citation that is not reproducible is not a citation."""
        facts = _knowledge()
        first = search_runbooks(facts, "error rate", service="cart")
        second = search_runbooks(facts, "error rate", service="cart")
        assert first == second


class TestTheTool:
    @pytest.fixture
    def context(self, monkeypatch) -> ToolContext:  # type: ignore[no-untyped-def]
        from firebreak.tools import runbooks as module

        monkeypatch.setattr(module, "load_knowledge", _knowledge)

        class _Backend:
            def fingerprint(self):  # type: ignore[no-untyped-def]
                from firebreak.tools.evidence import BackendFingerprint, BackendMode

                return BackendFingerprint(mode=BackendMode.BUNDLE, identity="inc_000000000000")

        return ToolContext(backend=_Backend())  # type: ignore[arg-type]

    def test_it_returns_sections_and_records_evidence(self, context) -> None:  # type: ignore[no-untyped-def]
        result = runbook_search(context, RunbookSearchInput(query="error rate", service="payment"))
        assert result.tool == "runbook_search"
        assert result.evidence_id is not None
        assert result.data["sections"]
        assert result.data["sections"][0]["runbook_id"] == "payment-failures"

    def test_the_same_question_gives_the_same_evidence_id(self, context) -> None:  # type: ignore[no-untyped-def]
        first = runbook_search(context, RunbookSearchInput(query="error rate"))
        second = runbook_search(context, RunbookSearchInput(query="error rate"))
        assert first.evidence_id == second.evidence_id

    def test_a_different_question_gives_a_different_evidence_id(self, context) -> None:  # type: ignore[no-untyped-def]
        first = runbook_search(context, RunbookSearchInput(query="error rate"))
        second = runbook_search(context, RunbookSearchInput(query="error rate", service="cart"))
        assert first.evidence_id != second.evidence_id

    def test_an_unknown_service_is_refused_rather_than_ignored(self, context) -> None:  # type: ignore[no-untyped-def]
        """Silently searching every runbook would hide the caller's mistake."""
        with pytest.raises(ToolError, match="not a service in the knowledge graph"):
            runbook_search(context, RunbookSearchInput(query="errors", service="phantom"))

    def test_finding_nothing_says_so_usefully(self, context) -> None:  # type: ignore[no-untyped-def]
        result = runbook_search(context, RunbookSearchInput(query="kubernetes etcd quorum"))
        assert result.data["sections"] == []
        assert "no runbook section matches" in result.summary
        # An absent procedure is a finding, not a dead end.
        assert "worth reporting" in result.summary

    def test_section_text_is_capped(self, context, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from firebreak.tools import runbooks as module

        long_body = "## Long\n\n" + ("cart error " * 400)

        def _big() -> Knowledge:
            facts = _knowledge()
            return facts.model_copy(
                update={"runbooks": (facts.runbooks[0].model_copy(update={"body": long_body}),)}
            )

        monkeypatch.setattr(module, "load_knowledge", _big)
        result = runbook_search(context, RunbookSearchInput(query="cart error"))
        assert len(result.data["sections"][0]["text"]) <= MAX_SECTION_CHARS

    def test_unreadable_knowledge_is_an_error_not_an_empty_result(
        self, context, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """ "No runbook covers this" and "the files are broken" are different facts."""
        from firebreak.graph.knowledge import KnowledgeError
        from firebreak.tools import runbooks as module

        def _broken() -> Knowledge:
            raise KnowledgeError("services.yaml is missing")

        monkeypatch.setattr(module, "load_knowledge", _broken)
        with pytest.raises(ToolError, match="knowledge files could not be read"):
            runbook_search(context, RunbookSearchInput(query="errors"))


class TestRegistration:
    def test_the_tool_is_in_the_registry(self) -> None:
        assert "runbook_search" in build_registry(ALL_TOOLS)

    def test_no_specialist_can_call_it(self) -> None:
        """Reading the team's procedure is a decision, not signal analysis.

        Handing it to all four specialists would also mean four of them
        retrieving the same document and paying for it four times.
        """
        for role, allowed in SPECIALIST_TOOLS.items():
            assert "runbook_search" not in allowed, f"{role} should not have it"

    def test_the_commander_can(self) -> None:
        assert "runbook_search" in COMMANDER_TOOLS

    def test_its_description_says_when_not_to_use_it(self) -> None:
        """SPEC.md Section 6.3. A smell check, not a proof: the review is reading it."""
        spec = build_registry(ALL_TOOLS).spec("runbook_search")
        assert "knows nothing about the current state" in spec.description
        assert "not evidence" in spec.description
