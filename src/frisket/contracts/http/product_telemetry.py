"""Closed browser product-telemetry request."""

from __future__ import annotations

from pydantic import Field

from frisket.contracts.http.models import WireModel


class ProductTelemetryRequest(WireModel):
    monthly_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    type: str = Field(min_length=1, max_length=64)
    properties: dict[str, str]


__all__ = ["ProductTelemetryRequest"]
