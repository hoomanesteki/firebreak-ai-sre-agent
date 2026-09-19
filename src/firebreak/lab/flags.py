"""Read and set the demo's feature flags.

Faults are injected by changing a flag's default variant. The lab goes
through flagd-ui's REST API rather than editing the flag file, so nothing
under `vendor/` is touched and no volume has to be remapped.

Writing replaces the whole configuration, which is how the upstream API
works, so every write is a read, one change, and a write back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from firebreak.lab.endpoints import DEFAULT_ENDPOINTS, DEMO_TAG, DemoEndpoints

OFF_VARIANT = "off"
DEFAULT_TIMEOUT_SECONDS = 10.0


class FlagError(Exception):
    """Base class for flag control failures."""


class UnknownFlagError(FlagError):
    """The demo does not define this flag at the pinned tag."""


class UnknownVariantError(FlagError):
    """The flag exists but does not offer this variant."""


class FlagVerificationError(FlagError):
    """The change was written but the running system did not take it."""


@dataclass(frozen=True)
class FlagState:
    """One flag as the demo currently has it configured."""

    name: str
    default_variant: str
    variants: tuple[str, ...]
    state: str

    @property
    def is_on(self) -> bool:
        """True when the flag is set to anything other than off."""
        return self.default_variant != OFF_VARIANT


def parse_flag_config(config: dict[str, Any]) -> dict[str, FlagState]:
    """Turn a flagd configuration document into typed flag states."""
    flags = config.get("flags")
    if not isinstance(flags, dict):
        raise FlagError("flag configuration has no 'flags' object")
    parsed: dict[str, FlagState] = {}
    for name, body in flags.items():
        if not isinstance(body, dict):
            raise FlagError(f"flag {name!r} is not an object")
        variants = body.get("variants")
        if not isinstance(variants, dict):
            raise FlagError(f"flag {name!r} has no variants")
        parsed[name] = FlagState(
            name=name,
            default_variant=str(body.get("defaultVariant", "")),
            variants=tuple(variants),
            state=str(body.get("state", "")),
        )
    return parsed


def read_vendored_defaults(flag_file: Path) -> dict[str, str]:
    """Read the resting variant of every flag from the pinned demo's own file.

    This is the only thing the lab needs the vendored file for, and it is what
    `reset` restores to, so a recording never inherits a flag another run left
    on.
    """
    config = json.loads(flag_file.read_text(encoding="utf-8"))
    return {name: state.default_variant for name, state in parse_flag_config(config).items()}


class FlagController:
    """Reads and writes the demo's flag configuration over HTTP."""

    def __init__(
        self,
        client: httpx.Client,
        endpoints: DemoEndpoints = DEFAULT_ENDPOINTS,
    ) -> None:
        self._client = client
        self._endpoints = endpoints

    def read_config(self) -> dict[str, Any]:
        """Fetch the whole flag configuration document."""
        response = self._client.get(f"{self._endpoints.flag_api}/read")
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise FlagError("flag API returned a non-object body")
        return body

    def list_flags(self) -> dict[str, FlagState]:
        """Return every flag the demo defines, with its current variant."""
        return parse_flag_config(self.read_config())

    def get_variant(self, flag: str) -> str:
        """Return the configured default variant of one flag."""
        flags = self.list_flags()
        if flag not in flags:
            raise UnknownFlagError(f"{flag!r} is not defined at demo tag {DEMO_TAG}")
        return flags[flag].default_variant

    def set_variant(self, flag: str, variant: str) -> FlagState:
        """Set one flag's default variant and confirm the write landed."""
        config = self.read_config()
        flags = parse_flag_config(config)
        if flag not in flags:
            raise UnknownFlagError(f"{flag!r} is not defined at demo tag {DEMO_TAG}")
        if variant not in flags[flag].variants:
            offered = ", ".join(sorted(flags[flag].variants))
            raise UnknownVariantError(f"{flag!r} has no variant {variant!r}; it offers {offered}")

        config["flags"][flag]["defaultVariant"] = variant
        self._write_config(config)

        written = self.get_variant(flag)
        if written != variant:
            raise FlagVerificationError(
                f"wrote {flag}={variant} but the configuration reads back as {written!r}"
            )
        return FlagState(
            name=flag,
            default_variant=variant,
            variants=flags[flag].variants,
            state=flags[flag].state,
        )

    def reset_to(self, defaults: dict[str, str]) -> list[str]:
        """Restore every flag that has drifted from its resting variant.

        Returns the flags that were changed, so a recorder can log that the
        system started clean rather than assume it.
        """
        config = self.read_config()
        flags = parse_flag_config(config)
        changed: list[str] = []
        for name, resting in defaults.items():
            if name not in flags:
                continue
            if flags[name].default_variant != resting:
                config["flags"][name]["defaultVariant"] = resting
                changed.append(name)
        if changed:
            self._write_config(config)
        return sorted(changed)

    def evaluate(self, flag: str) -> str:
        """Ask flagd which variant it is actually serving.

        The configuration read says what was written. This says what the
        running system decided, which is what the injected fault depends on.
        """
        url = f"{self._endpoints.flagd_ofrep}/ofrep/v1/evaluate/flags/{flag}"
        response = self._client.post(url, json={"context": {}})
        if response.status_code == 404:
            raise UnknownFlagError(f"flagd does not know {flag!r}")
        response.raise_for_status()
        body = response.json()
        variant = body.get("variant") if isinstance(body, dict) else None
        if not isinstance(variant, str):
            raise FlagVerificationError(f"flagd returned no variant for {flag!r}")
        return variant

    def _write_config(self, config: dict[str, Any]) -> None:
        response = self._client.post(f"{self._endpoints.flag_api}/write", json={"data": config})
        response.raise_for_status()
