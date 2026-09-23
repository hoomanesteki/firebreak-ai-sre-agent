"""Tests for firebreak.triage.log_templates: a deterministic log template masker."""

from __future__ import annotations

from firebreak.triage.log_templates import cluster_templates, mask_log_line

# --- mask_log_line: one substitution at a time --------------------------


def test_mask_log_line_replaces_a_uuid():
    line = "request 123e4567-e89b-12d3-a456-426614174000 accepted"

    assert mask_log_line(line) == "request <uuid> accepted"


def test_mask_log_line_replaces_a_long_hex_id():
    line = "container 0123456789abcdef started"

    assert mask_log_line(line) == "container <hex> started"


def test_mask_log_line_replaces_an_ip_address():
    line = "connect to 10.0.0.5 failed"

    assert mask_log_line(line) == "connect to <ip> failed"


def test_mask_log_line_replaces_an_iso_timestamp():
    line = "started at 2025-01-02T03:04:05.123Z sharp"

    assert mask_log_line(line) == "started at <ts> sharp"


def test_mask_log_line_replaces_a_double_quoted_string():
    line = 'saw "hello world" in the body'

    assert mask_log_line(line) == "saw <str> in the body"


def test_mask_log_line_replaces_a_single_quoted_string():
    line = "saw 'hello world' in the body"

    assert mask_log_line(line) == "saw <str> in the body"


def test_mask_log_line_replaces_a_bare_number():
    line = "retrying after 42 attempts"

    assert mask_log_line(line) == "retrying after <num> attempts"


# --- the whole point: same shape, same template ---------------------------


def test_two_lines_differing_only_by_id_mask_to_the_same_template():
    first = "payment charge req-11111111 failed for order 42"
    second = "payment charge req-22222222 failed for order 99"

    assert mask_log_line(first) == mask_log_line(second)


def test_two_lines_differing_only_by_number_mask_to_the_same_template():
    first = "checkout retried 3 times"
    second = "checkout retried 12 times"

    assert mask_log_line(first) == mask_log_line(second)


def test_two_genuinely_different_messages_mask_to_different_templates():
    first = "payment charge failed for order 42"
    second = "shipping quote request timed out"

    assert mask_log_line(first) != mask_log_line(second)


# --- determinism and purity -------------------------------------------------


def test_mask_log_line_is_deterministic():
    line = 'user 7 from 192.168.1.1 saw "error" at 2025-01-01T00:00:00Z'

    assert mask_log_line(line) == mask_log_line(line)


# --- cluster_templates ------------------------------------------------------


def test_cluster_templates_counts_occurrences_most_frequent_first():
    bodies = [
        "found user 1",
        "found user 2",
        "found user 3",
        "different message",
        "found user 4",
    ]

    ranked = cluster_templates(bodies)

    assert ranked[0] == ("found user <num>", 4)
    assert ranked[1] == ("different message", 1)


def test_cluster_templates_of_empty_input_returns_empty_list():
    assert cluster_templates([]) == []
