"""The model client, with the four modes SPEC.md Section 13 names.

`stub` answers from deterministic rules with no network and no model. `replay`
serves recorded responses keyed by prompt hash. `local` and `api` call a real
endpoint. Every mode returns the same shape, so a node cannot tell which it is
talking to, which is the point: the graph is tested in `stub` and run in `api`
without changing a line of it.

**Why stub is not a mock.** A mock returns whatever a test told it to and
proves the test wrote a mock. This stub reads the brief it is given and answers
from the evidence in it, deterministically. That makes an integration test in
stub mode a real test of the graph's wiring, its budgets and its gates, and it
makes the deterministic floor in SPEC.md Section 6.7 reachable without a model
at all.

**Structured output, validated, with one repair.** Every call names the model
it expects back. A response that does not parse is retried once with the
validation error appended, because the common failure is a near miss a model
can fix when shown it. A second failure escalates a tier rather than retrying,
per SPEC.md Section 6.7: retrying a schema error at the same tier is how a
budget disappears into a model that cannot produce the shape.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from firebreak.settings import LlmMode

ModelT = TypeVar("ModelT", bound=BaseModel)

# SPEC.md Section 6.7. A schema failure is repaired once by showing the model
# its own error, and never twice: the second failure escalates.
MAX_REPAIRS = 1


class Tier(StrEnum):
    """Which class of model a call wants.

    SPEC.md Section 6.7. `small` for specialists and extraction, `strong` for
    the commander, critic and reporter, `judge` for offline evaluation only and
    from a different family where possible.
    """

    SMALL = "small"
    STRONG = "strong"
    JUDGE = "judge"


class LlmError(Exception):
    """A model call could not be made, or could not be made to produce a shape."""


class ModelUnavailableError(LlmError):
    """Every model in a tier failed.

    Distinct from a schema failure because the response is different: SPEC.md
    Section 6.7 falls back to the deterministic floor here, and retries there.
    """


@dataclass(frozen=True)
class Completion:
    """One model response, and what it cost."""

    text: str
    model: str
    tier: Tier
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    repaired: bool = False
    escalated: bool = False


@dataclass
class LlmClient:
    """Calls a model, or something standing in for one.

    `stub_handlers` maps a call's `purpose` to a function producing the
    structured answer. A purpose with no handler in stub mode is an error
    rather than an empty answer, because a node silently receiving nothing
    would look like a model that had nothing to say.
    """

    mode: LlmMode = LlmMode.STUB
    stub_handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = field(
        default_factory=dict
    )
    recordings_dir: Path | None = None
    calls: list[str] = field(default_factory=list)

    def complete(
        self,
        purpose: str,
        payload: dict[str, Any],
        schema: type[ModelT],
        tier: Tier = Tier.SMALL,
    ) -> tuple[ModelT, Completion]:
        """Ask for a structured answer, and get one or raise.

        `purpose` names what is being asked rather than which prompt is being
        used, so a stub handler and a replay cassette key off the same thing and
        a prompt can be rewritten without invalidating either.
        """
        self.calls.append(purpose)
        if self.mode is LlmMode.STUB:
            return self._stub(purpose, payload, schema, tier)
        if self.mode is LlmMode.REPLAY:
            return self._replay(purpose, payload, schema, tier)
        raise ModelUnavailableError(
            f"{self.mode.value} mode needs a configured endpoint; set LLM_BASE_URL and "
            "LLM_API_KEY, or run in stub mode"
        )

    def _stub(
        self, purpose: str, payload: dict[str, Any], schema: type[ModelT], tier: Tier
    ) -> tuple[ModelT, Completion]:
        handler = self.stub_handlers.get(purpose)
        if handler is None:
            raise LlmError(
                f"no stub handler for {purpose!r}; known: "
                f"{', '.join(sorted(self.stub_handlers)) or 'none'}"
            )
        answer = handler(payload)
        try:
            parsed = schema.model_validate(answer)
        except ValidationError as error:
            # A stub producing the wrong shape is a bug in the stub, not a
            # model to be repaired, so it fails loudly rather than retrying.
            raise LlmError(
                f"the stub handler for {purpose!r} produced a shape "
                f"{schema.__name__} rejects: {error}"
            ) from error
        return parsed, Completion(
            text=json.dumps(answer, sort_keys=True, default=str),
            model="stub",
            tier=tier,
            # Deliberately zero. A stub costs nothing, and reporting an invented
            # token count would put fiction into the cost column of every eval
            # run made in stub mode.
            tokens_in=0,
            tokens_out=0,
            usd=0.0,
        )

    def _replay(
        self, purpose: str, payload: dict[str, Any], schema: type[ModelT], tier: Tier
    ) -> tuple[ModelT, Completion]:
        """Serve a recorded response, keyed by what was asked.

        Keyed on a hash of the purpose and the payload rather than on a call
        index, so a cassette survives the graph asking its questions in a
        different order, which it will as soon as the loop changes.
        """
        if self.recordings_dir is None:
            raise LlmError("replay mode needs a recordings directory")
        key = cassette_key(purpose, payload)
        path = self.recordings_dir / f"{key}.json"
        if not path.is_file():
            raise LlmError(
                f"no recorded response for {purpose!r} under {key}; record one in api "
                "mode first, or run in stub mode"
            )
        recorded = json.loads(path.read_text(encoding="utf-8"))
        parsed = schema.model_validate(recorded["answer"])
        return parsed, Completion(
            text=json.dumps(recorded["answer"], sort_keys=True, default=str),
            model=str(recorded.get("model", "replay")),
            tier=tier,
            tokens_in=int(recorded.get("tokens_in", 0)),
            tokens_out=int(recorded.get("tokens_out", 0)),
            usd=float(recorded.get("usd", 0.0)),
        )


def cassette_key(purpose: str, payload: dict[str, Any]) -> str:
    """A stable key for one question.

    Sorted keys and a fixed separator, so that two payloads that differ only in
    dictionary order produce the same key. Without that a cassette recorded on
    one run would miss on the next for no reason a reader could see.
    """
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(f"{purpose}\x00{body}".encode()).hexdigest()
    return f"{purpose}_{digest[:16]}"
