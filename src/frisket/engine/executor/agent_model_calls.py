"""Returned-call accounting shared by the two bounded row-agent capabilities."""

from __future__ import annotations

import copy
import uuid

from frisket.ai.research.row_answer import model_call_accounting
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt import attempt_in_scope
from frisket.ops.base import RecipeInvocationHalt


def require_agent_attempt(ctx):
    attempt = attempt_in_scope(ctx.extras)
    if attempt is None or attempt.run_id != ctx.extras.get("run_id"):
        raise RecipeInvocationHalt(
            "consent_missing", "A row agent requires its admitted execution attempt"
        )
    return attempt


class AccountedAgentRouter:
    def __init__(self, router, ctx, model, accounting_by_row):
        self._router, self._ctx, self._model = router, ctx, model
        self._accounting = accounting_by_row
        self._responses = []
        self._facts = []

    def __getattr__(self, name):
        return getattr(self._router, name)

    def retain_unrecorded_error_calls(self, error):
        """The row exception path must not remint IDs for durable responses."""
        if getattr(error, "wire_calls", None):
            recorded = {id(response) for response in self._responses}
            error.wire_calls = [
                response
                for response in error.wire_calls
                if id(response) not in recorded
            ]

    async def complete_transport(self, request, **kwargs):
        if request.model != self._model:
            raise ValueError("Agent model differs from its admitted model")
        attempt = require_agent_attempt(self._ctx)
        cancelled = self._ctx.extras.get("cancelled")
        if callable(cancelled) and cancelled():
            import asyncio

            raise asyncio.CancelledError
        response = await self._router.complete_transport(request, **kwargs)
        self._responses.append(response)
        accounting = model_call_accounting(self._model, self._responses)
        fact = accounting["model_calls"][-1]
        fact["id"] = "agent_call_" + uuid.uuid4().hex
        self._facts.append(fact)
        accounting["model_calls"] = copy.deepcopy(self._facts)
        ctx = self._ctx
        row_id = ctx.extras["row_id"]
        self._accounting[row_id] = accounting
        # Persist returned spend before another model/tool call can fail or be
        # cancelled. Reusing these IDs in row publication is idempotent.
        if not ctx.extras.get("preview"):
            RunResultStore(ctx.project).write_returned_call_accounting(
                ctx.extras["run_id"],
                [{"row_id": row_id, "column_id": None, "model_calls": [fact]}],
                writer_attempt_id=attempt.attempt_id,
                authorized_attempt_id=attempt.attempt_id,
                claim_token=ctx.extras.get("claim_token"),
            )
        return response
