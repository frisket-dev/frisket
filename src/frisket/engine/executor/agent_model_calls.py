"""Returned-call accounting shared by the two bounded row-agent capabilities."""

from __future__ import annotations

import copy
import uuid
from typing import Any

from frisket.ai.research.row_answer import model_call_accounting
from frisket.ai.models.accounting import merge_accounting_meta
from frisket.ai.llm.types import LLMRequest, LLMResponse, wire_responses
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.writer_fence import WriterFence
from frisket.execution.attempt import attempt_in_scope
from frisket.ops.base import ActionProgramInvocationHalt


def require_agent_attempt(ctx):
    attempt = attempt_in_scope(ctx.extras)
    if attempt is None or attempt.run_id != ctx.extras.get("run_id"):
        raise ActionProgramInvocationHalt(
            "consent_missing", "A row agent requires its admitted execution attempt"
        )
    return attempt


class AccountedAgentRouter:
    def __init__(self, router, ctx, model, accounting_by_row):
        self._router, self._ctx, self._model = router, ctx, model
        self._accounting = accounting_by_row
        self._responses = []
        self._facts = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._router, name)

    def has_provider(self, provider: str) -> bool:
        return self._router.has_provider(provider)

    def local_endpoint_identity(self, model: str) -> tuple[str, str]:
        return self._router.local_endpoint_identity(model)

    def secret_values_for_model(self, model: str) -> tuple[str, ...]:
        return self._router.secret_values_for_model(model)

    def note_schema_reject(self, provider: str) -> None:
        self._router.note_schema_reject(provider)

    async def complete(self, req: LLMRequest, **kwargs: Any) -> LLMResponse:
        return await self._router.complete(req, **kwargs)

    def retain_unrecorded_error_calls(self, error):
        """Hand off stable accounting and only wires not yet owned by this capability."""
        accounting = self._accounting.get(self._ctx.extras.get("row_id"))
        if accounting is not None:
            error.accounting = copy.deepcopy(
                merge_accounting_meta(getattr(error, "accounting", None), accounting)
            )
        if getattr(error, "wire_calls", None):
            recorded = {id(response) for response in self._responses}
            error.wire_calls = [
                response
                for response in error.wire_calls
                if id(response) not in recorded
            ]

    async def complete_transport(self, req: LLMRequest, **kwargs: Any) -> LLMResponse:
        if req.model != self._model:
            raise ValueError("Agent model differs from its admitted model")
        attempt = require_agent_attempt(self._ctx)
        from frisket.execution.dispatch_control import dispatch_for_context

        dispatch_for_context(self._ctx).check()
        try:
            response = await self._router.complete_transport(req, **kwargs)
        except BaseException as error:
            self._record_returned(
                wire_responses(getattr(error, "wire_calls", ()) or ()), attempt
            )
            raise
        self._record_returned(wire_responses([response]), attempt)
        return response

    def _record_returned(self, returned, attempt):
        known = {id(response) for response in self._responses}
        fresh = [response for response in returned if id(response) not in known]
        if not fresh:
            return
        self._responses.extend(fresh)
        fresh_accounting = model_call_accounting(self._model, fresh)
        facts = fresh_accounting["model_calls"]
        hosted_ids = {
            call["id"]
            for response in fresh
            if response.actual_accounting is not None
            for call in response.actual_accounting.model_calls
        }
        for fact in facts:
            # Hosted receipts already own an immutable provider fact ID.
            if fact["id"] not in hosted_ids:
                fact["id"] = "agent_call_" + uuid.uuid4().hex
        self._facts.extend(facts)
        accounting = model_call_accounting(self._model, self._responses)
        accounting["model_calls"] = copy.deepcopy(self._facts)
        ctx = self._ctx
        row_id = ctx.extras["row_id"]
        self._accounting[row_id] = accounting
        # Persist every returned purchase before another step can fail or stop.
        if not ctx.extras.get("preview"):
            RunResultStore(ctx.project).write_returned_call_accounting(
                ctx.extras["run_id"],
                [{"row_id": row_id, "column_id": None, "model_calls": facts}],
                fence=WriterFence(
                    run_id=ctx.extras["run_id"],
                    writer_attempt_id=attempt.attempt_id,
                    claim_token=ctx.extras.get("claim_token"),
                    authorized_attempt_id=attempt.attempt_id,
                ),
            )
