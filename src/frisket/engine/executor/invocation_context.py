"""The narrow author-facing view of an existing host cancellation signal."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HostInvocationContext:
    _cancelled: Callable[[], bool] | None

    def check_cancelled(self) -> None:
        if self._cancelled is not None and self._cancelled():
            # This BaseException follows the runner's owned-task cancellation
            # path, bypassing ordinary row-error conversion and repair.
            raise asyncio.CancelledError
