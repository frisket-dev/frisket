"""A borrowed router view carrying one explicit execution owner."""

from __future__ import annotations

from frisket.execution.dispatch_control import DispatchControl


class ExecutionModelRouter:
    def __init__(self, router, execution: DispatchControl):
        self._router = router
        self._execution = execution

    def __getattr__(self, name):
        return getattr(self._router, name)

    def for_execution(self, execution: DispatchControl):
        if execution != self._execution:
            raise ValueError("A bound model router cannot change execution owner")
        return self

    async def complete(self, request, **kwargs):
        return await self._router.complete(
            request, _execution=self._execution, **kwargs
        )

    async def complete_transport(self, request, **kwargs):
        return await self._router.complete_transport(
            request, _execution=self._execution, **kwargs
        )

    async def embed(self, texts, **kwargs):
        return await self._router.embed(texts, _execution=self._execution, **kwargs)

    async def embed_batch(self, texts, **kwargs):
        return await self._router.embed_batch(
            texts, _execution=self._execution, **kwargs
        )

    async def aclose(self):
        # This view does not own the shared adapter/client/cache resources.
        pass
