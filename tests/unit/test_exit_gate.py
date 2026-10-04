"""Tests for the exit gate's six checks.

SPEC.md Section 17 Phase 7 names four rejections by hand, and each has a class
here: a fabricated number, a fabricated evidence id, a timeline out of order, and
a high-confidence single-signal report.

The reviewer focus for that phase is that the gate cannot be bypassed, so the
last classes check that all six checks always run, that an absent re-execution is
not silently treated as a pass, and that a bundle whose hash moved is refused
even though a live record that shifted is allowed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from firebreak.agent.gates import (
    HIGH_CONFIDENCE,
    CheckName,
    check_re_execution,
    run_all_checks,
    run_exit_gate,
)
from firebreak.agent.state import (
    CitedNumber,
    Claim,
    ClaimType,
    Confidence,
    Hypothesis,
    Notebook,
    Report,
)
from firebreak.tools.evidence import (
    BackendFingerprint,
    BackendMode,
    EvidenceKind,
    EvidenceRecord,
    Fact,
    TimeRange,
    build_record,
)

WINDOW = TimeRange(start=datetime(2025, 1, 1, tzinfo=UTC), end=datetime(2025, 1, 1, 1, tzinfo=UTC))
FABRICATED = "ev_metric_deadbeefdead"


def test_publication_abstains_when_root_conflicts_with_notebook():
    item = record()
    held = store(item)
    outcome = run_exit_gate(
        report(
            root_cause_service="cart", claims=(Claim(text="cart broke", evidence_ids=(item.id,)),)
        ),
        notebook(),
        held,
        held,
    )
    assert outcome.passed
    assert outcome.report.abstained


def test_publication_lowers_single_signal_confidence():
    item = record()
    held = store(item)
    outcome = run_exit_gate(
        report(
            confidence=Confidence.HIGH,
            claims=(Claim(text="payment broke", evidence_ids=(item.id,)),),
        ),
        notebook(),
        held,
        held,
    )
    assert outcome.passed
    assert outcome.report.confidence is not Confidence.HIGH


def test_removing_last_claim_also_removes_root_cause():
    outcome = run_exit_gate(
        report(claims=(Claim(text="payment broke", evidence_ids=(FABRICATED,)),)),
        notebook(),
        {},
        {},
    )
    assert outcome.passed
    assert outcome.report.abstained


def test_numeric_prose_requires_structured_provenance():
    item = record(facts=(Fact(field="score", value=12.5, unit="z"),))
    held = store(item)
    for text in (
        "payment scored 999999 deviations",
        "payment scored 999999.123.",
        "payment scored 1e6 deviations",
        "payment affected twenty services",
    ):
        outcome = run_exit_gate(
            report(claims=(Claim(text=text, evidence_ids=(item.id,)),)),
            notebook(),
            held,
            held,
        )
        assert not outcome.report.claims
        assert outcome.report.abstained


def test_numeric_units_must_match_the_fact():
    item = record(facts=(Fact(field="latency", value=12.5, unit="ms"),))
    held = store(item)
    claim = Claim(
        text="latency reached 12.5 seconds",
        evidence_ids=(item.id,),
        numbers=(CitedNumber(value=12.5, unit="seconds", evidence_id=item.id, field="latency"),),
    )
    assert not run_exit_gate(report(claims=(claim,)), notebook(), held, held).report.claims


def record(
    kind=EvidenceKind.METRIC,
    query="list_anomalies",
    facts=(),
    rows=(),
    mode=BackendMode.BUNDLE,
) -> EvidenceRecord:
    return build_record(
        kind=kind,
        query=query,
        parameters={},
        window=WINDOW,
        fingerprint=BackendFingerprint(mode=mode, identity="inc_000000000000"),
        rows=list(rows),
        facts=list(facts),
    )


def store(*records: EvidenceRecord) -> dict[str, EvidenceRecord]:
    return {item.id: item for item in records}


def notebook(service="payment", support=3) -> Notebook:
    return Notebook(
        hypotheses=(
            Hypothesis(
                id="h1",
                service=service,
                statement=f"{service} is the cause",
                supporting_evidence=tuple(f"ev_metric_{i:012x}" for i in range(support)),
            ),
        )
    )


def report(root_cause_service="payment", confidence=Confidence.MEDIUM, claims=()) -> Report:
    return Report(
        incident_id="inc_000000000000",
        root_cause_service=root_cause_service,
        confidence=confidence,
        claims=claims,
    )


def result(results, name):
    return next(r for r in results if r.name is name)


class TestAFabricatedNumberIsRejected:
    """SPEC.md Section 17 Phase 7, by name.

    The most dangerous fabrication, because a wrong number inside a correct
    sentence is what an operator acts on.
    """

    def test_a_number_the_evidence_does_not_report_fails(self) -> None:
        metric = record(facts=(Fact(field="anomaly_score", value=12.5, unit="z"),))
        claim = Claim(
            text="payment scored 99.9 robust deviations",
            claim_type=ClaimType.MEASUREMENT,
            evidence_ids=(metric.id,),
            numbers=(
                CitedNumber(value=99.9, unit="z", evidence_id=metric.id, field="anomaly_score"),
            ),
        )
        held = store(metric)
        numbers = result(
            run_all_checks(report(claims=(claim,)), notebook(), held, held, 2), CheckName.NUMBERS
        )
        assert not numbers.passed
        assert "99.9" in numbers.detail and "12.5" in numbers.detail
        assert numbers.failed_claims == (0,)

    def test_the_true_number_passes(self) -> None:
        metric = record(facts=(Fact(field="anomaly_score", value=12.5, unit="z"),))
        claim = Claim(
            text="payment scored 12.5 robust deviations",
            claim_type=ClaimType.MEASUREMENT,
            evidence_ids=(metric.id,),
            numbers=(
                CitedNumber(value=12.5, unit="z", evidence_id=metric.id, field="anomaly_score"),
            ),
        )
        held = store(metric)
        assert result(
            run_all_checks(report(claims=(claim,)), notebook(), held, held, 2), CheckName.NUMBERS
        ).passed

    def test_a_field_the_evidence_does_not_report_fails(self) -> None:
        """A plausible field name is as easy to invent as a number."""
        metric = record(facts=(Fact(field="anomaly_score", value=12.5, unit="z"),))
        claim = Claim(
            text="payment's p99 reached 12.5 seconds",
            claim_type=ClaimType.MEASUREMENT,
            evidence_ids=(metric.id,),
            numbers=(
                CitedNumber(value=12.5, unit="s", evidence_id=metric.id, field="p99_latency"),
            ),
        )
        held = store(metric)
        numbers = result(
            run_all_checks(report(claims=(claim,)), notebook(), held, held, 2), CheckName.NUMBERS
        )
        assert not numbers.passed
        assert "does not report" in numbers.detail

    def test_a_count_must_match_exactly(self) -> None:
        """There is no such thing as 4.02 services."""
        topology = record(
            kind=EvidenceKind.TOPOLOGY,
            query="downstream_of",
            facts=(Fact(field="reached", value=4.0, unit="count"),),
        )
        claim = Claim(
            text="four services are affected",
            claim_type=ClaimType.BLAST_RADIUS,
            evidence_ids=(topology.id,),
            numbers=(
                CitedNumber(value=4.02, unit="count", evidence_id=topology.id, field="reached"),
            ),
        )
        held = store(topology)
        assert not result(
            run_all_checks(report(claims=(claim,)), notebook(), held, held, 2), CheckName.NUMBERS
        ).passed

    def test_a_rate_may_differ_within_one_percent(self) -> None:
        metric = record(facts=(Fact(field="error_rate", value=0.400, unit="ratio"),))
        claim = Claim(
            text="the error rate reached 0.4",
            claim_type=ClaimType.MEASUREMENT,
            evidence_ids=(metric.id,),
            numbers=(
                CitedNumber(value=0.4008, unit="ratio", evidence_id=metric.id, field="error_rate"),
            ),
        )
        held = store(metric)
        assert result(
            run_all_checks(report(claims=(claim,)), notebook(), held, held, 2), CheckName.NUMBERS
        ).passed

    def test_a_number_sourced_from_evidence_the_claim_does_not_cite_fails(self) -> None:
        """Otherwise a claim could borrow a number from anywhere."""
        cited = record(facts=(Fact(field="anomaly_score", value=1.0, unit="z"),))
        other = record(
            query="query_metric", facts=(Fact(field="anomaly_score", value=9.0, unit="z"),)
        )
        assert cited.id != other.id
        claim = Claim(
            text="payment scored 9",
            claim_type=ClaimType.MEASUREMENT,
            evidence_ids=(cited.id,),
            numbers=(
                CitedNumber(value=9.0, unit="z", evidence_id=other.id, field="anomaly_score"),
            ),
        )
        held = store(cited, other)
        numbers = result(
            run_all_checks(report(claims=(claim,)), notebook(), held, held, 2), CheckName.NUMBERS
        )
        assert not numbers.passed
        assert "does not cite" in numbers.detail


class TestAFabricatedEvidenceIdIsRejected:
    """The cheapest fabrication there is."""

    def test_a_claim_citing_an_invented_id_fails_coverage(self) -> None:
        held = store(record())
        claim = Claim(text="payment broke", evidence_ids=(FABRICATED,))
        coverage = result(
            run_all_checks(report(claims=(claim,)), notebook(), held, held, 2), CheckName.COVERAGE
        )
        assert not coverage.passed
        assert "unknown" in coverage.detail

    def test_a_claim_citing_nothing_fails_coverage(self) -> None:
        claims = (Claim(text="payment broke"),)
        coverage = result(
            run_all_checks(report(claims=claims), notebook(), {}, {}, 2), CheckName.COVERAGE
        )
        assert not coverage.passed
        assert "cites nothing" in coverage.detail

    def test_the_gate_removes_it_and_says_how_many(self) -> None:
        real = record()
        held = store(real)
        claims = (
            Claim(text="real", evidence_ids=(real.id,)),
            Claim(text="invented", evidence_ids=(FABRICATED,)),
        )
        outcome = run_exit_gate(report(claims=claims), notebook(), held, held)
        assert outcome.removed == 1
        assert [c.text for c in outcome.report.claims] == ["real"]
        assert outcome.notice == "1 statement could not be verified and was removed."


class TestATimelineOutOfOrderIsRejected:
    def test_claims_dated_backwards_fail_consistency(self) -> None:
        real = record()
        held = store(real)
        later = datetime(2025, 1, 1, 0, 30, tzinfo=UTC)
        claims = (
            Claim(
                text="the fault began",
                claim_type=ClaimType.TIMELINE,
                evidence_ids=(real.id,),
                at=later,
            ),
            Claim(
                text="the alert fired",
                claim_type=ClaimType.TIMELINE,
                evidence_ids=(real.id,),
                at=later - timedelta(minutes=10),
            ),
        )
        consistency = result(
            run_all_checks(report(claims=claims), notebook(), held, held, 2),
            CheckName.CONSISTENCY,
        )
        assert not consistency.passed
        assert "dated after" in consistency.detail
        assert consistency.failed_claims == (0,)

    def test_claims_in_order_pass(self) -> None:
        real = record()
        held = store(real)
        first = datetime(2025, 1, 1, 0, 10, tzinfo=UTC)
        claims = (
            Claim(text="a", claim_type=ClaimType.TIMELINE, evidence_ids=(real.id,), at=first),
            Claim(
                text="b",
                claim_type=ClaimType.TIMELINE,
                evidence_ids=(real.id,),
                at=first + timedelta(minutes=5),
            ),
        )
        assert result(
            run_all_checks(report(claims=claims), notebook(), held, held, 2),
            CheckName.CONSISTENCY,
        ).passed

    def test_an_undated_timeline_claim_does_not_constrain_the_order(self) -> None:
        """The check is on the timestamps, and a claim without one has none.

        Falling back to prose order here would make the check assert the very
        thing it exists to distrust.
        """
        real = record()
        held = store(real)
        claims = (
            Claim(text="a", claim_type=ClaimType.TIMELINE, evidence_ids=(real.id,)),
            Claim(
                text="b",
                claim_type=ClaimType.TIMELINE,
                evidence_ids=(real.id,),
                at=datetime(2025, 1, 1, 0, 5, tzinfo=UTC),
            ),
        )
        assert result(
            run_all_checks(report(claims=claims), notebook(), held, held, 2),
            CheckName.CONSISTENCY,
        ).passed

    def test_a_root_cause_the_notebook_disagrees_with_fails(self) -> None:
        """A report contradicting the reasoning it came from."""
        real = record()
        held = store(real)
        claim = Claim(text="cart broke", evidence_ids=(real.id,))
        consistency = result(
            run_all_checks(
                report(root_cause_service="cart", claims=(claim,)),
                notebook("payment"),
                held,
                held,
                2,
            ),
            CheckName.CONSISTENCY,
        )
        assert not consistency.passed
        assert "leading hypothesis is payment" in consistency.detail

    def test_a_blast_radius_asserted_from_prose_fails(self) -> None:
        """SPEC.md Section 6.9 check 4's third clause.

        The gate holds no graph client, so it checks provenance rather than
        recomputing the set: a blast radius has to come from a topology query.
        """
        metric = record()
        held = store(metric)
        claim = Claim(
            text="four downstream services were affected",
            claim_type=ClaimType.BLAST_RADIUS,
            evidence_ids=(metric.id,),
        )
        consistency = result(
            run_all_checks(report(claims=(claim,)), notebook(), held, held, 2),
            CheckName.CONSISTENCY,
        )
        assert not consistency.passed
        assert "cites no topology evidence" in consistency.detail

    def test_a_blast_radius_citing_the_graph_passes(self) -> None:
        topology = record(kind=EvidenceKind.TOPOLOGY, query="downstream_of")
        held = store(topology)
        claim = Claim(
            text="four downstream services were affected",
            claim_type=ClaimType.BLAST_RADIUS,
            evidence_ids=(topology.id,),
        )
        assert result(
            run_all_checks(report(claims=(claim,)), notebook(), held, held, 2),
            CheckName.CONSISTENCY,
        ).passed


class TestAHighConfidenceSingleSignalReportIsRejected:
    """A confident report on one signal is the shape of every plausible wrong answer."""

    def test_high_confidence_on_one_signal_type_fails(self) -> None:
        metric = record(kind=EvidenceKind.METRIC)
        held = store(metric)
        claim = Claim(text="payment broke", evidence_ids=(metric.id,))
        assert Confidence.HIGH.as_probability > HIGH_CONFIDENCE
        sanity = result(
            run_all_checks(
                report(confidence=Confidence.HIGH, claims=(claim,)), notebook(), held, held, 2
            ),
            CheckName.CONFIDENCE_SANITY,
        )
        assert not sanity.passed
        assert "only 1 signal type" in sanity.detail
        assert "metric" in sanity.detail

    def test_high_confidence_on_two_signal_types_passes(self) -> None:
        metric = record(kind=EvidenceKind.METRIC)
        trace = record(kind=EvidenceKind.TRACE, query="find_traces")
        held = store(metric, trace)
        claims = (
            Claim(text="metrics moved", evidence_ids=(metric.id,)),
            Claim(text="traces agree", evidence_ids=(trace.id,)),
        )
        assert result(
            run_all_checks(
                report(confidence=Confidence.HIGH, claims=claims), notebook(), held, held, 2
            ),
            CheckName.CONFIDENCE_SANITY,
        ).passed

    def test_two_records_of_the_same_kind_are_still_one_signal_type(self) -> None:
        """Otherwise the check counts queries, and two queries are cheap."""
        first = record(kind=EvidenceKind.METRIC, query="list_anomalies")
        second = record(kind=EvidenceKind.METRIC, query="query_metric")
        held = store(first, second)
        claims = (
            Claim(text="anomalies", evidence_ids=(first.id,)),
            Claim(text="the series", evidence_ids=(second.id,)),
        )
        assert not result(
            run_all_checks(
                report(confidence=Confidence.HIGH, claims=claims), notebook(), held, held, 2
            ),
            CheckName.CONFIDENCE_SANITY,
        ).passed

    def test_medium_confidence_on_one_signal_passes(self) -> None:
        """The rule is about confidence, not about breadth for its own sake."""
        metric = record()
        held = store(metric)
        claim = Claim(text="payment broke", evidence_ids=(metric.id,))
        assert result(
            run_all_checks(
                report(confidence=Confidence.MEDIUM, claims=(claim,)), notebook(), held, held, 2
            ),
            CheckName.CONFIDENCE_SANITY,
        ).passed


class TestAbstention:
    def test_a_weakly_supported_root_cause_becomes_insufficient_evidence(self) -> None:
        real = record()
        held = store(real)
        claim = Claim(text="payment might have broken", evidence_ids=(real.id,))
        outcome = run_exit_gate(
            report(claims=(claim,)), notebook("payment", support=1), held, held, min_support=2
        )
        assert outcome.report.root_cause_service is None
        assert outcome.report.confidence is None
        assert outcome.report.abstained
        # The claims survive: the ranked candidates and what was checked are what
        # make an abstention useful rather than a shrug.
        assert [c.text for c in outcome.report.claims] == ["payment might have broken"]

    def test_a_well_supported_root_cause_survives(self) -> None:
        real = record()
        held = store(real)
        claim = Claim(text="payment broke", evidence_ids=(real.id,))
        outcome = run_exit_gate(
            report(claims=(claim,)), notebook("payment", support=3), held, held, min_support=2
        )
        assert outcome.report.root_cause_service == "payment"
        assert outcome.passed

    def test_a_contested_hypothesis_loses_support(self) -> None:
        """Support is evidence for minus evidence against, so an objection counts.

        This is what stops the gate being satisfied by a hypothesis that
        collected three reasons and one refutation.
        """
        real = record()
        held = store(real)
        contested = Notebook(
            hypotheses=(
                Hypothesis(
                    id="h1",
                    service="payment",
                    statement="payment is the cause",
                    supporting_evidence=("a", "b"),
                    contradicting_evidence=("c",),
                ),
            )
        )
        claim = Claim(text="payment broke", evidence_ids=(real.id,))
        outcome = run_exit_gate(report(claims=(claim,)), contested, held, held, min_support=2)
        assert outcome.report.root_cause_service is None


class TestTheGateCannotBeBypassed:
    """SPEC.md Section 17 Phase 7's reviewer focus."""

    def test_an_absent_re_execution_is_not_a_pass(self) -> None:
        """A gate that treated "nothing was re-run" as "everything matched"
        would become a no-op the first time a caller forgot, and this is the
        check the others depend on."""
        real = record()
        claim = Claim(text="payment broke", evidence_ids=(real.id,))
        check = check_re_execution(report(claims=(claim,)), store(real), rerun=None)
        assert not check.passed
        assert "were not re-run" in check.detail

    def test_a_bundle_that_re_runs_differently_fails(self) -> None:
        """The only check that catches evidence which drifted or was never real."""
        original = record(rows=({"service": "payment", "score": 12.0},))
        drifted = record(rows=({"service": "payment", "score": 3.0},))
        claim = Claim(text="payment broke", evidence_ids=(original.id,))
        # Same id, different result: the id covers the question, not the answer,
        # which is exactly the case a hash comparison exists for.
        assert original.id == drifted.id
        assert original.result_sha256 != drifted.result_sha256
        check = check_re_execution(report(claims=(claim,)), store(original), store(drifted))
        assert not check.passed
        assert "did not re-run to the same result" in check.detail

    def test_live_evidence_may_shift_while_its_cited_numbers_hold(self) -> None:
        """SPEC.md Section 6.9 check 2's second half.

        A live query re-run a minute later has a minute of new samples, so a
        hash comparison alone would fail every report `make live` produces.
        """
        original = record(
            mode=BackendMode.LIVE,
            rows=({"t": 1},),
            facts=(Fact(field="error_rate", value=0.40, unit="ratio"),),
        )
        fresh = record(
            mode=BackendMode.LIVE,
            rows=({"t": 1}, {"t": 2}),
            facts=(Fact(field="error_rate", value=0.4009, unit="ratio"),),
        )
        assert original.result_sha256 != fresh.result_sha256
        claim = Claim(
            text="the error rate reached 0.4",
            claim_type=ClaimType.MEASUREMENT,
            evidence_ids=(original.id,),
            numbers=(
                CitedNumber(value=0.40, unit="ratio", evidence_id=original.id, field="error_rate"),
            ),
        )
        check = check_re_execution(report(claims=(claim,)), store(original), store(fresh))
        assert check.passed, check.detail
        assert "shifted" in check.detail

    def test_live_evidence_whose_cited_number_moved_too_far_fails(self) -> None:
        original = record(
            mode=BackendMode.LIVE,
            rows=({"t": 1},),
            facts=(Fact(field="error_rate", value=0.40, unit="ratio"),),
        )
        fresh = record(
            mode=BackendMode.LIVE,
            rows=({"t": 2},),
            facts=(Fact(field="error_rate", value=0.02, unit="ratio"),),
        )
        claim = Claim(
            text="the error rate reached 0.4",
            claim_type=ClaimType.MEASUREMENT,
            evidence_ids=(original.id,),
            numbers=(
                CitedNumber(value=0.40, unit="ratio", evidence_id=original.id, field="error_rate"),
            ),
        )
        check = check_re_execution(report(claims=(claim,)), store(original), store(fresh))
        assert not check.passed

    def test_live_evidence_nobody_quoted_a_number_from_cannot_drift(self) -> None:
        """Most evidence is cited as prose support and quotes no number.

        Letting the tolerance path cover those would mean any drift at all
        passed, and check 2 would stop meaning anything in live mode.
        """
        original = record(mode=BackendMode.LIVE, rows=({"t": 1},))
        fresh = record(mode=BackendMode.LIVE, rows=({"t": 2},))
        claim = Claim(text="there were no errors on payment", evidence_ids=(original.id,))
        check = check_re_execution(report(claims=(claim,)), store(original), store(fresh))
        assert not check.passed

    def test_all_six_checks_always_run(self) -> None:
        """A gate that skipped a check when it looked unnecessary would be a
        gate nobody could reason about."""
        outcome = run_exit_gate(report(), notebook(), {}, {})
        assert [r.name for r in outcome.results] == list(CheckName)

    def test_a_clean_report_passes_every_check(self) -> None:
        metric = record(facts=(Fact(field="anomaly_score", value=9.0, unit="z"),))
        trace = record(kind=EvidenceKind.TRACE, query="find_traces")
        held = store(metric, trace)
        claims = (
            Claim(
                text="payment scored 9",
                claim_type=ClaimType.MEASUREMENT,
                evidence_ids=(metric.id,),
                numbers=(
                    CitedNumber(value=9.0, unit="z", evidence_id=metric.id, field="anomaly_score"),
                ),
            ),
            Claim(text="traces agree", evidence_ids=(trace.id,)),
        )
        outcome = run_exit_gate(
            report(confidence=Confidence.HIGH, claims=claims), notebook(), held, held
        )
        assert outcome.passed, outcome.failed_checks
        assert outcome.removed == 0
        assert not outcome.repaired
        assert outcome.notice is None

    def test_repair_happens_once_and_is_recorded(self) -> None:
        real = record()
        held = store(real)
        claims = (
            Claim(text="real", evidence_ids=(real.id,)),
            Claim(text="invented", evidence_ids=(FABRICATED,)),
        )
        outcome = run_exit_gate(report(claims=claims), notebook(), held, held)
        assert outcome.repaired
        assert outcome.removed == 1
        assert outcome.passed

    def test_removing_claims_can_leave_a_confidence_that_no_longer_holds(self) -> None:
        """Repair re-runs every check, so the second pass sees the smaller report.

        A gate that repaired and published without re-checking would let a high
        confidence report lose its second signal type and keep the confidence.
        """
        metric = record()
        held = store(metric)
        claims = (
            Claim(text="metrics moved", evidence_ids=(metric.id,)),
            Claim(text="traces agree", evidence_ids=(FABRICATED,)),
        )
        outcome = run_exit_gate(
            report(confidence=Confidence.HIGH, claims=claims), notebook(), held, held
        )
        assert outcome.removed == 1
        assert outcome.passed
        assert outcome.report.confidence is Confidence.MEDIUM
        assert any(not r.passed for r in outcome.initial_results)

    def test_repair_can_be_switched_off_for_inspection(self) -> None:
        held = store(record())
        claims = (Claim(text="invented", evidence_ids=(FABRICATED,)),)
        outcome = run_exit_gate(report(claims=claims), notebook(), held, held, repair=False)
        assert not outcome.passed
        assert outcome.removed == 0
        assert not outcome.repaired
        assert CheckName.COVERAGE in outcome.failed_checks

    def test_an_empty_report_still_runs_the_checks_it_can(self) -> None:
        """Nothing cited is not the same as nothing checked."""
        outcome = run_exit_gate(report(claims=()), notebook(), {}, {})
        assert result(outcome.results, CheckName.COVERAGE).passed
        assert result(outcome.results, CheckName.RE_EXECUTION).passed
        assert result(outcome.results, CheckName.ABSTENTION).passed


class TestTheOutcomeIsReportable:
    def test_it_serialises_every_check(self) -> None:
        outcome = run_exit_gate(report(), notebook(), {}, {})
        payload = outcome.as_dict()
        checks = payload["checks"]
        assert len(checks) == len(CheckName)
        for check in checks:
            assert set(check) == {"check", "passed", "detail", "failed_claims"}
        assert payload["removed_claims"] == 0
        assert payload["passed"] is outcome.passed

    def test_the_notice_is_plural_aware(self) -> None:
        held = store(record())
        claims = tuple(Claim(text=f"invented {i}", evidence_ids=(FABRICATED,)) for i in range(2))
        outcome = run_exit_gate(report(claims=claims), notebook(), held, held)
        assert outcome.notice == "2 statements could not be verified and were removed."

    def test_no_notice_when_nothing_was_removed(self) -> None:
        assert run_exit_gate(report(), notebook(), {}, {}).notice is None
