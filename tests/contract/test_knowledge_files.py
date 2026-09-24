"""The knowledge files in this repository must load and stay consistent.

`tests/unit/test_knowledge.py` tests the validator against hand built inputs.
This tests the actual files, because a broken edit to a runbook should fail
the build rather than surface later as a query that returns nothing.

It also asserts the properties that make the files useful rather than merely
valid: that the inventory matches the pinned demo, that ownership reaches
every service, and that retrieval finds the obviously right runbook for an
obvious question.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from firebreak.graph.knowledge import KNOWLEDGE_DIR, RemediationKind, Tier, load_knowledge
from firebreak.tools.runbooks import search_runbooks

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET_SYSTEM = REPO_ROOT / "docs" / "target-system.md"

# Every application service docs/target-system.md lists. Written out rather
# than parsed so that a service disappearing from the doc is a visible
# conflict between two files rather than a silently shorter list.
APPLICATION_SERVICES = frozenset(
    {
        "accounting",
        "ad",
        "cart",
        "checkout",
        "currency",
        "email",
        "fraud-detection",
        "frontend",
        "frontend-proxy",
        "image-provider",
        "load-generator",
        "payment",
        "product-catalog",
        "quote",
        "recommendation",
        "shipping",
    }
)


@pytest.fixture(scope="module")
def knowledge():  # type: ignore[no-untyped-def]
    return load_knowledge()


class TestTheFilesLoad:
    def test_they_exist_where_the_loader_looks(self) -> None:
        assert KNOWLEDGE_DIR.is_dir(), f"{KNOWLEDGE_DIR} is missing"

    def test_they_validate_and_cross_reference(self, knowledge) -> None:  # type: ignore[no-untyped-def]
        """`load_knowledge` raises on any inconsistency, so reaching here is the test."""
        assert knowledge.services
        assert knowledge.teams
        assert knowledge.runbooks
        assert knowledge.remediations


class TestTheInventoryMatchesThePinnedDemo:
    def test_every_application_service_is_described(self, knowledge) -> None:  # type: ignore[no-untyped-def]
        missing = APPLICATION_SERVICES - knowledge.service_names
        assert not missing, f"services in the demo with no knowledge entry: {sorted(missing)}"

    def test_nothing_is_described_that_does_not_exist(self, knowledge) -> None:  # type: ignore[no-untyped-def]
        """A service in this file that the demo does not run is fiction."""
        extra = knowledge.service_names - APPLICATION_SERVICES
        assert not extra, f"described but not in the demo: {sorted(extra)}"

    def test_the_service_list_still_agrees_with_the_target_system_doc(self) -> None:
        """Both files describe the same pinned release and must not drift.

        docs/target-system.md is the one read from the vendored submodule, so
        it is the authority; this asserts the constant above still matches it.
        """
        text = TARGET_SYSTEM.read_text(encoding="utf-8")
        section = text.split("## Services", 1)[1].split("## Ports", 1)[0]
        listed = set(re.findall(r"`([a-z0-9-]+)`", section))
        assert listed >= APPLICATION_SERVICES, sorted(APPLICATION_SERVICES - listed)


class TestOwnership:
    def test_every_service_has_exactly_one_owner(self, knowledge) -> None:  # type: ignore[no-untyped-def]
        """The validator enforces this; asserting it here names the file."""
        for name in sorted(knowledge.service_names):
            assert knowledge.team_for(name) is not None, f"{name} has no owning team"

    def test_every_team_has_an_escalation_route(self, knowledge) -> None:  # type: ignore[no-untyped-def]
        """A team with no channel is a page that goes nowhere."""
        for team in knowledge.teams:
            assert team.escalation.strip(), f"{team.name} has no escalation route"

    def test_the_customer_facing_services_are_covered(self, knowledge) -> None:  # type: ignore[no-untyped-def]
        """An edge or core service with no runbook is the gap that hurts."""
        uncovered = sorted(
            service.name
            for service in knowledge.services
            if service.tier in {Tier.EDGE, Tier.CORE} and not knowledge.runbooks_for(service.name)
        )
        assert not uncovered, f"no runbook covers: {uncovered}"


class TestRemediationsAreSafe:
    def test_the_fallback_exists(self, knowledge) -> None:  # type: ignore[no-untyped-def]
        kinds = {r.kind for r in knowledge.remediations}
        assert RemediationKind.PAGE_OWNING_TEAM in kinds

    def test_nothing_irreversible_skips_approval(self, knowledge) -> None:  # type: ignore[no-untyped-def]
        for remediation in knowledge.remediations:
            if not remediation.reversible:
                assert remediation.requires_approval, remediation.id

    def test_every_action_states_its_blast_radius(self, knowledge) -> None:  # type: ignore[no-untyped-def]
        """An action whose consequences are unstated cannot be approved."""
        for remediation in knowledge.remediations:
            assert len(remediation.blast_radius.strip()) > 20, remediation.id

    def test_the_two_risky_actions_carry_preconditions(self, knowledge) -> None:  # type: ignore[no-untyped-def]
        """Paging a human needs none. Changing the system does."""
        for remediation in knowledge.remediations:
            if remediation.kind is RemediationKind.PAGE_OWNING_TEAM:
                continue
            assert remediation.preconditions, f"{remediation.id} has no preconditions"


class TestRunbooksAreUsable:
    def test_each_one_has_the_expected_sections(self, knowledge) -> None:  # type: ignore[no-untyped-def]
        """Consistent headings are what make section retrieval worth doing."""
        required = ("Symptoms", "First checks", "Likely causes", "Remediation", "Escalation")
        for runbook in knowledge.runbooks:
            for heading in required:
                assert f"## {heading}" in runbook.body, f"{runbook.id} has no {heading} section"

    def test_each_one_declares_what_it_covers_and_when_it_applies(self, knowledge) -> None:  # type: ignore[no-untyped-def]
        """`covers` and `symptoms` are the structured half of retrieval.

        A runbook with neither can only ever be found by term overlap, which
        is the weaker signal by design.
        """
        for runbook in knowledge.runbooks:
            assert runbook.covers, f"{runbook.id} covers nothing"
            assert runbook.symptoms, f"{runbook.id} declares no symptoms"

    def test_the_id_matches_the_filename(self, knowledge) -> None:  # type: ignore[no-untyped-def]
        """So a citation's path and its id cannot disagree."""
        for runbook in knowledge.runbooks:
            assert Path(runbook.path).stem == runbook.id

    @pytest.mark.parametrize(
        ("query", "service", "expected"),
        [
            ("payment is returning errors", "payment", "payment-declines-and-errors"),
            ("memory climbing with no errors", "email", "service-using-too-much-memory"),
            ("orders placed but not recorded", None, "queue-consumers-falling-behind"),
            ("cannot reach the database", "product-catalog", "datastore-dependency-failures"),
            ("should I restart it", "cart", "restarting-a-service-safely"),
            ("everything got slower at once", None, "high-traffic-low-complexity-services"),
        ],
    )
    def test_an_obvious_question_finds_the_obvious_runbook(
        self,
        knowledge,  # type: ignore[no-untyped-def]
        query: str,
        service: str | None,
        expected: str,
    ) -> None:
        """Retrieval that returns plausible nonsense is worse than none.

        These are the questions an operator actually types, and the answers
        are unambiguous, so they are a fair check on whether the scoring works
        rather than merely runs.
        """
        results = search_runbooks(knowledge, query, service, limit=1)
        assert results, f"nothing found for {query!r}"
        assert results[0][0].runbook_id == expected
