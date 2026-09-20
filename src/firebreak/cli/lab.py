"""`firebreak lab` commands: drive the target system and check it responds."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

from firebreak.lab.endpoints import DEFAULT_ENDPOINTS, DEMO_TAG
from firebreak.lab.flags import (
    FlagController,
    FlagError,
    read_flag_file,
    read_vendored_defaults,
)
from firebreak.lab.load import LoadController, LoadError
from firebreak.lab.smoke import EXCLUDED_FLAGS, build_report, smoke_one_flag
from firebreak.lab.stack import render_flag_store, render_prometheus_config
from firebreak.lab.verify import build_report as build_verification_report
from firebreak.lab.verify import run_checks
from firebreak.lab.webhook import AlertSink, build_server

REPO_ROOT = Path(__file__).resolve().parents[3]
VENDORED_FLAG_FILE = REPO_ROOT / "vendor" / "otel-demo" / "src" / "flagd" / "demo.flagd.json"
VENDORED_PROMETHEUS_CONFIG = (
    REPO_ROOT / "vendor" / "otel-demo" / "src" / "prometheus" / "prometheus-config.yaml"
)
GENERATED_PROMETHEUS_CONFIG = REPO_ROOT / "ops" / "generated" / "prometheus-config.yaml"
GENERATED_FLAG_DIR = REPO_ROOT / "ops" / "generated" / "flagd"
SMOKE_REPORT = REPO_ROOT / "reports" / "lab" / "flag_smoke.json"
INVENTORY_REPORT = REPO_ROOT / "reports" / "lab" / "flag_inventory.json"
# Verification reports are evidence for a phase gate, so a later failing
# run must not overwrite the record of a passing one. Each run writes its
# own file named for the moment it ran.
VERIFICATION_DIR = REPO_ROOT / "reports" / "lab" / "live_verification"
WEBHOOK_LOG = REPO_ROOT / "reports" / "lab" / "alerts.jsonl"
HTTP_TIMEOUT_SECONDS = 15.0

lab_app = typer.Typer(help="Drive the pinned OpenTelemetry Demo.", no_args_is_help=True)
flags_app = typer.Typer(help="Read and set feature flags.", no_args_is_help=True)
load_app = typer.Typer(help="Control the load generator.", no_args_is_help=True)
lab_app.add_typer(flags_app, name="flags")
lab_app.add_typer(load_app, name="load")
console = Console()


def _client() -> httpx.Client:
    return httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)


def _fail(message: str) -> None:
    console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=1)


@flags_app.command("list")
def list_flags(
    only_on: bool = typer.Option(False, "--only-on", help="Show only active flags"),
) -> None:
    """List every flag the demo defines and its current variant."""
    with _client() as client:
        try:
            flags = FlagController(client).list_flags()
        except (FlagError, httpx.HTTPError) as error:
            _fail(f"could not read flags: {error}")
            return
    table = Table(title=f"Feature flags (demo {DEMO_TAG})")
    table.add_column("Flag")
    table.add_column("Variant")
    table.add_column("Offers")
    for name in sorted(flags):
        state = flags[name]
        if only_on and not state.is_on:
            continue
        table.add_row(name, state.default_variant, ", ".join(sorted(state.variants)))
    console.print(table)


@flags_app.command("inventory")
def flag_inventory() -> None:
    """Write every flag and variant in the pinned demo to a report.

    Reads the vendored file, so this needs no running stack. Phase 2 checks
    scenario specs against it instead of against flag names typed by hand.
    """
    if not VENDORED_FLAG_FILE.is_file():
        _fail(
            f"pinned flag file not found at {VENDORED_FLAG_FILE}; run git submodule update --init"
        )
        return
    try:
        flags = read_flag_file(VENDORED_FLAG_FILE)
    except FlagError as error:
        _fail(str(error))
        return
    report = {
        "demo_tag": DEMO_TAG,
        "source": str(VENDORED_FLAG_FILE.relative_to(REPO_ROOT)),
        "flag_count": len(flags),
        "flags": {
            name: {
                "resting_variant": state.default_variant,
                "variants": sorted(state.variants),
                "state": state.state,
            }
            for name, state in sorted(flags.items())
        },
    }
    INVENTORY_REPORT.parent.mkdir(parents=True, exist_ok=True)
    INVENTORY_REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    console.print(
        f"[green]{len(flags)} flag(s) at demo {DEMO_TAG}; "
        f"wrote {INVENTORY_REPORT.relative_to(REPO_ROOT)}[/green]"
    )


@flags_app.command("get")
def get_flag(flag: str) -> None:
    """Show one flag's configured variant and the variant flagd serves."""
    with _client() as client:
        controller = FlagController(client)
        try:
            configured = controller.get_variant(flag)
        except (FlagError, httpx.HTTPError) as error:
            _fail(f"could not read {flag}: {error}")
            return
        try:
            served = controller.evaluate(flag)
        except (FlagError, httpx.HTTPError) as error:
            served = f"unavailable ({error})"
    console.print(f"{flag}: configured={configured} served={served}")


@flags_app.command("set")
def set_flag(flag: str, variant: str) -> None:
    """Set one flag's variant and wait until flagd serves it."""
    with _client() as client:
        controller = FlagController(client)
        try:
            _state, waited = controller.apply_fault(flag, variant)
        except (FlagError, httpx.HTTPError) as error:
            _fail(f"could not set {flag}={variant}: {error}")
            return
    console.print(f"[green]{flag} = {variant}, served by flagd after {waited:.2f}s[/green]")


@flags_app.command("reset")
def reset_flags() -> None:
    """Restore every flag to the resting variant in the pinned demo's file."""
    if not VENDORED_FLAG_FILE.is_file():
        _fail(
            f"pinned flag file not found at {VENDORED_FLAG_FILE}; run git submodule update --init"
        )
        return
    try:
        defaults = read_vendored_defaults(VENDORED_FLAG_FILE)
    except FlagError as error:
        _fail(str(error))
        return
    with _client() as client:
        try:
            changed = FlagController(client).reset_to(defaults)
        except (FlagError, httpx.HTTPError) as error:
            _fail(f"could not reset flags: {error}")
            return
    if changed:
        console.print(f"[green]reset {len(changed)} flag(s): {', '.join(changed)}[/green]")
    else:
        console.print("all flags were already at their resting variant")


@load_app.command("set")
def set_load(
    users: int,
    spawn_rate: float = typer.Option(5.0, "--spawn-rate", help="Users added per second"),
) -> None:
    """Ramp the load generator to a user count and wait for it to get there."""
    with _client() as client:
        try:
            state = LoadController(client).set_users(users, spawn_rate, wait=True)
        except (LoadError, httpx.HTTPError) as error:
            _fail(f"could not set load: {error}")
            return
    console.print(f"[green]load: {state.user_count} user(s), state {state.state}[/green]")


@load_app.command("stop")
def stop_load() -> None:
    """Stop generating load."""
    with _client() as client:
        try:
            state = LoadController(client).stop()
        except (LoadError, httpx.HTTPError) as error:
            _fail(f"could not stop load: {error}")
            return
    console.print(f"load stopped, state {state.state}")


@load_app.command("status")
def load_status() -> None:
    """Show the load generator's state and user count."""
    with _client() as client:
        try:
            state = LoadController(client).read_state()
        except (LoadError, httpx.HTTPError) as error:
            _fail(f"could not read load state: {error}")
            return
    console.print(f"state={state.state} users={state.user_count}")


@lab_app.command("render-config")
def render_config(
    keep_flags: bool = typer.Option(
        False, "--keep-flags", help="Leave an existing flag store alone"
    ),
) -> None:
    """Generate the Prometheus config and flag store the live stack mounts."""
    prometheus = render_prometheus_config(VENDORED_PROMETHEUS_CONFIG, GENERATED_PROMETHEUS_CONFIG)
    flags = render_flag_store(VENDORED_FLAG_FILE, GENERATED_FLAG_DIR, overwrite=not keep_flags)
    console.print(f"[green]wrote {prometheus.relative_to(REPO_ROOT)}[/green]")
    console.print(f"[green]wrote {flags.relative_to(REPO_ROOT)}[/green]")


@lab_app.command("verify")
def verify(
    flag: str = typer.Option("paymentFailure", "--flag", help="Flag to evaluate"),
) -> None:
    """Check that the running stack is fit to record incidents from."""
    with _client() as client:
        checks = run_checks(client, DEFAULT_ENDPOINTS, flag, time.time())

    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT / "vendor" / "otel-demo",
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    report = build_verification_report(checks, DEMO_TAG, vendor_clean=not dirty)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = VERIFICATION_DIR / f"{stamp}.json"
    VERIFICATION_DIR.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    table = Table(title=f"Live stack verification (demo {DEMO_TAG})")
    table.add_column("Check")
    table.add_column("Result")
    table.add_column("Measured")
    for check in checks:
        mark = "[green]pass[/green]" if check.passed else "[red]fail[/red]"
        table.add_row(check.name, mark, check.detail)
    console.print(table)
    console.print(f"wrote {destination.relative_to(REPO_ROOT)}")
    if not report["ready_to_record"]:
        _fail(f"not ready to record: {', '.join(report['failed_checks']) or 'vendor dirty'}")


@lab_app.command("webhook")
def webhook(port: int = typer.Option(8000, "--port", help="Port to listen on")) -> None:
    """Receive Alertmanager deliveries and append them to a file."""
    sink = AlertSink(WEBHOOK_LOG, listener=console.print)
    server = build_server(port, sink)
    console.print(f"listening on http://127.0.0.1:{port}/, writing {WEBHOOK_LOG.name}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print(f"stopped after {sink.count} delivery(ies)")
    finally:
        server.server_close()


@lab_app.command("smoke")
def smoke(
    hold_seconds: float = typer.Option(120.0, "--hold", help="Seconds to hold each flag on"),
    flag: list[str] | None = typer.Option(None, "--flag", help="Test only these flags"),
) -> None:
    """Turn each flag on in turn and record whether the signals moved."""
    with _client() as client:
        controller = FlagController(client)
        try:
            states = controller.list_flags()
        except (FlagError, httpx.HTTPError) as error:
            _fail(f"could not read flags: {error}")
            return

        selected = sorted(
            name for name in states if name not in EXCLUDED_FLAGS and (not flag or name in flag)
        )
        if not selected:
            _fail("no flags selected")
            return

        results = []
        for name in selected:
            console.print(f"testing {name} ...")
            results.append(
                smoke_one_flag(controller, client, DEFAULT_ENDPOINTS, states[name], hold_seconds)
            )
        controller.reset_to(read_vendored_defaults(VENDORED_FLAG_FILE))

    report = build_report(results, DEMO_TAG, hold_seconds)
    SMOKE_REPORT.parent.mkdir(parents=True, exist_ok=True)
    SMOKE_REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    observed = report["flags_with_observed_effect"]
    console.print(
        f"[green]{observed} of {len(results)} flag(s) moved a signal; "
        f"wrote {SMOKE_REPORT.relative_to(REPO_ROOT)}[/green]"
    )
