"""The Console over real HTTP, started the way `make console` starts it.

**Why this is separate from `test_console.py`.** That file drives the application object
through FastAPI's `TestClient`, which never opens a socket. All 34 of its tests passed while
`make console` pointed at a module that did not exist. A suite that only ever calls the app
directly cannot tell a working Console from an unreachable one, so exactly one test here
starts the process, binds a port, and asks for pages over the wire.

One test process, several assertions, because starting a server is the expensive part and
splitting it into six would pay that cost six times for no extra coverage.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONSOLE_DIR = REPO_ROOT / "reports" / "console"

# Long enough for an interpreter start and an app build on a loaded CI runner, short enough
# that a genuinely broken server fails the test rather than hanging the suite.
STARTUP_TIMEOUT_SECONDS = 30.0


def _free_port() -> int:
    """A port the kernel says is free, so a developer's own Console on 8080 does not make
    this test fail or, worse, pass by answering for it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    port = _free_port()
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT / "src")
    environment["FIREBREAK_WEB_PORT"] = str(port)
    process = subprocess.Popen(
        [sys.executable, "-m", "firebreak.web.serve"],
        cwd=REPO_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    try:
        while True:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                pytest.fail(f"the server exited with {process.returncode}:\n{output}")
            try:
                with urllib.request.urlopen(f"{url}/", timeout=2) as response:
                    if response.status == 200:
                        break
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
            if time.monotonic() >= deadline:
                process.terminate()
                output = process.stdout.read() if process.stdout else ""
                pytest.fail(
                    f"the server did not answer within {STARTUP_TIMEOUT_SECONDS}s:\n{output}"
                )
            time.sleep(0.2)
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


class TestTheConsoleAnswersOverHttp:
    """The defect: `make console` ran `python -m firebreak.web.serve`, and no such module
    existed. Every page test passed anyway, because none of them opened a socket."""

    def test_every_page_answers(self, base_url: str) -> None:
        for path in ("/", "/evaluation", "/approvals", "/feedback"):
            status, body = _get(f"{base_url}{path}")
            assert status == 200, f"{path} returned {status}"
            assert "| Firebreak</title>" in body, f"{path} served no Console page"

    def test_a_report_and_its_timeline_answer(self, base_url: str) -> None:
        stored = sorted(CONSOLE_DIR.glob("*.json"))
        if not stored:
            pytest.skip("no stored report; run make demo-offline")
        name = stored[0].stem
        for path in (f"/report/{name}", f"/live/{name}"):
            status, _ = _get(f"{base_url}{path}")
            assert status == 200, f"{path} returned {status}"

    def test_an_evidence_record_answers_as_json(self, base_url: str) -> None:
        """The endpoint every claim's link opens. Over the wire rather than through the
        app, because a link a reader clicks goes over the wire."""
        import json

        for path in sorted(CONSOLE_DIR.glob("*.json")):
            stored = json.loads(path.read_text(encoding="utf-8"))
            claims = stored.get("claims") or []
            if not claims:
                continue
            reference = claims[0]["evidence_ids"][0]
            status, body = _get(f"{base_url}/evidence/{path.stem}/{reference}")
            assert status == 200
            assert json.loads(body)["query"]
            return
        pytest.skip("no stored report with claims; run make demo-offline")

    def test_an_invented_evidence_id_is_a_404(self, base_url: str) -> None:
        stored = sorted(CONSOLE_DIR.glob("*.json"))
        if not stored:
            pytest.skip("no stored report; run make demo-offline")
        status, _ = _get(f"{base_url}/evidence/{stored[0].stem}/ev_metric_deadbeefdead")
        assert status == 404
