"""Invocation-owned metadata probes, content caching, and borrowed blob lifetimes."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from frisket.actions.types import RowError
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.ops.media_metadata import (
    _finalize_bounds,
    cache_entry_from_envelope,
    envelope_from_cache,
    extract_media_metadata,
    media_metadata_cache_compatible,
    media_metadata_cache_facts_size,
    overlay_reference_evidence,
)


_BLOB_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_MAX_RUN_TEMPLATE_CACHE_ENTRIES = 256
_MAX_RUN_TEMPLATE_CACHE_BYTES = 16 * 1024 * 1024
_MAX_CONCURRENT_PROBES = 8
_CacheKey = tuple[str, bool]


async def _settle(task: asyncio.Future[Any]) -> None:
    """Wait through caller cancellation without cancelling a running worker."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    if not task.cancelled():
        # Retrieve failures even when the last reader waiter was cancelled.
        task.exception()


class AdmittedMediaMetadataReader:
    def __init__(
        self,
        project: Project,
        *,
        preview: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self._project = project
        self._preview = preview
        self._cancelled = cancelled
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._slots = asyncio.Semaphore(_MAX_CONCURRENT_PROBES)
        self._futures: dict[_CacheKey, asyncio.Task[dict[str, Any]]] = {}
        self._completed_templates: OrderedDict[
            _CacheKey, tuple[dict[str, Any], int]
        ] = OrderedDict()
        self._completed_template_bytes = 0
        self._probe_cancellations: set[threading.Event] = set()

    @property
    def closed(self) -> bool:
        return self._closed

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("media metadata reader is closed")
        if self._cancelled is not None and self._cancelled():
            raise asyncio.CancelledError

    async def read(self, cell: Any, *, refresh: bool = False) -> dict[str, Any] | None:
        self._check_open()
        if type(refresh) is not bool:
            raise TypeError("refresh must be boolean")
        if cell is None or (isinstance(cell, str) and not cell.strip()):
            return None
        if not isinstance(cell, dict):
            raise RowError(
                "invalid_media_cell", "The media cell must be a blob-backed object."
            )
        digest = cell.get("blob")
        if not isinstance(digest, str) or not _BLOB_DIGEST.fullmatch(digest):
            raise RowError(
                "invalid_media_cell", "The media cell has an invalid blob digest."
            )

        blob = MediaBlobStore(self._project).blob_row(digest)
        if blob is None:
            raise RowError(
                "missing_blob",
                "The media cell references a blob that is not available.",
            )
        filename = cell.get("filename")
        if not isinstance(filename, str):
            filename = blob["filename"] if isinstance(blob["filename"], str) else None
        claimed_mime = cell.get("mime")
        if not isinstance(claimed_mime, str):
            claimed_mime = blob["mime"] if isinstance(blob["mime"], str) else None

        key = (digest, refresh)
        completed = self._completed_templates.get(key)
        if completed is not None:
            self._completed_templates.move_to_end(key)
            template = completed[0]
        else:
            future = self._futures.get(key)
            if future is None:
                future = asyncio.create_task(
                    self._load_and_remember(key, size_bytes=int(blob["size"]))
                )
                self._futures[key] = future
                future.add_done_callback(lambda done: self._forget_future(key, done))
            try:
                template = await asyncio.shield(future)
            except (OSError, RuntimeError, ValueError) as error:
                raise RowError(
                    "metadata_extract_failed", "Media metadata extraction failed."
                ) from error
        return overlay_reference_evidence(
            template, filename=filename, claimed_mime=claimed_mime
        )

    def _forget_future(self, key: _CacheKey, future: asyncio.Future[Any]) -> None:
        if self._futures.get(key) is future:
            self._futures.pop(key)
        if not future.cancelled():
            future.exception()

    def _remember(self, key: _CacheKey, template: dict[str, Any]) -> None:
        previous = self._completed_templates.pop(key, None)
        if previous is not None:
            self._completed_template_bytes -= previous[1]
        try:
            weight = media_metadata_cache_facts_size(template)
        except (RecursionError, TypeError, ValueError):
            return
        if weight > _MAX_RUN_TEMPLATE_CACHE_BYTES:
            return
        self._completed_templates[key] = (template, weight)
        self._completed_template_bytes += weight
        while self._completed_templates and (
            len(self._completed_templates) > _MAX_RUN_TEMPLATE_CACHE_ENTRIES
            or self._completed_template_bytes > _MAX_RUN_TEMPLATE_CACHE_BYTES
        ):
            _, (_, removed_weight) = self._completed_templates.popitem(last=False)
            self._completed_template_bytes -= removed_weight

    async def _load_and_remember(
        self, key: _CacheKey, *, size_bytes: int
    ) -> dict[str, Any]:
        async with self._slots:
            template = await self._load_content_template(
                digest=key[0], size_bytes=size_bytes, refresh=key[1]
            )
            self._remember(key, template)
            return template

    async def _load_content_template(
        self, *, digest: str, size_bytes: int, refresh: bool
    ) -> dict[str, Any]:
        blob_store = MediaBlobStore(self._project)
        existing = blob_store.media_metadata_cache(digest)
        if (
            not refresh
            and existing is not None
            and media_metadata_cache_compatible(existing)
        ):
            return envelope_from_cache(existing, digest=digest, size_bytes=size_bytes)

        with self._project.materialize_blob(digest) as raw_path:
            cancel_event = threading.Event()
            self._probe_cancellations.add(cancel_event)
            probe = asyncio.create_task(
                asyncio.to_thread(
                    extract_media_metadata,
                    Path(raw_path),
                    digest=digest,
                    size_bytes=size_bytes,
                    filename=None,
                    claimed_mime=None,
                    cancel_event=cancel_event,
                )
            )
            try:
                while not probe.done():
                    if self._cancelled is not None and self._cancelled():
                        raise asyncio.CancelledError
                    await asyncio.wait({probe}, timeout=0.05)
                envelope = await asyncio.shield(probe)
                if self._cancelled is not None and self._cancelled():
                    raise asyncio.CancelledError
            except BaseException:
                cancel_event.set()
                await _settle(probe)
                raise
            finally:
                self._probe_cancellations.discard(cancel_event)

        if self._preview:
            return envelope
        try:
            proposal = cache_entry_from_envelope(envelope)
        except ValueError:
            return _cache_bypass_warning(envelope)
        raw_generation = (
            existing.get("generation") if isinstance(existing, dict) else None
        )
        expected_generation = (
            raw_generation if type(raw_generation) is int and raw_generation >= 0 else 0
        )
        try:
            blob_store.compare_and_swap_media_metadata_cache(
                digest, proposal, expected_generation=expected_generation
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return _cache_bypass_warning(envelope)
        return envelope

    async def aclose(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._finish_close())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await _settle(self._close_task)
            raise

    async def _finish_close(self) -> None:
        pending = tuple(self._futures.values())
        for event in self._probe_cancellations:
            event.set()
        for future in pending:
            future.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._futures.clear()
        self._completed_templates.clear()
        self._completed_template_bytes = 0


def _cache_bypass_warning(envelope: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(envelope)
    warning = {
        "code": "internal_error",
        "subtype": "cache_write_bypassed",
        "source": "cache",
        "message": "Metadata was extracted but the shared blob cache could not be updated.",
    }
    warnings = [item for item in result.get("warnings") or [] if isinstance(item, dict)]
    if warning not in warnings:
        warnings.append(warning)
    result["warnings"] = warnings
    return _finalize_bounds(result)
