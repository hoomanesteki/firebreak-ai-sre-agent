"""Every Console page works with seeded data and with none.

SPEC.md Section 17 Phase 11's second and third acceptance criteria: every page works with
seeded and empty data, and every report claim links to its evidence.

**The empty case is not an edge case here.** This repository is the empty case: eight
bundles of a hundred and fourteen, no credentials, no feedback, no approvals. A page that
rendered a blank table would be useless on a fresh clone and on the first day of a real
deployment, which are the two times somebody looks at a Console for the first time.

**Why the evidence link test is the one that matters.** The project's claim is that its
reports cite re-runnable evidence. A claim whose link opens nothing is that claim being
false in the place a reader would check it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from firebreak.web import app as web_app

REPO_ROOT = Path(__file__).resolve().parents[2]
CONSOLE_DIR = REPO_ROOT / "reports" / "console"

PAGES = ("/", "/evaluation", "/approvals", "/feedback")


@pytest.fixture
def seeded() -> TestClient:
    """The Console over this repository, which has reports and no approvals."""
    return TestClient(web_app.create_app())


@pytest.fixture
def empty(tmp_path: Path, monkeypatch) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    """The Console over nothing at all, which is a fresh clone.

    Every directory the app reads is pointed at an empty temporary path, so this is the
    real empty case rather than a mock of one.
    """
    for name in ("REPORTS_DIR", "BUNDLES_DIR", "CASSETTES_DIR"):
        monkeypatch.setattr(web_app, name, tmp_path / name.lower())
    yield TestClient(web_app.create_app())


def a_stored_report() -> tuple[str, dict[str, object]]:
    """One report with claims, so the evidence link tests have something to open."""
    for path in sorted(CONSOLE_DIR.glob("*.json")):
        stored = json.loads(path.read_text(encoding="utf-8"))
        if stored.get("claims"):
            return path.stem, stored
    pytest.skip("no stored report with claims; run make demo-offline")


class TestEveryPageWorksWithSeededData:
    @pytest.mark.parametrize("path", PAGES)
    def test_it_returns_a_page(self, seeded: TestClient, path: str) -> None:
        response = seeded.get(path)
        assert response.status_code == 200
        # The brand renders as markup (fire<span>break</span>), so the title is what
        # a page can be asserted on.
        assert "| Firebreak</title>" in response.text

    @pytest.mark.parametrize("path", PAGES)
    def test_it_says_what_the_page_is_for(self, seeded: TestClient, path: str) -> None:
        """This phase's other reviewer focus is clarity for a non-engineer, and a page
        that opens with a table and no sentence fails that before anything else."""
        response = seeded.get(path)
        assert 'class="lead"' in response.text

    def test_the_report_page_works(self, seeded: TestClient) -> None:
        name, _ = a_stored_report()
        assert seeded.get(f"/report/{name}").status_code == 200

    def test_the_investigation_page_works(self, seeded: TestClient) -> None:
        name, _ = a_stored_report()
        response = seeded.get(f"/live/{name}")
        assert response.status_code == 200
        assert "Budget" in response.text

    def test_the_incidents_page_lists_the_showcase(self, seeded: TestClient) -> None:
        response = seeded.get("/")
        assert "Showcase" in response.text
        assert "why in the showcase" in response.text

    def test_the_evaluation_page_labels_what_is_quotable(self, seeded: TestClient) -> None:
        """A figure from a tunable split says how well the system fits data it was
        developed against, and a page that showed it without saying so would be the
        project's own honesty rule broken in its own interface."""
        response = seeded.get("/evaluation")
        assert "not quotable as a result" in response.text

    def test_the_evaluation_page_does_not_print_a_cost_of_zero(self, seeded: TestClient) -> None:
        """Zero tokens means no model ran. Showing 0.000000 USD would say the system is
        free rather than unmeasured."""
        response = seeded.get("/evaluation")
        assert "not measured" in response.text


class TestEveryPageWorksWithNoData:
    @pytest.mark.parametrize("path", PAGES)
    def test_it_still_returns_a_page(self, empty: TestClient, path: str) -> None:
        response = empty.get(path)
        assert response.status_code == 200

    @pytest.mark.parametrize("path", PAGES)
    def test_it_says_what_is_missing_and_what_would_fill_it(
        self, empty: TestClient, path: str
    ) -> None:
        """A blank table is useless on a fresh clone and on day one of a real
        deployment, which are the two times somebody first opens a Console."""
        response = empty.get(path)
        assert "What would fill it" in response.text
        assert "make " in response.text, "the page should name a command"

    def test_a_report_that_does_not_exist_explains_itself(self, empty: TestClient) -> None:
        response = empty.get("/report/inc_000000000000")
        assert response.status_code == 200
        assert "No stored report" in response.text
        assert "make demo-offline" in response.text

    def test_a_timeline_that_does_not_exist_explains_itself(self, empty: TestClient) -> None:
        response = empty.get("/live/inc_000000000000")
        assert response.status_code == 200
        assert "No timeline" in response.text

    def test_no_page_renders_a_traceback(self, empty: TestClient) -> None:
        """`read_json` returns None rather than raising, because a stack trace in a
        browser is the least useful way to say a file is missing."""
        for path in (*PAGES, "/report/inc_000000000000", "/live/inc_000000000000"):
            assert "Traceback" not in empty.get(path).text


class TestEveryClaimLinksToItsEvidence:
    """SPEC.md Section 17 Phase 11's third acceptance criterion, by name."""

    def test_every_claim_renders_a_link_for_each_citation(self, seeded: TestClient) -> None:
        name, stored = a_stored_report()
        page = seeded.get(f"/report/{name}").text
        claims = stored["claims"]
        assert isinstance(claims, list)
        for claim in claims:
            for reference in claim["evidence_ids"]:
                assert f"/evidence/{name}/{reference}" in page

    def test_every_link_opens_the_record_it_names(self, seeded: TestClient) -> None:
        """A claim whose link opens nothing is the project's central claim being false in
        the place a reader would check it."""
        name, stored = a_stored_report()
        claims = stored["claims"]
        assert isinstance(claims, list)
        opened = 0
        for claim in claims:
            for reference in claim["evidence_ids"]:
                response = seeded.get(f"/evidence/{name}/{reference}")
                assert response.status_code == 200, f"{reference} does not open"
                opened += 1
        assert opened, "the report under test cites nothing, so this proves nothing"

    def test_the_record_carries_the_query_and_the_window(self, seeded: TestClient) -> None:
        """Not a rendering of the evidence: the query, window and fingerprint that
        produced it, so a reader can re-run it."""
        name, stored = a_stored_report()
        claims = stored["claims"]
        assert isinstance(claims, list)
        reference = claims[0]["evidence_ids"][0]
        record = seeded.get(f"/evidence/{name}/{reference}").json()
        assert record["query"]
        assert record["window"]["start"]
        assert record["fingerprint"]["identity"]
        assert "result_sha256" in record

    def test_the_record_carries_how_to_re_run_it(self, seeded: TestClient) -> None:
        """The tool and its validated arguments, which the registry stamps on. Without
        them the exit gate's re-execution check could not run and neither could a
        reader."""
        name, stored = a_stored_report()
        claims = stored["claims"]
        assert isinstance(claims, list)
        reference = claims[0]["evidence_ids"][0]
        record = seeded.get(f"/evidence/{name}/{reference}").json()
        assert record["tool"]
        assert "arguments" in record

    def test_an_invented_evidence_id_is_a_404_not_an_empty_panel(self, seeded: TestClient) -> None:
        """A citation nobody can open is the failure the exit gate exists to prevent, so
        it is worth a status code rather than a blank region."""
        name, _ = a_stored_report()
        response = seeded.get(f"/evidence/{name}/ev_metric_deadbeefdead")
        assert response.status_code == 404
        assert "no evidence" in response.json()["error"]


class TestTheConsoleShowsTheGateAndTheAbstentions:
    def test_the_report_page_shows_all_six_gate_checks(self, seeded: TestClient) -> None:
        """Which check failed is what tells a reader what to distrust, so a verdict alone
        would be less useful than the list."""
        name, _ = a_stored_report()
        page = seeded.get(f"/report/{name}").text
        for check in (
            "coverage",
            "re_execution",
            "numbers",
            "consistency",
            "confidence_sanity",
            "abstention",
        ):
            assert check in page

    def test_an_abstention_is_shown_as_such_rather_than_as_blank(self, seeded: TestClient) -> None:
        abstained = [
            path.stem
            for path in sorted(CONSOLE_DIR.glob("*.json"))
            if json.loads(path.read_text(encoding="utf-8"))["root_cause_service"] is None
        ]
        if not abstained:
            pytest.skip("no abstaining report stored")
        page = seeded.get(f"/report/{abstained[0]}").text
        assert "insufficient evidence" in page

    def test_the_approvals_page_says_it_cannot_approve(self, seeded: TestClient) -> None:
        """Giving this page a button that called the approval service would put the
        decision and the credential back in one process, which is the arrangement the
        whole design avoids."""
        page = seeded.get("/approvals").text
        assert "What this page cannot do" in page


class TestTheStoredViewIsNotRecomputed:
    def test_the_payload_carries_the_evidence_it_needs(self) -> None:
        """The Console holds no backend, so the records travel with the report."""
        name, stored = a_stored_report()
        evidence = stored["evidence"]
        assert isinstance(evidence, dict)
        assert evidence, f"{name} stored no evidence"

    def test_it_carries_no_ground_truth(self) -> None:
        """`firebreak.web` is an agent package as far as `config/leakage.yaml` is
        concerned, and a Console page is the most public thing in the system."""
        for path in sorted(CONSOLE_DIR.glob("*.json")):
            text = path.read_text(encoding="utf-8")
            for leaked in ("target_service", "fault_class", "scenario_id"):
                assert leaked not in text, f"{path.name} carries {leaked}"
