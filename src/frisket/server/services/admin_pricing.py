"""Admin pricing diagnostics service."""

from __future__ import annotations

from typing import Any

from frisket.ai.external_pricing import external_pricing_catalog


class AdminPricingService:
    def external_pricing(self) -> dict[str, dict[str, Any]]:
        return external_pricing_catalog()
