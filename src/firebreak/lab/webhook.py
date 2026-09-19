"""A webhook receiver that records Alertmanager deliveries.

Firebreak's API arrives in a later phase. This exists so Phase 1 can prove
that a rule fired, Alertmanager routed it, and the payload reached a listener
on the host, which is the whole alert path apart from the agent.

It writes one JSON line per delivery so a recording can be read back later
without a database.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MAX_BODY_BYTES = 1_000_000


class AlertSink:
    """Appends received alert payloads to a JSON lines file."""

    def __init__(self, path: Path, listener: Callable[[str], None] | None = None) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._listener = listener
        self.count = 0

    def record(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Store one delivery and return the line that was written."""
        line = {
            "received_at": datetime.now(UTC).isoformat(),
            "payload": payload,
        }
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")
        self.count += 1
        if self._listener is not None:
            self._listener(f"[{self.count}] {summarise(payload)}")
        return line


def summarise(payload: dict[str, Any]) -> str:
    """One line describing a delivery, for the terminal."""
    alerts = payload.get("alerts")
    if not isinstance(alerts, list) or not alerts:
        return "delivery with no alerts"
    names = []
    for alert in alerts:
        labels = alert.get("labels", {}) if isinstance(alert, dict) else {}
        status = alert.get("status", "?") if isinstance(alert, dict) else "?"
        names.append(f"{labels.get('alertname', 'unknown')}[{status}]")
    return f"{len(alerts)} alert(s): " + ", ".join(names)


class _Handler(BaseHTTPRequestHandler):
    sink: AlertSink
    server_version = "firebreak-lab-webhook"

    def do_POST(self) -> None:
        """Accept an Alertmanager delivery."""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._respond(400, {"error": "missing or oversized body"})
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self._respond(400, {"error": "body is not JSON"})
            return
        if not isinstance(payload, dict):
            self._respond(400, {"error": "body is not a JSON object"})
            return
        self.sink.record(payload)
        self._respond(200, {"status": "recorded"})

    def do_GET(self) -> None:
        """Health check, so a script can wait for this to be up."""
        self._respond(200, {"status": "ok", "received": self.sink.count})

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default request log; the sink reports deliveries instead."""

    def _respond(self, code: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def build_server(port: int, sink: AlertSink) -> ThreadingHTTPServer:
    """Build a webhook server bound to localhost."""
    handler = type("BoundHandler", (_Handler,), {"sink": sink})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)
