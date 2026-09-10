"""Row-admitted yt-dlp downloads; project writes belong to accepted publication."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import tempfile
import threading
import uuid
from pathlib import Path

from frisket.actions.media_download_types import DownloadedFiles, download_outputs
from frisket.actions.types import Outcome, RowError
from frisket.engine.executor.blob_outputs import RowBlobOutput, RowBlobPlan
from frisket.engine.executor.visual_cuts_read import _settle
from frisket.engine.store.artifact_timeline import canonical_json_hash
from frisket.engine.store.cell_writes import EditCellWrite, insert_edits
from frisket.engine.store.media_blobs import owned_media_metadata_document
from frisket.ops.http_urls import is_http_url
from frisket.ops.egress_policy import media_egress_policy
from frisket.ops.subtitles import subtitle_text
from frisket.ops import ytdlp


def download_max_concurrency():
    from frisket.ops.media_proxy import resolve_media_proxy

    if resolve_media_proxy() is None:
        return None
    try:
        return max(1, int(os.environ.get("FRISKET_MEDIA_PROXY_MAX_CONCURRENCY") or "1"))
    except ValueError:
        return 1


class AdmittedMediaDownloader:
    def __init__(self, project, stager, *, cancelled=None):
        self._project, self._stager = project, stager
        self._cancelled = cancelled
        self._closed = False
        self._tasks = set()
        self.calls_by_row = {}

    def cancelled(self):
        return self._closed or bool(self._cancelled and self._cancelled())

    def check_open(self):
        if self.cancelled():
            raise asyncio.CancelledError

    def bind_row(self, row, *, sheet_id, row_id, sources):
        self.check_open()
        return _BoundMediaDownloader(
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


class _BoundMediaDownloader:
    def __init__(self, owner, stager, sheet_id, row_id, sources):
        self._owner, self._stager = owner, stager
        self._sheet_id, self._row_id, self._sources = sheet_id, row_id, sources

    async def download(
        self, url, *, media_type="video", format_selector=None, extra_opts=None
    ):
        owner = self._owner
        owner.check_open()
        if not is_http_url(url):
            raise RowError("invalid_url", "Download requires an HTTP(S) media URL.")
        if media_type not in {"audio", "video"}:
            raise RowError("invalid_params", "media_type must be audio or video.")
        if format_selector is not None and (
            not isinstance(format_selector, str) or not format_selector.strip()
        ):
            raise RowError("invalid_params", "format_selector must be nonblank text.")
        options = copy.deepcopy({} if extra_opts is None else extra_opts)
        try:
            ytdlp.validate_extra_opts(options)
        except (TypeError, ValueError) as exc:
            raise RowError("invalid_params", str(exc)) from None
        active = download_outputs(media_type, options)
        if "subtitles" in active:
            options.setdefault("writesubtitles", True)
            options.setdefault("writeautomaticsub", True)
        url = url.strip()
        call = {
            "call_id": uuid.uuid4().hex,
            "provider": "yt_dlp",
            "service": "yt-dlp",
            "external_api": True,
            "url": url,
            "media_type": media_type,
            "format_selector": format_selector,
            "extra_opts": copy.deepcopy(options) or None,
            "status": "attempted",
        }
        owner.calls_by_row.setdefault(self._row_id, []).append(call)
        stopped = threading.Event()
        task = asyncio.create_task(
            asyncio.to_thread(
                ytdlp.download_media,
                url,
                media_type=media_type,
                format_selector=format_selector,
                extra_opts=options or None,
                policy=media_egress_policy(owner._project),
                should_cancel=lambda: stopped.is_set() or owner.cancelled(),
            )
        )
        owner._tasks.add(task)
        try:
            result = await asyncio.shield(task)
            owner.check_open()
            call["status"] = "returned"
        except BaseException:
            stopped.set()
            call["status"] = "failed"
            raise
        finally:
            await _settle(task)
            owner._tasks.discard(task)
        if not isinstance(result, ytdlp.DownloadedMedia):
            raise RowError("invalid_output", "Downloader returned invalid media.")
        acquisition = copy.deepcopy(result.metadata)
        call["provider"] = str(acquisition.get("provider") or "yt_dlp")
        if result.duration_seconds is not None:
            acquisition.setdefault("duration_seconds", result.duration_seconds)
        if result.runtime_evidence:
            acquisition["managed_runtime"] = copy.deepcopy(result.runtime_evidence)
        facts = {
            "kind": "media_download",
            "provider": str(acquisition.get("provider") or "yt_dlp"),
            "url": url,
            "sheet_id": self._sheet_id,
            "row_id": self._row_id,
            "sources": self._sources,
        }
        if acquisition.get("provider") == "youtube":
            facts["channel"] = {
                key: acquisition[key]
                for key in ("channel_id", "channel_title")
                if acquisition.get(key)
            }

        def stage(value, *, role, image=False, media=None, metadata=None):
            with tempfile.TemporaryDirectory(prefix="frisket-ytdlp-stage-") as scratch:
                path = Path(scratch) / "download"
                path.write_bytes(value.data)
                return self._stager.stage_output(
                    RowBlobOutput(
                        primary=RowBlobPlan(
                            role=role,
                            content_digest=hashlib.sha256(value.data).hexdigest(),
                            staged_path=path,
                            filename=value.filename,
                            mime=value.mime,
                            source_url=url,
                            metadata=metadata or {},
                        ),
                        facts=facts if media else {**facts, "channel": {}},
                    ),
                    image=image,
                    media_type=media,
                )

        values = {key: Outcome.ok(None) for key in active if key != media_type}
        values.update(
            {
                media_type: stage(
                    result,
                    role="download",
                    media=media_type,
                    metadata=owned_media_metadata_document(acquisition=acquisition),
                )
            }
        )
        found_subtitles = False
        for sidecar in result.sidecars:
            key = {
                "thumbnail": "thumbnail",
                "subtitles": "subtitles",
                "info_json": "info",
            }.get(sidecar.kind)
            if key not in active:
                continue
            if key == "subtitles":
                found_subtitles = True
            try:
                values[key] = Outcome.ok(
                    stage(sidecar, role=sidecar.kind, image=key == "thumbnail")
                )
                if key == "subtitles":
                    values["subtitles_text"] = Outcome.ok(
                        subtitle_text(sidecar.data, sidecar.filename)
                    )
            except RowError as exc:
                values[key] = Outcome.failed(exc.code, exc.message)
                if key == "subtitles":
                    values["subtitles_text"] = Outcome.failed(exc.code, exc.message)
        if "subtitles" in active and not found_subtitles:
            missing = Outcome.failed(
                "subtitles_unavailable",
                "Subtitles were requested but yt-dlp produced no manual or automatic captions.",
            )
            values["subtitles"] = missing
            values["subtitles_text"] = missing
        return DownloadedFiles(**values)


def publish_download_channel_facts(
    project, descriptor, *, run_id, op_id, row_id, **_kwargs
):
    """Inside accepted file publication, preserve fill-empty edits and undo."""
    facts = descriptor["facts"]
    channel = facts.get("channel") or {}
    if not channel or facts.get("row_id") != row_id:
        return
    row = project.db.execute(
        "SELECT sheet_id, hidden FROM rows WHERE id=?", (row_id,)
    ).fetchone()
    if row is None or row["hidden"] or row["sheet_id"] != facts.get("sheet_id"):
        return
    for captured in facts.get("sources", {}).values():
        values, refs = project.get_values_with_refs(
            row["sheet_id"], captured["column_id"], row_ids=[row_id]
        )
        if canonical_json_hash(values.get(row_id)) != canonical_json_hash(
            captured["value"]
        ) or refs.get(row_id) != captured.get("value_ref"):
            return
    edits: list[EditCellWrite] = []
    for column in project.columns(row["sheet_id"]):
        value = channel.get(column["name"])
        if (
            value
            and column["name"] in {"channel_id", "channel_title"}
            and not project.get_values(
                row["sheet_id"], column["id"], row_ids=[row_id]
            ).get(row_id)
        ):
            edits.append(
                EditCellWrite(
                    row_id=row_id,
                    column_id=int(column["id"]),
                    value=value,
                )
            )
    if edits:
        edit_op = project.append_op(
            "edit",
            {"count": len(edits), "source_run_id": run_id, "source_op_id": op_id},
            label="youtube channel backfill",
            commit=False,
        )
        insert_edits(project.db, op_id=edit_op, edits=edits)
