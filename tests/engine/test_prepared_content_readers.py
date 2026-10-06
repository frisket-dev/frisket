from __future__ import annotations

import json
from pathlib import Path

from frisket.engine.store import Project
from frisket.engine.store.evidence import record_source_artifact
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.value_codec import encode_stored_value
from frisket.querysets import resolve_sheet_filter_rows


def _stage_document_and_page(
    project: Project,
) -> tuple[object, object]:
    project.db.execute("BEGIN IMMEDIATE")
    producing_op_id = project.append_op(
        "prepare.test", {}, label="prepare test PDF", commit=False
    )
    artifact = record_source_artifact(
        project,
        artifact_kind="document",
        media_type="application/pdf",
        page_count=2,
    )
    store = PreparedContentStore(project)
    document_ref = store.stage_reference(
        source_artifact_id=int(artifact["id"]),
        producing_op_id=producing_op_id,
        pages=(
            PreparedPageDraft(page_number=1, text="alpha boundary-left"),
            PreparedPageDraft(page_number=2, text="boundary-right omega"),
        ),
    )
    resolved = store.resolve(document_ref.ref_id)
    page_ref = store.stage_selector(set_id=resolved.set_id, page_number=2)
    return document_ref, page_ref


def test_current_reads_and_sql_filter_resolve_prepared_content(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "prepared-current.frisket")
    try:
        sheet_id = project.add_sheet("Documents")
        body_id = project.add_column(sheet_id, "body", type="text")
        count_id = project.add_column(sheet_id, "count", type="integer")
        document_ref, page_ref = _stage_document_and_page(project)
        row_ids = project.add_rows(
            sheet_id,
            [
                {"body": document_ref, "count": 7},
                {"body": page_ref, "count": 8},
            ],
            {"body": body_id, "count": count_id},
            commit=False,
        )
        project.db.commit()

        values, refs = project.get_values_with_refs(sheet_id, body_id, row_ids)
        assert values == {
            row_ids[0]: "alpha boundary-left\n\nboundary-right omega",
            row_ids[1]: "boundary-right omega",
        }
        assert refs[row_ids[0]]["prepared_ref_id"] == document_ref.ref_id
        assert refs[row_ids[1]]["prepared_ref_id"] == page_ref.ref_id

        candidate_values, candidate_refs = project.get_values_with_refs(
            sheet_id, body_id, row_ids, apply_edits=False
        )
        assert candidate_values == values
        assert candidate_refs[row_ids[0]]["prepared_ref_id"] == document_ref.ref_id
        assert candidate_refs[row_ids[1]]["prepared_ref_id"] == page_ref.ref_id

        crossing = resolve_sheet_filter_rows(
            project,
            sheet_id,
            filter_=json.dumps(
                {"body": {"contains": "boundary-left\n\nboundary-right"}}
            ),
        )
        assert crossing.row_ids == [row_ids[0]]

        assert project.get_values(sheet_id, count_id, row_ids) == {
            row_ids[0]: 7,
            row_ids[1]: 8,
        }
        scalar = project.db.execute(
            "SELECT value_kind,value,prepared_ref_id FROM current_cell_values "
            "WHERE column_id=? AND row_id=?",
            (count_id, row_ids[0]),
        ).fetchone()
        assert tuple(scalar) == ("integer", 7, None)
    finally:
        project.close()


def test_historical_result_readers_resolve_prepared_content(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "prepared-history.frisket")
    try:
        sheet_id = project.add_sheet("Documents")
        body_id = project.add_column(sheet_id, "body", type="text", ai_generated=True)
        document_ref, _page_ref = _stage_document_and_page(project)
        row_id = project.add_rows(sheet_id, [{}], {}, commit=False)[0]
        run_op_id = project.append_op("map.test", {}, label="map test", commit=False)
        run_id = int(
            project.db.execute(
                "INSERT INTO runs (op_id,sheet_id,action_kind) VALUES (?,?,?)",
                (run_op_id, sheet_id, "map.test"),
            ).lastrowid
        )
        project.db.execute(
            "INSERT INTO run_output_generations "
            "(run_id,column_id,output_role,compatibility_key,write_mode,state,"
            "claim_token) VALUES (?,?,?,'prepared-reader-test','create','active',?)",
            (run_id, body_id, "body", "claim"),
        )
        value_kind, stored_value = encode_stored_value(document_ref)
        project.db.execute(
            "INSERT INTO results "
            "(run_id,row_id,column_id,value_kind,value,outcome,publication_effect) "
            "VALUES (?,?,?,?,?,'ok','publish_value')",
            (run_id, row_id, body_id, value_kind, stored_value),
        )
        project.db.execute(
            "INSERT INTO cell_result_heads (column_id,row_id,run_id) VALUES (?,?,?)",
            (body_id, row_id, run_id),
        )
        project.db.commit()

        decoded = RunResultStore(project).decoded_result_rows(
            [(run_id, row_id, body_id)]
        )[(run_id, row_id, body_id)]
        assert decoded["value"] == "alpha boundary-left\n\nboundary-right omega"
        assert decoded["value_kind"] == "text"
        assert decoded["prepared_ref_id"] == document_ref.ref_id

        head = ResultGenerationStore(project).read_cell_heads(
            body_id, row_ids=[row_id]
        )[row_id]
        assert head.value == "alpha boundary-left\n\nboundary-right omega"
        assert head.prepared_ref_id == document_ref.ref_id
    finally:
        project.close()
