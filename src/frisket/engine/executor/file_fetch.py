"""Actual-argument URL acquisition without early project materialization."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import tempfile
import uuid
from pathlib import Path

from frisket.actions.types import RowError
from frisket.engine.executor.blob_outputs import RowBlobOutput, RowBlobPlan
from frisket.engine.executor.visual_cuts_read import _settle
from frisket.engine.store.media_blobs import owned_media_metadata_document
from frisket.features.url_classification import (
    classify_url,
    is_supported_ytdlp_media_url,
)
from frisket.ops import enclosures, ytdlp
from frisket.ops.egress_policy import media_egress_policy


class AdmittedFileFetcher:
    def __init__(self, project, stager, *, cancelled=None):
        self._project, self._stager = project, stager
        self._cancelled = cancelled
        self._closed = False
        self._tasks = set()
        self.calls_by_row = {}

    def _check_open(self):
        if self._closed:
            raise RuntimeError("file fetcher is closed")
        if self._cancelled is not None and self._cancelled():
            raise asyncio.CancelledError

    def bind_row(self, row, *, sheet_id, row_id, sources):
        self._check_open()
        return _BoundFileFetcher(
            self,
            self._stager.bind_row(row_id),
            sheet_id,
            row_id,
            copy.deepcopy(dict(sources or {})),
        )

    async def aclose(self):
        self._closed = True
        for task in tuple(self._tasks):
            await _settle(task)


class _BoundFileFetcher:
    def __init__(self, owner, stager, sheet_id, row_id, sources):
        self._owner, self._stager = owner, stager
        self._sheet_id, self._row_id, self._sources = sheet_id, row_id, sources

    async def fetch(self, url):
        owner = self._owner
        owner._check_open()
        if not isinstance(url, str) or not url.strip():
            raise RowError("invalid_url", "URL cell is empty or not text.")
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise RowError("invalid_url", "URL must start with http:// or https://.")
        from frisket.authoring.workbench.plugin_runtime_capabilities import (
            enabled_workbench_plugin_ids,
        )

        plugins = enabled_workbench_plugin_ids(owner._project)
        download_media = is_supported_ytdlp_media_url(url, enabled_plugin_ids=plugins)
        classification = classify_url(url, enabled_plugin_ids=plugins)
        provider = (
            str(classification.provider or "yt_dlp")
            if download_media and classification is not None
            else "direct_url"
        )
        policy = media_egress_policy(owner._project)
        call = {
            "call_id": uuid.uuid4().hex,
            "provider": provider,
            "service": "yt-dlp" if download_media else "httpx",
            "external_api": True,
            "url": url,
            "status": "attempted",
        }

        def acquire():
            owner._check_open()
            owner.calls_by_row.setdefault(self._row_id, []).append(call)
            try:
                if download_media:
                    result = ytdlp.download_media(url, policy=policy)
                    acquisition = dict(result.metadata or {})
                    if result.duration_seconds is not None:
                        acquisition.setdefault(
                            "duration_seconds", result.duration_seconds
                        )
                    value = (
                        result.data,
                        result.mime,
                        result.filename,
                        owned_media_metadata_document(acquisition=acquisition),
                    )
                else:
                    data, mime, filename, error = enclosures.download_url(
                        url, policy=policy
                    )
                    if error is not None:
                        raise RowError("url_fetch_failed", "URL download failed.")
                    value = data, mime, filename, {}
                call["status"] = "returned"
                return value
            except BaseException:
                call["status"] = "failed"
                raise

        task = asyncio.create_task(asyncio.to_thread(acquire))
        owner._tasks.add(task)
        try:
            try:
                while not task.done():
                    owner._check_open()
                    await asyncio.wait({task}, timeout=0.05)
                data, mime, filename, metadata = await asyncio.shield(task)
            except BaseException:
                await _settle(task)
                raise
        except RowError:
            raise
        except Exception:
            raise RowError("url_fetch_failed", "URL download failed.") from None
        finally:
            owner._tasks.discard(task)
        with tempfile.TemporaryDirectory(prefix="frisket-fetch-") as scratch:
            path = Path(scratch) / "download"
            path.write_bytes(data)
            return self._stager.stage_output(
                RowBlobOutput(
                    primary=RowBlobPlan(
                        role="download",
                        content_digest=hashlib.sha256(data).hexdigest(),
                        staged_path=path,
                        filename=filename,
                        mime=mime,
                        source_url=url,
                        metadata=metadata,
                    ),
                    facts={
                        "kind": "fetch",
                        "provider": provider,
                        "url": url,
                        "sheet_id": self._sheet_id,
                        "row_id": self._row_id,
                        "sources": self._sources,
                    },
                )
            )
