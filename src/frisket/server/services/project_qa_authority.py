"""The small request fact needed to recheck background research authority."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from frisket.execution.provider import ExecutionCompositionContext


@dataclass(frozen=True, repr=False)
class BackgroundAskState:
    execution_composition_context: ExecutionCompositionContext
    user: Mapping[str, Any] | None = None


@dataclass(frozen=True, repr=False)
class BackgroundAskContext:
    """Retain credentials only in memory, without retaining an HTTP request.

    Edition authorizers resolve these credentials afresh before every operation.
    This object must never be persisted as part of agent messages or events.
    """

    headers: Mapping[str, str] = field(repr=False)
    cookies: Mapping[str, str] = field(repr=False)
    state: BackgroundAskState = field(repr=False)

    @classmethod
    def capture(cls, request: Any, workspace: Any) -> BackgroundAskContext:
        headers = getattr(request, "headers", {})
        state = getattr(request, "state", None)
        user = getattr(state, "user", None)
        return cls(
            headers=MappingProxyType(
                {
                    key: headers[key]
                    for key in ("authorization", "cookie")
                    if key in headers
                }
            ),
            cookies=MappingProxyType(dict(getattr(request, "cookies", {}))),
            state=BackgroundAskState(
                execution_composition_context=workspace.edition_execution_composition_context_for(
                    request
                ),
                user=MappingProxyType(dict(user))
                if isinstance(user, Mapping)
                else None,
            ),
        )
