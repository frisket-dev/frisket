"""Serial row extraction using one invocation-owned MCP session collection."""

from __future__ import annotations

import asyncio
import copy
import inspect
from contextlib import AsyncExitStack

from frisket.actions.types import DynamicOutput, Outcome, RowError
from frisket.ai.llm import LLMError
from frisket.ai.message_content import render_input_block
from frisket.engine.executor.agent_model_calls import (
    AccountedAgentRouter,
    require_agent_attempt,
)
from frisket.engine.executor.visual_cuts_read import _settle
from frisket.ops.base import RecipeInvocationHalt
from frisket.ops.mcp_extract import open_mcp_extractor


class AdmittedMcpExtractor:
    def __init__(self, ctx, *, model: str, server_ids, response_schema: dict):
        self._ctx, self._model = ctx, model
        self._servers = tuple(server_ids)
        self._schema = copy.deepcopy(response_schema)
        self._resources = AsyncExitStack()
        self._extractor = None
        self._closed = False
        self._tasks = set()
        self._rows = set()
        self._serial = asyncio.Lock()
        self.accounting_by_row = {}
        self.calls_by_row = {}

    async def start(self, *, expected_rows):
        if self._closed or self._extractor is not None:
            raise RuntimeError("MCP invocation is already started or closed")
        if self._ctx.extras.get("preview"):
            raise RecipeInvocationHalt(
                "preview_unsupported", "MCP tools require an admitted run"
            )
        require_agent_attempt(self._ctx)
        factory = self._ctx.extras.get("mcp_session_factory")
        if factory is not None:
            opened = factory(self._servers, self._ctx)
            if inspect.isawaitable(opened):
                opened = await opened
        else:
            from frisket.ops.integrations.mcp_project import ProjectMcpSessionCollection

            opened = ProjectMcpSessionCollection(self._ctx.project, self._servers)
        self._extractor = await self._resources.enter_async_context(
            open_mcp_extractor(opened, self._servers)
        )

    async def aclose(self):
        self._closed = True
        for task in tuple(self._tasks):
            task.cancel()
        for task in tuple(self._tasks):
            await _settle(task)
        await self._resources.aclose()
        self._extractor = None

    def bind_row(self, row, *, sheet_id, row_id, sources, ctx):
        if self._closed or self._extractor is None:
            raise RuntimeError("MCP extraction requires an active invocation")
        return _BoundMcpExtractor(self, row, row_id, ctx)


class _BoundMcpExtractor:
    def __init__(self, owner, row, row_id, ctx):
        self._owner, self._row, self._row_id, self._ctx = owner, row, row_id, ctx

    async def extract(self, row, *, context, instruction):
        owner = self._owner
        if owner._closed or row is not self._row or self._row_id in owner._rows:
            raise RowError(
                "invalid_input_ref", "MCP extraction requires its admitted row, once"
            )
        owner._rows.add(self._row_id)
        task = asyncio.create_task(self._extract(copy.deepcopy(context), instruction))
        owner._tasks.add(task)
        try:
            return await task
        finally:
            await _settle(task)
            owner._tasks.discard(task)

    async def _extract(self, context, instruction):
        owner, ctx = self._owner, self._ctx
        router = AccountedAgentRouter(
            ctx.extras["router"], ctx, owner._model, owner.accounting_by_row
        )
        async with owner._serial:
            try:
                data, _ = await owner._extractor.extract(
                    render_input_block(context),
                    instruction=instruction,
                    model=owner._model,
                    schema=copy.deepcopy(owner._schema),
                    first_output=next(iter(owner._schema["properties"])),
                    recipe_version="1",
                    router=router,
                )
            except LLMError as exc:
                router.retain_unrecorded_error_calls(exc)
                raise
            except (asyncio.CancelledError, RecipeInvocationHalt):
                raise
            except Exception as exc:
                raise RowError(
                    "mcp_extract_failed", str(exc)[:500] or "MCP extraction failed"
                ) from exc
        if data.get("error") and data.get("outcome") == "model_error":
            raise RowError(
                data.get("error_code", "mcp_extract_failed"), data["error"][:500]
            )
        return DynamicOutput({key: Outcome.ok(value) for key, value in data.items()})
