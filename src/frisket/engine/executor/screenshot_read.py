"""Invocation-owned browser capture and occurrence-bound image evidence."""

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
from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.store.media_blobs import owned_media_metadata_document
from frisket.ops.capture import url as url_capture


def _bounded_integer(value, maximum):
    return type(value) is int and 1 <= value <= maximum


class AdmittedScreenshotter:
    def __init__(self, stager, *, cancelled=None, browser_renderer=None):
        self._stager = stager
        self._cancelled = cancelled
        self._browser = browser_renderer
        self._closed = False
        self._reads = set()
        self.calls_by_row = {}

    def _check_open(self):
        if self._closed:
            raise RuntimeError("screenshotter is closed")
        if self._cancelled is not None and self._cancelled():
            raise asyncio.CancelledError

    def bind_row(self, row, *, sheet_id, row_id, sources):
        self._check_open()
        if type(sheet_id) is not int or type(row_id) is not int:
            raise RowError(
                "invalid_input_ref", "Screenshotter requires its admitted row."
            )
        return _BoundScreenshotter(
            self,
            self._stager.bind_row(row_id),
            sheet_id,
            row_id,
            copy.deepcopy(dict(sources or {})),
        )

    async def aclose(self):
        self._closed = True
        for task in tuple(self._reads):
            await _settle(task)


class _BoundScreenshotter:
    def __init__(self, owner, stager, sheet_id, row_id, sources):
        self._owner = owner
        self._stager = stager
        self._sheet_id = sheet_id
        self._row_id = row_id
        self._sources = sources

    async def capture(
        self,
        url,
        *,
        full_page=True,
        viewport=(1280, 720),
        max_bytes=5_000_000,
        timeout_ms=30_000,
    ):
        owner = self._owner
        owner._check_open()
        if not isinstance(url, str) or not url.strip():
            raise RowError("invalid_url", "URL cell is empty or not text.")
        if (
            type(full_page) is not bool
            or not isinstance(viewport, tuple)
            or len(viewport) != 2
            or not all(_bounded_integer(value, 4096) for value in viewport)
            or not _bounded_integer(max_bytes, 50_000_000)
            or not _bounded_integer(timeout_ms, 300_000)
        ):
            raise RowError("invalid_params", "Invalid screenshot capture options.")
        url = url.strip()

        def render(actual_url, **options):
            # This wrapper is entered only after capture's URL-safety check.
            call = {
                "call_id": uuid.uuid4().hex,
                "provider": "browser",
                "external_api": True,
                "url": actual_url,
                "full_page": full_page,
                "viewport": {"width": viewport[0], "height": viewport[1]},
                "max_bytes": max_bytes,
                "timeout_ms": timeout_ms,
                "status": "attempted",
            }
            owner.calls_by_row.setdefault(self._row_id, []).append(call)
            try:
                result = (owner._browser or url_capture.render_playwright_url)(
                    actual_url, **options
                )
            except BaseException:
                call["status"] = "failed"
                raise
            call["status"] = "returned"
            return result

        task = asyncio.create_task(
            asyncio.to_thread(
                url_capture.capture_playwright_url,
                url,
                capture_screenshot=True,
                include_warc=False,
                full_page=full_page,
                viewport_width=viewport[0],
                viewport_height=viewport[1],
                max_bytes=max_bytes,
                timeout_ms=timeout_ms,
                render=render,
            )
        )
        owner._reads.add(task)
        try:
            try:
                while not task.done():
                    owner._check_open()
                    await asyncio.wait({task}, timeout=0.05)
                capture = await asyncio.shield(task)
                owner._check_open()
            except BaseException:
                await _settle(task)
                raise
        finally:
            owner._reads.discard(task)
        if capture.status != "captured":
            raise RowError(
                capture.reason or "capture_failed",
                capture.error or "Browser capture failed.",
            )
        data = capture.screenshot_bytes
        if (
            not data
            or not data.startswith(b"\x89PNG\r\n\x1a\n")
            or capture.screenshot_mime != "image/png"
        ):
            raise RowError(
                "invalid_screenshot", "Browser capture did not return a PNG image."
            )
        viewport_fact = {"width": viewport[0], "height": viewport[1]}
        facts = {
            "kind": "screenshot",
            "sheet_id": self._sheet_id,
            "sources": self._sources,
            "url": url,
            "final_url": capture.final_url,
            "canonical_url": capture.canonical_url,
            "title": capture.title,
            "captured_at": capture.captured_at,
            "status_code": capture.status_code,
            "content_type": capture.content_type,
            "byte_count": capture.byte_count,
            "network_metadata": capture.network_metadata,
            "warnings": capture.warnings,
            "full_page": full_page,
            "viewport": viewport_fact,
            "max_bytes": max_bytes,
            "timeout_ms": timeout_ms,
        }
        metadata = owned_media_metadata_document(
            probe={"kind": "image"},
            owner={
                "capture_kind": "web_page_screenshot",
                "full_page": full_page,
                "viewport": viewport_fact,
            },
        )
        with tempfile.TemporaryDirectory(prefix="frisket-screenshot-") as scratch:
            path = Path(scratch) / "screenshot.png"
            path.write_bytes(data)
            plan = RowBlobPlan(
                role="screenshot",
                content_digest=hashlib.sha256(data).hexdigest(),
                staged_path=path,
                filename=f"screenshot-{self._row_id}.png",
                mime="image/png",
                source_url=capture.final_url,
                metadata=metadata,
            )
            owner._check_open()
            return self._stager.stage_output(
                RowBlobOutput(primary=plan, facts=facts), image=True
            )


def publish_row_file_evidence(
    project,
    descriptor,
    *,
    row_id,
    column_id,
    output_key,
    item_path,
    run_id,
    op_id,
):
    """Publish actual browser facts inside the host's result/evidence savepoint."""
    facts = descriptor["facts"]
    primary = descriptor["primary"]
    if facts["kind"] != "screenshot":
        raise ValueError("invalid screenshot descriptor")
    sources = facts["sources"]
    source_column_id = (
        next(iter(sources.values()))["column_id"] if len(sources) == 1 else None
    )
    capture_metadata = {
        key: facts[key]
        for key in (
            "status_code",
            "content_type",
            "byte_count",
            "warnings",
            "full_page",
            "viewport",
            "captured_at",
            "max_bytes",
            "timeout_ms",
            "network_metadata",
        )
    }
    capture_metadata.update(render_mode="playwright", role="screenshot")
    artifact = record_source_artifact(
        project,
        artifact_kind="capture_screenshot",
        media_type=primary["mime"],
        blob_hash=primary["blob_hash"],
        source_url=facts["final_url"] or facts["url"],
        canonical_url=facts["canonical_url"],
        title=facts["title"],
        filename=primary["filename"],
        source_sheet_id=facts["sheet_id"],
        source_row_id=row_id,
        source_column_id=source_column_id,
        metadata=capture_metadata,
    )
    span = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="image",
        selector={"blob_hash": primary["blob_hash"]},
        snippet=facts["title"],
        metadata=capture_metadata,
    )
    producer = {"capability": "Screenshotter", "render_mode": "playwright"}
    occurrence = {
        "output_key": output_key,
        "item_path": item_path,
        "screenshot_blob_hash": primary["blob_hash"],
    }
    links = []
    for source in sources.values():
        link = record_evidence_link(
            project,
            subject_kind="cell",
            subject_ref=source["value_ref"] or {},
            spans=[{"span_id": span["id"], "rank": 1}],
            sheet_id=facts["sheet_id"],
            row_id=row_id,
            column_id=source["column_id"],
            run_id=run_id,
            op_id=op_id,
            link_role="web_capture_page",
            producer=producer,
            metadata=occurrence,
        )
        links.append({"id": link["id"], "stable_id": link["stable_id"]})
    link = record_evidence_link(
        project,
        subject_kind="cell",
        subject_ref={
            "kind": "run_result",
            "run_id": run_id,
            "op_id": op_id,
            "row_id": row_id,
            "column_id": column_id,
        },
        spans=[{"span_id": span["id"], "rank": 1}],
        sheet_id=facts["sheet_id"],
        row_id=row_id,
        column_id=column_id,
        run_id=run_id,
        op_id=op_id,
        link_role="screenshot_output",
        producer=producer,
        metadata=occurrence,
    )
    links.append({"id": link["id"], "stable_id": link["stable_id"]})
    return {
        "artifact_id": artifact["id"],
        "artifact_stable_id": artifact["stable_id"],
        "span_id": span["id"],
        "span_stable_id": span["stable_id"],
        "links": links,
    }


def verify_row_file_evidence(project, descriptor, publication):
    """Refuse replay when the screenshot's issued evidence no longer exists."""
    if not isinstance(publication, dict) or set(publication) != {
        "artifact_id",
        "artifact_stable_id",
        "span_id",
        "span_stable_id",
        "links",
    }:
        raise ValueError("screenshot publication evidence is incomplete")
    if any(
        type(publication[key]) is not int or publication[key] <= 0
        for key in ("artifact_id", "span_id")
    ) or any(
        not isinstance(publication[key], str) or not publication[key]
        for key in ("artifact_stable_id", "span_stable_id")
    ):
        raise ValueError("screenshot publication evidence has invalid identities")
    facts = descriptor["facts"]
    artifact = project.db.execute(
        "SELECT id FROM source_artifacts WHERE id=? AND stable_id=? "
        "AND artifact_kind='capture_screenshot' AND blob_hash=? "
        "AND source_sheet_id=? AND source_row_id=?",
        (
            publication["artifact_id"],
            publication["artifact_stable_id"],
            descriptor["primary"]["blob_hash"],
            facts["sheet_id"],
            descriptor["row_id"],
        ),
    ).fetchone()
    span = project.db.execute(
        "SELECT id FROM source_spans WHERE id=? AND stable_id=? AND artifact_id=? AND span_kind='image'",
        (
            publication["span_id"],
            publication["span_stable_id"],
            publication["artifact_id"],
        ),
    ).fetchone()
    links = publication["links"]
    if (
        artifact is None
        or span is None
        or not isinstance(links, list)
        or len(links) != len(facts["sources"]) + 1
    ):
        raise ValueError("screenshot publication evidence is missing")
    seen = set()
    for ref in links:
        if (
            not isinstance(ref, dict)
            or set(ref) != {"id", "stable_id"}
            or type(ref["id"]) is not int
            or ref["id"] <= 0
            or not isinstance(ref["stable_id"], str)
            or not ref["stable_id"]
            or ref["id"] in seen
        ):
            raise ValueError("screenshot evidence link is incomplete")
        seen.add(ref["id"])
        link = project.db.execute(
            "SELECT l.id FROM evidence_links l JOIN evidence_link_spans s ON s.link_id=l.id "
            "WHERE l.id=? AND l.stable_id=? AND s.span_id=? AND l.row_id=?",
            (ref["id"], ref["stable_id"], publication["span_id"], descriptor["row_id"]),
        ).fetchone()
        if link is None:
            raise ValueError("screenshot evidence link is missing")
