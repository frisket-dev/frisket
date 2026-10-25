from __future__ import annotations

from pathlib import Path
import json

import pytest

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.store.media_blobs import media_cell
from frisket.server.services.project_evidence import (
    ProjectEvidenceRouteError,
    ProjectEvidenceService,
)
from frisket.server.services.row_source_documents import resolve_row_source_documents


class _Workspace:
    def __init__(self, project: Project):
        self.project = project

    def get(self, project_id: str) -> Project:
        assert project_id == "project"
        return self.project


def _document_row(project: Project, name: str, filename: str) -> tuple[int, int, int]:
    sheet_id = project.add_sheet(name)
    column_id = project.add_column(sheet_id, "document", type="file")
    digest = project.add_blob(
        f"%PDF-1.4 {filename}".encode(),
        filename=filename,
        mime="application/pdf",
    )
    [row_id] = project.add_rows(
        sheet_id,
        [{"document": media_cell(digest, mime="application/pdf", filename=filename)}],
        {"document": column_id},
    )
    return sheet_id, row_id, column_id


def _child_row(
    project: Project,
    name: str,
    parent_sheet_id: int,
    parent_row_id: int,
) -> tuple[int, int]:
    sheet_id = project.add_sheet(name, parent_sheet_id=parent_sheet_id)
    column_id = project.add_column(sheet_id, "value")
    [row_id] = project.add_rows(
        sheet_id,
        [{"value": name}],
        {"value": column_id},
        parent_row_ids=[parent_row_id],
    )
    return sheet_id, row_id


def _join_row(
    project: Project,
    left: tuple[int, int],
    right: tuple[int, int],
) -> tuple[int, int]:
    op_id = project.append_op("derive.join", {"action_id": "derive.join"})
    sheet_id = project.add_sheet("Joined", parent_sheet_id=left[0], parent_op_id=op_id)
    column_id = project.add_column(sheet_id, "joined")
    [row_id] = project.add_rows(
        sheet_id,
        [{"joined": "result"}],
        {"joined": column_id},
        parent_row_ids=[left[1]],
    )
    project.db.executemany(
        "INSERT INTO materialized_row_sources "
        "(materialized_row_id,source_row_id,source_sheet_id,op_id,role) "
        "VALUES (?,?,?,?,?)",
        [
            (row_id, left[1], left[0], op_id, "join_left"),
            (row_id, right[1], right[0], op_id, "join_right"),
        ],
    )
    project.db.commit()
    return sheet_id, row_id


def _row_evidence(project: Project, sheet_id: int, row_id: int) -> str:
    artifact = record_source_artifact(
        project,
        artifact_kind="file",
        media_type="application/pdf",
        title="Left source",
    )
    span = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="text",
        quote="supported quote",
        snippet="supported quote",
    )
    link = record_evidence_link(
        project,
        subject_kind="row",
        subject_ref={
            "kind": "materialized_row",
            "sheet_id": sheet_id,
            "row_id": row_id,
        },
        spans=[{"span_id": span["id"], "span_role": "support", "required": True}],
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=None,
        link_role="primary_support",
    )
    return str(link["stable_id"])


def test_join_row_sources_find_every_original_document_and_existing_evidence(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "project.frisket")
    try:
        left_document = _document_row(project, "Left documents", "left.pdf")
        right_document = _document_row(project, "Right documents", "right.pdf")
        left_expanded = _child_row(
            project, "Left rows", left_document[0], left_document[1]
        )
        right_expanded = _child_row(
            project, "Right rows", right_document[0], right_document[1]
        )
        evidence_id = _row_evidence(project, *left_expanded)
        _join_sheet, join_row = _join_row(project, left_expanded, right_expanded)

        payload = ProjectEvidenceService(_Workspace(project)).row_source_documents(  # type: ignore[arg-type]
            "project", row_id=join_row
        )

        assert payload["schema_version"] == "frisket.row_source_documents.v1"
        assert payload["ancestry_found"] is True
        assert payload["unavailable_source_count"] == 0
        assert payload["truncated"] is False
        assert [link["stable_id"] for link in payload["evidence_links"]] == [
            evidence_id
        ]
        assert {
            (document["sheet_name"], document["column_name"], document["row_id"])
            for document in payload["documents"]
        } == {
            ("Left documents", "document", left_document[1]),
            ("Right documents", "document", right_document[1]),
        }
    finally:
        project.close()


def test_parent_fallback_reports_unavailable_ancestor_separately_from_no_ancestry(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "project.frisket")
    try:
        source = _document_row(project, "Documents", "source.pdf")
        _child_sheet, child_row = _child_row(project, "Rows", source[0], source[1])
        project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (source[1],))
        project.db.commit()
        service = ProjectEvidenceService(_Workspace(project))  # type: ignore[arg-type]

        unavailable = service.row_source_documents("project", row_id=child_row)
        assert unavailable["ancestry_found"] is True
        assert unavailable["unavailable_source_count"] == 1
        assert unavailable["documents"] == []

        root_sheet = project.add_sheet("Ordinary")
        root_column = project.add_column(root_sheet, "value")
        [root_row] = project.add_rows(
            root_sheet, [{"value": "root"}], {"value": root_column}
        )
        no_ancestry = service.row_source_documents("project", row_id=root_row)
        assert no_ancestry["ancestry_found"] is False
        assert no_ancestry["unavailable_source_count"] == 0
    finally:
        project.close()


def test_source_resolution_is_cycle_safe_and_rejects_a_hidden_requested_row(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "project.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        column_id = project.add_column(sheet_id, "value")
        first, second = project.add_rows(
            sheet_id,
            [{"value": "first"}, {"value": "second"}],
            {"value": column_id},
        )
        project.db.execute(
            "UPDATE rows SET parent_row_id=? WHERE id=?", (second, first)
        )
        project.db.execute(
            "UPDATE rows SET parent_row_id=? WHERE id=?", (first, second)
        )
        project.db.commit()
        service = ProjectEvidenceService(_Workspace(project))  # type: ignore[arg-type]

        cycle = service.row_source_documents("project", row_id=first)
        assert cycle["ancestry_found"] is True
        assert cycle["documents"] == []
        assert cycle["truncated"] is False

        project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (first,))
        project.db.commit()
        with pytest.raises(ProjectEvidenceRouteError) as exc_info:
            service.row_source_documents("project", row_id=first)
        assert exc_info.value.status_code == 404
    finally:
        project.close()


def test_carried_file_copies_collapse_to_the_deepest_original_locator(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "project.frisket")
    try:
        source = _document_row(project, "Originals", "same.pdf")
        value = project.get_values(source[0], source[2], [source[1]])[source[1]]

        carried_rows: list[tuple[int, int]] = []
        for name in ("Left carried", "Right carried"):
            sheet_id = project.add_sheet(name, parent_sheet_id=source[0])
            column_id = project.add_column(sheet_id, "document", type="file")
            [row_id] = project.add_rows(
                sheet_id,
                [{"document": value}],
                {"document": column_id},
                parent_row_ids=[source[1]],
            )
            carried_rows.append((sheet_id, row_id))
        _join_sheet, join_row = _join_row(project, carried_rows[0], carried_rows[1])

        service = ProjectEvidenceService(_Workspace(project))  # type: ignore[arg-type]
        payload = service.row_source_documents("project", row_id=join_row)
        assert len(payload["documents"]) == 1
        assert payload["documents"][0] == {
            "sheet_id": source[0],
            "sheet_name": "Originals",
            "row_id": source[1],
            "row_position": 1,
            "column_id": source[2],
            "column_name": "document",
            "column_type": "file",
            "filename": "same.pdf",
            "page": None,
        }

    finally:
        project.close()


def test_refreshed_ancestor_marks_downstream_partial_membership_stale(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "project.frisket")
    try:
        left_document = _document_row(project, "Left documents", "left.pdf")
        right_document = _document_row(project, "Right documents", "right.pdf")
        left = _child_row(project, "Left rows", left_document[0], left_document[1])
        right = _child_row(project, "Right rows", right_document[0], right_document[1])
        join_sheet, join_row = _join_row(project, left, right)

        refresh_op = project.append_op("sheet.refresh", {"sheet_id": left[0]})
        project.db.execute(
            "UPDATE sheets SET last_verified_op_cursor=? WHERE id=?",
            (refresh_op, left[0]),
        )
        # This is the real FK-cascade outcome when a non-fast-pointer join side
        # is refreshed: that side's membership disappears while the other side
        # survives. The endpoint must not present the survivor as exhaustive.
        project.db.execute(
            "DELETE FROM materialized_row_sources "
            "WHERE materialized_row_id=? AND source_row_id=?",
            (join_row, left[1]),
        )
        project.db.commit()

        payload = ProjectEvidenceService(_Workspace(project)).row_source_documents(  # type: ignore[arg-type]
            "project", row_id=join_row
        )
        assert payload["sheet_id"] == join_sheet
        assert payload["lineage_stale"] is True
        assert [document["sheet_name"] for document in payload["documents"]] == [
            "Right documents"
        ]
        assert payload["evidence_links"] == []
    finally:
        project.close()


def test_real_right_side_refresh_marks_join_lineage_incomplete(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "project.frisket")
    try:
        source_sheet = project.add_sheet("Right source")
        source_column = project.add_column(source_sheet, "items", type="json")
        project.add_rows(
            source_sheet,
            [{"items": [{"key": "match"}]}],
            {"items": source_column},
        )
        left_sheet = project.add_sheet("Left")
        left_column = project.add_column(left_sheet, "key")
        project.add_rows(left_sheet, [{"key": "match"}], {"key": left_column})
        project.db.commit()

        derived = run_action_spec(
            project,
            {
                "action_id": "derive.table_from_list",
                "scope": {"kind": "project"},
                "sheet_name": "Right expanded",
                "params": {
                    "source": {
                        "kind": "column",
                        "sheet_id": source_sheet,
                        "column_id": source_column,
                    }
                },
                "idempotency_key": "right-expanded",
            },
            project_id="project",
        )
        assert derived.status == "completed", derived.errors
        right_sheet = derived.outputs[0].sheet_id

        joined = run_action_spec(
            project,
            {
                "action_id": "derive.join",
                "scope": {"kind": "sheet_rows", "sheet_id": left_sheet},
                "sheet_name": "Joined",
                "params": {
                    "right": {"sheet_id": right_sheet},
                    "join_keys": [{"left_column": "key", "right_column": "key"}],
                    "how": "inner",
                },
                "idempotency_key": "joined",
            },
            project_id="project",
        )
        assert joined.status == "completed", joined.errors
        join_sheet = joined.outputs[0].sheet_id
        join_row = project.visible_row_ids(join_sheet)[0]
        assert project.db.execute(
            "SELECT COUNT(*) FROM materialized_row_sources "
            "WHERE materialized_row_id=? AND role='join_right'",
            (join_row,),
        ).fetchone()[0]

        refreshed = run_action_spec(
            project,
            {
                "action_id": "sheet.refresh",
                "scope": {"kind": "project"},
                "params": {"sheet_id": right_sheet},
                "idempotency_key": "refresh-right",
            },
            project_id="project",
        )
        assert refreshed.status == "completed", refreshed.errors
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM materialized_row_sources "
                "WHERE materialized_row_id=? AND role='join_right'",
                (join_row,),
            ).fetchone()[0]
            == 0
        )

        payload = ProjectEvidenceService(_Workspace(project)).row_source_documents(  # type: ignore[arg-type]
            "project", row_id=join_row
        )
        assert payload["lineage_stale"] is True
        assert payload["ancestry_found"] is True
    finally:
        project.close()


def test_mismatched_row_subject_is_not_exposed_as_current_evidence(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "project.frisket")
    try:
        source = _document_row(project, "Documents", "source.pdf")
        child = _child_row(project, "Rows", source[0], source[1])
        stable_id = _row_evidence(project, *child)
        project.db.execute(
            "UPDATE evidence_links SET subject_ref_json=? WHERE stable_id=?",
            (
                json.dumps(
                    {"kind": "materialized_row", "sheet_id": child[0], "row_id": 999}
                ),
                stable_id,
            ),
        )
        project.db.commit()

        payload = ProjectEvidenceService(_Workspace(project)).row_source_documents(  # type: ignore[arg-type]
            "project", row_id=child[1]
        )
        assert payload["evidence_links"] == []
        assert payload["documents"]
    finally:
        project.close()


def test_source_walk_reports_when_the_fixed_depth_bound_is_reached(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "project.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        column_id = project.add_column(sheet_id, "value")
        [row_id] = project.add_rows(sheet_id, [{"value": "root"}], {"value": column_id})
        for index in range(18):
            _sheet_id, row_id = _child_row(
                project, f"Derived {index}", sheet_id, row_id
            )
            sheet_id = _sheet_id

        payload = ProjectEvidenceService(_Workspace(project)).row_source_documents(  # type: ignore[arg-type]
            "project", row_id=row_id
        )
        assert payload["ancestry_found"] is True
        assert payload["truncated"] is True
    finally:
        project.close()


def test_source_membership_read_stops_at_the_row_budget_sentinel(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "project.frisket")
    try:
        source_sheet = project.add_sheet("Sources")
        source_column = project.add_column(source_sheet, "value")
        source_rows = project.add_rows(
            source_sheet,
            [{"value": f"source-{index}"} for index in range(300)],
            {"value": source_column},
        )
        derived_sheet = project.add_sheet("Derived")
        derived_column = project.add_column(derived_sheet, "value")
        [derived_row] = project.add_rows(
            derived_sheet, [{"value": "result"}], {"value": derived_column}
        )
        op_id = project.append_op("derive.reduce", {"action_id": "derive.reduce"})
        project.db.executemany(
            "INSERT INTO materialized_row_sources "
            "(materialized_row_id,source_row_id,source_sheet_id,op_id,role) "
            "VALUES (?,?,?,?,?)",
            [
                (derived_row, source_row, source_sheet, op_id, "aggregate_source")
                for source_row in source_rows
            ],
        )
        project.db.commit()
        statements: list[str] = []
        project.db.set_trace_callback(statements.append)
        try:
            payload = resolve_row_source_documents(
                project, row_id=derived_row, project_id="project"
            )
        finally:
            project.db.set_trace_callback(None)

        assert payload["ancestry_found"] is True
        assert payload["truncated"] is True
        assert any(
            "FROM MATERIALIZED_ROW_SOURCES" in statement.upper()
            and "UNION ALL" in statement.upper()
            and "LIMIT 256" in statement.upper()
            for statement in statements
        )
    finally:
        project.close()


def test_row_evidence_summaries_are_capped_and_report_truncation(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "project.frisket")
    try:
        source = _document_row(project, "Documents", "source.pdf")
        child = _child_row(project, "Rows", source[0], source[1])
        artifact = record_source_artifact(
            project,
            artifact_kind="file",
            media_type="application/pdf",
            title="Source",
        )
        span = record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="text",
            quote="supported quote",
        )
        for _index in range(65):
            record_evidence_link(
                project,
                subject_kind="row",
                subject_ref={
                    "kind": "materialized_row",
                    "sheet_id": child[0],
                    "row_id": child[1],
                },
                spans=[{"span_id": span["id"], "span_role": "support"}],
                sheet_id=child[0],
                row_id=child[1],
                column_id=None,
                link_role="primary_support",
            )

        statements: list[str] = []
        project.db.set_trace_callback(statements.append)
        try:
            payload = resolve_row_source_documents(
                project, row_id=child[1], project_id="project"
            )
        finally:
            project.db.set_trace_callback(None)
        assert len(payload["evidence_links"]) == 64
        assert payload["truncated"] is True
        assert payload["documents"]
        assert any(
            "FROM EVIDENCE_LINKS" in statement.upper()
            and "SUBJECT_REF_JSON" in statement.upper()
            and "LIMIT 65" in statement.upper()
            for statement in statements
        )
    finally:
        project.close()
