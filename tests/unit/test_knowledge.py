"""Tests for firebreak.graph.knowledge: the cross checks and how they fail.

The validation is the module's whole reason to exist, so the tests are
mostly about what it refuses. Every rejection here corresponds to a failure
that is silent at load time and expensive later: a runbook covering a
misspelled service is a runbook no query ever returns, and a service owned
by nobody is a page that goes nowhere at three in the morning.

Built from dicts rather than from `knowledge/`, so that editing a runbook
does not break unrelated tests, and so each failure can be provoked one at a
time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from firebreak.graph.knowledge import (
    Knowledge,
    KnowledgeError,
    RemediationKind,
    Tier,
    load_knowledge,
    parse_runbook,
)


def _service(name: str = "cart", **overrides: Any) -> dict[str, Any]:
    base = {
        "name": name,
        "language": "dotnet",
        "tier": "core",
        "description": "Holds carts.",
        "owning_team": "Storefront",
        "container": name,
    }
    return {**base, **overrides}


def _team(name: str = "Storefront", services: tuple[str, ...] = ("cart",)) -> dict[str, Any]:
    return {
        "name": name,
        "slug": name.lower(),
        "description": "Owns the storefront.",
        "escalation": "#storefront-oncall",
        "services": list(services),
    }


def _remediation(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "page-owning-team",
        "kind": "page_owning_team",
        "title": "Page the owning team",
        "description": "The fallback that always applies.",
        "blast_radius": "One team's pager.",
        "reversible": True,
        "requires_approval": False,
    }
    return {**base, **overrides}


def _runbook(
    runbook_id: str = "cart-errors", covers: tuple[str, ...] = ("cart",)
) -> dict[str, Any]:
    return {
        "id": runbook_id,
        "title": "Cart errors",
        "covers": list(covers),
        "symptoms": ["errors on add to cart"],
        "updated": "2026-09-24",
        "path": f"knowledge/runbooks/{runbook_id}.md",
        "body": "Check upstream first.",
    }


def _knowledge(**overrides: Any) -> dict[str, Any]:
    base = {
        "services": [_service()],
        "teams": [_team()],
        "remediations": [_remediation()],
        "runbooks": [_runbook()],
    }
    return {**base, **overrides}


class TestAValidSet:
    def test_it_validates(self) -> None:
        facts = Knowledge.model_validate(_knowledge())
        assert facts.service_names == {"cart"}

    def test_lookups_work(self) -> None:
        facts = Knowledge.model_validate(_knowledge())
        assert facts.service("cart") is not None
        assert facts.service("nope") is None
        team = facts.team_for("cart")
        assert team is not None and team.name == "Storefront"
        assert [r.id for r in facts.runbooks_for("cart")] == ["cart-errors"]
        assert facts.runbooks_for("nope") == ()

    def test_tier_is_an_enum_not_a_free_string(self) -> None:
        facts = Knowledge.model_validate(_knowledge())
        assert facts.services[0].tier is Tier.CORE


class TestCrossReferences:
    """Each of these is a name that points at nothing."""

    def test_a_service_owned_by_an_unknown_team_is_refused(self) -> None:
        payload = _knowledge(services=[_service(owning_team="Ghosts")])
        with pytest.raises(ValueError, match="unknown team"):
            Knowledge.model_validate(payload)

    def test_a_team_owning_an_unknown_service_is_refused(self) -> None:
        payload = _knowledge(teams=[_team(services=("cart", "phantom"))])
        with pytest.raises(ValueError, match="unknown service"):
            Knowledge.model_validate(payload)

    def test_a_runbook_covering_an_unknown_service_is_refused(self) -> None:
        payload = _knowledge(runbooks=[_runbook(covers=("phantom",))])
        with pytest.raises(ValueError, match="covers unknown service"):
            Knowledge.model_validate(payload)

    def test_a_service_citing_an_unknown_runbook_is_refused(self) -> None:
        payload = _knowledge(services=[_service(runbooks=["missing-runbook"])])
        with pytest.raises(ValueError, match="cites unknown runbook"):
            Knowledge.model_validate(payload)

    def test_a_remediation_citing_an_unknown_runbook_is_refused(self) -> None:
        payload = _knowledge(remediations=[_remediation(runbooks=["missing-runbook"])])
        with pytest.raises(ValueError, match="cites unknown runbook"):
            Knowledge.model_validate(payload)


class TestOwnershipIsTotalAndUnambiguous:
    def test_an_unowned_service_is_refused(self) -> None:
        """A page that goes nowhere is worse than no page at all."""
        payload = _knowledge(
            services=[_service(), _service("payment", owning_team="Storefront")],
            teams=[_team(services=("cart",))],
        )
        with pytest.raises(ValueError, match="owned by no team"):
            Knowledge.model_validate(payload)

    def test_a_service_owned_twice_is_refused(self) -> None:
        """Two teams each assuming the other has it."""
        payload = _knowledge(
            teams=[_team(), _team("Payments", services=("cart",))],
        )
        with pytest.raises(ValueError, match="owned by 2 teams"):
            Knowledge.model_validate(payload)

    def test_a_team_owning_nothing_is_refused(self) -> None:
        payload = _knowledge(teams=[_team(), _team("Empty", services=())])
        with pytest.raises(ValueError, match="owns no services"):
            Knowledge.model_validate(payload)


class TestRemediationSafety:
    def test_there_must_be_a_fallback_action(self) -> None:
        """Without it an incident with no safe action has no proposal at all."""
        payload = _knowledge(
            remediations=[_remediation(id="restart", kind="restart_service", reversible=True)]
        )
        with pytest.raises(ValueError, match="no fallback action"):
            Knowledge.model_validate(payload)

    def test_an_irreversible_action_must_require_approval(self) -> None:
        """The one rule in the file whose violation cannot be undone."""
        payload = _knowledge(
            remediations=[
                _remediation(),
                _remediation(
                    id="restart",
                    kind="restart_service",
                    reversible=False,
                    requires_approval=False,
                ),
            ]
        )
        with pytest.raises(ValueError, match="irreversible and must require approval"):
            Knowledge.model_validate(payload)

    def test_an_irreversible_action_with_approval_is_allowed(self) -> None:
        payload = _knowledge(
            remediations=[
                _remediation(),
                _remediation(
                    id="restart",
                    kind="restart_service",
                    reversible=False,
                    requires_approval=True,
                ),
            ]
        )
        facts = Knowledge.model_validate(payload)
        assert len(facts.remediations) == 2

    def test_the_action_space_is_an_allowlist(self) -> None:
        """An unknown kind is refused rather than carried through.

        SPEC.md Section 7 puts remediation behind an approval gate, and an
        open ended action space would make that gate the only thing between
        a language model and a production change.
        """
        payload = _knowledge(remediations=[_remediation(), _remediation(id="x", kind="rm-rf")])
        with pytest.raises(ValueError):
            Knowledge.model_validate(payload)

    def test_the_allowlist_has_exactly_three_kinds(self) -> None:
        assert {kind.value for kind in RemediationKind} == {
            "disable_feature_flag",
            "restart_service",
            "page_owning_team",
        }


class TestStrictness:
    def test_an_unknown_field_is_refused(self) -> None:
        """A typo in a key would otherwise be silently ignored."""
        payload = _knowledge(services=[_service(oops="typo")])
        with pytest.raises(ValueError):
            Knowledge.model_validate(payload)

    def test_a_service_name_must_be_kebab_case(self) -> None:
        payload = _knowledge(
            services=[_service("Cart_Service")], teams=[_team(services=("Cart_Service",))]
        )
        with pytest.raises(ValueError):
            Knowledge.model_validate(payload)


class TestRunbookParsing:
    def test_frontmatter_and_body_are_both_read(self, tmp_path: Path) -> None:
        path = tmp_path / "cart-errors.md"
        path.write_text(
            "---\n"
            "id: cart-errors\n"
            "title: Cart errors\n"
            "covers: [cart]\n"
            "symptoms: [errors]\n"
            "updated: '2026-09-24'\n"
            "---\n"
            "## Symptoms\n\nErrors on add to cart.\n",
            encoding="utf-8",
        )
        runbook = parse_runbook(path)
        assert runbook.id == "cart-errors"
        assert "Errors on add to cart" in runbook.body

    def test_a_runbook_without_frontmatter_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "bare.md"
        path.write_text("# Just a heading\n", encoding="utf-8")
        with pytest.raises(KnowledgeError, match="no YAML frontmatter"):
            parse_runbook(path)

    def test_invalid_frontmatter_names_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.md"
        path.write_text("---\nid: [unclosed\n---\nbody\n", encoding="utf-8")
        with pytest.raises(KnowledgeError, match=r"broken\.md"):
            parse_runbook(path)

    def test_frontmatter_that_is_not_a_mapping_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "list.md"
        path.write_text("---\n- one\n- two\n---\nbody\n", encoding="utf-8")
        with pytest.raises(KnowledgeError, match="not a mapping"):
            parse_runbook(path)

    def test_missing_required_frontmatter_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "partial.md"
        path.write_text("---\nid: partial\n---\nbody\n", encoding="utf-8")
        with pytest.raises(KnowledgeError, match="invalid"):
            parse_runbook(path)


class TestLoading:
    def test_a_missing_directory_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(KnowledgeError, match="does not exist"):
            load_knowledge(tmp_path / "nowhere")

    def test_a_missing_runbooks_directory_says_so(self, tmp_path: Path) -> None:
        import yaml

        (tmp_path / "services.yaml").write_text(yaml.safe_dump([_service()]), encoding="utf-8")
        (tmp_path / "teams.yaml").write_text(yaml.safe_dump([_team()]), encoding="utf-8")
        (tmp_path / "remediations.yaml").write_text(
            yaml.safe_dump([_remediation()]), encoding="utf-8"
        )
        with pytest.raises(KnowledgeError, match="runbooks directory"):
            load_knowledge(tmp_path)

    def test_a_file_that_is_not_a_list_says_which(self, tmp_path: Path) -> None:
        import yaml

        (tmp_path / "services.yaml").write_text("just a string\n", encoding="utf-8")
        (tmp_path / "teams.yaml").write_text(yaml.safe_dump([_team()]), encoding="utf-8")
        (tmp_path / "remediations.yaml").write_text(
            yaml.safe_dump([_remediation()]), encoding="utf-8"
        )
        (tmp_path / "runbooks").mkdir()
        with pytest.raises(KnowledgeError, match=r"services\.yaml"):
            load_knowledge(tmp_path)

    def test_a_top_level_key_is_accepted_as_well_as_a_bare_list(self, tmp_path: Path) -> None:
        """Both shapes read the same, since either is a reasonable thing to write."""
        import yaml

        (tmp_path / "services.yaml").write_text(
            yaml.safe_dump({"services": [_service()]}), encoding="utf-8"
        )
        (tmp_path / "teams.yaml").write_text(yaml.safe_dump({"teams": [_team()]}), encoding="utf-8")
        (tmp_path / "remediations.yaml").write_text(
            yaml.safe_dump({"remediations": [_remediation()]}), encoding="utf-8"
        )
        runbooks = tmp_path / "runbooks"
        runbooks.mkdir()
        (runbooks / "cart-errors.md").write_text(
            "---\nid: cart-errors\ntitle: Cart errors\ncovers: [cart]\n"
            "symptoms: [errors]\nupdated: '2026-09-24'\n---\nbody\n",
            encoding="utf-8",
        )
        facts = load_knowledge(tmp_path)
        assert facts.service_names == {"cart"}

    def test_a_cross_reference_failure_names_the_problem(self, tmp_path: Path) -> None:
        import yaml

        (tmp_path / "services.yaml").write_text(
            yaml.safe_dump([_service(owning_team="Ghosts")]), encoding="utf-8"
        )
        (tmp_path / "teams.yaml").write_text(yaml.safe_dump([_team()]), encoding="utf-8")
        (tmp_path / "remediations.yaml").write_text(
            yaml.safe_dump([_remediation()]), encoding="utf-8"
        )
        (tmp_path / "runbooks").mkdir()
        with pytest.raises(KnowledgeError, match="unknown team"):
            load_knowledge(tmp_path)
