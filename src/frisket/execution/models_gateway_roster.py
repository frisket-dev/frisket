"""Resolve the three-part configuration for a managed models deployment."""

from __future__ import annotations

import os
from collections.abc import Mapping

from frisket.ai.models.gateway_config import (
    InvalidModelsGatewayConfig,
    resolve_models_gateway_env,
)
from frisket.contracts.models_gateway_roster import (
    InvalidModelsGatewayRoster,
    MODELS_GATEWAY_ROSTER_ENV,
    ModelsGatewayRoster,
    parse_models_gateway_roster,
)


class InvalidManagedModelsGatewayConfig(ValueError):
    """The managed gateway's connection and roster are not a complete unit."""


def resolve_managed_models_gateway_roster(
    environment: Mapping[str, str] | None = None,
) -> ModelsGatewayRoster:
    """Resolve a complete URL/token/roster deployment without live discovery."""

    values = os.environ if environment is None else environment
    try:
        connection = resolve_models_gateway_env(values)
    except InvalidModelsGatewayConfig as exc:
        raise InvalidManagedModelsGatewayConfig(
            f"{MODELS_GATEWAY_ROSTER_ENV} is inactive because the managed "
            "models gateway URL/token pair is invalid"
        ) from exc
    if connection is None:
        raise InvalidManagedModelsGatewayConfig(
            f"{MODELS_GATEWAY_ROSTER_ENV} is inactive because the managed "
            "models gateway URL/token pair is missing"
        )
    raw_roster = (values.get(MODELS_GATEWAY_ROSTER_ENV) or "").strip()
    if not raw_roster:
        raise InvalidManagedModelsGatewayConfig(
            f"{MODELS_GATEWAY_ROSTER_ENV} is missing or empty"
        )
    try:
        roster = parse_models_gateway_roster(raw_roster)
    except InvalidModelsGatewayRoster as exc:
        raise InvalidManagedModelsGatewayConfig(str(exc)) from exc
    return roster


__all__ = [
    "InvalidManagedModelsGatewayConfig",
    "resolve_managed_models_gateway_roster",
]
