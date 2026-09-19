"""Tests for firebreak.lab.verify."""

import httpx

from firebreak.lab.endpoints import DemoEndpoints
from firebreak.lab.verify import (
    DENSITY_QUERY,
    EDGE_QUERY,
    MIN_SAMPLES_IN_RATE_WINDOW,
    RATE_WINDOW_SECONDS,
    SAMPLE_STEP_SECONDS,
    SPAN_METRIC_QUERY,
    Check,
    build_report,
    check_alertmanager,
    check_flagd,
    check_load_generator,
    check_prometheus_rules,
    check_sample_density,
    check_service_graph,
    check_span_metrics,
    run_checks,
)

ENDPOINTS = DemoEndpoints()
NOW = 1_700_000_000.0


def _client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


# --- check_prometheus_rules -----------------------------------------------


def test_check_prometheus_rules_passes_and_reports_names_when_rule_present():
    def handler(request):
        assert request.url.path == "/api/v1/rules"
        return httpx.Response(
            200,
            json={
                "data": {
                    "groups": [
                        {"rules": [{"name": "checkout_error_rate_high"}, {"name": "zeta_rule"}]},
                        {"rules": [{"name": "alpha_rule"}]},
                    ]
                }
            },
        )

    check = check_prometheus_rules(_client(handler), ENDPOINTS)

    assert check.name == "prometheus_rules_loaded"
    assert check.passed is True
    assert check.detail == "3 rule(s) loaded"
    assert check.measured == {"rules": ["alpha_rule", "checkout_error_rate_high", "zeta_rule"]}


def test_check_prometheus_rules_fails_when_rule_missing():
    def handler(request):
        return httpx.Response(200, json={"data": {"groups": [{"rules": [{"name": "other_rule"}]}]}})

    check = check_prometheus_rules(_client(handler), ENDPOINTS)

    assert check.passed is False
    assert check.detail == "1 rule(s) loaded"
    assert check.measured == {"rules": ["other_rule"]}


# --- check_service_graph ---------------------------------------------------


def test_check_service_graph_passes_on_nonzero_count():
    def handler(request):
        assert request.url.path == "/api/v1/query"
        assert request.url.params["query"] == EDGE_QUERY
        return httpx.Response(200, json={"data": {"result": [{"value": [1700000000, "4"]}]}})

    check = check_service_graph(_client(handler), ENDPOINTS)

    assert check.name == "service_graph_edges"
    assert check.passed is True
    assert check.detail == "4 dependency edge series"
    assert check.measured == {"edge_series": 4}


def test_check_service_graph_fails_on_empty_result():
    def handler(request):
        return httpx.Response(200, json={"data": {"result": []}})

    check = check_service_graph(_client(handler), ENDPOINTS)

    assert check.passed is False
    assert check.detail == "0 dependency edge series"
    assert check.measured == {"edge_series": 0}


# --- check_span_metrics -----------------------------------------------------


def test_check_span_metrics_passes_on_nonzero_count():
    def handler(request):
        assert request.url.params["query"] == SPAN_METRIC_QUERY
        return httpx.Response(200, json={"data": {"result": [{"value": [1700000000, "7"]}]}})

    check = check_span_metrics(_client(handler), ENDPOINTS)

    assert check.name == "span_metrics_present"
    assert check.passed is True
    assert check.detail == "7 span metric series"
    assert check.measured == {"series": 7}


def test_check_span_metrics_fails_on_empty_result():
    def handler(request):
        return httpx.Response(200, json={"data": {"result": []}})

    check = check_span_metrics(_client(handler), ENDPOINTS)

    assert check.passed is False
    assert check.detail == "0 span metric series"
    assert check.measured == {"series": 0}


# --- check_sample_density ---------------------------------------------------


def test_check_sample_density_passes_when_points_meet_minimum():
    def handler(request):
        return httpx.Response(
            200, json={"data": {"result": [{"values": [[1, "1"], [2, "1"], [3, "1"]]}]}}
        )

    check = check_sample_density(_client(handler), ENDPOINTS, NOW)

    assert check.name == "sample_density"
    assert check.passed is True
    assert check.detail == "3 of 20 possible points in a 300s window"
    assert check.measured == {"points": 3, "possible": 20}


def test_check_sample_density_fails_below_minimum():
    def handler(request):
        return httpx.Response(200, json={"data": {"result": [{"values": [[1, "1"], [2, "1"]]}]}})

    check = check_sample_density(_client(handler), ENDPOINTS, NOW)

    assert MIN_SAMPLES_IN_RATE_WINDOW == 3
    assert check.passed is False
    assert check.detail == "2 of 20 possible points in a 300s window"
    assert check.measured == {"points": 2, "possible": 20}


def test_check_sample_density_reports_zero_points_when_result_empty():
    def handler(request):
        return httpx.Response(200, json={"data": {"result": []}})

    check = check_sample_density(_client(handler), ENDPOINTS, NOW)

    assert check.passed is False
    assert check.detail == "0 of 20 possible points in a 300s window"
    assert check.measured == {"points": 0, "possible": 20}


def test_check_sample_density_sends_start_end_step_derived_from_now():
    captured = {}

    def handler(request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"data": {"result": []}})

    check_sample_density(_client(handler), ENDPOINTS, NOW)

    assert captured["params"] == {
        "query": DENSITY_QUERY,
        "start": str(int(NOW - RATE_WINDOW_SECONDS)),
        "end": str(int(NOW)),
        "step": str(SAMPLE_STEP_SECONDS),
    }


# --- check_flagd -------------------------------------------------------------


def test_check_flagd_passes_when_variant_is_string():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/ofrep/v1/evaluate/flags/cartFailure"
        return httpx.Response(200, json={"variant": "10%"})

    check = check_flagd(_client(handler), ENDPOINTS, "cartFailure")

    assert check.name == "flagd_serving"
    assert check.passed is True
    assert check.detail == "cartFailure served as '10%'"
    assert check.measured == {"flag": "cartFailure", "variant": "10%"}


def test_check_flagd_fails_when_variant_missing():
    def handler(request):
        return httpx.Response(200, json={"reason": "DEFAULT"})

    check = check_flagd(_client(handler), ENDPOINTS, "cartFailure")

    assert check.passed is False
    assert check.detail == "cartFailure served as None"
    assert check.measured == {"flag": "cartFailure", "variant": None}


def test_check_flagd_posts_no_context_body_unlike_flag_controller_evaluate():
    """Documents a bug: FlagController.evaluate (flags.py) always posts
    json={"context": {}} to this same OFREP path, but check_flagd posts with
    no body and no content-type header at all. Against a real flagd server
    this check exercises a different request shape than the evaluation path
    it is supposed to stand in for, and OFREP implementations that require a
    JSON body would reject this call while flags.py's identical-looking call
    succeeds.
    """
    captured = {}

    def handler(request):
        captured["content"] = request.content
        captured["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json={"variant": "10%"})

    check_flagd(_client(handler), ENDPOINTS, "cartFailure")

    assert captured["content"] == b""
    assert captured["content_type"] is None


# --- check_load_generator ----------------------------------------------------


def test_check_load_generator_passes_when_users_present():
    def handler(request):
        assert request.url.path == "/loadgen/stats/requests"
        return httpx.Response(200, json={"user_count": 12, "state": "running"})

    check = check_load_generator(_client(handler), ENDPOINTS)

    assert check.name == "load_generator_running"
    assert check.passed is True
    assert check.detail == "12 user(s), state running"
    assert check.measured == {"user_count": 12, "state": "running"}


def test_check_load_generator_fails_at_zero_users():
    def handler(request):
        return httpx.Response(200, json={"user_count": 0, "state": "stopped"})

    check = check_load_generator(_client(handler), ENDPOINTS)

    assert check.passed is False
    assert check.detail == "0 user(s), state stopped"
    assert check.measured == {"user_count": 0, "state": "stopped"}


def test_check_load_generator_defaults_missing_fields_safely():
    def handler(request):
        return httpx.Response(200, json={})

    check = check_load_generator(_client(handler), ENDPOINTS)

    assert check.passed is False
    assert check.detail == "0 user(s), state None"
    assert check.measured == {"user_count": 0, "state": None}


# --- check_alertmanager -------------------------------------------------------


def test_check_alertmanager_passes_on_200_with_cluster_status():
    def handler(request):
        assert request.url.path == "/api/v2/status"
        return httpx.Response(200, json={"cluster": {"status": "ready"}})

    check = check_alertmanager(_client(handler), ENDPOINTS)

    assert check.name == "alertmanager_ready"
    assert check.passed is True
    assert check.detail == "cluster status ready"
    assert check.measured == {"cluster_status": "ready"}


# --- run_checks ---------------------------------------------------------------


def _all_pass_handler(request):
    path = request.url.path
    if path == "/api/v1/rules":
        return httpx.Response(
            200, json={"data": {"groups": [{"rules": [{"name": "checkout_error_rate_high"}]}]}}
        )
    if path == "/api/v1/query":
        return httpx.Response(200, json={"data": {"result": [{"value": [1, "2"]}]}})
    if path == "/api/v1/query_range":
        return httpx.Response(
            200, json={"data": {"result": [{"values": [[1, "1"], [2, "1"], [3, "1"]]}]}}
        )
    if path.startswith("/ofrep/"):
        return httpx.Response(200, json={"variant": "on"})
    if path == "/loadgen/stats/requests":
        return httpx.Response(200, json={"user_count": 3, "state": "running"})
    if path == "/api/v2/status":
        return httpx.Response(200, json={"cluster": {"status": "ready"}})
    raise AssertionError(f"unexpected request: {request.method} {request.url}")


EXPECTED_CHECK_ORDER = [
    "prometheus_rules_loaded",
    "service_graph_edges",
    "span_metrics_present",
    "sample_density",
    "flagd_serving",
    "load_generator_running",
    "alertmanager_ready",
]


def test_run_checks_returns_one_check_per_check_in_order():
    checks = run_checks(_client(_all_pass_handler), ENDPOINTS, "cartFailure", NOW)

    assert [check.name for check in checks] == EXPECTED_CHECK_ORDER
    assert all(check.passed for check in checks)


def test_run_checks_turns_connect_error_into_failed_check_without_stopping_others():
    def handler(request):
        if request.url.path == "/api/v1/rules":
            raise httpx.ConnectError("connection refused", request=request)
        return _all_pass_handler(request)

    checks = run_checks(_client(handler), ENDPOINTS, "cartFailure", NOW)

    assert [check.name for check in checks] == EXPECTED_CHECK_ORDER
    failed = checks[0]
    assert failed.name == "prometheus_rules_loaded"
    assert failed.passed is False
    assert failed.detail == "ConnectError: connection refused"
    assert failed.measured == {}
    assert all(check.passed for check in checks[1:])


# --- build_report --------------------------------------------------------------


def test_build_report_counts_passed_and_failed_and_lists_failed_names():
    checks = [
        Check(name="a", passed=True, detail="ok"),
        Check(name="b", passed=False, detail="bad"),
        Check(name="c", passed=True, detail="ok"),
    ]

    report = build_report(checks, "abcdef1", vendor_clean=True)

    assert report["demo_tag"] == "abcdef1"
    assert report["checks_run"] == 3
    assert report["checks_passed"] == 2
    assert report["failed_checks"] == ["b"]
    assert report["vendor_submodule_clean"] is True
    assert len(report["checks"]) == 3
    assert report["checks"][1] == {"name": "b", "passed": False, "detail": "bad", "measured": {}}


def test_build_report_ready_to_record_false_when_a_check_failed():
    checks = [
        Check(name="a", passed=True, detail="ok"),
        Check(name="b", passed=False, detail="bad"),
    ]

    report = build_report(checks, "tag", vendor_clean=True)

    assert report["ready_to_record"] is False


def test_build_report_ready_to_record_false_when_vendor_dirty_even_if_all_passed():
    checks = [Check(name="a", passed=True, detail="ok")]

    report = build_report(checks, "tag", vendor_clean=False)

    assert report["failed_checks"] == []
    assert report["checks_passed"] == 1
    assert report["vendor_submodule_clean"] is False
    assert report["ready_to_record"] is False


def test_build_report_ready_to_record_true_when_all_passed_and_vendor_clean():
    checks = [Check(name="a", passed=True, detail="ok")]

    report = build_report(checks, "tag", vendor_clean=True)

    assert report["ready_to_record"] is True
