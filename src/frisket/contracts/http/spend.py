"""Closed browser projection for the organization spend dashboard."""

from __future__ import annotations

from frisket.contracts.http.models import WireModel


class SpendRow(WireModel):
    project: str
    model: str | None
    month: str | None
    runs: int
    rows: int
    cost: float


class SpendReport(WireModel):
    rows: list[SpendRow]
    total_cost: float
    has_unknown_costs: bool
    unknown_cost_models: list[str]


__all__ = ["SpendReport", "SpendRow"]
