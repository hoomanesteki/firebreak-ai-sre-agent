"""Tests for the Console's server entry point.

**Why these exist at all.** Phase 11 tested every Console page through FastAPI's
`TestClient`, which speaks to the application object directly and never starts a server. All
34 of those tests passed while `make console` pointed at a module that did not exist, so the
one command a reader would actually type was broken and the suite said the Console worked.
A test through the ASGI app is not a test of the thing that serves it.

**The property worth guarding is the bind address.** The Console renders incident telemetry,
including whatever a service under attack wrote into a log line, and it has no
authentication. Binding it to every interface by default would publish that to the network as
a side effect of starting it.
"""

from __future__ import annotations

import pytest

from firebreak.web import serve


class TestItBindsToLoopbackByDefault:
    """The security property. A default of 0.0.0.0 would publish incident data to the
    network as a side effect of running a make target."""

    def test_the_default_host_is_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(serve.HOST_VARIABLE, raising=False)
        assert serve.bind_host() == "127.0.0.1"

    def test_the_default_is_not_every_interface(self) -> None:
        """Asserted by value rather than by the constant, so editing the constant to
        0.0.0.0 fails here rather than passing quietly."""
        assert serve.DEFAULT_HOST not in {"0.0.0.0", "::", ""}

    def test_another_host_takes_a_deliberate_act(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(serve.HOST_VARIABLE, "0.0.0.0")
        assert serve.bind_host() == "0.0.0.0"

    def test_an_empty_host_falls_back_to_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unset variable and one set to the empty string are the same intent, and the
        empty string would otherwise bind every interface."""
        monkeypatch.setenv(serve.HOST_VARIABLE, "")
        assert serve.bind_host() == "127.0.0.1"


class TestThePort:
    def test_the_default_is_used_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(serve.PORT_VARIABLE, raising=False)
        assert serve.bind_port() == 8080

    def test_a_configured_port_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(serve.PORT_VARIABLE, "9001")
        assert serve.bind_port() == 9001

    @pytest.mark.parametrize("value", ["eighty-eighty", "8080.5", "0x1f90"])
    def test_a_port_that_is_not_a_number_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Refused rather than silently defaulted. Falling back would start a server on a
        port nobody asked for, and the first symptom would be something else failing to
        connect to the port that was asked for."""
        monkeypatch.setenv(serve.PORT_VARIABLE, value)
        with pytest.raises(SystemExit, match="is not a number"):
            serve.bind_port()

    @pytest.mark.parametrize("value", ["0", "-1", "65536", "70000"])
    def test_a_port_outside_the_range_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(serve.PORT_VARIABLE, value)
        with pytest.raises(SystemExit, match="is not a port number"):
            serve.bind_port()


class TestTheEntryPointExists:
    """The defect this file was written for: `make console` ran
    `python -m firebreak.web.serve`, and that module did not exist."""

    def test_the_module_make_console_runs_is_importable(self) -> None:
        import importlib

        assert importlib.import_module("firebreak.web.serve") is serve

    def test_it_has_a_main_for_python_dash_m(self) -> None:
        assert callable(serve.main)

    def test_the_makefile_target_names_this_module(self) -> None:
        """The Makefile and the module have to agree, and nothing else checks that they
        do. This is the assertion that would have caught the broken target."""
        from pathlib import Path

        makefile = (Path(__file__).resolve().parents[2] / "Makefile").read_text(encoding="utf-8")
        assert "python -m firebreak.web.serve" in makefile

    def test_it_builds_an_application_without_starting_one(self) -> None:
        """`create_app` is a factory precisely so importing this module starts no server,
        which is what lets these tests run at all."""
        from firebreak.web.app import create_app

        assert create_app() is not create_app()
