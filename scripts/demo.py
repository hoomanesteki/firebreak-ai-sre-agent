"""`make demo`: run the best demo this machine can, and say which one it ran.

SPEC.md Section 17's Phase 12 acceptance criterion is that a fresh clone on another machine
runs `make demo`. It could not: the target went straight at the live stack, so on a clone with
no Docker it failed with a Compose error about a missing submodule, which tells a first-time
reader nothing about Firebreak.

**So this chooses.** With the live stack up it investigates a live incident. Without it, it
replays the offline showcase, which needs no model, no network and no Docker. Either way it says
which it chose and what would have changed the choice, because a demo that quietly runs the lesser
path is a demo that misrepresents what the reader just saw.

**It does not start Docker or the stack.** Bringing up a six-gigabyte Compose project is not
something a command called `demo` should do to somebody who typed it to see what this is. The
live path is offered by name, not taken silently.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

SUBMODULE = REPO_ROOT / "vendor" / "otel-demo" / "compose.yaml"
CASSETTE_MANIFEST = REPO_ROOT / "recordings" / "cassettes" / "manifest.json"


def docker_is_running() -> bool:
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(("docker", "info"), capture_output=True, text=True, check=False).returncode
        == 0
    )


def stack_is_up() -> bool:
    """Whether the demo's own frontend answers.

    Asks the application rather than counting containers, because a Compose project can be up
    with the frontend still starting, and an investigation against a half-started stack looks
    like an investigation of a healthy system.
    """
    if not docker_is_running():
        return False
    try:
        with urllib.request.urlopen("http://localhost:8080/", timeout=3) as response:
            return bool(200 <= response.status < 400)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return False


def has_credentials() -> bool:
    from firebreak.settings import Settings

    settings = Settings()
    return bool(settings.llm_base_url and settings.llm_api_key)


def run(command: tuple[str, ...]) -> int:
    print(f"\n$ {' '.join(command)}\n", flush=True)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(command, cwd=REPO_ROOT, env=environment, check=False).returncode


def main() -> int:
    live = stack_is_up()
    credentials = has_credentials()

    print("Firebreak demo")
    print(f"  live stack answering on :8080   {'yes' if live else 'no'}")
    print(f"  model credentials configured    {'yes' if credentials else 'no'}")

    if not CASSETTE_MANIFEST.is_file() and not live:
        print(
            "\nNothing to demo: there is no live stack and no replay cassettes.\n"
            "Run `make cassettes` to record the offline showcase, or `make live` for the stack.",
            file=sys.stderr,
        )
        return 1

    if live:
        print("\nRunning against the live stack.")
        if not credentials:
            print(
                "  No credentials, so the deterministic floor will publish triage's answer\n"
                "  labelled as having had no AI analysis. Set LLM_BASE_URL and LLM_API_KEY\n"
                "  for the full loop."
            )
        return run((sys.executable, "-m", "firebreak.cli.app", "investigate-live"))

    print("\nNo live stack, so running the offline showcase instead.")
    print("  It replays recorded answers against bundles rebuilt from scenario specs, with")
    print("  no model, no network and no Docker. `make live` then `make demo` investigates a")
    print("  live incident instead.")
    return run((sys.executable, str(REPO_ROOT / "scripts" / "demo_offline.py")))


if __name__ == "__main__":
    raise SystemExit(main())
