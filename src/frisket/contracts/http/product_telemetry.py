"""Closed browser product-telemetry request."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from frisket.contracts.http.models import WireModel


class ProductTelemetryClient(WireModel):
    platform: Literal["linux", "macos", "windows", "ios", "android", "other"] = "other"
    browser: Literal["chrome", "edge", "firefox", "safari", "electron", "other"] = (
        "other"
    )
    browser_major: str | None = Field(default=None, pattern=r"^[0-9]{1,4}$")


class ProductTelemetryRequest(WireModel):
    monthly_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    type: str = Field(min_length=1, max_length=64)
    properties: dict[str, str]
    client: ProductTelemetryClient = Field(default_factory=ProductTelemetryClient)


__all__ = ["ProductTelemetryRequest"]
