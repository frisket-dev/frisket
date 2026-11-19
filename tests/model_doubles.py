"""Provider adapter doubles installed into a real ModelRouter.

Only the wire is faked: routing, retries, cache and receipts run the real
router code around the adapter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY


class CannedReplyAdapter:
    """Answers every completion with one JSON reply at fixed usage.

    Each request is appended to ``requests``.
    """

    def __init__(
        self,
        reply: dict[str, Any],
        *,
        tokens_in: int = 10,
        tokens_out: int = 10,
        cost: float = 0.0,
    ) -> None:
        self.reply = reply
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.cost = cost
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client: Any) -> LLMResponse:
        self.requests.append(req)
        return LLMResponse(
            content=json.dumps(self.reply),
            data=json.loads(json.dumps(self.reply)),
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            cost=self.cost,
            model=req.model,
        )


def canned_reply_router(
    reply: dict[str, Any],
    *,
    tokens_in: int = 10,
    tokens_out: int = 10,
    cost: float = 0.0,
) -> tuple[ModelRouter, CannedReplyAdapter]:
    """An uncached anthropic router whose provider answers with ``reply``."""
    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        keys={"anthropic": "k"},
        cache=None,
        cache_mode="off",
    )
    adapter = CannedReplyAdapter(
        reply, tokens_in=tokens_in, tokens_out=tokens_out, cost=cost
    )
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return router, adapter


@dataclass
class ScriptedAdapter:
    """Plays ``outcomes`` in order, repeating the last one.

    An outcome is an ``LLMResponse`` to return, an exception to raise, or a
    callable of the request. Each request is appended to ``seen``.
    """

    outcomes: list[Any]
    base_url: str = "http://mock"
    seen: list[Any] = field(default_factory=list)
    _i: int = 0

    async def complete(self, req: Any, client: Any) -> LLMResponse:
        self.seen.append(req)
        outcome = self.outcomes[min(self._i, len(self.outcomes) - 1)]
        self._i += 1
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(req)
        return outcome
