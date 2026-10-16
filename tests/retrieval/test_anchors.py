from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.retrieval.anchors import (
    resolve_cell_anchor,
    resolve_cell_anchor_async,
)
from frisket.retrieval.contracts import CellAnchor, PageRange, SourceLocator, TimeRange
from frisket.retrieval.encoding import AudioEncodingInput, ImageEncodingInput
from frisket.retrieval.media import AudioWindow, LocatedFrame, PresentationPoint
from frisket.retrieval.query import RetrievalQueryError


def _blob_cell(
    project: Project,
    *,
    column_type: str,
    mime: str,
    data: bytes,
    page: int | None = None,
) -> tuple[int, int, int]:
    sheet_id = project.add_sheet("Sources")
    column_id = project.add_column(sheet_id, "source", type=column_type)
    digest = project.add_blob(data, mime=mime)
    value = {"blob": digest, "mime": mime}
    if page is not None:
        value["page"] = page
    row_id = project.add_rows(sheet_id, [{"source": value}], {"source": column_id})[0]
    return sheet_id, row_id, column_id


def test_image_anchor_uses_exact_current_blob_bytes(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "image.frisket")
    image = b"\x89PNG\r\n\x1a\nfixture"
    try:
        sheet_id, row_id, column_id = _blob_cell(
            project, column_type="image", mime="image/png", data=image
        )

        resolved = resolve_cell_anchor(project, CellAnchor(sheet_id, row_id, column_id))

        assert resolved.anchor.source.source_kind == "blob"
        assert resolved.anchor.locator == SourceLocator(kind="image")
        assert resolved.anchor.binding is not None
        assert resolved.anchor.binding.dependencies[0].retrieval_revision > 0
        assert resolved.input == ImageEncodingInput(image)
    finally:
        project.close()


def test_pdf_anchor_requires_one_explicit_current_page(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pdf-location.frisket")
    try:
        sheet_id, row_id, column_id = _blob_cell(
            project,
            column_type="file",
            mime="application/pdf",
            data=b"pdf fixture",
        )

        with pytest.raises(RetrievalQueryError) as caught:
            resolve_cell_anchor(project, CellAnchor(sheet_id, row_id, column_id))

        assert caught.value.code == "source_location_required"
    finally:
        project.close()


def test_pdf_anchor_renders_only_selected_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "pdf-page.frisket")
    calls: list[list[int] | None] = []

    async def render(source, scratch, *, pages, **kwargs):
        calls.append(pages)
        output = scratch / "page-2.png"
        output.write_bytes(b"\x89PNG\r\n\x1a\npage two")
        return type("Rendered", (), {"pages": ((2, output),)})()

    monkeypatch.setattr("frisket.retrieval.anchors.render_pdf_pages", render)
    try:
        sheet_id, row_id, column_id = _blob_cell(
            project,
            column_type="file",
            mime="application/pdf",
            data=b"pdf fixture",
        )
        selected = CellAnchor(
            sheet_id,
            row_id,
            column_id,
            locator=SourceLocator(kind="pdf_page", pages=PageRange(2, 2)),
        )

        resolved = asyncio.run(resolve_cell_anchor_async(project, selected))

        assert calls == [[2]]
        assert resolved.anchor.locator.pages == PageRange(2, 2)
        assert isinstance(resolved.input, ImageEncodingInput)
    finally:
        project.close()


def test_video_anchor_returns_decoder_observed_frame_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "video.frisket")

    async def decode(source, at_seconds, **kwargs):
        assert at_seconds == 1.25
        return LocatedFrame(PresentationPoint(6, 1, 4), ImageEncodingInput(b"jpeg"))

    monkeypatch.setattr("frisket.retrieval.anchors.decode_video_frame", decode)
    try:
        sheet_id, row_id, column_id = _blob_cell(
            project, column_type="video", mime="video/mp4", data=b"video fixture"
        )
        selected = CellAnchor(
            sheet_id,
            row_id,
            column_id,
            locator=SourceLocator(kind="video_frame", time=TimeRange(1.25, 1.25)),
        )

        async def call_sync_adapter():
            return resolve_cell_anchor(project, selected)

        resolved = asyncio.run(call_sync_adapter())

        assert resolved.anchor.locator.time == TimeRange(1.5, 1.5)
        assert resolved.input == ImageEncodingInput(b"jpeg")
    finally:
        project.close()


def test_audio_anchor_returns_effective_decoded_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "audio.frisket")

    async def decode(source, start_seconds, end_seconds, **kwargs):
        assert (start_seconds, end_seconds) == (4.0, 9.0)
        return AudioWindow(4.0, 7.25, AudioEncodingInput(b"\0\0" * 10))

    monkeypatch.setattr("frisket.retrieval.anchors.decode_audio_interval", decode)
    try:
        sheet_id, row_id, column_id = _blob_cell(
            project, column_type="audio", mime="audio/wav", data=b"audio fixture"
        )
        selected = CellAnchor(
            sheet_id,
            row_id,
            column_id,
            locator=SourceLocator(kind="audio_window", time=TimeRange(4.0, 9.0)),
        )

        resolved = resolve_cell_anchor(project, selected)

        assert resolved.anchor.locator.time == TimeRange(4.0, 7.25)
        assert isinstance(resolved.input, AudioEncodingInput)
    finally:
        project.close()
