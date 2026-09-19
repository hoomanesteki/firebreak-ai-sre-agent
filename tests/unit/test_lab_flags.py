"""Tests for firebreak.lab.flags."""

import copy
import json
from pathlib import Path

import httpx
import pytest

from firebreak.lab.endpoints import DEMO_TAG
from firebreak.lab.flags import (
    FlagController,
    FlagError,
    FlagState,
    FlagVerificationError,
    UnknownFlagError,
    UnknownVariantError,
    parse_flag_config,
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
