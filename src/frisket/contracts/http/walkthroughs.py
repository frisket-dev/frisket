"""Typed HTTP response for the fixed walkthrough catalog."""

from __future__ import annotations

from frisket.contracts.http.models import WireModel


class WalkthroughBadge(WireModel):
    id: str
    badges: list[str]


class WalkthroughCatalogResponse(WireModel):
    walkthroughs: list[WalkthroughBadge]
