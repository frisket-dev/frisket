"""Dependency-light local provider config readers and resolvers shared with execution.

The local tier stores UI-entered provider keys, local model endpoints, models
gateway credentials, and search preferences in a gitignored workspace file
(``<workspace>/.frisket/provider_keys.json``). This module deliberately imports
nothing from ``frisket.server`` or ``frisket.team`` so engine jobs, CLI
commands, and server routes share one canonical reader and resolution order.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from frisket.ai.llm.endpoint_config import (
    LocalModelEndpointConfig,
    normalize_ollama_url,
    origin_is_https_or_loopback,
    resolve_env_local_endpoint,
)
from frisket.ai.models.gateway_config import (
    ModelsGatewayConnection,
    resolve_models_gateway_env,
)
from frisket.ai.research.search import SearchService, normalize_search_preference
from frisket.local_model_ids import validate_local_endpoint_id
from frisket.provider_definitions import PROVIDERS, providers_for

# Projections retain their narrow meanings: LLM routing never receives search
# or document credentials merely because settings can configure those providers.
MODEL_KEY_PROVIDERS = tuple(p.id for p in providers_for(category="llm"))
SEARCH_KEY_PROVIDERS = tuple(p.id for p in providers_for(category="search"))
KEY_PROVIDERS = MODEL_KEY_PROVIDERS
WORKSPACE_KEY_PROVIDERS = tuple(p.id for p in providers_for(scope="workspace"))
ENV_VAR = {p.id: p.env_var for p in PROVIDERS}

MODELS_GATEWAY_CONFIG_KEY = "models_gateway_v1"
LOCAL_MODEL_ENDPOINTS_KEY = "local_model_endpoints_v1"
MAX_LOCAL_ENDPOINTS = 16
SEARCH_PROVIDER_CONFIG_KEY = "search_provider_v1"

_ENDPOINT_FIELDS = frozenset(
    {
        "id",
        "name",
        "url",
        "inference_token",
        "provisioning_token",
        "edge_auth",
        "pull_enabled",
    }
)


class InvalidProviderConfigError(ValueError):
    """Raised when an unsafe mutation or malformed read of workspace config is refused."""


def local_secrets_path(root: str | Path) -> Path:
    return Path(root) / ".frisket" / "provider_keys.json"


def validate_config_shape(data: Any, *, path: Path) -> dict[str, str]:
    """Return ``data`` only when JSON serialization cannot filter or coerce it."""
    if not isinstance(data, dict):
        raise InvalidProviderConfigError(
            f"cannot safely update invalid provider settings at {path}: "
            "expected a JSON object"
        )
    if any(
        not isinstance(name, str) or not isinstance(value, str) or not value
        for name, value in data.items()
    ):
        raise InvalidProviderConfigError(
            f"cannot safely update invalid provider settings at {path}: "
            "every name must be a string and every value a non-empty string"
        )
    return dict(data)


def read_valid_config(path: Path) -> dict[str, str]:
    """Decode the complete, losslessly round-trippable config at ``path``."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except UnicodeError as exc:
        raise InvalidProviderConfigError(
            f"cannot safely update invalid provider settings at {path}"
        ) from exc
    except OSError as exc:
        raise InvalidProviderConfigError(
            f"cannot safely update provider settings at {path}"
        ) from exc
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise InvalidProviderConfigError(
            f"cannot safely update invalid provider settings at {path}"
        ) from exc
    return validate_config_shape(data, path=path)


def read_config_file(root: str | Path) -> dict[str, str]:
    """Return the raw config to tolerant readers, or ``{}`` when invalid."""
    path = local_secrets_path(root)
    try:
        return read_valid_config(path)
    except InvalidProviderConfigError:
        return {}


def load_local_provider_keys(root: str | Path) -> dict[str, str]:
    return {
        provider: value
        for provider, value in read_config_file(root).items()
        if provider in WORKSPACE_KEY_PROVIDERS
    }


def _decode_models_gateway(value: str | None) -> ModelsGatewayConnection | None:
    if value is None:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise InvalidProviderConfigError(
            "cannot safely read malformed models gateway settings"
        ) from exc
    if (
        not isinstance(decoded, dict)
        or set(decoded) != {"origin", "token"}
        or not isinstance(decoded.get("origin"), str)
        or not isinstance(decoded.get("token"), str)
    ):
        raise InvalidProviderConfigError(
            "cannot safely read malformed models gateway settings"
        )
    try:
        return ModelsGatewayConnection(
            origin=decoded["origin"],
            token=decoded["token"],
            source="stored",
        )
    except ValueError as exc:
        raise InvalidProviderConfigError(
            "cannot safely read malformed models gateway settings"
        ) from exc


def load_local_models_gateway(root: str | Path) -> ModelsGatewayConnection | None:
    """Read the workspace gateway pair from the private config map."""

    return _decode_models_gateway(read_config_file(root).get(MODELS_GATEWAY_CONFIG_KEY))


def resolve_models_gateway(
    root: str | Path,
    env: Mapping[str, str] | None = None,
) -> ModelsGatewayConnection | None:
    """Resolve env-over-stored gateway authority without partial fallback."""

    environment = resolve_models_gateway_env(os.environ if env is None else env)
    return environment or load_local_models_gateway(root)


def normalize_local_endpoint_name(name: str) -> str:
    normalized = " ".join((name or "").split())
    if not normalized:
        raise ValueError("local model server name is required")
    if len(normalized) > 80:
        raise ValueError("local model server name is too long")
    return normalized


def _decode_one_local_endpoint(
    raw: Any,
    seen_ids: set[str],
) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or set(raw) != _ENDPOINT_FIELDS:
        return None
    endpoint_id = raw.get("id")
    name = raw.get("name")
    url = raw.get("url")
    raw_inference = raw.get("inference_token")
    raw_provisioning = raw.get("provisioning_token")
    edge_auth = raw.get("edge_auth")
    pull_enabled = raw.get("pull_enabled")
    if (
        not isinstance(endpoint_id, str)
        or endpoint_id in seen_ids
        or not isinstance(name, str)
        or not isinstance(url, str)
        or (raw_inference is not None and not isinstance(raw_inference, str))
        or (raw_provisioning is not None and not isinstance(raw_provisioning, str))
        or not isinstance(edge_auth, bool)
        or not isinstance(pull_enabled, bool)
    ):
        return None
    try:
        validated_id = validate_local_endpoint_id(endpoint_id)
        normalized_name = normalize_local_endpoint_name(name)
        normalized_url = normalize_ollama_url(url)
    except ValueError:
        return None
    inference_token = raw_inference or None
    provisioning_token = raw_provisioning or None
    if (inference_token or provisioning_token) and not origin_is_https_or_loopback(
        normalized_url
    ):
        return None
    seen_ids.add(validated_id)
    return {
        "id": validated_id,
        "name": normalized_name,
        "url": normalized_url,
        "inference_token": inference_token,
        "provisioning_token": provisioning_token,
        "edge_auth": edge_auth,
        "pull_enabled": pull_enabled,
    }


def decode_local_endpoints(
    value: str | None,
    *,
    strict: bool,
) -> list[dict[str, Any]]:
    if value is None:
        return []
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        if strict:
            raise InvalidProviderConfigError(
                "cannot safely update malformed local model endpoint settings"
            ) from exc
        return []
    if not isinstance(decoded, list) or len(decoded) > MAX_LOCAL_ENDPOINTS:
        if strict:
            raise InvalidProviderConfigError(
                "cannot safely update malformed local model endpoint settings"
            )
        return []
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in decoded:
        parsed = _decode_one_local_endpoint(raw, seen_ids)
        if parsed is None:
            if strict:
                raise InvalidProviderConfigError(
                    "cannot safely update malformed local model endpoint settings"
                )
            return []
        rows.append(parsed)
    return rows


def load_local_endpoints(
    root: str | Path,
) -> tuple[LocalModelEndpointConfig, ...]:
    rows = decode_local_endpoints(
        read_config_file(root).get(LOCAL_MODEL_ENDPOINTS_KEY),
        strict=False,
    )
    return tuple(
        LocalModelEndpointConfig(
            origin=row["url"],
            inference_token=row["inference_token"],
            provisioning_token=row["provisioning_token"],
            edge_auth=row["edge_auth"],
            pull_enabled=row["pull_enabled"],
            source="local_file",
            endpoint_id=row["id"],
            display_name=row["name"],
        )
        for row in rows
    )


def resolve_local_endpoints(
    root: str | Path,
    env: Mapping[str, str] | None = None,
) -> tuple[tuple[LocalModelEndpointConfig, ...], list[str]]:
    """Resolve one explicit endpoint collection with no singular aliases."""
    env = os.environ if env is None else env
    env_endpoint, notes = resolve_env_local_endpoint(env)
    persisted = load_local_endpoints(root)
    endpoints = ((env_endpoint,) if env_endpoint is not None else ()) + persisted
    ids = [endpoint.endpoint_id for endpoint in endpoints]
    origins = [endpoint.origin for endpoint in endpoints]
    if len(ids) != len(set(ids)):
        raise InvalidProviderConfigError("local model endpoint ids must be unique")
    if len(origins) != len(set(origins)):
        raise InvalidProviderConfigError("local model endpoint origins must be unique")
    return endpoints, notes


def resolve_effective_keys(
    root: str | Path, env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """File-stored remote-provider keys with environment overrides removed."""
    env = os.environ if env is None else env
    out: dict[str, str] = {}
    for provider, value in load_local_provider_keys(root).items():
        env_name = ENV_VAR.get(provider)
        if env_name and env.get(env_name):
            continue
        out[provider] = value
    return out


def resolve_search_credentials(
    root: str | Path, env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Actual effective local search keys; never consult this inside adapters."""
    env = os.environ if env is None else env
    stored = load_local_provider_keys(root)
    return {
        provider: str(env.get(ENV_VAR[provider]) or stored.get(provider) or "")
        for provider in SEARCH_KEY_PROVIDERS
        if env.get(ENV_VAR[provider]) or stored.get(provider)
    }


def load_search_provider(root: str | Path) -> str:
    raw = read_config_file(root).get(SEARCH_PROVIDER_CONFIG_KEY)
    try:
        return normalize_search_preference(raw)
    except ValueError as exc:
        raise InvalidProviderConfigError(
            "cannot safely read invalid search provider settings"
        ) from exc


def configured_search_service(
    root: str | Path,
    *,
    effective_keys: Mapping[str, str],
    credential_source: str = "local",
) -> SearchService:
    """Build search from caller-authorized keys and persisted preference."""

    return SearchService(
        preference=load_search_provider(root),
        effective_keys=effective_keys,
        credential_source=credential_source,
    )


def local_search_service(root: str | Path) -> SearchService:
    """Build the effective local-tier search service for one workspace."""

    return configured_search_service(
        root, effective_keys=resolve_search_credentials(root)
    )
