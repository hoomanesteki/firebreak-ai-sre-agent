"""Control the demo's load generator.

Tag 3.1.0 runs Locust, which exposes a web API. The proxy routes `/loadgen/`
to it with the prefix stripped, so the API is reachable from the host without
publishing another port.

Load level is part of a scenario's identity: the same fault at 5 users and at
50 users produces different symptoms, so a recording that cannot set the load
cannot be reproduced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from firebreak.lab.endpoints import DEFAULT_ENDPOINTS, DemoEndpoints

MIN_USERS = 1
MAX_USERS = 500
DEFAULT_SPAWN_RATE = 5.0


class LoadError(Exception):
    """Base class for load generator failures."""


class InvalidLoadError(LoadError):
    """The requested load is outside the range the lab allows."""


@dataclass(frozen=True)
class LoadState:
    """What the load generator reports it is doing."""

    state: str
    user_count: int

    @property
    def is_running(self) -> bool:
        """True when Locust reports it is spawning or holding users."""
        return self.state in {"running", "spawning"}


class LoadController:
    """Sets and reads the load level on the demo's Locust generator."""

    def __init__(
        self,
        client: httpx.Client,
        endpoints: DemoEndpoints = DEFAULT_ENDPOINTS,
    ) -> None:
        self._client = client
        self._endpoints = endpoints

    def set_users(self, users: int, spawn_rate: float = DEFAULT_SPAWN_RATE) -> LoadState:
        """Ramp the generator to a user count and return the resulting state."""
        if not MIN_USERS <= users <= MAX_USERS:
            raise InvalidLoadError(
                f"users must be between {MIN_USERS} and {MAX_USERS}, got {users}"
            )
        if spawn_rate <= 0:
            raise InvalidLoadError(f"spawn_rate must be positive, got {spawn_rate}")

        response = self._client.post(
            f"{self._endpoints.load_api}/swarm",
            data={"user_count": str(users), "spawn_rate": str(spawn_rate)},
        )
        response.raise_for_status()
        return self.read_state()

    def stop(self) -> LoadState:
        """Stop generating load."""
        response = self._client.get(f"{self._endpoints.load_api}/stop")
        response.raise_for_status()
        return self.read_state()

    def read_state(self) -> LoadState:
        """Read the current run state and user count."""
        response = self._client.get(f"{self._endpoints.load_api}/stats/requests")
        response.raise_for_status()
        body: Any = response.json()
        if not isinstance(body, dict):
            raise LoadError("load generator returned a non-object body")
        return LoadState(
            state=str(body.get("state", "unknown")),
            user_count=int(body.get("user_count", 0)),
        )
