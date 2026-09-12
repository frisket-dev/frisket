"""Organization models-gateway storage, worker port, and HTTP routes."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

import httpx
import sqlalchemy as sa
from fastapi import APIRouter, FastAPI, HTTPException, Request

from frisket.ai.models.gateway_config import (
    InvalidModelsGatewayConfig,
    MODELS_GATEWAY_TOKEN_ENV,
    MODELS_GATEWAY_URL_ENV,
    ModelsGatewayConnection,
    resolve_models_gateway_env,
)
from frisket.contracts.http.models_gateway import (
    ModelsGatewayCandidateRequest,
    ModelsGatewaySaveRequest,
    ModelsGatewayStatus,
    ModelsGatewayValidationResponse,
)
from frisket.server.route_errors import http_error_responses
from frisket.server.routes.models_gateway import ModelsGatewayAPIRoute
from frisket.server.services.models_gateway import ModelsGatewayService
from frisket.team.control_plane import control_plane_engine
from frisket.team.db import atomic_upsert, locked_transaction
from frisket.team.schema import org_env_vars
from frisket.team.security.secrets import key_hint


def resolve_team_models_gateway(
    engine: sa.Engine,
    *,
    org_id: int,
    decrypt: Callable[[str], str],
    env: Mapping[str, str] | None = None,
) -> ModelsGatewayConnection | None:
    """Resolve environment over the existing encrypted org environment rows."""

    environment = resolve_models_gateway_env(os.environ if env is None else env)
    if environment is not None:
        return environment
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(org_env_vars.c.name, org_env_vars.c.encrypted).where(
                org_env_vars.c.org_id == org_id,
                org_env_vars.c.name.in_(
                    (MODELS_GATEWAY_URL_ENV, MODELS_GATEWAY_TOKEN_ENV)
                ),
            )
        ).all()
    encrypted = {str(row.name): str(row.encrypted) for row in rows}
    origin_value = encrypted.get(MODELS_GATEWAY_URL_ENV)
    token_value = encrypted.get(MODELS_GATEWAY_TOKEN_ENV)
    if origin_value is None and token_value is None:
        return None
    if origin_value is None or token_value is None:
        raise InvalidModelsGatewayConfig(
            "stored models gateway configuration is incomplete"
        )
    return ModelsGatewayConnection(
        origin=decrypt(origin_value),
        token=decrypt(token_value),
        source="stored",
    )


class TeamModelsGatewayStore:
    """Atomic pair access over the existing encrypted org environment table."""

    def __init__(
        self,
        engine: sa.Engine,
        *,
        org_id: int,
        encrypt: Callable[[str], str],
        decrypt: Callable[[str], str],
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.engine = engine
        self.org_id = org_id
        self._encrypt = encrypt
        self._decrypt = decrypt
        self._env = env

    def resolve(self) -> ModelsGatewayConnection | None:
        return resolve_team_models_gateway(
            self.engine,
            org_id=self.org_id,
            decrypt=self._decrypt,
            env=self._env,
        )

    def save(self, origin: str, token: str) -> ModelsGatewayConnection:
        connection = ModelsGatewayConnection(
            origin=origin,
            token=token,
            source="stored",
        )
        values = {
            MODELS_GATEWAY_URL_ENV: connection.origin,
            MODELS_GATEWAY_TOKEN_ENV: connection.token,
        }
        with locked_transaction(
            self.engine,
            lock_scope=("org-models-gateway", self.org_id),
        ) as database:
            for name, value in values.items():
                stored = {
                    "encrypted": self._encrypt(value),
                    "hint": key_hint(value),
                }
                atomic_upsert(
                    database,
                    table=org_env_vars,
                    values={
                        "org_id": self.org_id,
                        "name": name,
                        **stored,
                    },
                    conflict_columns=("org_id", "name"),
                    update_values=stored,
                )
        return connection


class TeamOrgModelsGatewayPort:
    """Worker resolver for encrypted org gateway settings."""

    def __init__(self, decrypt: Callable[[str], str]) -> None:
        self._decrypt = decrypt

    def models_gateway_connection(
        self, *, org_id: int, control_database_url: str | None
    ) -> ModelsGatewayConnection | None:
        if not control_database_url:
            return resolve_models_gateway_env()
        engine = control_plane_engine(control_database_url)
        return resolve_team_models_gateway(
            engine,
            org_id=org_id,
            decrypt=self._decrypt,
        )


def team_models_gateway_service(
    engine: sa.Engine,
    *,
    org_id: int,
    encrypt: Callable[[str], str],
    decrypt: Callable[[str], str],
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> ModelsGatewayService:
    store = TeamModelsGatewayStore(
        engine,
        org_id=org_id,
        encrypt=encrypt,
        decrypt=decrypt,
        env=env,
    )
    return ModelsGatewayService(
        authority="organization",
        receipt_scope=f"organization:{org_id}",
        resolve_connection=store.resolve,
        save_connection=store.save,
        transport=transport,
    )


def register_team_models_gateway_routes(
    app: FastAPI,
    *,
    service: ModelsGatewayService,
    require_member: Callable[[Request], dict[str, Any]],
    require_owner: Callable[[Request], dict[str, Any]],
    member_can_mutate: Callable[[dict[str, Any]], bool],
) -> None:
    router = APIRouter(route_class=ModelsGatewayAPIRoute)

    @router.get(
        "/api/org/models-gateway",
        response_model=ModelsGatewayStatus,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 500),
    )
    def get_org_models_gateway(request: Request) -> dict:
        actor = require_member(request)
        return service.status(can_mutate=member_can_mutate(actor))

    @router.post(
        "/api/org/models-gateway/validate",
        response_model=ModelsGatewayValidationResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 422, 500),
    )
    def validate_org_models_gateway(
        request: Request, body: ModelsGatewayCandidateRequest
    ) -> dict:
        require_owner(request)
        return service.validate_candidate(origin=body.origin, token=body.token)

    @router.put(
        "/api/org/models-gateway",
        response_model=ModelsGatewayStatus,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 422, 500),
    )
    def set_org_models_gateway(
        request: Request, body: ModelsGatewaySaveRequest
    ) -> dict:
        require_owner(request)
        try:
            return service.save(
                origin=body.origin,
                token=body.token,
                validation_token=body.validation_token,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    app.include_router(router)


__all__ = [
    "TeamModelsGatewayStore",
    "TeamOrgModelsGatewayPort",
    "register_team_models_gateway_routes",
    "resolve_team_models_gateway",
    "team_models_gateway_service",
]
