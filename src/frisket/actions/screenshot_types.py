"""The admitted browser's actual-argument screenshot capability."""

from typing import Protocol

from frisket.actions.types import StagedImage


class Screenshotter(Protocol):
    async def capture(
        self,
        url: str,
        *,
        full_page: bool = True,
        viewport: tuple[int, int] = (1280, 720),
        max_bytes: int = 5_000_000,
        timeout_ms: int = 30_000,
    ) -> StagedImage: ...
