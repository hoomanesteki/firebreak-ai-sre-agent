"""Tests for incident memory, its retrieval, and feedback.

The train-only invariant has its own file under `tests/leakage/`, because it is a
leakage control rather than a feature. What is tested here is whether memory is
useful and whether it stays advisory, which are the two ways it can be wrong without
leaking anything: a retrieval that matches on the wrong thing, and an entry that
arrives with enough weight to carry a report.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from firebreak.memory.feedback import (
    Feedback,
    FeedbackStore,
    Verdict,
    promote,
)
from firebreak.memory.store import (
    MINIMUM_SIMILARITY,
    TOP_MATCHES,
    IncidentMemoryError,
    MemoryStore,
    entry_from_label,
    tokenise,
)

WHEN = datetime(2026, 1, 1, tzinfo=UTC)

CASES = (
    (
        "inc_payment",
        "payment",
        "error_injection",
        "payment error rate jumped to 40 percent and checkout reported failed charges",
        ("payment", "checkout", "frontend"),
    ),
    (
        "inc_shipping",
        "shipping",
        "latency",
        "shipping p99 latency rose tenfold and quote calls queued behind it",
        ("shipping", "quote"),
    ),
    (
        "inc_kafka",
        "checkout",
        "queue_lag",
        "kafka consumer lag grew steadily while accounting and fraud-detection fell behind",
        ("checkout", "accounting", "fraud-detection"),
    ),
    (
        "inc_cart",
        "cart",
        "health_check_failure",
        "cart readiness probe failed repeatedly and the pod restarted four times",
        ("cart",),
    ),
)


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    filled = MemoryStore(tmp_path / "memory.jsonl")
    for incident_id, service, fault, summary, services in CASES:
        filled.admit(
            entry_from_label(
                incident_id=incident_id,
                scenario_id=f"{service}-scenario",
                split="train",
                target_service=service,
                fault_class=fault,
                symptom_summary=summary,
                confirmed_by="owner",
                evidence_types=("metric", "log"),
                services_involved=services,
                confirmed_at=WHEN,
            )
        )
    return filled


class TestRetrievalFindsTheRightIncident:
    def test_a_matching_symptom_retrieves_its_incident_first(self, store: MemoryStore) -> None:
        matches = store.similar("error rate jumped on payment and charges failed at checkout")
        assert matches[0].entry.incident_id == "inc_payment"

    def test_latency_symptoms_retrieve_the_latency_incident(self, store: MemoryStore) -> None:
        matches = store.similar("p99 latency rose tenfold on shipping and quote queued")
        assert matches[0].entry.incident_id == "inc_shipping"

    def test_an_unrelated_query_retrieves_nothing(self, store: MemoryStore) -> None:
        """A memory entry sharing only a stopword is noise presented as a prior, and a
        prior is the most expensive kind of noise here."""
        assert store.similar("the moon landing was faked by a rogue satellite") == []

    def test_it_returns_at_most_three(self, store: MemoryStore) -> None:
        """Few enough that a wrong one is visibly a minority rather than a consensus."""
        matches = store.similar(
            "error rate latency lag probe payment shipping checkout cart kafka failed"
        )
        assert len(matches) <= TOP_MATCHES

    def test_shared_services_raise_a_match_above_a_closer_summary(self, store: MemoryStore) -> None:
        """Graph overlap, per SPEC.md Section 10.3: two incidents on the same services
        are more likely related."""
        without = store.similar("readiness probe failed and the pod restarted")
        with_services = store.similar(
            "readiness probe failed and the pod restarted", services=("cart",)
        )
        assert with_services[0].entry.incident_id == "inc_cart"
        assert with_services[0].similarity > without[0].similarity
        assert with_services[0].shared_services == ("cart",)

    def test_a_rare_word_decides_which_entry_wins(self, store: MemoryStore) -> None:
        """What rarity weighting actually buys, which is not what it first looked like.

        The score is normalised by the query, so a query whose every token is present
        scores 1.0 whether those tokens are rare or common. Rarity does not change
        that; it decides which entry a mixed query ranks first.

        "kafka" is in one summary of the four and "failed" is in two, so an entry
        matching on "kafka" alone outranks one matching on "failed" alone: 0.59 to
        0.41. Without the weighting the two would tie, and a query pairing a service
        name with a word like "failed" would be decided by whichever entry sorted
        first.
        """
        matches = store.similar("kafka failed")
        assert matches[0].entry.incident_id == "inc_kafka"
        assert matches[0].similarity > matches[1].similarity

    def test_a_fully_matched_query_scores_the_same_however_rare_its_words(
        self, store: MemoryStore
    ) -> None:
        """Stated because it is the surprising half of the above, and a reader who
        expected rarity to raise the absolute score would misread every number."""
        rare = store.similar("kafka consumer lag")
        common = store.similar("failed repeatedly")
        assert rare[0].similarity == pytest.approx(common[0].similarity)

    def test_the_order_is_deterministic(self, store: MemoryStore) -> None:
        """A retrieval that reshuffled would make pass^3 measure the shuffling, which
        is the same reason the candidate ranking breaks ties by name."""
        query = "error rate jumped and latency rose"
        first = [m.entry.incident_id for m in store.similar(query)]
        second = [m.entry.incident_id for m in store.similar(query)]
        assert first == second

    def test_nothing_below_the_floor_is_returned(self, store: MemoryStore) -> None:
        assert all(
            match.similarity >= MINIMUM_SIMILARITY for match in store.similar("payment error rate")
        )

    def test_an_empty_store_returns_nothing_rather_than_failing(self, tmp_path: Path) -> None:
        assert MemoryStore(tmp_path / "nothing.jsonl").similar("anything at all") == []

    def test_a_query_of_only_stopwords_retrieves_nothing(self, store: MemoryStore) -> None:
        assert store.similar("the and of was with it") == []


class TestMemoryStaysAdvisory:
    def test_an_entry_becomes_a_hypothesis_not_a_finding(self, store: MemoryStore) -> None:
        entry = store.entries()[0]
        hypothesis = entry.as_hypothesis(1)
        assert "lead to test" in hypothesis["statement"]
        assert "not evidence" in hypothesis["statement"]

    def test_the_wording_is_in_the_statement_not_left_to_a_prompt(self, store: MemoryStore) -> None:
        """A prompt saying "these are advisory" and a statement reading like a finding
        will lose that argument."""
        for entry in store.entries():
            assert "confirm it against this incident" in entry.as_hypothesis(1)["statement"]

    def test_it_names_which_evidence_types_mattered(self, store: MemoryStore) -> None:
        """The most useful thing memory carries: where to spend the first tool calls."""
        assert store.entries()[0].as_hypothesis(1)["evidence_types_that_mattered"] == "metric, log"


class TestTheStoreItself:
    def test_a_malformed_line_is_an_error_not_a_skip(self, tmp_path: Path) -> None:
        path = tmp_path / "memory.jsonl"
        path.write_text('{"not": "an entry"}\n')
        with pytest.raises(IncidentMemoryError, match="is not a memory entry"):
            MemoryStore(path).entries()

    def test_a_summary_too_short_to_retrieve_on_is_refused(self) -> None:
        with pytest.raises(ValueError):
            entry_from_label(
                incident_id="i",
                scenario_id="s",
                split="train",
                target_service="payment",
                fault_class="error",
                symptom_summary="bad",
                confirmed_by="owner",
            )

    def test_tokenising_drops_stopwords_and_keeps_service_names(self) -> None:
        tokens = tokenise("The payment-service and the frontend were on fire")
        assert "payment-service" in tokens
        assert "the" not in tokens
        assert "and" not in tokens


class TestFeedback:
    def test_a_report_marked_wrong_needs_a_correction(self) -> None:
        """A report marked wrong with no correction is a complaint rather than
        feedback, and it cannot become a test case."""
        with pytest.raises(ValueError, match="cannot become a test case"):
            Feedback(
                incident_id="inc_a",
                reviewer="owner",
                verdict=Verdict.INCORRECT,
                submitted_at=WHEN,
            )

    def test_a_correct_report_needs_no_correction(self) -> None:
        feedback = Feedback(
            incident_id="inc_a", reviewer="owner", verdict=Verdict.CORRECT, submitted_at=WHEN
        )
        assert feedback.promotable

    def test_partly_right_does_not_confirm_the_root_cause(self) -> None:
        """Naming the frontend when payment was broken is wrong, and it is not as wrong
        as naming the recommendation service. Memory admits confirmed incidents only,
        so treating partly as a confirmation would store a service that was merely in
        the call path."""
        feedback = Feedback(
            incident_id="inc_a",
            reviewer="owner",
            verdict=Verdict.PARTLY,
            true_root_cause="payment",
            submitted_at=WHEN,
        )
        assert not feedback.verdict.confirms_the_root_cause
        assert feedback.promotable

    def test_two_reviewers_are_both_kept(self, tmp_path: Path) -> None:
        """Two reviewers disagreeing is a fact worth keeping, and overwriting would
        hide exactly the ambiguous cases."""
        store = FeedbackStore(tmp_path / "feedback.jsonl")
        for reviewer, verdict, cause in (
            ("alice", Verdict.CORRECT, None),
            ("bob", Verdict.INCORRECT, "cart"),
        ):
            store.record(
                Feedback(
                    incident_id="inc_a",
                    reviewer=reviewer,
                    verdict=verdict,
                    true_root_cause=cause,
                    submitted_at=WHEN,
                )
            )
        assert len(store.for_incident("inc_a")) == 2

    def test_agreement_is_unknown_with_one_reviewer(self, tmp_path: Path) -> None:
        store = FeedbackStore(tmp_path / "feedback.jsonl")
        store.record(
            Feedback(
                incident_id="inc_a", reviewer="alice", verdict=Verdict.CORRECT, submitted_at=WHEN
            )
        )
        assert store.agreement("inc_a") is None

    def test_disagreement_is_reported(self, tmp_path: Path) -> None:
        """A contested incident should not become a labelled test case on one
        reviewer's word: the label would encode a disagreement as a fact."""
        store = FeedbackStore(tmp_path / "feedback.jsonl")
        store.record(
            Feedback(
                incident_id="inc_a", reviewer="alice", verdict=Verdict.CORRECT, submitted_at=WHEN
            )
        )
        store.record(
            Feedback(
                incident_id="inc_a",
                reviewer="bob",
                verdict=Verdict.INCORRECT,
                true_root_cause="cart",
                submitted_at=WHEN,
            )
        )
        assert store.agreement("inc_a") is False


class TestPromotion:
    def feedback(self, **overrides) -> Feedback:  # type: ignore[no-untyped-def]
        fields = {
            "incident_id": "inc_live_001",
            "reviewer": "owner",
            "verdict": Verdict.INCORRECT,
            "true_root_cause": "payment",
            "incorrect_claims": (2,),
            "submitted_at": WHEN,
        }
        fields.update(overrides)
        return Feedback(**fields)  # type: ignore[arg-type]

    def test_it_writes_a_label_with_the_confirmed_cause(self, tmp_path: Path) -> None:
        result = promote(self.feedback(), split="train", labels_root=tmp_path, bundle_exists=True)
        assert Path(result.label_path).is_file()
        assert result.runnable

    def test_a_missing_bundle_is_reported_rather_than_hidden(self, tmp_path: Path) -> None:
        """A promotion that wrote a label and did not snapshot a bundle produced a task
        nothing can run, and the caller has to know that now rather than at the next
        eval."""
        result = promote(self.feedback(), split="train", labels_root=tmp_path, bundle_exists=False)
        assert not result.runnable
        assert any("snapshot the bundle" in step for step in result.remaining_steps)

    def test_a_failure_with_no_named_claims_is_reported(self, tmp_path: Path) -> None:
        """A task that fails without saying which claim failed cannot show when it has
        been fixed."""
        result = promote(
            self.feedback(incorrect_claims=()),
            split="train",
            labels_root=tmp_path,
            bundle_exists=True,
        )
        assert any("which claims were wrong" in step for step in result.remaining_steps)

    def test_the_split_is_recorded_and_not_derived(self, tmp_path: Path) -> None:
        """Deriving it would put a real incident into a held-out split by accident,
        which is the accident the split assignment file exists to prevent."""
        result = promote(self.feedback(), split="train", labels_root=tmp_path, bundle_exists=True)
        assert result.split == "train"

    def test_unpromotable_feedback_cannot_be_built_in_the_first_place(self) -> None:
        """`promote` checks `promotable` as defence in depth and the check is
        unreachable, because `model_post_init` runs even for `model_construct`. The
        type is what prevents this, and that is worth knowing rather than testing an
        unreachable branch into looking covered.
        """
        with pytest.raises(ValueError, match="cannot become a test case"):
            Feedback.model_construct(
                incident_id="inc_a",
                reviewer="owner",
                verdict=Verdict.INCORRECT,
                true_root_cause=None,
                incorrect_claims=(),
                note="",
                submitted_at=WHEN,
            )
