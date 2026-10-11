"""A hard ceiling on what one run can spend with the language model."""

from __future__ import annotations


class BudgetExceeded(RuntimeError):
    pass


class _Messages:
    def __init__(self, outer: BudgetedClient):
        self._o = outer

    async def create(self, **kw):
        o = self._o
        if o.used >= o.budget:  # checked BEFORE spending: once over, no further call is ever made
            raise BudgetExceeded(f"model token budget of {o.budget} exhausted ({o.used} used)")
        resp = await o._create(kw)
        u = getattr(resp, "usage", None)
        if u is not None:
            o.used += sum(int(getattr(u, k, 0) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens"))
        o.calls += 1
        return resp


FALLBACK_BETA = "server-side-fallback-2026-07-01"


class BudgetedClient:
    """Wraps an Anthropic-style client. Counts input+output(+cache write) tokens; refuses calls once over budget.
    Everything else (e.g. the `scripted` marker) is forwarded to the wrapped client."""

    def __init__(self, inner, budget: int, refusal_fallback: bool = True):
        self._inner, self.budget, self.used, self.calls = inner, int(budget), 0, 0
        # If the primary model declines a request, let the API re-run it on a fallback model instead of failing the
        # finding. Used only when the wrapped client supports the beta; switched off for good if the server rejects it.
        self.refusal_fallback = bool(refusal_fallback) and hasattr(getattr(inner, "beta", None), "messages")
        self.fallbacks_used = 0
        self.messages = _Messages(self)

    async def _create(self, kw: dict):
        if self.refusal_fallback:
            try:
                resp = await self._inner.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kw)
                if any(getattr(b, "type", "") == "fallback" for b in getattr(resp, "content", []) or []):
                    self.fallbacks_used += 1
                return resp
            except Exception as e:
                text = str(e).lower()
                if getattr(e, "status_code", None) in (400, 404) and ("beta" in text or "fallback" in text):
                    self.refusal_fallback = False  # not supported here: remember, and use the plain call from now on
                else:
                    raise
        return await self._inner.messages.create(**kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.budget
