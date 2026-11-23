"""A borrowed router view carrying one explicit execution owner."""

from __future__ import annotations

from typing import Any

from frisket.ai.llm.types import LLMRequest, LLMResponse
from frisket.execution.dispatch_control import DispatchControl


class ExecutionModelRouter:
    def __init__(self, router: Any, execution: DispatchControl):
        self._router = router
        self._execution = execution

    def __getattr__(self, name: str) -> Any:
        return getattr(self._router, name)

    def has_provider(self, provider: str) -> bool:
        return self._router.has_provider(provider)

    def local_endpoint_identity(self, model: str) -> tuple[str, str]:
        return self._router.local_endpoint_identity(model)

    def destination_for_model(self, model: str) -> str | None:
        return self._router.destination_for_model(model)

    def secret_values_for_model(self, model: str) -> tuple[str, ...]:
        return self._router.secret_values_for_model(model)

    def note_schema_reject(self, provider: str) -> None:
        self._router.note_schema_reject(provider)

    def for_execution(self, execution: DispatchControl) -> ExecutionModelRouter:
        if execution != self._execution:
            raise ValueError("A bound model router cannot change execution owner")
        return self

    async def complete(self, req: LLMRequest, **kwargs: Any) -> LLMResponse:
        return await self._router.complete(req, _execution=self._execution, **kwargs)

    async def complete_transport(self, req: LLMRequest, **kwargs: Any) -> LLMResponse:
        return await self._router.complete_transport(
            req, _execution=self._execution, **kwargs
        )

    async def embed(self, texts: Any, **kwargs: Any) -> Any:
        return await self._router.embed(texts, _execution=self._execution, **kwargs)

    async def embed_batch(self, texts: Any, **kwargs: Any) -> Any:
        return await self._router.embed_batch(
            texts, _execution=self._execution, **kwargs
        )

    async def aclose(self) -> None:
        # This view does not own the shared adapter/client/cache resources.
        pass
