"""Trusted connection identity for the distinct frisket models gateway."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from frisket.ai.llm.endpoint_config import (
    normalize_ollama_url,
    origin_is_https_or_loopback,
)

MODELS_GATEWAY_URL_ENV = "FRISKET_MODELS_URL"
MODELS_GATEWAY_TOKEN_ENV = "FRISKET_MODELS_TOKEN"
MODELS_GATEWAY_ENV_NAMES = (MODELS_GATEWAY_URL_ENV, MODELS_GATEWAY_TOKEN_ENV)


class InvalidModelsGatewayConfig(ValueError):
    """The gateway pair is partial or cannot safely carry its bearer token."""


@dataclass(frozen=True, slots=True)
class ModelsGatewayConnection:
    """One atomic gateway origin/token pair resolved from a trusted store."""

    origin: str
    token: str = field(repr=False)
    source: Literal["environment", "stored"]

    def __post_init__(self) -> None:
        normalized = (
            _normalize_models_gateway_env_origin(self.origin)
            if self.source == "environment"
            else normalize_models_gateway_origin(self.origin)
        )
        token = self.token.strip()
        if not token:
            raise InvalidModelsGatewayConfig("models gateway token is required")
        object.__setattr__(self, "origin", normalized)
        object.__setattr__(self, "token", token)


def normalize_models_gateway_origin(origin: str) -> str:
    """Normalize an origin and require HTTPS except on an exact loopback host."""

    try:
        normalized = normalize_ollama_url(origin)
    except ValueError as exc:
        raise InvalidModelsGatewayConfig(
            "models gateway origin must be an http(s) origin with no credentials, "
            "path, query, or fragment"
        ) from exc
    if not origin_is_https_or_loopback(normalized):
        raise InvalidModelsGatewayConfig(
            "models gateway origin must use https unless it is loopback"
        )
    return normalized


def _normalize_models_gateway_env_origin(origin: str) -> str:
    """Preserve trusted operator HTTP origins used by the Compose topology."""

    try:
        return normalize_ollama_url(origin)
    except ValueError as exc:
        raise InvalidModelsGatewayConfig(
            "models gateway origin must be an http(s) origin with no credentials, "
            "path, query, or fragment"
        ) from exc


def resolve_models_gateway_env(
    env: Mapping[str, str] | None = None,
) -> ModelsGatewayConnection | None:
    """Resolve the environment pair, refusing a nonblank partial override."""

    values = os.environ if env is None else env
    origin = (values.get(MODELS_GATEWAY_URL_ENV) or "").strip()
    token = (values.get(MODELS_GATEWAY_TOKEN_ENV) or "").strip()
    if not origin and not token:
        return None
    if not origin:
        raise InvalidModelsGatewayConfig(
            f"{MODELS_GATEWAY_URL_ENV} is required when "
            f"{MODELS_GATEWAY_TOKEN_ENV} is set"
        )
    if not token:
        raise InvalidModelsGatewayConfig(
            f"{MODELS_GATEWAY_TOKEN_ENV} is required when "
            f"{MODELS_GATEWAY_URL_ENV} is set"
        )
    return ModelsGatewayConnection(
        origin=origin,
        token=token,
        source="environment",
    )


__all__ = [
    "InvalidModelsGatewayConfig",
    "MODELS_GATEWAY_ENV_NAMES",
    "MODELS_GATEWAY_TOKEN_ENV",
    "MODELS_GATEWAY_URL_ENV",
    "ModelsGatewayConnection",
    "normalize_models_gateway_origin",
    "resolve_models_gateway_env",
]
