"""Edition-owned authority and settlement for a finite background Ask turn.

The hosted edition captures trusted request context at admission. Public callers
provide project/turn/call identities, never funding values from request JSON.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ProjectQATurnRuntime(Protocol):
    def call_scope(self, router: Any) -> AbstractContextManager[None]: ...

    def settle_call(self, *, project: Any, turn_id: str, call_id: str) -> None: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class ProjectQARuntimePort(Protocol):
    async def prepare_turn(
        self, *, project: Any, project_id: str, turn_id: str, actor: str | None
    ) -> ProjectQATurnRuntime: ...
