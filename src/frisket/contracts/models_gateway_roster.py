"""Stable deployment roster for a managed models gateway.

The roster records configuration, not health.  A deployment may therefore
advertise a cold engine here while execution remains responsible for waking it
and reporting any runtime failure.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


MODELS_GATEWAY_ROSTER_ENV = "FRISKET_MODELS_ROSTER_JSON"
MODELS_GATEWAY_ROSTER_SCHEMA_VERSION = "frisket.models_gateway_roster.v1"


class InvalidModelsGatewayRoster(ValueError):
    """The managed gateway roster does not satisfy its versioned contract."""


@dataclass(frozen=True, order=True, slots=True)
class ModelsGatewayRosterEntry:
    route: str
    name: str

    def __post_init__(self) -> None:
        if not self.route.startswith("/") or any(
            character.isspace() for character in self.route
        ):
            raise InvalidModelsGatewayRoster("engine route must be an absolute path")
        if (
            not self.name
            or self.name != self.name.strip()
            or any(character.isspace() for character in self.name)
        ):
            raise InvalidModelsGatewayRoster("engine name must be a non-blank token")


@dataclass(frozen=True, slots=True)
class ModelsGatewayRoster:
    engines: tuple[ModelsGatewayRosterEntry, ...]

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.engines))
        if len(set(ordered)) != len(ordered):
            raise InvalidModelsGatewayRoster("duplicate engine route/name pair")
        object.__setattr__(self, "engines", ordered)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": MODELS_GATEWAY_ROSTER_SCHEMA_VERSION,
            "engines": [
                {"name": entry.name, "route": entry.route} for entry in self.engines
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), separators=(",", ":"), sort_keys=True)

    def as_catalog_capabilities(self) -> dict[str, Any]:
        """Project configured engines into the existing catalog input shape."""

        return {
            "configured": True,
            "managed_roster": True,
            "engines": [
                {"name": entry.name, "route": entry.route, "available": True}
                for entry in self.engines
            ],
        }


def parse_models_gateway_roster(value: str) -> ModelsGatewayRoster:
    """Parse one canonical managed-roster environment value."""

    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise InvalidModelsGatewayRoster(
            f"{MODELS_GATEWAY_ROSTER_ENV} must contain valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise InvalidModelsGatewayRoster(
            f"{MODELS_GATEWAY_ROSTER_ENV} must contain a JSON object"
        )
    if set(payload) != {"schema_version", "engines"}:
        raise InvalidModelsGatewayRoster(
            f"{MODELS_GATEWAY_ROSTER_ENV} must contain schema_version and engines"
        )
    if payload["schema_version"] != MODELS_GATEWAY_ROSTER_SCHEMA_VERSION:
        raise InvalidModelsGatewayRoster(
            f"{MODELS_GATEWAY_ROSTER_ENV} has an unsupported schema_version"
        )
    rows = payload["engines"]
    if not isinstance(rows, list):
        raise InvalidModelsGatewayRoster(
            f"{MODELS_GATEWAY_ROSTER_ENV} engines must be a list"
        )
    entries: list[ModelsGatewayRosterEntry] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"name", "route"}:
            raise InvalidModelsGatewayRoster(
                f"{MODELS_GATEWAY_ROSTER_ENV} engine entries require name and route"
            )
        name = row["name"]
        route = row["route"]
        if not isinstance(name, str) or not isinstance(route, str):
            raise InvalidModelsGatewayRoster(
                f"{MODELS_GATEWAY_ROSTER_ENV} engine name and route must be strings"
            )
        entries.append(ModelsGatewayRosterEntry(route=route, name=name))
    try:
        return ModelsGatewayRoster(tuple(entries))
    except InvalidModelsGatewayRoster as exc:
        raise InvalidModelsGatewayRoster(f"{MODELS_GATEWAY_ROSTER_ENV}: {exc}") from exc


def models_gateway_roster_from_capabilities(
    payload: Mapping[str, Any],
) -> ModelsGatewayRoster:
    """Extract stable route/name declarations from a gateway response."""

    rows = payload.get("engines")
    if not isinstance(rows, list):
        raise InvalidModelsGatewayRoster("gateway capabilities engines must be a list")
    entries: list[ModelsGatewayRosterEntry] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise InvalidModelsGatewayRoster(
                "gateway capabilities engine entries must be objects"
            )
        name = row.get("name")
        route = row.get("route")
        if not isinstance(name, str) or not isinstance(route, str):
            raise InvalidModelsGatewayRoster(
                "gateway capabilities engine entries require string name and route"
            )
        configured = row.get("configured")
        if not isinstance(configured, bool):
            raise InvalidModelsGatewayRoster(
                "gateway capabilities engine entries require boolean configured"
            )
        if not configured:
            continue
        entries.append(ModelsGatewayRosterEntry(route=route, name=name))
    return ModelsGatewayRoster(tuple(entries))


__all__ = [
    "InvalidModelsGatewayRoster",
    "MODELS_GATEWAY_ROSTER_ENV",
    "MODELS_GATEWAY_ROSTER_SCHEMA_VERSION",
    "ModelsGatewayRoster",
    "ModelsGatewayRosterEntry",
    "models_gateway_roster_from_capabilities",
    "parse_models_gateway_roster",
]
