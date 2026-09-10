"""Stable response envelope for the in-app Diagnose probes."""

from __future__ import annotations

from pydantic import ConfigDict, Field, JsonValue

from frisket.contracts.http.models import WireModel


class DiagnosticsReport(WireModel):
    """Stable report core with independently versioned probe payloads."""

    model_config = ConfigDict(extra="allow")

    # Probe additions remain lossless JSON, but the product-level envelope is
    # required so generated clients cannot mistake an arbitrary object for a
    # renderable report.
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)

    healthy: bool
    core: dict[str, JsonValue]
    info: dict[str, JsonValue]


__all__ = ["DiagnosticsReport"]
