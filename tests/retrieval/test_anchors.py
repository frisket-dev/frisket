from __future__ import annotations

import asyncio

import pytest

from frisket.engine.store import Project
from frisket.retrieval.anchors import (
    resolve_cell_anchor,
    resolve_cell_anchor_async,
)
from frisket.retrieval.contracts import (
    CellAnchor,
    PageRange,
    SourceLocator,
    TextRange,
    TimeRange,
)
from frisket.retrieval.encoding import (
    AudioEncodingInput,
    ImageEncodingInput,
    TextEncodingInput,
)
from frisket.retrieval.media import (
    AudioWindow,
    LocatedFrame,
    MediaProductionError,
    PresentationPoint,
)
from frisket.retrieval.query import RetrievalQueryError
from frisket.retrieval.source_adapter import resolve_cell_text_sources


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


def test_long_text_anchor_requires_a_bounded_passage(make_project) -> None:
    project = make_project("long-text")
    sheet_id = project.add_sheet("Sources")
    column_id = project.add_column(sheet_id, "source")
    row_id = project.add_rows(
        sheet_id, [{"source": "x" * 2049}], {"source": column_id}
    )[0]

    with pytest.raises(RetrievalQueryError) as caught:
        resolve_cell_anchor(project, CellAnchor(sheet_id, row_id, column_id))

    assert caught.value.code == "source_location_required"
    assert "at most 2048" in str(caught.value)


def test_text_anchor_encodes_the_exact_bounded_selection(make_project) -> None:
    project = make_project("selected-text")
    sheet_id = project.add_sheet("Sources")
    column_id = project.add_column(sheet_id, "source")
    value = "prefix selected passage suffix"
    row_id = project.add_rows(sheet_id, [{"source": value}], {"source": column_id})[0]
    with project.read_snapshot() as snapshot:
        source = resolve_cell_text_sources(
            snapshot,
            sheet_id=sheet_id,
            column_id=column_id,
            row_ids=[row_id],
        )[row_id]
    surface = source.surface
    start = value.index("selected")
    end = start + len("selected passage")
    selected = CellAnchor(
        sheet_id,
        row_id,
        column_id,
        locator=SourceLocator(
            kind="text",
            text=TextRange(
                surface.text_surface_id,
                surface.content_hash,
                start,
                end,
            ),
        ),
    )

    resolved = resolve_cell_anchor(project, selected)

    assert resolved.anchor.locator == selected.locator
    assert resolved.input == TextEncodingInput("selected passage")


def test_image_anchor_uses_exact_current_blob_bytes(make_project) -> None:
    project = make_project("image")
    image = b"\x89PNG\r\n\x1a\nfixture"
    sheet_id, row_id, column_id = _blob_cell(
        project, column_type="image", mime="image/png", data=image
    )

    resolved = resolve_cell_anchor(project, CellAnchor(sheet_id, row_id, column_id))

    assert resolved.anchor.source.source_kind == "blob"
    assert resolved.anchor.locator == SourceLocator(kind="image")
    assert resolved.anchor.binding is not None
    assert resolved.anchor.binding.dependencies[0].retrieval_revision > 0
    assert resolved.input == ImageEncodingInput(image)


def test_pdf_anchor_requires_one_explicit_current_page(make_project) -> None:
    project = make_project("pdf-location")
    sheet_id, row_id, column_id = _blob_cell(
        project,
        column_type="file",
        mime="application/pdf",
        data=b"pdf fixture",
    )

    with pytest.raises(RetrievalQueryError) as caught:
        resolve_cell_anchor(project, CellAnchor(sheet_id, row_id, column_id))

    assert caught.value.code == "source_location_required"


def test_pdf_anchor_renders_only_selected_page(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project("pdf-page")
    calls: list[list[int] | None] = []

    async def render(source, scratch, *, pages, **kwargs):
        calls.append(pages)
        output = scratch / "page-2.png"
        output.write_bytes(b"\x89PNG\r\n\x1a\npage two")
        return type("Rendered", (), {"pages": ((2, output),)})()

    monkeypatch.setattr("frisket.retrieval.anchors.render_pdf_pages", render)
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


def test_video_anchor_returns_decoder_observed_frame_time(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project("video")
    borrowed = []

    async def decode(source, at_seconds, **kwargs):
        assert at_seconds == 1.25
        assert source.borrow_path().is_file()
        borrowed.append(source)
        return LocatedFrame(PresentationPoint(6, 1, 4), ImageEncodingInput(b"jpeg"))

    monkeypatch.setattr("frisket.retrieval.anchors.decode_video_frame", decode)
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
    with pytest.raises(MediaProductionError, match="lease has ended"):
        borrowed[0].borrow_path()


def test_audio_anchor_returns_effective_decoded_interval(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project("audio")
    borrowed = []

    async def decode(source, start_seconds, end_seconds, **kwargs):
        assert (start_seconds, end_seconds) == (4.0, 9.0)
        assert source.borrow_path().is_file()
        borrowed.append(source)
        return AudioWindow(4.0, 7.25, AudioEncodingInput(b"\0\0" * 10))

    monkeypatch.setattr("frisket.retrieval.anchors.decode_audio_interval", decode)
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
    with pytest.raises(MediaProductionError, match="lease has ended"):
        borrowed[0].borrow_path()


@pytest.mark.parametrize("kind", ["text", "image"])
def test_explicit_anchor_can_use_hidden_active_column_but_not_withdrawn_column(
    make_project, kind
):
    from frisket.retrieval.source_adapter import resolve_cell_blob_source

    project = make_project("hidden-anchor")
    if kind == "image":
        image = b"\x89PNG\r\n\x1a\nfixture"
        sheet, row, column = _blob_cell(
            project, column_type="image", mime="image/png", data=image
        )
        expected = ImageEncodingInput(image)
    else:
        sheet = project.add_sheet("Sources")
        column = project.add_column(sheet, "source")
        row = project.add_rows(
            sheet, [{"source": "explicit hidden text"}], {"source": column}
        )[0]
        expected = TextEncodingInput("explicit hidden text")
    project.db.execute("UPDATE columns SET hidden=1 WHERE id=?", (column,))
    project.db.commit()
    anchor = CellAnchor(sheet, row, column)
    assert resolve_cell_anchor(project, anchor).input == expected
    with project.read_snapshot() as snapshot:
        # Ordinary visible discovery does not inherit the anchor's intent.
        assert (
            resolve_cell_text_sources(
                snapshot, sheet_id=sheet, column_id=column, row_ids=[row]
            )
            == {}
        )
        assert (
            resolve_cell_blob_source(
                snapshot, sheet_id=sheet, column_id=column, row_id=row
            )
            is None
        )
    project.db.execute("UPDATE columns SET active=0 WHERE id=?", (column,))
    project.db.commit()
    with pytest.raises(RetrievalQueryError) as caught:
        resolve_cell_anchor(project, anchor)
    assert caught.value.code == "source_anchor_unavailable"
