"""Tests for firebreak.lab.load."""

from urllib.parse import parse_qs

import httpx
import pytest

from firebreak.lab.load import (
    MAX_USERS,
    MIN_USERS,
    InvalidLoadError,
    LoadController,
    LoadError,
    LoadState,
)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_set_users_rejects_zero_users():
    controller = LoadController(_client(lambda request: httpx.Response(200, json={})))

    with pytest.raises(InvalidLoadError, match=f"between {MIN_USERS} and {MAX_USERS}"):
        controller.set_users(0)


def test_set_users_rejects_501_users():
    controller = LoadController(_client(lambda request: httpx.Response(200, json={})))

    with pytest.raises(InvalidLoadError, match="got 501"):
        controller.set_users(501)


def test_set_users_rejects_non_positive_spawn_rate():
    controller = LoadController(_client(lambda request: httpx.Response(200, json={})))

    with pytest.raises(InvalidLoadError, match="spawn_rate must be positive, got 0"):
        controller.set_users(10, spawn_rate=0)


def test_set_users_posts_form_data_and_reads_resulting_state():
    posted = {}

    def handler(request):
        if request.url.path == "/loadgen/swarm" and request.method == "POST":
            posted.update(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/loadgen/stats/requests":
            return httpx.Response(200, json={"state": "spawning", "user_count": 10})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    controller = LoadController(_client(handler))

    result = controller.set_users(10, spawn_rate=2.5)

    assert posted == {"user_count": ["10"], "spawn_rate": ["2.5"]}
    assert result == LoadState(state="spawning", user_count=10)


def test_read_state_defaults_to_unknown_and_zero_when_fields_missing():
    controller = LoadController(_client(lambda request: httpx.Response(200, json={})))

    state = controller.read_state()

    assert state == LoadState(state="unknown", user_count=0)


def test_read_state_raises_load_error_on_non_object_body():
    controller = LoadController(_client(lambda request: httpx.Response(200, json=[1, 2, 3])))

    with pytest.raises(LoadError):
        controller.read_state()


def test_stop_calls_load_api_stop_endpoint():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/loadgen/stop":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/loadgen/stats/requests":
            return httpx.Response(200, json={"state": "stopped", "user_count": 0})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    controller = LoadController(_client(handler))

    result = controller.stop()

    assert ("GET", "/loadgen/stop") in calls
    assert result == LoadState(state="stopped", user_count=0)


@pytest.mark.parametrize(
    ("state", "expected"),
    [("running", True), ("spawning", True), ("stopped", False), ("ready", False)],
)
def test_load_state_is_running_matches_locust_states(state, expected):
    assert LoadState(state=state, user_count=1).is_running is expected
