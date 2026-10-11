from types import SimpleNamespace

import pytest

from auditor.budget import BudgetedClient, BudgetExceeded


class Inner:
    scripted = True

    def __init__(self, tokens=(100, 50, 0)):
        self.tokens, self.calls, self.messages = tokens, 0, self

    async def create(self, **kw):
        self.calls += 1
        i, o, c = self.tokens
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=i, output_tokens=o, cache_creation_input_tokens=c))


async def test_counts_usage_and_stops_before_exceeding_nothing_more_is_ever_sent():
    inner = Inner((100, 50, 25))
    c = BudgetedClient(inner, budget=300)
    await c.messages.create(model="m")
    await c.messages.create(model="m")  # 175 + 175 = 350 > 300: the second call is allowed (checked before), the third is not
    assert c.used == 350 and c.exhausted and inner.calls == 2
    with pytest.raises(BudgetExceeded, match="exhausted"):
        await c.messages.create(model="m")
    assert inner.calls == 2  # the refused call never reached the model


async def test_zero_budget_refuses_everything_and_other_attributes_are_forwarded():
    inner = Inner()
    c = BudgetedClient(inner, budget=0)
    with pytest.raises(BudgetExceeded):
        await c.messages.create(model="m")
    assert inner.calls == 0 and c.scripted is True  # `scripted` marker still visible through the wrapper


async def test_a_response_without_usage_does_not_crash_accounting():
    class Bare:
        def __init__(self):
            self.messages = self

        async def create(self, **kw):
            return SimpleNamespace()

    c = BudgetedClient(Bare(), budget=10)
    await c.messages.create()
    assert c.used == 0 and c.calls == 1


async def test_a_failing_call_is_not_counted_as_spend():
    class Boom:
        def __init__(self):
            self.messages = self

        async def create(self, **kw):
            raise RuntimeError("api down")

    c = BudgetedClient(Boom(), budget=100)
    with pytest.raises(RuntimeError):
        await c.messages.create()
    assert c.used == 0 and c.calls == 0


# ---------------------------------------------------------------- refusal fallback
class BetaInner:
    def __init__(self, fail_beta_with=None):
        self.beta_calls, self.plain_calls, self.fail = [], [], fail_beta_with
        self.messages = SimpleNamespace(create=self._plain)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._beta))

    async def _plain(self, **kw):
        self.plain_calls.append(kw)
        return SimpleNamespace(content=[], usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0))

    async def _beta(self, **kw):
        if self.fail:
            raise self.fail
        self.beta_calls.append(kw)
        block = SimpleNamespace(type="fallback")
        return SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0))


class ApiError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status_code = status


async def test_fallback_is_requested_when_supported_and_counted_when_used():
    inner = BetaInner()
    c = BudgetedClient(inner, 10_000)
    await c.messages.create(model="m", max_tokens=5, messages=[])
    assert inner.beta_calls and inner.beta_calls[0]["fallbacks"] == "default"
    assert inner.beta_calls[0]["betas"] == ["server-side-fallback-2026-07-01"] and inner.beta_calls[0]["model"] == "m"
    assert inner.plain_calls == [] and c.fallbacks_used == 1 and c.used == 15


async def test_a_client_without_the_beta_surface_just_uses_the_plain_call():
    c = BudgetedClient(Inner(), 10_000)
    assert c.refusal_fallback is False
    await c.messages.create(model="m")


async def test_if_the_server_rejects_the_beta_it_is_switched_off_once_and_the_call_still_succeeds():
    inner = BetaInner(fail_beta_with=ApiError(400, "Unexpected value(s) for the `anthropic-beta` header"))
    c = BudgetedClient(inner, 10_000)
    await c.messages.create(model="m")
    await c.messages.create(model="m")
    assert len(inner.plain_calls) == 2 and c.refusal_fallback is False  # recovered, and did not retry the beta again


async def test_an_unrelated_api_error_is_not_swallowed_by_the_fallback_logic():
    inner = BetaInner(fail_beta_with=ApiError(500, "internal error"))
    c = BudgetedClient(inner, 10_000)
    with pytest.raises(ApiError):
        await c.messages.create(model="m")
    assert c.refusal_fallback is True and inner.plain_calls == []  # a real outage is reported, not hidden


async def test_fallback_can_be_disabled_by_configuration():
    inner = BetaInner()
    c = BudgetedClient(inner, 10_000, refusal_fallback=False)
    await c.messages.create(model="m")
    assert inner.beta_calls == [] and len(inner.plain_calls) == 1
