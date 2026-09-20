"""Tests for firebreak.lab.flags."""

import copy
import json
from pathlib import Path

import httpx
import pytest

from firebreak.lab.endpoints import DEMO_TAG
from firebreak.lab.flags import (
    FlagController,
    FlagConvergenceError,
    FlagError,
    FlagState,
    FlagVerificationError,
    UnknownFlagError,
    UnknownVariantError,
    parse_flag_config,
    read_flag_file,
    read_vendored_defaults,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDORED_FLAG_FILE = REPO_ROOT / "vendor" / "otel-demo" / "src" / "flagd" / "demo.flagd.json"


def _client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


def _cart_failure_config():
    return {
        "flags": {
            "cartFailure": {
                "defaultVariant": "off",
                "state": "ENABLED",
                "variants": {"off": 0, "10%": 0.1, "100%": 1},
            },
            "adFailure": {
                "defaultVariant": "off",
                "state": "ENABLED",
                "variants": {"off": False, "on": True},
            },
        }
    }


# --- parse_flag_config -------------------------------------------------


def test_parse_flag_config_rejects_document_with_no_flags_key():
    with pytest.raises(FlagError, match="flags"):
        parse_flag_config({"notFlags": {}})


def test_parse_flag_config_rejects_flag_that_is_not_an_object():
    with pytest.raises(FlagError, match="adFailure"):
        parse_flag_config({"flags": {"adFailure": "not-an-object"}})


def test_parse_flag_config_rejects_flag_with_no_variants():
    with pytest.raises(FlagError, match="variants"):
        parse_flag_config({"flags": {"adFailure": {"defaultVariant": "off"}}})


# --- read_vendored_defaults ---------------------------------------------


def test_read_vendored_defaults_returns_off_for_payment_failure():
    defaults = read_vendored_defaults(VENDORED_FLAG_FILE)

    assert defaults["paymentFailure"] == "off"


# --- FlagState -----------------------------------------------------------


@pytest.mark.parametrize(
    ("variant", "expected"),
    [("off", False), ("on", True), ("50%", True), ("10sec", True)],
)
def test_flag_state_is_on_false_only_for_off(variant, expected):
    state = FlagState(name="x", default_variant=variant, variants=("off", variant), state="ENABLED")

    assert state.is_on is expected


# --- FlagController.read_config / get_variant ----------------------------


def test_read_config_raises_flag_error_when_body_is_not_an_object():
    client = _client(lambda request: httpx.Response(200, json=["not", "an", "object"]))
    controller = FlagController(client)

    with pytest.raises(FlagError):
        controller.read_config()


def test_get_variant_raises_unknown_flag_error_for_undefined_flag():
    client = _client(lambda request: httpx.Response(200, json=_cart_failure_config()))
    controller = FlagController(client)

    with pytest.raises(UnknownFlagError, match=f"demo tag {DEMO_TAG}"):
        controller.get_variant("doesNotExist")


# --- FlagController.set_variant -------------------------------------------


def test_set_variant_raises_unknown_flag_error_for_undefined_flag():
    client = _client(lambda request: httpx.Response(200, json=_cart_failure_config()))
    controller = FlagController(client)

    with pytest.raises(UnknownFlagError, match=f"demo tag {DEMO_TAG}"):
        controller.set_variant("doesNotExist", "on")


def test_set_variant_raises_unknown_variant_error_listing_offered_variants():
    client = _client(lambda request: httpx.Response(200, json=_cart_failure_config()))
    controller = FlagController(client)

    with pytest.raises(UnknownVariantError, match=r"10%, 100%, off"):
        controller.set_variant("cartFailure", "999%")


def test_set_variant_reads_writes_and_reads_back_new_state():
    initial = _cart_failure_config()
    store = {"config": copy.deepcopy(initial)}
    write_bodies = []

    def handler(request):
        if request.url.path == "/feature/api/read" and request.method == "GET":
            return httpx.Response(200, json=store["config"])
        if request.url.path == "/feature/api/write" and request.method == "POST":
            body = json.loads(request.content)
            write_bodies.append(body)
            store["config"] = body["data"]
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    controller = FlagController(_client(handler))

    result = controller.set_variant("cartFailure", "10%")

    assert result == FlagState(
        name="cartFailure",
        default_variant="10%",
        variants=("off", "10%", "100%"),
        state="ENABLED",
    )
    assert len(write_bodies) == 1
    expected_config = copy.deepcopy(initial)
    expected_config["flags"]["cartFailure"]["defaultVariant"] = "10%"
    assert write_bodies[0] == {"data": expected_config}
    assert write_bodies[0]["data"]["flags"]["adFailure"]["defaultVariant"] == "off"


def test_set_variant_raises_flag_verification_error_when_readback_shows_old_variant():
    unchanged = _cart_failure_config()

    def handler(request):
        if request.url.path == "/feature/api/read":
            return httpx.Response(200, json=unchanged)
        if request.url.path == "/feature/api/write":
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    controller = FlagController(_client(handler))

    with pytest.raises(FlagVerificationError, match="10%"):
        controller.set_variant("cartFailure", "10%")


# --- FlagController.reset_to ----------------------------------------------


def test_reset_to_writes_only_drifted_flags_and_returns_sorted_names():
    config = {
        "flags": {
            "cartFailure": {
                "defaultVariant": "10%",
                "state": "ENABLED",
                "variants": {"off": 0, "10%": 0.1},
            },
            "adFailure": {
                "defaultVariant": "on",
                "state": "ENABLED",
                "variants": {"off": False, "on": True},
            },
            "adHighCpu": {
                "defaultVariant": "off",
                "state": "ENABLED",
                "variants": {"off": False, "on": True},
            },
        }
    }
    store = {"config": copy.deepcopy(config)}
    write_bodies = []

    def handler(request):
        if request.url.path == "/feature/api/read":
            return httpx.Response(200, json=store["config"])
        if request.url.path == "/feature/api/write":
            body = json.loads(request.content)
            write_bodies.append(body)
            store["config"] = body["data"]
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    controller = FlagController(_client(handler))

    changed = controller.reset_to(
        {"cartFailure": "off", "adFailure": "off", "adHighCpu": "off", "doesNotExist": "off"}
    )

    assert changed == ["adFailure", "cartFailure"]
    assert len(write_bodies) == 1
    assert write_bodies[0]["data"]["flags"]["cartFailure"]["defaultVariant"] == "off"
    assert write_bodies[0]["data"]["flags"]["adFailure"]["defaultVariant"] == "off"
    assert write_bodies[0]["data"]["flags"]["adHighCpu"]["defaultVariant"] == "off"


def test_reset_to_returns_empty_list_and_does_not_write_when_nothing_drifted():
    config = _cart_failure_config()

    def handler(request):
        if request.url.path == "/feature/api/read":
            return httpx.Response(200, json=config)
        raise AssertionError("write should not have been called when nothing drifted")

    controller = FlagController(_client(handler))

    changed = controller.reset_to({"cartFailure": "off", "adFailure": "off"})

    assert changed == []


# --- FlagController.evaluate ----------------------------------------------


def test_evaluate_returns_variant_from_ofrep_response():
    def handler(request):
        assert request.url.path == "/ofrep/v1/evaluate/flags/adFailure"
        return httpx.Response(200, json={"variant": "on", "value": True})

    controller = FlagController(_client(handler))

    assert controller.evaluate("adFailure") == "on"


def test_evaluate_raises_unknown_flag_error_on_404():
    controller = FlagController(_client(lambda request: httpx.Response(404, json={})))

    with pytest.raises(UnknownFlagError, match="doesNotExist"):
        controller.evaluate("doesNotExist")


def test_evaluate_raises_flag_verification_error_when_variant_is_not_a_string():
    controller = FlagController(_client(lambda request: httpx.Response(200, json={"value": True})))

    with pytest.raises(FlagVerificationError, match="adFailure"):
        controller.evaluate("adFailure")


# --- convergence -------------------------------------------------------


class _Clock:
    """A monotonic clock that only advances when sleep is called."""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def _ofrep_handler(variants):
    """Serve a queue of variants from the OFREP endpoint, one per call."""
    remaining = list(variants)

    def handler(request):
        if "/ofrep/" in request.url.path:
            value = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            return httpx.Response(200, json={"key": "f", "variant": value, "value": True})
        raise AssertionError(f"unexpected request: {request.url}")

    return handler


def test_await_variant_returns_immediately_when_already_served():
    clock = _Clock()
    controller = FlagController(_client(_ofrep_handler(["on"])))

    waited = controller.await_variant(
        "adFailure", "on", sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert waited == 0.0


def test_await_variant_polls_until_flagd_catches_up():
    clock = _Clock()
    controller = FlagController(_client(_ofrep_handler(["off", "off", "on"])))

    waited = controller.await_variant(
        "adFailure",
        "on",
        poll_seconds=0.25,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert waited == pytest.approx(0.5)


def test_await_variant_raises_when_flagd_never_catches_up():
    clock = _Clock()
    controller = FlagController(_client(_ofrep_handler(["off"])))

    with pytest.raises(FlagConvergenceError, match="still serves 'off'"):
        controller.await_variant(
            "adFailure",
            "on",
            timeout_seconds=1.0,
            poll_seconds=0.25,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )


def test_await_variant_tolerates_a_flagd_response_with_no_variant():
    clock = _Clock()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"reason": "DEFAULT"})
        return httpx.Response(200, json={"variant": "on"})

    controller = FlagController(_client(handler))

    waited = controller.await_variant(
        "adFailure", "on", poll_seconds=0.5, sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert waited == pytest.approx(0.5)


def test_apply_fault_writes_then_waits_for_flagd():
    store = {"config": copy.deepcopy(_cart_failure_config())}
    served = {"variant": "off"}

    def handler(request):
        path = request.url.path
        if path.endswith("/feature/api/read"):
            return httpx.Response(200, json=store["config"])
        if path.endswith("/feature/api/write"):
            store["config"] = json.loads(request.content)["data"]
            return httpx.Response(200, json={"status": "ok"})
        if "/ofrep/" in path:
            current = served["variant"]
            served["variant"] = "10%"
            return httpx.Response(200, json={"variant": current})
        raise AssertionError(f"unexpected request: {path}")

    clock = _Clock()
    controller = FlagController(_client(handler))

    state, waited = controller.apply_fault(
        "cartFailure", "10%", sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert state.default_variant == "10%"
    assert store["config"]["flags"]["cartFailure"]["defaultVariant"] == "10%"
    assert waited > 0.0


# --- integrity of reset and file reading ---------------------------------


def test_reset_to_raises_when_the_write_did_not_take():
    """A reset that silently does nothing poisons the next recording."""

    def handler(request):
        if request.url.path.endswith("/feature/api/read"):
            config = _cart_failure_config()
            config["flags"]["cartFailure"]["defaultVariant"] = "100%"
            return httpx.Response(200, json=config)
        if request.url.path.endswith("/feature/api/write"):
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(f"unexpected request: {request.url}")

    controller = FlagController(_client(handler))

    with pytest.raises(FlagVerificationError, match="would inherit these"):
        controller.reset_to({"cartFailure": "off"})


def test_reset_to_returns_changed_names_when_the_write_took():
    store = {"config": _cart_failure_config()}
    store["config"]["flags"]["cartFailure"]["defaultVariant"] = "100%"

    def handler(request):
        if request.url.path.endswith("/feature/api/read"):
            return httpx.Response(200, json=store["config"])
        if request.url.path.endswith("/feature/api/write"):
            store["config"] = json.loads(request.content)["data"]
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(f"unexpected request: {request.url}")

    controller = FlagController(_client(handler))

    assert controller.reset_to({"cartFailure": "off"}) == ["cartFailure"]


def test_read_flag_file_reports_invalid_json_as_a_flag_error(tmp_path: Path):
    path = tmp_path / "demo.flagd.json"
    path.write_text('{"flags": {', encoding="utf-8")

    with pytest.raises(FlagError, match="not valid JSON"):
        read_flag_file(path)


def test_read_flag_file_reports_a_missing_file_as_a_flag_error(tmp_path: Path):
    with pytest.raises(FlagError, match="cannot read"):
        read_flag_file(tmp_path / "absent.json")


def test_read_flag_file_rejects_a_json_document_that_is_not_an_object(tmp_path: Path):
    path = tmp_path / "demo.flagd.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(FlagError, match="does not contain a JSON object"):
        read_flag_file(path)


def test_await_variant_times_out_at_the_configured_deadline():
    """A timeout that fires far too late would otherwise ship unnoticed."""
    clock = _Clock()
    controller = FlagController(_client(_ofrep_handler(["off"])))

    with pytest.raises(FlagConvergenceError):
        controller.await_variant(
            "adFailure",
            "on",
            timeout_seconds=2.0,
            poll_seconds=0.5,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert clock.now == pytest.approx(2.0)


def test_await_variant_keeps_waiting_while_flagd_is_unreachable():
    """flagd refuses connections briefly while it reloads or restarts."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"variant": "on"})

    clock = _Clock()
    controller = FlagController(_client(handler))

    waited = controller.await_variant(
        "adFailure", "on", poll_seconds=0.5, sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert waited == pytest.approx(1.0)
    assert calls["n"] == 3
