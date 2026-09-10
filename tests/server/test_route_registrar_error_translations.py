from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.authoring.workbench.plugin_runtime_shared import (
    WorkbenchPluginActivationError,
)
from frisket.server.routes.notifications import register_notification_routes
from frisket.server.routes.projects import register_project_lifecycle_routes
from frisket.server.routes.views import register_view_lens_routes
from frisket.server.routes.watches import register_watch_routes
from frisket.server.routes.workbench import register_workbench_routes
from frisket.server.services.notifications import NotificationRequestError
from frisket.server.services.projects import ProjectNotFound
from frisket.server.services.views import ViewNotFound
from frisket.server.services.watches import WatchNotFound


Registrar = Callable[..., None]


class _RaisingService:
    def __init__(self, exc: Exception):
        self.exc = exc

    def __getattr__(self, _name: str) -> Callable[..., Any]:
        def raise_error(*_args: Any, **_kwargs: Any) -> Any:
            raise self.exc

        return raise_error


@pytest.mark.parametrize(
    ("registrar", "method", "path", "exc", "status", "content"),
    [
        (
            register_project_lifecycle_routes,
            "GET",
            "/api/projects/missing",
            ProjectNotFound("no project 'missing'"),
            404,
            b'{"detail":"no project \'missing\'"}',
        ),
        (
            register_notification_routes,
            "GET",
            "/api/projects/p/notifications",
            NotificationRequestError("notification conflict", status_code=409),
            409,
            b'{"detail":"notification conflict"}',
        ),
        (
            register_view_lens_routes,
            "GET",
            "/api/projects/p/views/9",
            ViewNotFound("service text is intentionally hidden"),
            404,
            b'{"detail":"view not found"}',
        ),
        (
            register_watch_routes,
            "POST",
            "/api/projects/p/watches/9/run",
            WatchNotFound("service text is intentionally hidden"),
            404,
            b'{"detail":"watch not found"}',
        ),
        (
            register_workbench_routes,
            "GET",
            "/api/projects/p/workbench/plugins/demo/env",
            WorkbenchPluginActivationError(
                "plugin_blocked",
                "plugin is blocked",
                status_code=403,
                details={"permission": "network"},
            ),
            403,
            b'{"detail":{"code":"plugin_blocked","message":"plugin is blocked",'
            b'"details":{"permission":"network"}}}',
        ),
    ],
)
def test_registrar_translates_its_typed_service_errors_standalone(
    registrar: Registrar,
    method: str,
    path: str,
    exc: Exception,
    status: int,
    content: bytes,
) -> None:
    app = FastAPI()
    service = _RaisingService(exc)
    with pytest.raises(type(exc)) as direct:
        service.any_direct_call()
    assert direct.value is exc
    registrar(app, service=service)

    response = TestClient(app).request(method, path)

    assert response.status_code == status
    assert response.content == content
    assert response.headers["content-type"] == "application/json"


@pytest.mark.parametrize(
    ("registrar", "method", "path"),
    [
        (register_project_lifecycle_routes, "GET", "/api/projects/p"),
        (register_notification_routes, "GET", "/api/projects/p/notifications"),
        (register_view_lens_routes, "GET", "/api/projects/p/views/9"),
        (register_watch_routes, "POST", "/api/projects/p/watches/9/run"),
        (
            register_workbench_routes,
            "GET",
            "/api/projects/p/workbench/plugins/demo/env",
        ),
    ],
)
def test_registrar_does_not_translate_unrelated_value_errors(
    registrar: Registrar,
    method: str,
    path: str,
) -> None:
    app = FastAPI()
    service = _RaisingService(ValueError("unrelated"))
    registrar(app, service=service)

    with pytest.raises(ValueError, match="unrelated"):
        TestClient(app).request(method, path)

    with pytest.raises(ValueError, match="unrelated"):
        service.any_direct_call()
