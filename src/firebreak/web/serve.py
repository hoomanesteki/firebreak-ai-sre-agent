"""Serving the Console.

`python -m firebreak.web.serve`, which is what `make console` runs.

**Localhost by default, and that is a security decision rather than a convenience.** The
Console renders incident telemetry: customer identifiers, internal hostnames, and whatever a
service under attack put in a log line. It has no authentication and no authorisation, because
adding either would be a login system nobody reviewed. So the default bind address is
`127.0.0.1`, and reaching it from another machine is a deliberate act with `FIREBREAK_WEB_HOST`
rather than the default behaviour of running the command.

**Nothing here prints.** Uvicorn logs the address it bound, which is the one line a reader
needs, and `print` under `src/` is a lint error for a good reason: library code that prints
cannot be embedded.
"""

from __future__ import annotations

import os

import uvicorn

from firebreak.web.app import create_app

# Loopback, so the Console is not published to the network by the act of starting it. See the
# module docstring: this is the no-authentication default, not a placeholder.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080

# Read directly rather than through Settings, because these are properties of one process
# rather than of the system under investigation, and Settings is frozen configuration shared
# with the agent. Named with the same prefix so `.env` works the way it does everywhere else.
HOST_VARIABLE = "FIREBREAK_WEB_HOST"
PORT_VARIABLE = "FIREBREAK_WEB_PORT"


def bind_host() -> str:
    return os.environ.get(HOST_VARIABLE) or DEFAULT_HOST


def bind_port() -> int:
    """The port to listen on, refusing a value that is not a usable port.

    A bad value is an error rather than a silent fall back to the default. Falling back
    would start a server on a port the operator did not ask for, and the first symptom
    would be something else failing to connect.
    """
    raw = os.environ.get(PORT_VARIABLE)
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        raise SystemExit(f"{PORT_VARIABLE}={raw!r} is not a number") from None
    if not 1 <= port <= 65535:
        raise SystemExit(f"{PORT_VARIABLE}={port} is not a port number")
    return port


def main() -> None:
    uvicorn.run(create_app(), host=bind_host(), port=bind_port(), log_level="info")


if __name__ == "__main__":
    main()
