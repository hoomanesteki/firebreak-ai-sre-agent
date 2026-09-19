"""Tests for firebreak.lab.smoke."""

import json

import httpx
import pytest

from firebreak.lab.endpoints import DEFAULT_ENDPOINTS
from firebreak.lab.flags import FlagController, FlagState
from firebreak.lab.smoke import (
    EXCLUDED_FLAGS,
    SIGNAL_QUERIES,
    FlagSmokeResult,
    PrometheusQueryError,
    build_report,
    find_changed_signals,
    pick_strongest_variant,
    query_instant,
    relative_change,
    smoke_one_flag,
)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _success_body(value):
    return {"status": "success", "data": {"result": [{"value": [0, value]}]}}


def _state(variants):
    return FlagState(name="f", default_variant="off", variants=tuple(variants), state="ENABLED")


# --- query_instant ---------------------------------------------------------


def test_query_instant_returns_the_float_value():
    client = _client(lambda request: httpx.Response(200, json=_success_body("42.5")))

    assert query_instant(client, DEFAULT_ENDPOINTS, "up") == 42.5


def test_query_instant_returns_none_on_empty_result():
    body = {"status": "success", "data": {"result": []}}
    client = _client(lambda request: httpx.Response(200, json=body))

    assert query_instant(client, DEFAULT_ENDPOINTS, "up") is None


def test_query_instant_raises_when_status_is_not_success():
    body = {"status": "error", "errorType": "bad_data"}
    client = _client(lambda request: httpx.Response(200, json=body))

    with pytest.raises(PrometheusQueryError):
        query_instant(client, DEFAULT_ENDPOINTS, "up")


def test_query_instant_raises_when_value_shape_is_wrong():
    body = {"status": "success", "data": {"result": [{"value": "not-a-pair"}]}}
    client = _client(lambda request: httpx.Response(200, json=body))

    with pytest.raises(PrometheusQueryError):
        query_instant(client, DEFAULT_ENDPOINTS, "up")


def test_query_instant_returns_none_when_value_is_not_numeric():
    client = _client(lambda request: httpx.Response(200, json=_success_body(None)))

    assert query_instant(client, DEFAULT_ENDPOINTS, "up") is None


# --- relative_change ---------------------------------------------------------


@pytest.mark.parametrize(("before", "after"), [(None, 5.0), (5.0, None), (None, None)])
def test_relative_change_returns_none_when_either_input_is_none(before, after):
    assert relative_change(before, after) is None


def test_relative_change_returns_none_for_zero_baseline_and_zero_after():
    assert relative_change(0.0, 0.0) is None


def test_relative_change_returns_infinity_for_zero_baseline_and_nonzero_after():
    assert relative_change(0.0, 5.0) == float("inf")


def test_relative_change_computes_normal_case():
    assert relative_change(10.0, 15.0) == pytest.approx(0.5)
    assert relative_change(10.0, 5.0) == pytest.approx(-0.5)


# --- find_changed_signals ---------------------------------------------------------


def test_find_changed_signals_returns_only_signals_past_threshold_sorted():
    before = {"zeta": 10.0, "alpha": 10.0, "steady": 10.0}
    after = {"zeta": 20.0, "alpha": 20.0, "steady": 10.0}

    assert find_changed_signals(before, after, threshold=0.2) == ["alpha", "zeta"]


def test_find_changed_signals_skips_signals_whose_change_cannot_be_computed():
    before = {"missing_after": 10.0, "zero_to_zero": 0.0}
    after = {"zero_to_zero": 0.0}

    assert find_changed_signals(before, after) == []


# --- pick_strongest_variant ---------------------------------------------------------


def test_pick_strongest_variant_picks_the_largest_percentage():
    state = _state(["off", "10%", "25%", "50%", "75%", "90%", "100%"])

    assert pick_strongest_variant(state) == "100%"


def test_pick_strongest_variant_picks_the_longest_duration():
    state = _state(["off", "5sec", "10sec"])

    assert pick_strongest_variant(state) == "10sec"


def test_pick_strongest_variant_picks_the_largest_multiplier():
    state = _state(["off", "1x", "10x", "100x", "1000x", "10000x"])

    assert pick_strongest_variant(state) == "10000x"


def test_pick_strongest_variant_picks_on_for_a_boolean_flag():
    state = _state(["off", "on"])

    assert pick_strongest_variant(state) == "on"


def test_pick_strongest_variant_returns_none_when_only_off_is_offered():
    state = _state(["off"])

    assert pick_strongest_variant(state) is None


# --- smoke_one_flag ---------------------------------------------------------


def _make_smoke_handler(flag_name, config, write_log, prom_call_counts, *, fail_after=False):
    store = {"config": config}

    def handler(request):
        path = request.url.path
        if path == "/feature/api/read":
            return httpx.Response(200, json=store["config"])
        if path == "/feature/api/write":
            body = json.loads(request.content)
            new_config = body["data"]
            store["config"] = new_config
            write_log.append(new_config["flags"][flag_name]["defaultVariant"])
            return httpx.Response(200, json={"status": "ok"})
        if path == "/api/v1/query":
            query = request.url.params["query"]
            prom_call_counts[query] = prom_call_counts.get(query, 0) + 1
            n = prom_call_counts[query]
            if fail_after and n >= 2:
                return httpx.Response(200, json={"status": "error"})
            value = "1.0" if n == 1 else "5.0"
            return httpx.Response(200, json=_success_body(value))
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return handler


def _payment_failure_config():
    return {
        "flags": {
            "paymentFailure": {
                "defaultVariant": "off",
                "state": "ENABLED",
                "variants": {
                    "off": 0,
                    "10%": 0.1,
                    "25%": 0.25,
                    "50%": 0.5,
                    "75%": 0.75,
                    "90%": 0.9,
                    "100%": 1,
                },
            }
        }
    }


def _payment_failure_state():
    return FlagState(
        name="paymentFailure",
        default_variant="off",
        variants=("off", "10%", "25%", "50%", "75%", "90%", "100%"),
        state="ENABLED",
    )


def test_smoke_one_flag_turns_flag_on_then_off_and_records_observed_effect():
    write_log = []
    prom_calls = {}
    handler = _make_smoke_handler(
        "paymentFailure", _payment_failure_config(), write_log, prom_calls
    )
    client = _client(handler)
    controller = FlagController(client)
    sleeps = []

    result = smoke_one_flag(
        controller,
        client,
        DEFAULT_ENDPOINTS,
        _payment_failure_state(),
        hold_seconds=0.01,
        sleep=lambda seconds: sleeps.append(seconds),
    )

    assert sleeps == [0.01]
    assert write_log == ["100%", "off"]
    assert result.variant == "100%"
    assert result.observed_effect is True
    assert result.changed_signals == sorted(SIGNAL_QUERIES)
    assert result.error is None


def test_smoke_one_flag_turns_flag_off_even_when_sampling_raises():
    write_log = []
    prom_calls = {}
    handler = _make_smoke_handler(
        "paymentFailure", _payment_failure_config(), write_log, prom_calls, fail_after=True
    )
    client = _client(handler)
    controller = FlagController(client)

    with pytest.raises(PrometheusQueryError):
        smoke_one_flag(
            controller,
            client,
            DEFAULT_ENDPOINTS,
            _payment_failure_state(),
            hold_seconds=0.0,
            sleep=lambda seconds: None,
        )

    assert write_log[0] == "100%"
    assert write_log[-1] == "off"


def test_smoke_one_flag_returns_error_result_when_flag_offers_no_variant_other_than_off():
    def handler(request):
        raise AssertionError("no HTTP call should be made when there is no variant to try")

    client = _client(handler)
    controller = FlagController(client)
    state = _state(["off"])

    result = smoke_one_flag(
        controller,
        client,
        DEFAULT_ENDPOINTS,
        state,
        hold_seconds=0.0,
        sleep=lambda seconds: None,
    )

    assert result.variant == ""
    assert result.observed_effect is False
    assert result.error == "flag offers no variant other than off"


# --- build_report ---------------------------------------------------------


def test_build_report_counts_observed_effects_and_includes_demo_tag_and_queries():
    results = [
        FlagSmokeResult(
            flag="paymentFailure",
            variant="100%",
            before={},
            after={},
            changed_signals=["latency_p95_milliseconds"],
            observed_effect=True,
        ),
        FlagSmokeResult(
            flag="adManualGc",
            variant="on",
            before={},
            after={},
            changed_signals=[],
            observed_effect=False,
        ),
    ]

    report = build_report(results, demo_tag="3.1.0", hold_seconds=30.0)

    assert report["demo_tag"] == "3.1.0"
    assert report["hold_seconds"] == 30.0
    assert report["signal_queries"] == SIGNAL_QUERIES
    assert report["excluded_flags"] == sorted(EXCLUDED_FLAGS)
    assert report["flags_tested"] == 2
    assert report["flags_with_observed_effect"] == 1
    assert len(report["results"]) == 2
    assert report["results"][0]["flag"] == "paymentFailure"
