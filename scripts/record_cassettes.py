"""Record replay cassettes for the showcase incidents, so the demo needs no model.

SPEC.md Section 9.6 and Section 17 Phase 11. A cassette is one model response keyed by a
hash of what was asked, so a later run in `replay` mode serves the recorded answer and the
graph cannot tell the difference.

**Why this is the most valuable thing in this phase.** Every unmeasurable result in the
last three phase reports traces back to having no model. Cassettes do not fix that, and
they do fix something adjacent and nearly as useful: once a real run has happened, its
transcript can be replayed for ever, in CI, offline, on a laptop with no credentials. The
demo becomes a real investigation rather than a stub's imitation of one, and it stays
reproducible byte for byte.

**Recorded from whatever mode is configured, which today is stub.** A cassette recorded
from a stub is honest and is not interesting: it serves back what the stub would have said
anyway. The value arrives the first time this is run with credentials, and the file format
and the replay path are identical either way, which is the point of building it now. Every
cassette records which mode produced it, and `make demo-offline` says so, because a demo
replaying stub answers must not look like a demo replaying a model.

**Keyed by purpose and payload, not by call order.** `firebreak.agent.llm.cassette_key`
hashes both, so a cassette survives the graph asking its questions in a different order,
which it will as soon as the loop changes.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from firebreak.agent.graph import investigate, stub_handlers  # noqa: E402
from firebreak.agent.llm import LlmClient, cassette_key  # noqa: E402
from firebreak.demo.showcase import (  # noqa: E402
    SHOWCASE_RUN_ID,
    SHOWCASE_SEED,
    build_showcase,
)
from firebreak.settings import LlmMode, Settings  # noqa: E402

CASSETTES_DIR = REPO_ROOT / "recordings" / "cassettes"

# SPEC.md Section 17 Phase 11 asks for ten. Fewer is reported rather than padded: a
# cassette for an incident that was never recorded would be a cassette for nothing.
SHOWCASE_COUNT = 10


@dataclass
class RecordingClient:
    """Wraps a real client and writes every answer to a cassette.

    A wrapper rather than a flag on `LlmClient`, so the client that runs in production has
    no code path that writes files. Recording is a development activity and lives in a
    development object.
    """

    inner: LlmClient
    directory: Path
    written: list[str]

    def complete(self, purpose: str, payload: dict[str, Any], schema: Any, tier: Any = None) -> Any:
        parsed, completion = (
            self.inner.complete(purpose, payload, schema, tier=tier)
            if tier is not None
            else self.inner.complete(purpose, payload, schema)
        )
        key = cassette_key(purpose, payload)
        path = self.directory / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "purpose": purpose,
                    "answer": json.loads(parsed.model_dump_json()),
                    "model": completion.model,
                    "tier": completion.tier.value,
                    "tokens_in": completion.tokens_in,
                    "tokens_out": completion.tokens_out,
                    "usd": completion.usd,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.written.append(key)
        return parsed, completion

    # `LlmClient`'s other surface, forwarded so the graph cannot tell it is wrapped.
    @property
    def budget(self) -> Any:
        return self.inner.budget

    @budget.setter
    def budget(self, value: Any) -> None:
        self.inner.budget = value

    @property
    def mode(self) -> LlmMode:
        return self.inner.mode

    @property
    def calls(self) -> list[str]:
        return self.inner.calls

    @property
    def tiers_requested(self) -> list[Any]:
        return self.inner.tiers_requested

    @property
    def tier_override(self) -> Any:
        return self.inner.tier_override

    @tier_override.setter
    def tier_override(self, value: Any) -> None:
        self.inner.tier_override = value

    def effective_tier(self, requested: Any) -> Any:
        return self.inner.effective_tier(requested)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=SHOWCASE_COUNT, help="how many incidents to record"
    )
    parser.add_argument("--out", default=str(CASSETTES_DIR), help="where to write the cassettes")
    arguments = parser.parse_args()

    settings = Settings()
    directory = Path(arguments.out)
    recorded: dict[str, Any] = {}

    # Built into a temporary workspace from the scenario specs, deterministically, so the
    # cassettes replay anywhere rather than only where the recorded library happens to
    # live. The demo rebuilds the same bundles the same way.
    with tempfile.TemporaryDirectory(prefix="firebreak-cassettes-") as workspace:
        incidents = build_showcase(Path(workspace))[: arguments.limit or None]
        for incident in incidents:
            bundle = Path(workspace) / incident.bundle_id
            client = LlmClient(mode=settings.llm_mode, stub_handlers=stub_handlers())
            wrapper = RecordingClient(
                inner=client, directory=directory / incident.bundle_id, written=[]
            )
            result = investigate(bundle, llm=wrapper)  # type: ignore[arg-type]
            recorded[incident.bundle_id] = {
                "scenario_id": incident.spec.id,
                "reason": incident.reason,
                "cassettes": sorted(set(wrapper.written)),
                "root_cause_service": result.report.root_cause_service,
                "abstained": result.report.abstained,
                "gate_passed": result.gate.passed,
                "claims": len(result.report.claims),
            }
            named = result.report.root_cause_service or "abstained"
            print(f"{incident.spec.id:46s} {len(set(wrapper.written)):2d} cassette(s)  {named}")

    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                # The mode is the most important field here. A cassette recorded from a
                # stub serves back what the stub would have said, and a demo replaying
                # those must not look like a demo replaying a model.
                "recorded_from_mode": settings.llm_mode.value,
                "recorded_at": datetime.now(UTC).isoformat(),
                "run_id": SHOWCASE_RUN_ID,
                "seed": SHOWCASE_SEED,
                "incidents": recorded,
                "showcase_target": SHOWCASE_COUNT,
                "showcase_actual": len(recorded),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    total = sum(len(entry["cassettes"]) for entry in recorded.values())
    print()
    print(f"{total} cassette(s) across {len(recorded)} incident(s), mode={settings.llm_mode.value}")
    if settings.llm_mode is LlmMode.STUB:
        print(
            "Recorded from stub, so these replay what the stub would have said. Re-run "
            "with credentials to make the offline demo a real investigation."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
