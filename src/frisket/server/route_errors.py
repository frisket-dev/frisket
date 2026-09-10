"""One route-service error envelope.

Every route-facing service used to mint its own ``*RouteError`` (four shapes:
``.detail: str`` on ValueError, ``.detail`` on Exception, ``.detail: Any``,
and ``.content`` + ``bare_json``) and every route re-decided the
HTTPException-vs-bare-JSONResponse wire fork in its own except-block.

Now there is one ``RouteError`` and one app-level handler registered in
``create_app`` (``register_route_error_handler``), so routes raise through
their service and write NO except-block. Per-service subclasses (e.g.
``ImportCsvRouteError(RouteError)``) remain for isinstance scoping only.

Wire format is unchanged:
- default: ``{"detail": <content>}`` with the error status — byte-identical to
  FastAPI's ``HTTPException(status_code, detail)`` handler output;
- ``bare_json=True``: ``<content>`` itself as the JSON body (the action-runs
  contract responses that predate the envelope).

Unexpected exceptions also use a sanitized ``{"detail": "Internal Server
Error"}`` JSON response. The original exception is logged, never reflected to
the client.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from frisket.contracts.http.models import HttpError


LOG = logging.getLogger("frisket.server")


def http_error_responses(
    *statuses: int,
) -> dict[int, dict[str, type[HttpError]]]:
    """Declare generic JSON error responses on their owning FastAPI route."""

    return {status: {"model": HttpError} for status in statuses}


class RouteError(ValueError):
    """Service-raised HTTP error carrying its own status + payload."""

    def __init__(self, status_code: int, content: Any, *, bare_json: bool = False):
        super().__init__(str(content))
        self.status_code = status_code
        self.content = content
        self.bare_json = bare_json

    @property
    def detail(self) -> Any:
        """Alias for ``content`` (the pre-consolidation attribute name)."""
        return self.content


def route_error_to_response(exc: RouteError) -> JSONResponse:
    if exc.bare_json:
        return JSONResponse(status_code=exc.status_code, content=exc.content)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.content})


def register_route_error_handler(app: FastAPI) -> None:
    async def _handle_route_error(_request: Request, exc: RouteError) -> JSONResponse:
        return route_error_to_response(exc)

    async def _handle_unexpected_error(
        request: Request, exc: Exception
    ) -> JSONResponse:
        LOG.error(
            "unhandled_server_error",
            exc_info=exc,
            extra={
                "event": "unhandled_server_error",
                "method": request.method,
                "path": request.url.path,
            },
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal Server Error"},
        )

    app.add_exception_handler(RouteError, _handle_route_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)


ExceptionTypes = type[Exception] | tuple[type[Exception], ...]


def register_typed_error(
    app: FastAPI,
    exception_types: ExceptionTypes,
    status_code: int | None = None,
    detail: Any | Callable[[Exception], Any] = str,
) -> None:
    # Deliberately never registers a broad ValueError or KeyError handler.
    async def _handler(_request: Request, exc: Exception) -> JSONResponse:
        code = status_code if status_code is not None else getattr(exc, "status_code")
        content = detail(exc) if callable(detail) else detail
        if content is None:
            content = getattr(exc, "detail")
        return JSONResponse(status_code=code, content={"detail": content})

    types = (
        exception_types if isinstance(exception_types, tuple) else (exception_types,)
    )
    for exception_type in types:
        app.add_exception_handler(exception_type, _handler)


def reject_unknown_query_parameters(
    request: Request,
    query_model: type[BaseModel],
) -> None:
    """Reject keys outside a declared query model instead of ignoring them."""

    allowed = {
        str(field.alias or name) for name, field in query_model.model_fields.items()
    }
    unexpected = sorted(set(request.query_params) - allowed)
    if not unexpected:
        return
    raise HTTPException(
        status_code=422,
        detail=[
            {
                "type": "extra_forbidden",
                "loc": ["query", name],
                "msg": "Extra inputs are not permitted",
                "input": request.query_params.get(name),
            }
            for name in unexpected
        ],
    )
