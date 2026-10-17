from __future__ import annotations

import asyncio

import pytest

from frisket.engine.store import Project
from frisket.engine.pdf_text import PdfTextError, PdfTextSample
from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.engine.executor import run_action_spec
from frisket.retrieval.source_adapter import resolve_cell_blob_source
from frisket.server.route_errors import RouteError
from frisket.server.services import search_preparation_planning as planning
from tests.engine.test_transcript_segments_action import _seed_timestamped_transcript
from tests.engine.test_native_pdf_text_action import (
    _page,
    _pdf_bytes,
    _request,
    _source,
)


def test_automatic_plan_intersects_rows_that_remain_visible(tmp_path) -> None:
    project = Project.create(tmp_path / "visible-automatic-scope.frisket")
    try:
        sheet = project.add_sheet("Images")
        image = project.add_column(sheet, "image", type="image")
        rows = project.add_rows(
            sheet,
            [
                {
                    "image": {
                        "blob": project.add_blob(
                            f"image-{index}".encode(), mime="image/png"
                        ),
                        "mime": "image/png",
                    }
                }
                for index in range(3)
            ],
            {"image": image},
        )
        project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (rows[1],))

        with pytest.raises(RouteError, match="not visible") as error:
            planning.build_plan(
                project,
                sheet_id=sheet,
                row_ids=rows,
                force_ocr=False,
                requirements=(),
                requirement_dict=lambda *_args, **_kwargs: {},
            )
        assert error.value.status_code == 404

        automatic = planning.build_plan(
            project,
            sheet_id=sheet,
            row_ids=rows,
            force_ocr=False,
            requirements=(),
            requirement_dict=lambda *_args, **_kwargs: {},
            intersect_visible_rows=True,
        )
        assert [item["row_id"] for item in automatic["items"]] == [rows[0], rows[2]]
    finally:
        project.close()


def test_manual_edit_over_prepared_generation_stays_ready_for_same_source(
    tmp_path, monkeypatch
) -> None:
    from frisket.engine import pdf_text
    from frisket.retrieval.contracts import (
        RankingPolicy,
        RetrievalScope,
        SearchRequest,
        SheetScope,
        TextQuery,
    )
    from frisket.retrieval.query import search
    from frisket.search import drain_index

    async def native_text(_source, **_kwargs):
        return pdf_text.PdfTextBatch([_page(1, "native searchable text")], 1, None)

    monkeypatch.setattr(pdf_text, "extract_pdf_text_batch", native_text)
    project = Project.create(tmp_path / "edited-prepared-generation.frisket")
    try:
        sheet, document, row, _digest = _source(
            project, tmp_path, pdf=_pdf_bytes("native searchable text")
        )
        result = run_action_spec(
            project,
            _request(sheet, row, key="edited-prepared-generation@1"),
            project_id="edited-prepared-generation",
        )
        assert result.status == "completed", result.errors
        output = next(item for item in result.outputs if item.name == "prepared text")
        project.apply_edits(
            [
                {
                    "row_id": row,
                    "column_id": output.column_id,
                    "value": "editor corrected text",
                }
            ]
        )

        source = resolve_cell_blob_source(
            project, sheet_id=sheet, row_id=row, column_id=document
        )
        assert source is not None
        assert planning._has_current_result(project, source, "native_pdf")
        assert (
            planning.build_plan(
                project,
                sheet_id=sheet,
                row_ids=[row],
                force_ocr=False,
                requirements=(),
                requirement_dict=lambda *_args, **_kwargs: {},
            )["items"]
            == []
        )
        drain_index(project)

        def preparation_counts() -> tuple[int, int]:
            response = search(
                project,
                SearchRequest(
                    scope=RetrievalScope(
                        sheets=(SheetScope(sheet, column_ids=(document,)),)
                    ),
                    query=TextQuery("absent"),
                    mode="lexical",
                    ranking=RankingPolicy(rerank="off"),
                ),
            )
            gap = next(
                item
                for item in response.coverage.channels
                if item.channel == "search_preparation"
            )
            return int(gap.eligible_count or 0), int(gap.prepared_count or 0)

        assert preparation_counts() == (1, 1)

        replacement = project.add_blob(b"replacement pdf", mime="application/pdf")
        project.apply_edits(
            [
                {
                    "row_id": row,
                    "column_id": document,
                    "value": {"blob": replacement, "mime": "application/pdf"},
                }
            ]
        )
        changed_source = resolve_cell_blob_source(
            project, sheet_id=sheet, row_id=row, column_id=document
        )
        assert changed_source is not None
        assert not planning._has_current_result(project, changed_source, "native_pdf")
        assert preparation_counts() == (1, 0)
    finally:
        project.close()


def test_prepared_pdf_result_must_match_current_page_selector(
    tmp_path, monkeypatch
) -> None:
    project = Project.create(tmp_path / "page-selector.frisket")
    try:
        sheet = project.add_sheet("Documents")
        document = project.add_column(sheet, "document", type="file")
        output = project.add_column(sheet, "search text")
        digest = project.add_blob(b"pdf", mime="application/pdf")
        row = project.add_rows(
            sheet,
            [{"document": {"blob": digest, "mime": "application/pdf", "page": 1}}],
            {"document": document},
        )[0]
        project.db.execute("BEGIN IMMEDIATE")
        op_id = project.append_op("media.read_pdf_text", commit=False)
        artifact = record_source_artifact(
            project,
            artifact_kind="document",
            media_type="application/pdf",
            blob_hash=digest,
            page_count=2,
            source_sheet_id=sheet,
            source_row_id=row,
            source_column_id=document,
        )
        store = PreparedContentStore(project)
        whole = store.stage_reference(
            source_artifact_id=int(artifact["id"]),
            producing_op_id=op_id,
            pages=(
                PreparedPageDraft(1, "prepared first page"),
                PreparedPageDraft(2, "prepared second page"),
            ),
        )
        selected = store.stage_selector(
            set_id=store.resolve(whole.ref_id).set_id, page_number=1
        )
        project.db.commit()
        project.apply_edits([{"row_id": row, "column_id": output, "value": selected}])

        source = resolve_cell_blob_source(
            project, sheet_id=sheet, row_id=row, column_id=document
        )
        assert source is not None
        assert planning._has_current_result(project, source, "native_pdf")

        async def forbidden_extract(_path):
            raise AssertionError("current prepared text must bypass PDF extraction")

        monkeypatch.setattr(planning, "sample_pdf_text", forbidden_extract)
        assert (
            planning.build_plan(
                project,
                sheet_id=sheet,
                row_ids=[row],
                force_ocr=False,
                requirements=(),
                requirement_dict=lambda *_args, **_kwargs: {},
            )["items"]
            == []
        )

        project.apply_edits(
            [
                {
                    "row_id": row,
                    "column_id": document,
                    "value": {
                        "blob": digest,
                        "mime": "application/pdf",
                        "page": 2,
                    },
                }
            ]
        )
        source = resolve_cell_blob_source(
            project, sheet_id=sheet, row_id=row, column_id=document
        )
        assert source is not None
        assert not planning._has_current_result(project, source, "native_pdf")

        async def native_page(_path, *, page, **_kwargs):
            assert page == 2
            return PdfTextSample("native second page\ncontinued", 2, 2, False)

        monkeypatch.setattr(planning, "sample_pdf_text", native_page)
        text, count, origin, truncated = planning.sample_pdf(
            project,
            sheet,
            {
                "row_id": row,
                "column_id": document,
                "blob_hash": digest,
                "page_number": 2,
                "page_count": 2,
            },
            2,
        )
        assert (text, count, origin) == (
            "native second page\ncontinued",
            2,
            "native",
        )
        assert truncated is False
    finally:
        project.close()


def test_page_selected_pdf_preview_cannot_switch_to_another_page(
    tmp_path, monkeypatch
) -> None:
    project = Project.create(tmp_path / "selected-preview.frisket")
    try:
        sheet = project.add_sheet("Documents")
        document = project.add_column(sheet, "document", type="file")
        digest = project.add_blob(b"pdf", mime="application/pdf")
        row = project.add_rows(
            sheet,
            [{"document": {"blob": digest, "mime": "application/pdf", "page": 1}}],
            {"document": document},
        )[0]

        async def must_not_extract(_path):
            raise AssertionError(
                "an out-of-selector preview must fail before extraction"
            )

        monkeypatch.setattr(planning, "sample_pdf_text", must_not_extract)
        with pytest.raises(RouteError, match="selected PDF page") as error:
            planning.sample_pdf(
                project,
                sheet,
                {
                    "row_id": row,
                    "column_id": document,
                    "blob_hash": digest,
                    "page_number": 1,
                    "page_count": 3,
                },
                3,
            )
        assert error.value.status_code == 422
    finally:
        project.close()


def test_build_plan_is_async_safe_caches_pdf_inspection_and_rejects_bad_selector(
    tmp_path, monkeypatch
) -> None:
    project = Project.create(tmp_path / "pdf-plan.frisket")
    try:
        sheet = project.add_sheet("Documents")
        document = project.add_column(sheet, "document", type="file")
        digest = project.add_blob(b"pdf", mime="application/pdf")
        rows = project.add_rows(
            sheet,
            [
                {"document": {"blob": digest, "mime": "application/pdf", "page": 1}},
                {"document": {"blob": digest, "mime": "application/pdf", "page": 2}},
            ],
            {"document": document},
        )
        calls = []

        async def native_pages(_path, *, page, **_kwargs):
            calls.append(page)
            if page > 2:
                raise PdfTextError("PDF page is outside the document")
            return PdfTextSample("first" if page == 1 else "second", page, 2, False)

        monkeypatch.setattr(planning, "sample_pdf_text", native_pages)

        async def plan_on_route_loop():
            return planning.build_plan(
                project,
                sheet_id=sheet,
                row_ids=rows,
                force_ocr=False,
                requirements=(),
                requirement_dict=lambda *_args, **_kwargs: {},
            )

        plan = asyncio.run(plan_on_route_loop())
        assert [item["kind"] for item in plan["items"]] == [
            "native_pdf",
            "native_pdf",
        ]
        assert calls == [1, 2]

        project.apply_edits(
            [
                {
                    "row_id": rows[1],
                    "column_id": document,
                    "value": {
                        "blob": digest,
                        "mime": "application/pdf",
                        "page": 3,
                    },
                }
            ]
        )
        with pytest.raises(RouteError, match="preview could not be read") as error:
            asyncio.run(plan_on_route_loop())
        assert error.value.status_code == 422
    finally:
        project.close()


def test_imported_pdf_page_image_is_not_planned_beside_its_pdf_page(
    tmp_path, monkeypatch
) -> None:
    project = Project.create(tmp_path / "pdf-page-image.frisket")
    try:
        sheet = project.add_sheet("Imported PDF")
        document = project.add_column(sheet, "source", type="file")
        page_image = project.add_column(sheet, "page_image", type="image")
        unrelated_image = project.add_column(sheet, "other_image", type="image")
        pdf_hash = project.add_blob(b"pdf", mime="application/pdf")
        image_hash = project.add_blob(b"png", mime="image/png")
        unrelated_hash = project.add_blob(b"other", mime="image/png")
        row = project.add_rows(
            sheet,
            [
                {
                    "source": {
                        "blob": pdf_hash,
                        "mime": "application/pdf",
                        "page": 1,
                    },
                    "page_image": {"blob": image_hash, "mime": "image/png"},
                    "other_image": {
                        "blob": unrelated_hash,
                        "mime": "image/png",
                    },
                }
            ],
            {
                "source": document,
                "page_image": page_image,
                "other_image": unrelated_image,
            },
        )[0]
        artifact = record_source_artifact(
            project,
            artifact_kind="document",
            media_type="application/pdf",
            blob_hash=pdf_hash,
            page_count=1,
        )
        span = record_source_span(
            project,
            artifact_id=int(artifact["id"]),
            span_kind="page",
            page_start=1,
            page_end=1,
            preview={"blob_hash": image_hash, "mime": "image/png"},
        )
        record_evidence_link(
            project,
            subject_kind="cell_value",
            subject_ref={
                "kind": "source_cell",
                "sheet_id": sheet,
                "row_id": row,
                "column_id": page_image,
            },
            spans=[{"span_id": int(span["id"])}],
            sheet_id=sheet,
            row_id=row,
            column_id=page_image,
            link_role="primary_support",
        )

        async def blank_page(_path, **_kwargs):
            return PdfTextSample("", 1, 1, False)

        monkeypatch.setattr(planning, "sample_pdf_text", blank_page)
        plan = planning.build_plan(
            project,
            sheet_id=sheet,
            row_ids=[row],
            force_ocr=False,
            requirements=(),
            requirement_dict=lambda *_args, **_kwargs: {},
        )
        assert [(item["column_id"], item["kind"]) for item in plan["items"]] == [
            (document, "ocr"),
            (unrelated_image, "ocr"),
        ]
    finally:
        project.close()


def test_transcript_result_requires_exact_current_output_value_ref(tmp_path) -> None:
    project = Project.create(tmp_path / "transcript-current.frisket")
    try:
        seeded = _seed_timestamped_transcript(project)
        sheet = int(seeded["sheet_id"])
        row = int(seeded["row_id"])
        transcript = int(seeded["transcript_column_id"])
        media = next(
            int(column["id"])
            for column in project.columns(sheet)
            if column["type"] == "video"
        )
        source = resolve_cell_blob_source(
            project, sheet_id=sheet, row_id=row, column_id=media
        )
        assert source is not None
        assert planning._has_current_result(project, source, "transcribe")

        project.apply_edits(
            [{"row_id": row, "column_id": transcript, "value": "replacement"}]
        )
        # Preserve the historical marker to prove status alone is insufficient.
        project.db.execute(
            "UPDATE evidence_links SET status='active',stale_reason=NULL,stale_at=NULL "
            "WHERE stable_id=?",
            (seeded["evidence_stable_id"],),
        )
        project.db.commit()

        assert not planning._has_current_result(project, source, "transcribe")
    finally:
        project.close()


def test_pdf_preview_uses_latest_current_prepared_output(tmp_path, monkeypatch) -> None:
    project = Project.create(tmp_path / "replacement-preview.frisket")
    try:
        sheet = project.add_sheet("Documents")
        document = project.add_column(sheet, "document", type="file")
        native_output = project.add_column(sheet, "native text")
        ocr_output = project.add_column(sheet, "OCR text")
        digest = project.add_blob(b"pdf", mime="application/pdf")
        row = project.add_rows(
            sheet,
            [{"document": {"blob": digest, "mime": "application/pdf"}}],
            {"document": document},
        )[0]
        item = {
            "row_id": row,
            "column_id": document,
            "blob_hash": digest,
            "page_number": None,
            "page_count": 1,
        }
        project.db.execute("BEGIN IMMEDIATE")
        op_id = project.append_op("media.read_pdf_text", commit=False)
        artifact = record_source_artifact(
            project,
            artifact_kind="document",
            media_type="application/pdf",
            blob_hash=digest,
            page_count=1,
            source_sheet_id=sheet,
            source_row_id=row,
            source_column_id=document,
        )
        store = PreparedContentStore(project)
        original = store.stage_reference(
            source_artifact_id=int(artifact["id"]),
            producing_op_id=op_id,
            pages=(PreparedPageDraft(1, "garbled native text"),),
        )
        positions = [{"text": "correct OCR", "bbox": [0.1, 0.2, 0.3, 0.4]}]
        replacement = store.stage_reference(
            source_artifact_id=int(artifact["id"]),
            producing_op_id=op_id,
            pages=(PreparedPageDraft(1, "correct OCR", positions=positions),),
        )
        project.db.commit()
        project.apply_edits(
            [
                {"row_id": row, "column_id": native_output, "value": original},
                {"row_id": row, "column_id": ocr_output, "value": replacement},
            ]
        )

        async def forbidden_extract(_path):
            raise AssertionError("the current prepared text should supply the preview")

        monkeypatch.setattr(planning, "sample_pdf_text", forbidden_extract)

        assert planning.sample_pdf(project, sheet, item, 1) == (
            "correct OCR",
            1,
            "prepared",
            False,
        )
        assert store.resolve(replacement.ref_id).pins[0].positions == positions
    finally:
        project.close()


def test_large_pdf_plan_and_preview_use_selected_page_sample(
    tmp_path, monkeypatch
) -> None:
    project = Project.create(tmp_path / "large-pdf-sample.frisket")
    try:
        sheet = project.add_sheet("Documents")
        document = project.add_column(sheet, "document", type="file")
        digest = project.add_blob(b"pdf", mime="application/pdf")
        rows = project.add_rows(
            sheet,
            [{"document": {"blob": digest, "mime": "application/pdf"}}] * 2,
            {"document": document},
        )
        calls = []

        async def sample(_path, *, page, max_chars, **_kwargs):
            calls.append((page, max_chars))
            return PdfTextSample(
                "native sample" if page == 1 else "x" * max_chars,
                page,
                24909,
                page != 1,
            )

        monkeypatch.setattr(planning, "sample_pdf_text", sample)
        plan = planning.build_plan(
            project,
            sheet_id=sheet,
            row_ids=rows,
            force_ocr=False,
            requirements=(),
            requirement_dict=lambda *_args, **_kwargs: {},
        )
        assert calls == [(1, 12000)]
        assert [(item["kind"], item["page_count"]) for item in plan["items"]] == [
            ("native_pdf", 24909),
            ("native_pdf", 24909),
        ]
        assert planning.sample_pdf(project, sheet, plan["items"][0], 24909) == (
            "x" * 12000,
            24909,
            "native",
            True,
        )
        assert calls[-1] == (24909, 12000)

        async def unreadable(_path, **_kwargs):
            raise PdfTextError("PDF text extraction failed")

        monkeypatch.setattr(planning, "sample_pdf_text", unreadable)
        with pytest.raises(RouteError, match="preview could not be read") as error:
            planning.build_plan(
                project,
                sheet_id=sheet,
                row_ids=rows,
                force_ocr=False,
                requirements=(),
                requirement_dict=lambda *_args, **_kwargs: {},
            )
        assert error.value.status_code == 422
    finally:
        project.close()


def test_prepared_pdf_preview_reads_bounded_exact_page_with_nul(
    tmp_path, monkeypatch
) -> None:
    project = Project.create(tmp_path / "large-prepared-preview.frisket")
    try:
        sheet = project.add_sheet("Documents")
        document = project.add_column(sheet, "document", type="file")
        output = project.add_column(sheet, "text")
        digest = project.add_blob(b"pdf", mime="application/pdf")
        row = project.add_rows(
            sheet,
            [{"document": {"blob": digest, "mime": "application/pdf"}}],
            {"document": document},
        )[0]
        project.db.execute("BEGIN IMMEDIATE")
        op_id = project.append_op("media.read_pdf_text", commit=False)
        artifact = record_source_artifact(
            project,
            artifact_kind="document",
            media_type="application/pdf",
            blob_hash=digest,
            page_count=2,
            source_sheet_id=sheet,
            source_row_id=row,
            source_column_id=document,
        )
        body = "prefix\x00" + "é" * 1_000_000
        reference = PreparedContentStore(project).stage_reference(
            source_artifact_id=int(artifact["id"]),
            producing_op_id=op_id,
            pages=(
                PreparedPageDraft(1, "other page" * 100000),
                PreparedPageDraft(2, body),
            ),
        )
        project.db.commit()
        project.apply_edits([{"row_id": row, "column_id": output, "value": reference}])

        def forbidden_resolve(*_args, **_kwargs):
            raise AssertionError(
                "preview must not hydrate the complete prepared document"
            )

        monkeypatch.setattr(PreparedContentStore, "resolve", forbidden_resolve)
        item = {
            "row_id": row,
            "column_id": document,
            "blob_hash": digest,
            "page_number": None,
            "page_count": 2,
        }
        assert planning.sample_pdf(project, sheet, item, 2) == (
            body[:12000],
            2,
            "prepared",
            True,
        )
    finally:
        project.close()
