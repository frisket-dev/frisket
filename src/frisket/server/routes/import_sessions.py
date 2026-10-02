"""HTTP boundary for resumable, progressively visible file imports."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import FastAPI, File, Form, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from frisket.contracts.http.import_sessions import (
    ImportSessionList,
    ImportSessionStatus,
)
from frisket.server.route_errors import RouteError, http_error_responses
from frisket.server.services.import_bulk_types import BulkUpload, ImportBulkRouteError
from frisket.server.services.import_sessions import ImportSessionService
from frisket.server.thread_worker import await_thread_worker


class ImportSessionCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheet_name: str = Field(min_length=1, max_length=80)


class ImportSessionResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["keep", "remove"]


def _run_async(call: Callable[[], Any]) -> Any:
    """Run an async service operation wholly on the bounded thread worker."""

    return asyncio.run(call())


def _route_error(exc: Exception) -> RouteError:
    if isinstance(exc, ImportBulkRouteError):
        return exc
    return RouteError(409, str(exc))


def register_import_session_routes(
    app: FastAPI,
    *,
    service: ImportSessionService,
    max_rows_for_request: Callable[[str, Request], int | None],
) -> None:
    base = "/api/projects/{pid}/import/files/sessions"
    responses = http_error_responses(400, 401, 403, 404, 409, 413, 422, 500)

    @app.post(base, response_model=ImportSessionStatus, responses=responses)
    async def create_import_session(
        pid: str, body: ImportSessionCreateBody, request: Request
    ) -> ImportSessionStatus:
        try:
            return await await_thread_worker(
                service.create,
                pid,
                body.sheet_name,
                max_rows=max_rows_for_request(pid, request),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise _route_error(exc) from exc

    @app.get(base, response_model=ImportSessionList, responses=responses)
    async def list_import_sessions(pid: str) -> ImportSessionList:
        return await await_thread_worker(service.list, pid)

    @app.get(f"{base}/{{ref}}", response_model=ImportSessionStatus, responses=responses)
    async def get_import_session(pid: str, ref: str) -> ImportSessionStatus:
        try:
            return await await_thread_worker(service.status, pid, ref)
        except (OSError, ValueError) as exc:
            raise _route_error(exc) from exc

    @app.post(
        f"{base}/{{ref}}/files",
        response_model=ImportSessionStatus,
        responses=responses,
    )
    async def upload_import_session_files(
        pid: str,
        ref: str,
        files: Annotated[list[UploadFile], File()],
        logical_paths: Annotated[list[str], Form()],
        batch_id: Annotated[str, Form(min_length=1)],
    ) -> ImportSessionStatus:
        if len(files) != len(logical_paths):
            raise RouteError(400, "files and logical_paths must have the same length")
        uploads = [
            BulkUpload(
                filename=file.filename or "upload",
                logical_path=logical_path,
                mime=file.content_type or "application/octet-stream",
                file=file,
            )
            for file, logical_path in zip(files, logical_paths, strict=True)
        ]
        try:
            return await await_thread_worker(
                _run_async,
                lambda: service.upload(pid, ref, uploads, batch_id=batch_id),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise _route_error(exc) from exc

    async def transition(
        operation: Callable[..., Any], pid: str, ref: str, **kwargs: Any
    ) -> ImportSessionStatus:
        try:
            if asyncio.iscoroutinefunction(operation):
                return await await_thread_worker(
                    _run_async, lambda: operation(pid, ref, **kwargs)
                )
            return await await_thread_worker(operation, pid, ref, **kwargs)
        except (OSError, RuntimeError, ValueError) as exc:
            raise _route_error(exc) from exc

    @app.post(
        f"{base}/{{ref}}/seal", response_model=ImportSessionStatus, responses=responses
    )
    async def seal_import_session(pid: str, ref: str) -> ImportSessionStatus:
        return await transition(service.seal, pid, ref)

    @app.post(
        f"{base}/{{ref}}/cancel",
        response_model=ImportSessionStatus,
        responses=responses,
    )
    async def cancel_import_session(pid: str, ref: str) -> ImportSessionStatus:
        return await transition(service.cancel, pid, ref)

    @app.post(
        f"{base}/{{ref}}/resolve",
        response_model=ImportSessionStatus,
        responses=responses,
    )
    async def resolve_import_session(
        pid: str, ref: str, body: ImportSessionResolveBody
    ) -> ImportSessionStatus:
        return await transition(service.resolve, pid, ref, decision=body.decision)

    @app.post(
        f"{base}/{{ref}}/resume",
        response_model=ImportSessionStatus,
        responses=responses,
    )
    async def resume_import_session(pid: str, ref: str) -> ImportSessionStatus:
        return await transition(service.resume, pid, ref)


__all__ = ["register_import_session_routes"]
