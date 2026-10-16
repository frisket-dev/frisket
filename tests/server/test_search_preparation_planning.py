from __future__ import annotations

from types import SimpleNamespace

from frisket.engine.store import Project
from frisket.engine.store.evidence import record_source_artifact
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.retrieval.source_adapter import resolve_cell_blob_source
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
        project.apply_edits(
            [{"row_id": row, "column_id": output, "value": selected}]
        )

        source = resolve_cell_blob_source(
            project, sheet_id=sheet, row_id=row, column_id=document
        )
        assert source is not None
        assert planning._has_current_result(project, source, "native_pdf")

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
            },
            2,
        )
        assert (text, count, origin) == ("native second page", 1, "native")
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
