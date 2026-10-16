from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from frisket.engine.store import Project
from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.retrieval.source_adapter import resolve_cell_blob_source
from frisket.server.route_errors import RouteError
from frisket.server.services import search_preparation_planning as planning
from tests.engine.test_transcript_segments_action import _seed_timestamped_transcript


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

        monkeypatch.setattr(planning, "extract_pdf_text", forbidden_extract)
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

        async def native_page(_path):
            return [
                SimpleNamespace(
                    page=2,
                    tokens=(SimpleNamespace(text="native second page"),),
                )
            ]

        monkeypatch.setattr(planning, "extract_pdf_text", native_page)
        text, count, origin = planning.sample_pdf(
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
        assert (text, count, origin) == ("native second page", 2, "native")
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

        monkeypatch.setattr(planning, "extract_pdf_text", must_not_extract)
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

        async def native_pages(_path, **_kwargs):
            calls.append(True)
            return [
                SimpleNamespace(page=1, tokens=(SimpleNamespace(text="first"),)),
                SimpleNamespace(page=2, tokens=(SimpleNamespace(text="second"),)),
            ]

        monkeypatch.setattr(planning, "extract_pdf_text", native_pages)

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
        assert calls == [True]

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
        with pytest.raises(RouteError, match="outside the document") as error:
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
            return [SimpleNamespace(page=1, tokens=())]

        monkeypatch.setattr(planning, "extract_pdf_text", blank_page)
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
