"""Tests for firebreak.tools.evidence."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from firebreak.tools.evidence import (
    INLINE_ROW_LIMIT,
    BackendFingerprint,
    BackendMode,
    EvidenceKind,
    EvidenceRecord,
    EvidenceStore,
    Fact,
    TimeRange,
    build_record,
    canonical_query,
    evidence_id,
    hash_rows,
)


def _window(**overrides) -> TimeRange:
    fields = {
        "start": datetime(2026, 1, 1, tzinfo=UTC),
        "end": datetime(2026, 1, 1, 0, 10, tzinfo=UTC),
    }
    fields.update(overrides)
    return TimeRange(**fields)


def _fingerprint(**overrides) -> BackendFingerprint:
    fields = {
        "mode": BackendMode.BUNDLE,
        "identity": "inc_0123456789ab",
        "format_version": 1,
    }
    fields.update(overrides)
    return BackendFingerprint(**fields)


def _build(rows=None, **overrides) -> EvidenceRecord:
    fields = {
        "kind": EvidenceKind.METRIC,
        "query": "avg(rate)",
        "parameters": {},
        "window": _window(),
        "fingerprint": _fingerprint(),
    }
    fields.update(overrides)
    return build_record(rows=rows if rows is not None else [{"value": 1.0}], **fields)


# --- TimeRange ---------------------------------------------------------


def test_time_range_rejects_end_before_start():
    with pytest.raises(ValueError, match="must end at or after it starts"):
        TimeRange(
            start=datetime(2026, 1, 1, 0, 10, tzinfo=UTC),
            end=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        )


def test_time_range_accepts_equal_start_and_end():
    instant = datetime(2026, 1, 1, tzinfo=UTC)

    window = TimeRange(start=instant, end=instant)

    assert window.duration_seconds == 0.0


def test_time_range_duration_seconds_is_the_span_between_start_and_end():
    window = _window()

    assert window.duration_seconds == 600.0


def test_time_range_canonical_is_stable_for_the_same_inputs():
    first = _window().canonical()
    second = _window().canonical()

    assert first == second
    assert first == "2026-01-01T00:00:00+00:00/2026-01-01T00:10:00+00:00"


# --- BackendFingerprint --------------------------------------------------


def test_backend_fingerprint_canonical_includes_mode_identity_and_format_version():
    fingerprint = BackendFingerprint(
        mode=BackendMode.BUNDLE, identity="inc_abc123", format_version=2
    )

    assert fingerprint.canonical() == "bundle:inc_abc123:v2"


# --- canonical_query -----------------------------------------------------


def test_canonical_query_is_stable_regardless_of_parameter_insertion_order():
    """This is what stops the same question from producing two different evidence ids."""
    ordered = canonical_query("select 1", {"a": 1, "b": 2})
    reordered = canonical_query("select 1", {"b": 2, "a": 1})

    assert ordered == reordered


# --- evidence_id -----------------------------------------------------------


def test_evidence_id_is_deterministic_for_the_same_inputs():
    window = _window()
    fingerprint = _fingerprint()

    first = evidence_id(EvidenceKind.METRIC, "q", {"a": 1}, window, fingerprint)
    second = evidence_id(EvidenceKind.METRIC, "q", {"a": 1}, window, fingerprint)

    assert first == second


def test_evidence_id_changes_when_the_query_changes():
    window = _window()
    fingerprint = _fingerprint()

    first = evidence_id(EvidenceKind.METRIC, "q1", {}, window, fingerprint)
    second = evidence_id(EvidenceKind.METRIC, "q2", {}, window, fingerprint)

    assert first != second


def test_evidence_id_changes_when_a_parameter_changes():
    window = _window()
    fingerprint = _fingerprint()

    first = evidence_id(EvidenceKind.METRIC, "q", {"service": "cart"}, window, fingerprint)
    second = evidence_id(EvidenceKind.METRIC, "q", {"service": "payment"}, window, fingerprint)

    assert first != second


def test_evidence_id_changes_when_the_window_changes():
    fingerprint = _fingerprint()
    window_a = _window()
    window_b = _window(end=window_a.end + timedelta(seconds=1))

    first = evidence_id(EvidenceKind.METRIC, "q", {}, window_a, fingerprint)
    second = evidence_id(EvidenceKind.METRIC, "q", {}, window_b, fingerprint)

    assert first != second


def test_evidence_id_changes_when_the_fingerprint_changes():
    window = _window()
    fingerprint_a = _fingerprint()
    fingerprint_b = _fingerprint(identity="inc_other0000")

    first = evidence_id(EvidenceKind.METRIC, "q", {}, window, fingerprint_a)
    second = evidence_id(EvidenceKind.METRIC, "q", {}, window, fingerprint_b)

    assert first != second


def test_evidence_id_matches_the_documented_shape():
    eid = evidence_id(EvidenceKind.METRIC, "q", {}, _window(), _fingerprint())

    assert re.fullmatch(r"ev_metric_[0-9a-f]{12}", eid)


def test_evidence_id_different_kinds_over_same_query_and_window_do_not_collide():
    window = _window()
    fingerprint = _fingerprint()

    metric_id = evidence_id(EvidenceKind.METRIC, "q", {}, window, fingerprint)
    log_id = evidence_id(EvidenceKind.LOG, "q", {}, window, fingerprint)

    assert metric_id != log_id


# --- hash_rows -------------------------------------------------------------


def test_hash_rows_same_rows_produce_the_same_hash():
    rows = [{"a": 1}, {"b": 2}]

    assert hash_rows(rows) == hash_rows(rows)


def test_hash_rows_reordering_rows_produces_a_different_hash():
    """Order carries meaning for a ranked answer, so it has to be part of the hash."""
    forward = [{"rank": 1}, {"rank": 2}]
    backward = [{"rank": 2}, {"rank": 1}]

    assert hash_rows(forward) != hash_rows(backward)


def test_hash_rows_key_order_within_a_row_does_not_change_the_hash():
    first = [{"a": 1, "b": 2}]
    second = [{"b": 2, "a": 1}]

    assert hash_rows(first) == hash_rows(second)


# --- Fact.matches ------------------------------------------------------


def test_fact_matches_count_unit_compares_exactly():
    fact = Fact(field="error_count", value=10, unit="count")

    assert fact.matches(10, tolerance=0.5) is True
    assert fact.matches(10.0001, tolerance=0.5) is False


def test_fact_matches_rate_within_relative_tolerance():
    fact = Fact(field="error_rate", value=0.5, unit="rate")

    assert fact.matches(0.52, tolerance=0.1) is True


def test_fact_matches_rate_outside_relative_tolerance():
    fact = Fact(field="error_rate", value=0.5, unit="rate")

    assert fact.matches(0.7, tolerance=0.1) is False


def test_fact_matches_zero_value_within_absolute_tolerance():
    fact = Fact(field="error_rate", value=0.0, unit="rate")

    assert fact.matches(0.05, tolerance=0.1) is True
    assert fact.matches(0.5, tolerance=0.1) is False


# --- EvidenceRecord ------------------------------------------------------


def test_evidence_record_constructs_via_build_record():
    window = _window()
    fingerprint = _fingerprint()
    rows = [{"value": 1.0}, {"value": 2.0}]

    record = build_record(
        kind=EvidenceKind.METRIC,
        query="avg(rate)",
        parameters={"service": "payment"},
        window=window,
        fingerprint=fingerprint,
        rows=rows,
    )

    assert record.id == evidence_id(
        EvidenceKind.METRIC, "avg(rate)", {"service": "payment"}, window, fingerprint
    )
    assert record.row_count == 2
    assert record.rows == tuple(rows)
    assert record.result_sha256 == hash_rows(rows)
    assert record.truncated is False


def test_evidence_record_rejects_an_id_that_does_not_match_its_content():
    """A swapped record under a stale id would let a claim's citation lie about what it cites."""
    window = _window()
    fingerprint = _fingerprint()
    kind = EvidenceKind.METRIC
    query = "avg(rate)"
    parameters = {"service": "cart"}
    rows = [{"value": 1.0}]
    expected = evidence_id(kind, query, parameters, window, fingerprint)
    wrong_id = "ev_metric_ffffffffffff"
    assert wrong_id != expected

    with pytest.raises(ValueError, match=re.escape(expected)):
        EvidenceRecord(
            id=wrong_id,
            kind=kind,
            query=query,
            parameters=parameters,
            window=window,
            fingerprint=fingerprint,
            row_count=len(rows),
            rows=tuple(rows),
            result_sha256=hash_rows(rows),
        )


def test_evidence_record_fact_for_finds_fact_by_field_and_by_field_and_subject():
    facts = [
        Fact(field="error_rate", value=0.4, unit="rate", subject="payment"),
        Fact(field="error_rate", value=0.1, unit="rate", subject="cart"),
    ]
    record = _build(facts=facts)

    assert record.fact_for("error_rate").subject == "payment"
    assert record.fact_for("error_rate", subject="cart").subject == "cart"
    assert record.fact_for("does-not-exist") is None


def test_evidence_record_supports_is_true_when_any_fact_matches():
    record = _build(facts=[Fact(field="error_rate", value=0.42, unit="rate")])

    assert record.supports(0.42, tolerance=0.01) is True
    assert record.supports(0.9, tolerance=0.01) is False


# --- build_record ----------------------------------------------------------


def test_build_record_rows_beyond_inline_limit_are_not_stored_inline():
    rows = [{"value": i} for i in range(INLINE_ROW_LIMIT + 3)]

    record = build_record(
        kind=EvidenceKind.LOG,
        query="q",
        parameters={},
        window=_window(),
        fingerprint=_fingerprint(),
        rows=rows,
    )

    assert record.row_count == len(rows)
    assert len(record.rows) == INLINE_ROW_LIMIT
    assert record.truncated is True
    assert record.result_sha256 == hash_rows(rows)


# --- EvidenceStore -------------------------------------------------------


def test_evidence_store_add_returns_the_stored_record():
    store = EvidenceStore()
    record = _build()

    returned = store.add(record)

    assert returned == record
    assert record.id in store


def test_evidence_store_add_same_id_twice_keeps_one_record_and_increments_repeat_count():
    store = EvidenceStore()
    record = _build()

    first = store.add(record)
    second = store.add(record)

    assert first == second
    assert len(store) == 1
    assert store.repeat_count(record.id) == 1


def test_evidence_store_get_returns_none_for_unknown_id():
    store = EvidenceStore()

    assert store.get("ev_metric_000000000000") is None


def test_evidence_store_require_raises_key_error_naming_the_id():
    store = EvidenceStore()

    with pytest.raises(KeyError, match="ev_metric_000000000000"):
        store.require("ev_metric_000000000000")


def test_evidence_store_require_returns_the_record_for_a_known_id():
    store = EvidenceStore()
    record = _build()
    store.add(record)

    assert store.require(record.id) == record


def test_evidence_store_ids_returns_sorted_tuple():
    store = EvidenceStore()
    record_a = _build(query="a query")
    record_b = _build(query="b query")
    store.add(record_b)
    store.add(record_a)

    assert store.ids() == tuple(sorted((record_a.id, record_b.id)))


def test_evidence_store_total_rows_sums_row_count_across_records():
    store = EvidenceStore()
    store.add(_build(rows=[{"v": 1}, {"v": 2}], query="a"))
    store.add(_build(rows=[{"v": 3}], query="b"))

    assert store.total_rows() == 3


def test_evidence_store_contains_and_len_behave():
    store = EvidenceStore()
    record = _build()

    assert record.id not in store
    assert len(store) == 0

    store.add(record)

    assert record.id in store
    assert len(store) == 1
