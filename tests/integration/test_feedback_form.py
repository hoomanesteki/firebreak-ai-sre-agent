"""The Console's one write path.

Phase 11 recorded the absence of this form as "a gap rather than a decision": the store and
its rules existed and tested, and nothing let a person use them. This closes it.

**Why a write path here and not on the Approvals page.** Executing a remediation needs a
credential, and the design keeps the credential in a separate service, so a button here would
undo that. Recording what a human thought of a report needs no credential. The two pages differ
for a reason rather than by accident, and both say so on the page.

**The rule the form must not re-implement.** `Feedback` refuses a verdict of `partly` or
`incorrect` with no stated cause, because a report marked wrong with no correction cannot become
an evaluation task, which is the whole point of collecting it. The handler turns a form into a
model and the model's complaint into a sentence; it does not check the rule itself, because two
definitions of a rule disagree eventually.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from firebreak.memory import feedback as feedback_module
from firebreak.memory.feedback import Feedback, FeedbackStore, Verdict
from firebreak.web import app as web_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A Console whose feedback goes to a temporary file.

    Pointed away from `reports/feedback/` deliberately: a test suite that wrote real feedback
    would put a reviewer's answer into the repository every time it ran.
    """
    store = tmp_path / "feedback" / "feedback.jsonl"
    store.parent.mkdir(parents=True)
    monkeypatch.setattr(feedback_module, "FEEDBACK_PATH", store)
    monkeypatch.setattr(web_app, "REPORTS_DIR", tmp_path)
    yield TestClient(web_app.create_app())


def stored(client: TestClient) -> list[dict[str, object]]:
    path = feedback_module.FEEDBACK_PATH
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def reason_from(response: object) -> str:
    location = getattr(response, "headers", {}).get("location", "")
    return unquote(location.split("error=", 1)[1]) if "error=" in location else ""


class TestAnAnswerIsRecorded:
    def test_a_correct_verdict_needs_no_cause(self, client: TestClient) -> None:
        response = client.post(
            "/feedback",
            data={"incident_id": "inc_000000000001", "reviewer": "hooman", "verdict": "correct"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        records = stored(client)
        assert len(records) == 1
        assert records[0]["verdict"] == "correct"
        assert records[0]["true_root_cause"] is None

    def test_a_wrong_verdict_with_a_cause_is_recorded(self, client: TestClient) -> None:
        client.post(
            "/feedback",
            data={
                "incident_id": "inc_000000000002",
                "reviewer": "hooman",
                "verdict": "incorrect",
                "true_root_cause": "payment",
                "note": "it blamed the frontend",
            },
            follow_redirects=False,
        )
        record = stored(client)[0]
        assert record["verdict"] == "incorrect"
        assert record["true_root_cause"] == "payment"
        assert record["note"] == "it blamed the frontend"

    def test_claim_indices_are_parsed_from_one_comma_separated_field(
        self, client: TestClient
    ) -> None:
        client.post(
            "/feedback",
            data={
                "incident_id": "inc_000000000003",
                "reviewer": "hooman",
                "verdict": "partly",
                "true_root_cause": "cart",
                "incorrect_claims": "3, 1, 1",
            },
            follow_redirects=False,
        )
        # Sorted and de-duplicated, because a reviewer typing a number twice meant it once.
        assert stored(client)[0]["incorrect_claims"] == [1, 3]

    def test_unparseable_claim_indices_do_not_lose_the_answer(self, client: TestClient) -> None:
        """The claim list is the least important field on the form. A stray comma should not
        cost a reviewer a verdict and a cause that were both right."""
        client.post(
            "/feedback",
            data={
                "incident_id": "inc_000000000004",
                "reviewer": "hooman",
                "verdict": "incorrect",
                "true_root_cause": "payment",
                "incorrect_claims": "two, , 4",
            },
            follow_redirects=False,
        )
        records = stored(client)
        assert len(records) == 1
        assert records[0]["incorrect_claims"] == [4]

    def test_it_redirects_rather_than_rendering(self, client: TestClient) -> None:
        """303 and a redirect, so a reload shows the answer instead of recording it twice."""
        response = client.post(
            "/feedback",
            data={"incident_id": "inc_000000000005", "reviewer": "hooman", "verdict": "correct"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "saved=inc_000000000005" in response.headers["location"]

    def test_a_second_answer_is_kept_rather_than_overwriting(self, client: TestClient) -> None:
        """Two reviewers disagreeing is the signal, and `agreement()` needs both to say
        anything at all."""
        for reviewer, verdict, cause in (
            ("first", "correct", ""),
            ("second", "incorrect", "payment"),
        ):
            client.post(
                "/feedback",
                data={
                    "incident_id": "inc_000000000006",
                    "reviewer": reviewer,
                    "verdict": verdict,
                    "true_root_cause": cause,
                },
                follow_redirects=False,
            )
        records = stored(client)
        assert len(records) == 2
        assert {record["reviewer"] for record in records} == {"first", "second"}


class TestTheStoresRuleIsEnforcedAndExplained:
    def test_a_wrong_verdict_with_no_cause_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/feedback",
            data={"incident_id": "inc_000000000007", "reviewer": "hooman", "verdict": "incorrect"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert stored(client) == []

    def test_the_refusal_explains_why_the_rule_exists(self, client: TestClient) -> None:
        """Not "invalid input". The model's own sentence says a report marked wrong with no
        correction cannot become a test case, which tells a reviewer what to do next."""
        response = client.post(
            "/feedback",
            data={"incident_id": "inc_000000000008", "reviewer": "hooman", "verdict": "partly"},
            follow_redirects=False,
        )
        reason = reason_from(response)
        assert "cannot become a test case" in reason

    def test_the_refusal_is_not_a_link_to_pydantic(self, client: TestClient) -> None:
        """The obvious implementation reads the last line of `str(problem)`, which is a link to
        pydantic's documentation. It did, until `explain` existed."""
        response = client.post(
            "/feedback",
            data={"incident_id": "inc_000000000009", "reviewer": "hooman", "verdict": "incorrect"},
            follow_redirects=False,
        )
        reason = reason_from(response)
        assert "errors.pydantic.dev" not in reason
        assert "Value error," not in reason

    def test_an_empty_reviewer_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/feedback",
            data={"incident_id": "inc_000000000010", "reviewer": "", "verdict": "correct"},
            follow_redirects=False,
        )
        assert stored(client) == []
        assert reason_from(response)

    def test_a_verdict_outside_the_vocabulary_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/feedback",
            data={"incident_id": "inc_000000000011", "reviewer": "hooman", "verdict": "maybe"},
            follow_redirects=False,
        )
        assert stored(client) == []
        assert "Verdict" in reason_from(response)

    def test_a_refused_answer_is_shown_back_to_the_reviewer(self, client: TestClient) -> None:
        page = client.get("/feedback?error=the+stated+reason").text
        assert "Not recorded" in page
        assert "the stated reason" in page


class TestTheFormRendersInEveryState:
    """The bug this class exists for: the form and its notices sat inside `{% if awaiting %}`,
    so once every incident had an answer both disappeared. That is the same mistake the
    Approvals page had in Phase 11, where the paragraph explaining the absent button only
    rendered when the queue had records."""

    def test_the_form_renders_with_incidents_awaiting(self, client: TestClient) -> None:
        assert 'form method="post"' in client.get("/feedback").text

    def test_the_form_renders_when_every_incident_has_an_answer(self, client: TestClient) -> None:
        for incident in web_app.showcase_incidents():
            FeedbackStore(feedback_module.FEEDBACK_PATH).record(
                Feedback(
                    incident_id=incident["bundle_id"],
                    reviewer="first",
                    verdict=Verdict.CORRECT,
                    submitted_at=datetime.now(UTC),
                )
            )
        page = client.get("/feedback").text
        assert 'form method="post"' in page
        assert "second opinion" in page
        assert "already answered once" in page

    def test_the_form_renders_with_no_incidents_at_all(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(web_app, "showcase_incidents", lambda: [])
        page = client.get("/feedback").text
        assert 'form method="post"' in page

    def test_an_error_renders_even_with_nothing_awaiting(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure that made this worth a class: a refusal was invisible in exactly the
        state where somebody was most likely to be answering."""
        monkeypatch.setattr(web_app, "showcase_incidents", lambda: [])
        assert "Not recorded" in client.get("/feedback?error=a+reason").text

    def test_the_page_says_why_it_can_write_and_approvals_cannot(self, client: TestClient) -> None:
        page = client.get("/feedback").text
        assert "only write path" in page
        assert "credential" in page
