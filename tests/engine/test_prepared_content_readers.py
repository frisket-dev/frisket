from __future__ import annotations

import json
from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.current_cells import refresh_current_cell_pairs
from frisket.engine.store.evidence import record_source_artifact
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.value_codec import encode_stored_value
from frisket.querysets import resolve_sheet_filter_rows
from frisket.search import drain_index, search_project
from frisket.server.exports.sheet_csv import render_sheet_csv
from frisket.server.services.project_qa_sources import (
    find_source_text,
    read_source_text,
)


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
            PreparedPageDraft(
                page_number=1,
                text="alpha boundary-left",
                positions={"tokens": [{"text": "alpha"}]},
            ),
            PreparedPageDraft(
                page_number=2,
                text="boundary-right omega",
                positions={"tokens": [{"text": "omega"}]},
            ),
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


def test_ask_source_reads_pin_exact_ref_and_reject_same_text_ref_change(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "prepared-ask.frisket")
    try:
        sheet_id = project.add_sheet("Documents")
        body_id = project.add_column(sheet_id, "body", type="text")
        document_ref, _page_ref = _stage_document_and_page(project)
        row_id = project.add_rows(
            sheet_id,
            [{"body": document_ref}],
            {"body": body_id},
            commit=False,
        )[0]
        project.db.commit()

        source = read_source_text(project, (sheet_id, row_id, body_id), limit=10)
        assert source["text"] == "alpha boun"
        assert source["value_ref"]["prepared_ref_id"] == document_ref.ref_id
        assert source["version"]["prepared_ref_id"] == document_ref.ref_id
        found = find_source_text(
            project,
            (sheet_id, row_id, body_id),
            "boundary-left\n\nboundary-right",
        )
        assert found["matches"][0]["start"] == len("alpha ")

        project.db.execute("BEGIN IMMEDIATE")
        replacement_op_id = project.append_op(
            "prepare.test", {}, label="replace test PDF text", commit=False
        )
        replacement_ref = PreparedContentStore(project).stage_replacement(
            base_ref_id=document_ref.ref_id,
            producing_op_id=replacement_op_id,
            replacements=(
                PreparedPageDraft(page_number=1, text="alpha boundary-left"),
            ),
        )
        project.db.execute(
            "UPDATE cells SET value=? WHERE row_id=? AND column_id=?",
            (replacement_ref.ref_id, row_id, body_id),
        )
        refresh_current_cell_pairs(project.db, {(row_id, body_id)})
        project.db.commit()

        replacement = read_source_text(project, (sheet_id, row_id, body_id), limit=10)
        assert replacement["text"] == source["text"]
        assert replacement["version"] == {
            **source["version"],
            "prepared_ref_id": replacement_ref.ref_id,
        }
        with pytest.raises(ValueError, match="source_changed"):
            read_source_text(
                project,
                (sheet_id, row_id, body_id),
                cursor=source["next_cursor"],
            )
    finally:
        project.close()


def test_search_and_csv_export_use_full_prepared_document(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "prepared-search-export.frisket")
    try:
        sheet_id = project.add_sheet("Documents")
        body_id = project.add_column(sheet_id, "body", type="text")
        document_ref, _page_ref = _stage_document_and_page(project)
        row_id = project.add_rows(
            sheet_id,
            [{"body": document_ref}],
            {"body": body_id},
            commit=False,
        )[0]
        project.db.commit()

        drain_index(project)
        assert search_project(project, "omega", rerank="off")[0]["row_id"] == row_id
        assert (
            search_project(project, '"boundary left boundary right"', rerank="off")[0][
                "row_id"
            ]
            == row_id
        )

        _metadata, csv_text = render_sheet_csv(project, sheet_id)
        assert '"alpha boundary-left\n\nboundary-right omega"' in csv_text
    finally:
        project.close()


def test_page_edit_stages_replacement_but_document_edit_stays_flat(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "prepared-edits.frisket")
    try:
        sheet_id = project.add_sheet("Documents")
        body_id = project.add_column(sheet_id, "body", type="text")
        document_ref, page_ref = _stage_document_and_page(project)
        document_row, page_row = project.add_rows(
            sheet_id,
            [{"body": document_ref}, {"body": page_ref}],
            {"body": body_id},
            commit=False,
        )
        project.db.commit()
        store = PreparedContentStore(project)
        original_document = store.resolve(document_ref.ref_id)

        page_edit_op = project.apply_edits(
            [
                {
                    "row_id": page_row,
                    "column_id": body_id,
                    "value": "corrected second page",
                }
            ]
        )
        values, refs = project.get_values_with_refs(
            sheet_id, body_id, row_ids=[page_row]
        )
        edited_ref_id = refs[page_row]["prepared_ref_id"]
        edited = store.resolve(edited_ref_id)
        assert values[page_row] == "corrected second page"
        assert edited.page_number == 2
        assert original_document.pins[1].positions is not None
        assert edited.pins[0].positions is None
        assert edited.ref_id != page_ref.ref_id
        assert (
            project.db.execute(
                "SELECT producing_op_id FROM prepared_content_sets WHERE id=?",
                (edited.set_id,),
            ).fetchone()[0]
            == page_edit_op
        )
        assert tuple(
            project.db.execute(
                "SELECT value_kind,value FROM edits "
                "WHERE op_id=? AND row_id=? AND column_id=?",
                (page_edit_op, page_row, body_id),
            ).fetchone()
        ) == ("prepared_content_ref", edited_ref_id)
        original_versions = {
            pin.page_number: pin.version_id for pin in original_document.pins
        }
        edited_versions = {
            int(row["page_number"]): int(row["version_id"])
            for row in project.db.execute(
                "SELECT page_number,version_id FROM prepared_content_set_pages "
                "WHERE set_id=? ORDER BY page_number",
                (edited.set_id,),
            )
        }
        assert edited_versions[1] == original_versions[1]
        assert edited_versions[2] != original_versions[2]

        assert project.undo() == page_edit_op
        old_values, old_refs = project.get_values_with_refs(
            sheet_id, body_id, row_ids=[page_row]
        )
        assert old_values[page_row] == "boundary-right omega"
        assert old_refs[page_row]["prepared_ref_id"] == page_ref.ref_id
        assert project.redo() == page_edit_op
        redone_values, redone_refs = project.get_values_with_refs(
            sheet_id, body_id, row_ids=[page_row]
        )
        assert redone_values[page_row] == "corrected second page"
        assert redone_refs[page_row]["prepared_ref_id"] == edited_ref_id

        document_edit_op = project.apply_edits(
            [
                {
                    "row_id": document_row,
                    "column_id": body_id,
                    "value": "flat derived markdown",
                }
            ]
        )
        document_values, document_refs = project.get_values_with_refs(
            sheet_id, body_id, row_ids=[document_row]
        )
        assert document_values[document_row] == "flat derived markdown"
        assert "prepared_ref_id" not in document_refs[document_row]
        stored = project.db.execute(
            "SELECT value_kind,value FROM edits "
            "WHERE op_id=? AND row_id=? AND column_id=?",
            (document_edit_op, document_row, body_id),
        ).fetchone()
        assert tuple(stored) == ("text", "flat derived markdown")
        assert project.undo() == document_edit_op
        assert project.get_values(sheet_id, body_id, [document_row]) == {
            document_row: "alpha boundary-left\n\nboundary-right omega"
        }
        assert project.redo() == document_edit_op
        assert project.get_values(sheet_id, body_id, [document_row]) == {
            document_row: "flat derived markdown"
        }
    finally:
        project.close()


def test_page_edit_after_source_deletion_falls_back_to_flat_text(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "prepared-deleted-source-edit.frisket")
    try:
        sheet_id = project.add_sheet("Documents")
        body_id = project.add_column(sheet_id, "body", type="text")
        _document_ref, page_ref = _stage_document_and_page(project)
        page_row = project.add_rows(
            sheet_id,
            [{"body": page_ref}],
            {"body": body_id},
            commit=False,
        )[0]
        project.db.commit()
        source_artifact_id = (
            PreparedContentStore(project).resolve(page_ref.ref_id).source_artifact_id
        )
        project.db.execute(
            "DELETE FROM source_artifacts WHERE id=?", (source_artifact_id,)
        )
        project.db.commit()

        op_id = project.apply_edits(
            [
                {
                    "row_id": page_row,
                    "column_id": body_id,
                    "value": "surviving flat correction",
                }
            ]
        )

        assert project.get_values(sheet_id, body_id, [page_row]) == {
            page_row: "surviving flat correction"
        }
        stored = project.db.execute(
            "SELECT value_kind,value FROM edits "
            "WHERE op_id=? AND row_id=? AND column_id=?",
            (op_id, page_row, body_id),
        ).fetchone()
        assert tuple(stored) == ("text", "surviving flat correction")
    finally:
        project.close()
