"""Validation, receipt, status, and save service for a models gateway."""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from frisket.ai.models.gateway_config import (
    InvalidModelsGatewayConfig,
    MODELS_GATEWAY_ENV_NAMES,
    ModelsGatewayConnection,
)
from frisket.contracts.http.models_gateway import ModelsGatewayEngineCapability
from frisket.redaction import redact_text
from frisket.server import provider_config
from frisket.team.security.secrets import key_hint

MODELS_GATEWAY_PROBE_TIMEOUT_SECONDS = 10.0
MODELS_GATEWAY_PROBE_CONNECT_TIMEOUT_SECONDS = 2.0
MODELS_GATEWAY_PROBE_CACHE_TTL_SECONDS = 5.0


def _connection_fingerprint(connection: ModelsGatewayConnection) -> str:
    return hashlib.sha256(
        f"{connection.origin}\0{connection.token}".encode()
    ).hexdigest()


def _safe_capability_metadata(value: Any, token: str) -> Any:
    """A gateway's metadata cannot echo its bearer token to API readers."""

    if isinstance(value, str):
        return redact_text(value.replace(token, "[REDACTED]"), max_chars=2048)
    if isinstance(value, list):
        return [_safe_capability_metadata(item, token) for item in value]
    if isinstance(value, dict):
        return {
            _safe_capability_metadata(key, token): _safe_capability_metadata(
                item, token
            )
            for key, item in value.items()
        }
    return value


class ModelsGatewayProbeCache:
    """Small thread-safe cache keyed by a non-reversible connection fingerprint."""

    def __init__(
        self,
        *,
        ttl_seconds: float = MODELS_GATEWAY_PROBE_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._values: dict[str, tuple[float, dict[str, Any]]] = {}

    def peek(self, connection: ModelsGatewayConnection) -> dict[str, Any] | None:
        """Return a fresh cached fact without loading or extending its TTL."""

        fingerprint = _connection_fingerprint(connection)
        with self._lock:
            cached = self._values.get(fingerprint)
            if cached is None or cached[0] <= self._clock():
                return None
            return cached[1]

    def get(
        self,
        connection: ModelsGatewayConnection,
        loader: Callable[[], dict[str, Any]],
        *,
        bypass: bool,
    ) -> dict[str, Any]:
        fingerprint = _connection_fingerprint(connection)
        with self._lock:
            now = self._clock()
            cached = self._values.get(fingerprint)
            if not bypass and cached is not None and cached[0] > now:
                return cached[1]
        # Probing can block on the network; passive readers need only this lock
        # for a brief cache lookup. Concurrent misses may probe independently.
        value = loader()
        with self._lock:
            self._values[fingerprint] = (self._clock() + self._ttl_seconds, value)
        return value


def _empty_probe(
    detail: str,
    *,
    reachable: bool = False,
    status: int | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "reachable": reachable,
        "status": status,
        "detail": detail,
        "service": None,
        "version": None,
        "engines": [],
    }


def probe_models_gateway(
    connection: ModelsGatewayConnection,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Perform one bounded authenticated probe with redirects disabled."""

    timeout = httpx.Timeout(
        connect=MODELS_GATEWAY_PROBE_CONNECT_TIMEOUT_SECONDS,
        read=MODELS_GATEWAY_PROBE_TIMEOUT_SECONDS,
        write=MODELS_GATEWAY_PROBE_CONNECT_TIMEOUT_SECONDS,
        pool=MODELS_GATEWAY_PROBE_CONNECT_TIMEOUT_SECONDS,
    )
    try:
        with httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
        ) as client:
            response = client.get(
                f"{connection.origin}/capabilities",
                headers={"Authorization": f"Bearer {connection.token}"},
            )
    except Exception:  # noqa: BLE001 - return a fixed, credential-safe failure
        return _empty_probe("models gateway is unreachable")
    if 300 <= response.status_code < 400:
        return _empty_probe(
            "models gateway redirects are not allowed",
            reachable=True,
            status=response.status_code,
        )
    if response.status_code in (401, 403):
        return _empty_probe(
            "models gateway rejected authentication",
            reachable=True,
            status=response.status_code,
        )
    if not response.is_success:
        return _empty_probe(
            "models gateway capability probe failed",
            reachable=True,
            status=response.status_code,
        )
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - never expose response bodies or decoder errors
        return _empty_probe(
            "models gateway capabilities are malformed",
            reachable=True,
            status=response.status_code,
        )
    if not isinstance(body, dict) or body.get("service") != "frisket-models":
        return _empty_probe(
            "models gateway service is incompatible",
            reachable=True,
            status=response.status_code,
        )
    raw_engines = body.get("engines")
    if not isinstance(raw_engines, list):
        return _empty_probe(
            "models gateway capabilities are malformed",
            reachable=True,
            status=response.status_code,
        )
    engines: list[dict[str, Any]] = []
    try:
        for raw in raw_engines:
            if not isinstance(raw, dict):
                raise ValueError
            selected = {
                name: raw.get(name)
                for name in (
                    "name",
                    "route",
                    "available",
                    "loaded",
                    "models",
                    "error",
                    "contract_versions",
                    "revision",
                    "runtime_image_id",
                    "options",
                )
                if name in raw
            }
            engines.append(
                _safe_capability_metadata(
                    ModelsGatewayEngineCapability.model_validate(selected).model_dump(
                        mode="json"
                    ),
                    connection.token,
                )
            )
    except (ValidationError, ValueError, TypeError):
        return _empty_probe(
            "models gateway capabilities are malformed",
            reachable=True,
            status=response.status_code,
        )
    version = body.get("version")
    if version is not None and not isinstance(version, str):
        return _empty_probe(
            "models gateway capabilities are malformed",
            reachable=True,
            status=response.status_code,
        )
    return {
        "ok": True,
        "reachable": True,
        "status": response.status_code,
        "detail": None,
        "service": "frisket-models",
        "version": _safe_capability_metadata(version, connection.token),
        "engines": engines,
    }


class ModelsGatewayService:
    """One scope's resolver and mutation authority."""

    def __init__(
        self,
        *,
        authority: str,
        receipt_scope: str,
        resolve_connection: Callable[[], ModelsGatewayConnection | None],
        save_connection: Callable[[str, str], ModelsGatewayConnection],
        transport: httpx.BaseTransport | None = None,
        cache: ModelsGatewayProbeCache | None = None,
    ) -> None:
        self.authority = authority
        self.receipt_scope = receipt_scope
        self._resolve_connection = resolve_connection
        self._save_connection = save_connection
        self._transport = transport
        self._cache = cache or ModelsGatewayProbeCache()

    @classmethod
    def workspace(
        cls,
        root: str | Path,
        *,
        env: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        cache: ModelsGatewayProbeCache | None = None,
    ) -> ModelsGatewayService:
        workspace_root = Path(root)
        receipt_scope = (
            "workspace:"
            + hashlib.sha256(str(workspace_root.resolve()).encode()).hexdigest()
        )
        return cls(
            authority="workspace",
            receipt_scope=receipt_scope,
            resolve_connection=lambda: provider_config.resolve_models_gateway(
                workspace_root, env
            ),
            save_connection=lambda origin, token: (
                provider_config.save_local_models_gateway(
                    workspace_root, origin=origin, token=token
                )
            ),
            transport=transport,
            cache=cache,
        )

    def resolve_connection(self) -> ModelsGatewayConnection | None:
        """The same trusted resolver used by status, probes, and execution."""

        return self._resolve_connection()

    def _probe(
        self, connection: ModelsGatewayConnection, *, bypass_cache: bool
    ) -> dict[str, Any]:
        return self._cache.get(
            connection,
            lambda: probe_models_gateway(connection, transport=self._transport),
            bypass=bypass_cache,
        )

    def passive_status(self, *, can_mutate: bool) -> dict[str, Any]:
        """Project gateway facts without performing a network probe."""

        return self._status(can_mutate=can_mutate, include_probe=False)

    def status(self, *, can_mutate: bool, recheck: bool = False) -> dict[str, Any]:
        return self._status(
            can_mutate=can_mutate,
            include_probe=True,
            recheck=recheck,
        )

    def _status(
        self,
        *,
        can_mutate: bool,
        include_probe: bool,
        recheck: bool = False,
    ) -> dict[str, Any]:
        try:
            connection = self.resolve_connection()
        except (
            InvalidModelsGatewayConfig,
            provider_config.InvalidProviderConfigError,
        ) as exc:
            return {
                "schemaVersion": "frisket.models_gateway.v1",
                "configured": False,
                "source": "environment",
                "origin": None,
                "token_configured": False,
                "token_hint": None,
                "authority": self.authority,
                "can_mutate": False,
                "environment_names": list(MODELS_GATEWAY_ENV_NAMES),
                "error": str(exc),
                "probe": None,
            }
        if connection is None:
            return {
                "schemaVersion": "frisket.models_gateway.v1",
                "configured": False,
                "source": None,
                "origin": None,
                "token_configured": False,
                "token_hint": None,
                "authority": self.authority,
                "can_mutate": can_mutate,
                "environment_names": list(MODELS_GATEWAY_ENV_NAMES),
                "error": None,
                "probe": None,
            }
        return {
            "schemaVersion": "frisket.models_gateway.v1",
            "configured": True,
            "source": connection.source,
            "origin": connection.origin,
            "token_configured": True,
            "token_hint": key_hint(connection.token),
            "authority": self.authority,
            "can_mutate": can_mutate and connection.source != "environment",
            "environment_names": list(MODELS_GATEWAY_ENV_NAMES),
            "error": None,
            "probe": (
                self._probe(connection, bypass_cache=recheck)
                if include_probe
                else self._cache.peek(connection)
            ),
        }

    def validate_candidate(
        self, *, origin: str | None, token: str | None
    ) -> dict[str, Any]:
        candidate = origin is not None and token is not None
        if candidate:
            try:
                connection = ModelsGatewayConnection(
                    origin=origin,
                    token=token,
                    source="stored",
                )
            except InvalidModelsGatewayConfig as exc:
                probe = _empty_probe(str(exc))
                return {
                    "schemaVersion": "frisket.models_gateway_validation.v1",
                    "normalized_origin": None,
                    "validation_token": None,
                    "probe": probe,
                }
        else:
            try:
                connection = self.resolve_connection()
            except (
                InvalidModelsGatewayConfig,
                provider_config.InvalidProviderConfigError,
            ) as exc:
                connection = None
                probe = _empty_probe(str(exc))
                return {
                    "schemaVersion": "frisket.models_gateway_validation.v1",
                    "normalized_origin": None,
                    "validation_token": None,
                    "probe": probe,
                }
        if connection is None:
            probe = _empty_probe("models gateway is not configured")
            return {
                "schemaVersion": "frisket.models_gateway_validation.v1",
                "normalized_origin": None,
                "validation_token": None,
                "probe": probe,
            }
        probe = self._probe(connection, bypass_cache=True)
        receipt = (
            provider_config.issue_models_gateway_validation_token(
                self.receipt_scope,
                connection.origin,
                connection.token,
            )
            if candidate and probe["ok"]
            else None
        )
        return {
            "schemaVersion": "frisket.models_gateway_validation.v1",
            "normalized_origin": connection.origin,
            "validation_token": receipt,
            "probe": probe,
        }

    def save(self, *, origin: str, token: str, validation_token: str) -> dict[str, Any]:
        effective = self.resolve_connection()
        if effective is not None and effective.source == "environment":
            raise ValueError(
                "environment-owned models gateway configuration is read-only"
            )
        normalized = provider_config.require_models_gateway_validation_token(
            self.receipt_scope,
            origin,
            token,
            validation_token,
        )
        self._save_connection(normalized, token.strip())
        return self.status(can_mutate=True)


__all__ = [
    "ModelsGatewayProbeCache",
    "ModelsGatewayService",
    "probe_models_gateway",
]
