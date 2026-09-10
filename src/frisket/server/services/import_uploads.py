"""Small shared helpers for HTTP upload admission."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from contextvars import copy_context
from dataclasses import dataclass
from functools import partial
from typing import Any, BinaryIO, Callable, TypeVar

from fastapi import UploadFile
from frisket.engine.store.receipts import ReceiptStore
from frisket.server.route_errors import RouteError


_UPLOAD_CHUNK_BYTES = 1024 * 1024
_WorkerResult = TypeVar("_WorkerResult")


@dataclass(frozen=True)
class AdmittedUpload:
    """A bounded, borrowed request upload ready for one import operation."""

    filename: str
    mime: str
    source: BinaryIO
    sha256: str
    size: int


class ImportUploadRouteError(RouteError):
    """A direct-upload admission error rendered by the normal route handler."""


async def admit_upload(
    file: UploadFile, *, max_bytes: int | None = None
) -> AdmittedUpload:
    """Hash and bound one existing Starlette spool without taking ownership."""

    return await await_thread_worker(_admit_upload, file, max_bytes=max_bytes)


async def admit_uploads(
    files: Sequence[UploadFile],
    *,
    max_files: int | None = None,
    max_bytes: int | None = None,
) -> list[AdmittedUpload]:
    """Hash and bound request spools, applying aggregate upload quotas."""

    return await await_thread_worker(
        _admit_uploads,
        files,
        max_files=max_files,
        max_bytes=max_bytes,
    )


def _admit_uploads(
    files: Sequence[UploadFile],
    *,
    max_files: int | None = None,
    max_bytes: int | None = None,
) -> list[AdmittedUpload]:
    try:
        if max_files is not None and len(files) > max_files:
            raise ImportUploadRouteError(
                413, "upload file count exceeds deployment limit"
            )

        admitted: list[AdmittedUpload] = []
        total = 0
        for file in files:
            remaining = None if max_bytes is None else max_bytes - total
            upload = _admit_upload(file, max_bytes=remaining)
            total += upload.size
            admitted.append(upload)
        return admitted
    finally:
        for file in files:
            file.file.seek(0)


def _admit_upload(file: UploadFile, *, max_bytes: int | None = None) -> AdmittedUpload:
    digest = hashlib.sha256()
    size = 0
    try:
        file.file.seek(0)
        while chunk := file.file.read(_UPLOAD_CHUNK_BYTES):
            size += len(chunk)
            if max_bytes is not None and size > max_bytes:
                raise ImportUploadRouteError(
                    413, "upload bytes exceeds deployment limit"
                )
            digest.update(chunk)
        return AdmittedUpload(
            filename=file.filename or "upload",
            mime=file.content_type or "application/octet-stream",
            source=file.file,
            sha256=digest.hexdigest(),
            size=size,
        )
    finally:
        file.file.seek(0)


async def await_thread_worker(
    worker: Callable[..., _WorkerResult], /, *args: Any, **kwargs: Any
) -> _WorkerResult:
    """Keep caller-owned inputs alive until a cancelled worker has finished."""

    future = asyncio.get_running_loop().run_in_executor(
        None, copy_context().run, partial(worker, *args, **kwargs)
    )
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if future.done():
            try:
                future.result()
            except BaseException:
                pass
        raise


def upload_sheet_name(
    project: Any, action_id: str, key: str, requested: str, *, allocate: bool
) -> str:
    """Keep a retry's published name; allocate only for default upload names."""

    stored = ReceiptStore(project).find_by_idempotency_key(key)
    if stored is not None and stored.action_kind == action_id:
        for output in stored.parsed().outputs:
            if output.ref.get("kind") == "materialized_sheet":
                return output.name
    if not allocate:
        return requested
    names = {str(row["name"]) for row in project.db.execute("SELECT name FROM sheets")}
    candidate = requested
    suffix = 2
    while candidate in names:
        candidate = f"{requested}-{suffix}"
        suffix += 1
    return candidate
