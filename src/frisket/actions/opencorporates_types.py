"""The provider-domain operations offered by an admitted OpenCorporates client."""

from typing import Any, Protocol


class OpenCorporates(Protocol):
    async def reconcile(
        self, company_name: str, *, jurisdiction_code: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def fetch(
        self, jurisdiction_code: str, company_number: str
    ) -> dict[str, Any] | None: ...
