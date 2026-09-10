from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.features.investigations.rowsets import (
    INVESTIGATIVE_ROWSET_SCHEMA_VERSION,
    InvestigativeRowsetError,
    resolve_investigative_rowset,
)
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results


def _cell_by_name(record: dict[str, Any], name: str) -> dict[str, Any]:
    for cell in record["cells"]:
        if cell["column_name"] == name:
            return cell
    raise AssertionError(f"missing cell {name!r}")


def test_sheet_rowset_preserves_typed_values_refs_and_evidence(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "rowset.frisket", name="Rowset")
    try:
        sheet_id = project.add_sheet("People")
        columns = {
            "name": project.add_column(sheet_id, "name", "text"),
            "age": project.add_column(sheet_id, "age", "integer"),
            "profile": project.add_column(sheet_id, "profile", "json"),
            "risk": project.add_column(sheet_id, "risk", "number", ai_generated=True),
            "internal": project.add_column(sheet_id, "internal", "text"),
        }
        project.db.execute(
            "UPDATE columns SET hidden=1 WHERE id=?", (columns["internal"],)
        )
        project.db.commit()
        row_ids = project.add_rows(
            sheet_id,
            [
                {
                    "name": "Alice",
                    "age": 40,
                    "profile": {"aliases": ["A. Example"]},
                    "internal": "hide me",
                },
                {
                    "name": "Bob",
                    "age": 35,
                    "profile": {"aliases": []},
                    "internal": "hide me too",
                },
            ],
            columns,
        )

        op_id = project.append_op("map.score", {"output_column": "risk"})
        run_id = RunResultStore(project).start_run(
            op_id,
            sheet_id,
            "map.score",
            total_rows=2,
            row_ids=row_ids,
        )
        write_claimed_test_results(
            project,
            run_id,
            [
                {"row_id": row_ids[0], "column_id": columns["risk"], "value": 0.82},
                {"row_id": row_ids[1], "column_id": columns["risk"], "value": 0.24},
            ],
        )
        RunResultStore(project).finish_run(run_id)
        RunResultStore(project).point_column_at_run(op_id, columns["risk"], run_id)
        edit_op_id = project.apply_edits(
            [{"row_id": row_ids[0], "column_id": columns["age"], "value": 41}],
            label="correct age",
        )

        _values, refs = project.get_values_with_refs(
            sheet_id, columns["risk"], row_ids=[row_ids[0]]
        )
        artifact = record_source_artifact(
            project,
            stable_id="source_artifact:fixture-risk",
            artifact_kind="file",
            media_type="application/pdf",
            source_sheet_id=sheet_id,
            source_row_id=row_ids[0],
            source_column_id=columns["name"],
            title="Risk dossier",
        )
        span = record_source_span(
            project,
            stable_id="source_span:fixture-risk-page",
            artifact_id=artifact["id"],
            span_kind="text",
            quote="risk score 0.82",
        )
        evidence = record_evidence_link(
            project,
            stable_id="evidence_link:fixture-risk",
            subject_kind="cell_value",
            subject_ref=refs[row_ids[0]],
            spans=[{"span_id": span["id"]}],
            sheet_id=sheet_id,
            row_id=row_ids[0],
            column_id=columns["risk"],
            run_id=run_id,
            op_id=op_id,
        )

        resolved = resolve_investigative_rowset(
            project, {"kind": "sheet", "sheet_id": sheet_id}, project_id="case-1"
        )
    finally:
        project.close()

    assert resolved["schema_version"] == INVESTIGATIVE_ROWSET_SCHEMA_VERSION
    assert resolved["rowset"] == {"kind": "sheet", "sheet_id": sheet_id}
    assert resolved["sheet"] == {
        "id": sheet_id,
        "name": "People",
        "parent_sheet_id": None,
        "parent_op_id": None,
        "is_materialized": False,
    }
    assert [column["name"] for column in resolved["columns"]] == [
        "name",
        "age",
        "profile",
        "risk",
    ]
    assert resolved["columns"][1]["source_column_ref"] == {
        "kind": "column",
        "sheet_id": sheet_id,
        "column_id": columns["age"],
        "column_name": "age",
    }
    assert [ref["row_id"] for ref in resolved["row_refs"]] == row_ids
    assert len(resolved["records"]) == 2

    first = resolved["records"][0]
    assert first["row_ref"] == {
        "kind": "row",
        "sheet_id": sheet_id,
        "row_id": row_ids[0],
    }
    assert first["source_row_ref"] == first["row_ref"]
    assert first["lineage"] is None
    assert first["values"]["age"] == 41
    assert isinstance(first["values"]["age"], int)
    assert first["values"]["profile"] == {"aliases": ["A. Example"]}
    assert isinstance(first["values"]["profile"], dict)
    assert first["values"]["risk"] == 0.82
    assert isinstance(first["values"]["risk"], float)

    age_cell = _cell_by_name(first, "age")
    assert age_cell["value_ref"] == {
        "kind": "manual_edit",
        "op_id": edit_op_id,
        "row_id": row_ids[0],
        "column_id": columns["age"],
        "run_id": None,
    }
    assert age_cell["source_cell_ref"] == {
        "kind": "cell",
        "sheet_id": sheet_id,
        "row_id": row_ids[0],
        "column_id": columns["age"],
        "column_name": "age",
    }

    risk_cell = _cell_by_name(first, "risk")
    assert risk_cell["value_ref"]["kind"] == "run_result"
    assert risk_cell["value_ref"]["run_id"] == run_id
    assert risk_cell["evidence_refs"] == [
        {
            "id": evidence["id"],
            "stable_id": "evidence_link:fixture-risk",
            "export_ref": "evidence_link:fixture-risk",
            "status": "active",
            "role": "support",
            "evidence_kind": "text",
            "span_count": 1,
            "artifact_count": 1,
            "snippet": "risk score 0.82",
            "viewer_href": (
                "/api/projects/case-1/evidence/links/evidence_link:fixture-risk/viewer"
            ),
        }
    ]
    assert risk_cell["evidence"]["current_value_ref"] == risk_cell["value_ref"]


def test_materialized_sheet_rowset_exposes_parent_sheet_and_row_lineage(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "materialized.frisket", name="Materialized")
    try:
        source_sheet_id = project.add_sheet("Source People")
        source_columns = {
            "name": project.add_column(source_sheet_id, "name", "text"),
        }
        source_rows = project.add_rows(
            source_sheet_id,
            [{"name": "Alice"}, {"name": "Bob"}],
            source_columns,
        )
        derive_op_id = project.append_op("resolve.entities", {"target": "Reviewed"})
        child_sheet_id = project.add_sheet(
            "Reviewed People",
            parent_sheet_id=source_sheet_id,
            parent_op_id=derive_op_id,
        )
        child_columns = {
            "caption": project.add_column(child_sheet_id, "caption", "text"),
            "confidence": project.add_column(child_sheet_id, "confidence", "number"),
        }
        child_rows = project.add_rows(
            child_sheet_id,
            [
                {"caption": "Bob B.", "confidence": 0.91},
                {"caption": "Alice A.", "confidence": 0.88},
            ],
            child_columns,
            parent_row_ids=[source_rows[1], source_rows[0]],
        )

        resolved = resolve_investigative_rowset(
            project, {"kind": "materialized_sheet", "sheet_id": child_sheet_id}
        )
    finally:
        project.close()

    assert resolved["rowset"] == {
        "kind": "materialized_sheet",
        "sheet_id": child_sheet_id,
    }
    assert resolved["sheet"] == {
        "id": child_sheet_id,
        "name": "Reviewed People",
        "parent_sheet_id": source_sheet_id,
        "parent_op_id": derive_op_id,
        "is_materialized": True,
    }
    assert [ref["row_id"] for ref in resolved["row_refs"]] == child_rows
    assert [
        record["lineage"]["parent_row_ref"]["row_id"] for record in resolved["records"]
    ] == [source_rows[1], source_rows[0]]
    assert resolved["records"][0]["source_row_ref"] == {
        "kind": "row",
        "sheet_id": child_sheet_id,
        "row_id": child_rows[0],
    }
    assert resolved["records"][0]["lineage"] == {
        "parent_sheet_ref": {
            "kind": "sheet",
            "sheet_id": source_sheet_id,
        },
        "parent_row_ref": {
            "kind": "row",
            "sheet_id": source_sheet_id,
            "row_id": source_rows[1],
        },
        "parent_op_id": derive_op_id,
    }
    assert _cell_by_name(resolved["records"][0], "confidence")["source_cell_ref"] == {
        "kind": "cell",
        "sheet_id": child_sheet_id,
        "row_id": child_rows[0],
        "column_id": child_columns["confidence"],
        "column_name": "confidence",
    }


@pytest.mark.parametrize("kind", ["union", "virtual", "graph"])
def test_unsupported_rowset_kinds_fail_with_typed_errors(
    tmp_path: Path, kind: str
) -> None:
    project = Project.create(tmp_path / f"{kind}.frisket", name="Unsupported")
    try:
        with pytest.raises(InvestigativeRowsetError) as excinfo:
            resolve_investigative_rowset(project, {"kind": kind, "sheet_id": 1})
    finally:
        project.close()

    assert excinfo.value.code == "unsupported_rowset_kind"
    assert excinfo.value.field == "rowset.kind"
    assert excinfo.value.details == {"kind": kind}


def test_rowset_metadata_drift_fails_with_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.features.investigations import rowsets

    project = Project.create(tmp_path / "drift.frisket", name="Drift")
    try:
        sheet_id = project.add_sheet("Rows")
        columns = {"name": project.add_column(sheet_id, "name", "text")}
        row_ids = project.add_rows(sheet_id, [{"name": "Alice"}], columns)

        def fake_batches(*_args: Any, **_kwargs: Any) -> list[list[int]]:
            return [[row_ids[0], row_ids[0] + 10_000]]

        monkeypatch.setattr(rowsets.export_rowset, "iter_row_id_batches", fake_batches)

        with pytest.raises(InvestigativeRowsetError) as excinfo:
            resolve_investigative_rowset(
                project, {"kind": "sheet", "sheet_id": sheet_id}
            )
    finally:
        project.close()

    assert excinfo.value.code == "row_metadata_missing"
    assert excinfo.value.field == "rowset.rows"
    assert excinfo.value.details == {"row_ids": [row_ids[0] + 10_000]}


def test_rowset_metadata_lookup_batches_without_changing_order(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "chunked.frisket", name="Chunked")
    try:
        sheet_id = project.add_sheet("Rows")
        columns = {"name": project.add_column(sheet_id, "name", "text")}
        row_ids = project.add_rows(
            sheet_id,
            [{"name": "Alice"}, {"name": "Bob"}, {"name": "Carla"}],
            columns,
        )

        resolved = resolve_investigative_rowset(
            project,
            {"kind": "sheet", "sheet_id": sheet_id},
            batch_size=1,
        )
    finally:
        project.close()

    assert [ref["row_id"] for ref in resolved["row_refs"]] == row_ids
    assert [record["values"]["name"] for record in resolved["records"]] == [
        "Alice",
        "Bob",
        "Carla",
    ]


def test_missing_value_ref_fails_with_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "missing-ref.frisket", name="Missing Ref")
    try:
        sheet_id = project.add_sheet("Rows")
        columns = {"name": project.add_column(sheet_id, "name", "text")}
        row_id = project.add_rows(sheet_id, [{"name": "Alice"}], columns)[0]
        original = project.get_values_with_refs

        def fake_values_with_refs(
            sheet_id_arg: int,
            column_id_arg: int,
            row_ids: list[int] | None = None,
        ) -> tuple[dict[int, Any], dict[int, dict[str, Any]]]:
            values, refs = original(sheet_id_arg, column_id_arg, row_ids=row_ids)
            if column_id_arg == columns["name"]:
                refs.pop(row_id, None)
            return values, refs

        monkeypatch.setattr(project, "get_values_with_refs", fake_values_with_refs)

        with pytest.raises(InvestigativeRowsetError) as excinfo:
            resolve_investigative_rowset(
                project, {"kind": "sheet", "sheet_id": sheet_id}
            )
    finally:
        project.close()

    assert excinfo.value.code == "row_value_ref_missing"
    assert excinfo.value.field == "rowset.cells"
    assert excinfo.value.details == {
        "row_id": row_id,
        "column_id": columns["name"],
    }


def test_invalid_batch_size_fails_with_typed_error(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "batch.frisket", name="Batch")
    try:
        sheet_id = project.add_sheet("Rows")
        with pytest.raises(InvestigativeRowsetError) as excinfo:
            resolve_investigative_rowset(
                project, {"kind": "sheet", "sheet_id": sheet_id}, batch_size=0
            )
    finally:
        project.close()

    assert excinfo.value.code == "invalid_rowset_spec"
    assert excinfo.value.field == "batch_size"
