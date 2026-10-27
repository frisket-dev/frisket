"""Acquire URLs once into the existing unpublished file inventory."""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request

from frisket.authoring.workbench.plugin_runtime_capabilities import (
    enabled_workbench_plugin_ids,
)
from frisket.engine.executor import ExecutorDeps
from frisket.engine.store.disk_capacity import require_disk_headroom
from frisket.ops.url_import import acquire_url_records
from frisket.server.services.import_bulk_sources import fsync_directory
from frisket.server.services.import_urls import ImportUrlsRouteError
from frisket.server.thread_worker import await_thread_worker
from frisket.server.workspace import Workspace


class ImportUrlsPrepareService:
    def __init__(self, workspace: Workspace, *, pdf_import_service: Any):
        self._workspace = workspace
        self._pdf_import_service = pdf_import_service

    async def prepare(
        self, project_id: str, *, request: Request, urls: list[str]
    ) -> dict[str, Any]:
        if not urls:
            raise ImportUrlsRouteError(400, "no urls")
        project = self._workspace.get(project_id)
        factory = self._workspace.executor_deps_factory
        deps = (
            factory(project_id, request) if factory is not None else None
        ) or ExecutorDeps()
        limit = deps.url_import_limits.max_urls if deps.url_import_limits else None
        if limit is not None and len(urls) > limit:
            raise ImportUrlsRouteError(
                400, f"URL count exceeds the deployment limit of {limit}"
            )
        enabled = enabled_workbench_plugin_ids(project)
        root = project.path / ".bulk_import_staging"
        staging = root / f".draft-urls-{secrets.token_urlsafe(18)}"
        adopted: dict[str, Any] | None = None
        try:
            (staging / "files").mkdir(parents=True)
            entries = []
            downloaded = 0
            for index, url in enumerate(urls, start=1):
                if await request.is_disconnected():
                    raise ImportUrlsRouteError(499, "URL preparation was cancelled")
                entry = await await_thread_worker(
                    _acquire_one, staging, index, url.strip(), enabled
                )
                entries.append(entry)
                downloaded += entry.get("acquisition_error") is None

            def adopt() -> dict[str, Any]:
                nonlocal adopted
                adopted = self._pdf_import_service.adopt_staged(
                    project_id, staging, entries, source_label=f"{len(urls)} URLs"
                )
                return adopted

            draft = await await_thread_worker(adopt)
            failed = len(urls) - downloaded
            return {
                "kind": "pdf_import",
                "draft": draft,
                "downloaded": downloaded,
                "failed": failed,
                "warnings": [
                    f"Downloaded {downloaded} URLs; {failed} failed. Failed downloads remain error rows."
                ]
                if failed
                else [],
            }
        except asyncio.CancelledError:
            if adopted is not None:
                await self.discard(project_id, {"draft": adopted})
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            fsync_directory(root)

    async def discard(self, project_id: str, prepared: dict[str, Any]) -> None:
        draft_id = prepared["draft"]["draft_id"]
        await await_thread_worker(
            self._pdf_import_service.discard, project_id, draft_id
        )


def _acquire_one(
    staging: Path, index: int, url: str, enabled_plugin_ids: set[str] | frozenset[str]
) -> dict[str, Any]:
    records, _kinds, blobs, refs, sources, _errors = acquire_url_records(
        [url], enabled_plugin_ids=enabled_plugin_ids
    )
    record, source = records[0], sources[0]
    if "error" in record:
        try:
            filename = PurePosixPath(urlsplit(url).path).name or f"URL {index}"
        except ValueError:
            filename = f"URL {index}"
        return {
            "logical_path": f"urls/{index:08d}/{filename}",
            "filename": filename,
            "mime": "application/octet-stream",
            "size": 0,
            "source_url": url,
            "provider": source.get("provider"),
            "metadata": {},
            "acquisition_error": record["error"],
        }
    blob, ref = blobs[0], refs[0]
    filename = PurePosixPath(blob["filename"]).name or f"URL {index}"
    digest = blob["hash"]
    target = staging / "files" / digest
    if not target.exists():
        require_disk_headroom(staging, len(blob["data"]))
        with target.open("xb") as sink:
            sink.write(blob["data"])
            sink.flush()
            os.fsync(sink.fileno())
        fsync_directory(target.parent)
    return {
        "logical_path": f"urls/{index:08d}/{filename}",
        "filename": filename,
        "mime": blob["mime"],
        "sha256": digest,
        "size": len(blob["data"]),
        "path": f"files/{digest}",
        "source_url": ref["source_url"],
        "provider": ref["provider"],
        "metadata": blob["metadata"],
    }
