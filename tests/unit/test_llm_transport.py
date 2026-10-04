"""Exercise the real client against a provider-shaped HTTP transport."""

import json

import httpx
import pytest
from pydantic import BaseModel

from firebreak.agent.budget import BudgetExceededError, BudgetLimits, BudgetState
from firebreak.agent.llm import LlmClient, Tier
from firebreak.agent.models import ModelSpec, TierConfig, load_model_config
from firebreak.settings import LlmMode


class Answer(BaseModel):
    answer: str


def client(handler):
    config = load_model_config().model_copy(
        update={
            "tiers": {
                tier.value: TierConfig(models=(ModelSpec(id=f"test-{tier}", provider="test"),))
                for tier in Tier
            }
        }
    )
    return LlmClient(
        mode=LlmMode.LOCAL,
        base_url="http://localhost:11434/v1",
        api_key="test-placeholder",
        model_config=config,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )


def response(text):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4},
        },
    )


@pytest.mark.parametrize("mode", [LlmMode.LOCAL, LlmMode.API])
def test_real_modes_send_schema_and_parse_usage(mode):
    requests = []

    def handler(request):
        requests.append(request)
        return response('{"answer":"payment"}')

    llm = client(handler)
    llm.mode = mode
    answer, completion = llm.complete("specialist", {"service": "payment"}, Answer)
    assert answer.answer == "payment"
    assert completion.tokens_in == 12
    assert completion.tokens_out == 4
    assert completion.model == "test-small"
    assert requests[0].url.path == "/v1/chat/completions"
    body = json.loads(requests[0].content)
    assert "answer" in body["messages"][0]["content"]
    assert body["response_format"] == {"type": "json_object"}


def test_schema_repair_then_escalation_accounts_for_every_response():
    models = []

    def handler(request):
        model = json.loads(request.content)["model"]
        models.append(model)
        return response("not json" if model == "test-small" else '{"answer":"payment"}')

    answer, completion = client(handler).complete("specialist", {}, Answer)
    assert answer.answer == "payment"
    assert models == ["test-small", "test-small", "test-strong"]
    assert completion.escalated and completion.repaired
    assert completion.tokens_in == 36


def test_rate_limit_is_retried_without_escalation():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(429) if len(calls) == 1 else response('{"answer":"ok"}')

    client(handler).complete("specialist", {}, Answer)
    assert calls == ["test-small", "test-small"]


def test_token_budget_refuses_request_before_sending():
    requests = []
    llm = client(lambda request: requests.append(request) or response('{"answer":"ok"}'))
    llm.budget = BudgetState(limits=BudgetLimits(max_tokens=1))
    with pytest.raises(BudgetExceededError):
        llm.complete("specialist", {}, Answer)
    assert not requests


def test_failed_schema_responses_remain_in_budget():
    llm = client(lambda _: response("invalid"))
    llm.budget = BudgetState()
    from firebreak.agent.llm import ModelUnavailableError

    with pytest.raises(ModelUnavailableError):
        llm.complete("specialist", {}, Answer)
    assert llm.budget.tokens == 64


def test_exhausted_provider_timeouts_fail_with_no_secret_body():
    from firebreak.agent.llm import ModelUnavailableError

    def timeout(request):
        raise httpx.ReadTimeout("private incident content", request=request)

    llm = client(timeout)
    llm.budget = BudgetState()
    with pytest.raises(ModelUnavailableError) as caught:
        llm.complete("specialist", {}, Answer)
    assert "private incident" not in str(caught.value)


def test_hosted_budget_refuses_unknown_price():
    from firebreak.agent.llm import ModelUnavailableError

    llm = client(lambda _: pytest.fail("must not spend without a price"))
    llm.mode = LlmMode.API
    llm.budget = BudgetState()
    with pytest.raises(ModelUnavailableError, match="sourced price"):
        llm.complete("specialist", {}, Answer)


def test_dollar_budget_checks_before_spending_and_charges_actual_usage():
    from firebreak.agent.models import Price

    llm = client(lambda _: response('{"answer":"ok"}'))
    price = Price(
        input_per_million_usd=10,
        output_per_million_usd=20,
        source="https://example.test/test-price",
        checked_on="2026-10-03",
    )
    llm.model_config = llm.model_config.model_copy(
        update={
            "tiers": {
                tier.value: TierConfig(
                    models=(ModelSpec(id="test-model", provider="test", price=price),)
                )
                for tier in Tier
            }
        }
    )
    llm.budget = BudgetState(limits=BudgetLimits(max_usd=0.000001))
    with pytest.raises(BudgetExceededError):
        llm.complete("specialist", {}, Answer)
    assert llm.budget.usd == 0
    llm.budget = BudgetState()
    _, completion = llm.complete("specialist", {}, Answer)
    assert llm.budget.usd == completion.usd == price.cost_usd(12, 4)


def test_empty_tier_refuses_to_guess_a_model():
    from firebreak.agent.llm import ModelUnavailableError

    llm = client(lambda _: pytest.fail("no configured model"))
    llm.model_config = load_model_config()
    with pytest.raises(ModelUnavailableError, match="no models configured"):
        llm.complete("specialist", {}, Answer)
