"""Addresses of the pinned OpenTelemetry Demo stack.

Ports were read from the demo's `.env` at tag 3.1.0 and are listed in
`docs/target-system.md`. Jaeger, OpenSearch, and the flagd OFREP port are
reachable only because `ops/compose.live.yml` publishes them; the rest are
published by the demo itself.
"""

from __future__ import annotations

from dataclasses import dataclass

DEMO_TAG = "3.1.0"


@dataclass(frozen=True)
class DemoEndpoints:
    """Where each part of the running demo answers."""

    proxy: str = "http://localhost:8080"
    prometheus: str = "http://localhost:9090"
    jaeger: str = "http://localhost:16686"
    opensearch: str = "http://localhost:9200"
    flagd_ofrep: str = "http://localhost:8016"
    alertmanager: str = "http://localhost:9093"

    @property
    def flag_api(self) -> str:
        """flagd-ui's REST API, routed through the proxy."""
        return f"{self.proxy}/feature/api"

    @property
    def load_api(self) -> str:
        """Locust's web API, routed through the proxy with the prefix stripped."""
        return f"{self.proxy}/loadgen"


DEFAULT_ENDPOINTS = DemoEndpoints()
