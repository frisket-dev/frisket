"""derive.table_from_list column source (derivelist lane).

The named-result source can only feed derive.table_from_list from a completed
run whose receipt carries a feedable named-result ref (values read from the
`results` table). The column source instead materializes an existing JSON list
column's *live cells* — any provenance (import, edits, a prior run) — with no
run, results, model, or receipt-ref reconstruction. Child columns are inferred
from the list items; the caller declares neither item_schema nor columns.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from frisket.engine.store import Project
from helpers import run_cli


def _write_spec(tmp_path: Path, name: str, action: dict[str, Any]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(action), encoding="utf-8")
    return path


def _seed_import_action() -> dict[str, Any]:
    return {
        "action_id": "import.rows",
        "scope": {"kind": "project"},
        "sheet_name": "Filings",
        "params": {
            "columns": [
                {"name": "title", "type": "text"},
                {"name": "pdf_tables", "type": "json"},
            ],
            "rows": [
                {
                    "title": "Q1 filing",
                    "pdf_tables": [
                        {"vendor": "Acme", "amount": "1200"},
                        {"vendor": "Globex", "amount": "800"},
                    ],
                },
                {
                    "title": "Q2 filing",
                    "pdf_tables": [
                        {"vendor": "Initech", "amount": "450"},
                    ],
                },
            ],
            "source": {
                "kind": "inline",
                "label": "derive column seed",
                "fingerprint": "sha256:derive-column-seed",
            },
        },
        "idempotency_key": "derive_column_seed@sha256:v1",
    }


def _derive_column_action(
    *,
    sheet_id: int,
    column_id: int,
    target_sheet_name: str = "PDF Rows",
    idempotency_key: str = "derive_pdf_rows@sha256:v1",
    include_columns: list[str] | None = None,
) -> dict[str, Any]:
    source: dict[str, Any] = {
        "kind": "column",
        "sheet_id": sheet_id,
        "column_id": column_id,
    }
    if include_columns is not None:
        source["include_columns"] = include_columns
    return {
        "action_id": "derive.table_from_list",
        "scope": {"kind": "project"},
        "sheet_name": target_sheet_name,
        "params": {
            "source": source,
        },
        "idempotency_key": idempotency_key,
    }


def _seed_pdf_tables_action() -> dict[str, Any]:
    """A pdf_tables-shaped JSON column: 8 metadata keys BEFORE the real columns,
    two table_index groups sharing the same real shape."""

    def _row(
        table_index: int, table_row_index: int, real: dict[str, str]
    ) -> dict[str, Any]:
        return {
            "source_row_id": 1,
            "source_filename": "contracts.pdf",
            "source_blob_hash": "sha256:abc",
            "page_start": 1,
            "page_end": 1,
            "table_index": table_index,
            "table_row_index": table_row_index,
            "raw_cells_json": list(real.values()),
            **real,
        }

    return {
        "action_id": "import.rows",
        "scope": {"kind": "project"},
        "sheet_name": "Filings",
        "params": {
            "columns": [
                {"name": "title", "type": "text"},
                {"name": "pdf_tables", "type": "json"},
            ],
            "rows": [
                {
                    "title": "Filing A",
                    "pdf_tables": [
                        _row(0, 0, {"vendor": "Acme", "amount": "1200"}),
                        _row(0, 1, {"vendor": "Globex", "amount": "800"}),
                        _row(1, 0, {"vendor": "Initech", "amount": "450"}),
                    ],
                },
            ],
            "source": {
                "kind": "inline",
                "label": "pdf tables seed",
                "fingerprint": "sha256:pdf-tables-seed",
            },
        },
        "idempotency_key": "pdf_tables_seed@sha256:v1",
    }


def _counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("runs", "results")
    }


def test_derive_table_from_column_materializes_json_list_cells_without_model(
    tmp_path: Path,
) -> None:
    from frisket.contracts.action import (
        ActionResult,
        Receipt,
    )
    from frisket.actions.system import validate_root_action

    project_path = tmp_path / "derive-column.frisket"
    project = Project.create(project_path, name="Derive Column")
    project.close()

    seed_path = _write_spec(tmp_path, "seed.json", _seed_import_action())
    seed = run_cli(
        "action",
        "run",
        "--project",
        str(project_path),
        "--project-id",
        "project-derive-column",
        str(seed_path),
    )
    assert seed.returncode == 0, seed.stderr

    project = Project(project_path)
    try:
        sheet_id = int(
            project.db.execute("SELECT id FROM sheets WHERE name='Filings'").fetchone()[
                "id"
            ]
        )
        column_id = int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='pdf_tables'",
                (sheet_id,),
            ).fetchone()["id"]
        )
        source_row_ids = [
            int(row["id"])
            for row in project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=? ORDER BY position",
                (sheet_id,),
            ).fetchall()
        ]
        counts_before = _counts(project)
    finally:
        project.close()

    action = _derive_column_action(sheet_id=sheet_id, column_id=column_id)
    validation = validate_root_action(action)
    assert validation.ok is True, validation.model_dump()

    spec_path = _write_spec(tmp_path, "derive_column.json", action)
    run = run_cli(
        "action",
        "run",
        "--project",
        str(project_path),
        "--project-id",
        "project-derive-column",
        str(spec_path),
    )
    assert run.returncode == 0, run.stderr
    result = ActionResult.model_validate(json.loads(run.stdout))
    assert result.status == "completed"
    assert result.action.kind == "derive.table_from_list"
    assert result.receipt_id is not None

    child_sheet_id = next(
        output.sheet_id for output in result.outputs if output.kind == "sheet"
    )
    child_row_ids = next(
        output.row_ids for output in result.outputs if output.kind == "rows"
    )
    # 3 items total across the two source rows -> 3 child rows.
    assert len(child_row_ids) == 3

    project = Project(project_path)
    try:
        child = project.db.execute(
            "SELECT * FROM sheets WHERE id=?", (child_sheet_id,)
        ).fetchone()
        assert child["name"] == "PDF Rows"
        assert child["parent_sheet_id"] == sheet_id

        columns = {
            row["name"]: row
            for row in project.db.execute(
                "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
                (child_sheet_id,),
            ).fetchall()
        }
        assert list(columns) == ["vendor", "amount"]

        rows = project.db.execute(
            "SELECT * FROM rows WHERE sheet_id=? ORDER BY position",
            (child_sheet_id,),
        ).fetchall()
        assert [int(row["parent_row_id"]) for row in rows] == [
            source_row_ids[0],
            source_row_ids[0],
            source_row_ids[1],
        ]
        vendors = project.get_values(child_sheet_id, int(columns["vendor"]["id"]))
        assert list(vendors.values()) == ["Acme", "Globex", "Initech"]

        # No model, no run, no results: the column source never touches the
        # per-row result machinery.
        assert _counts(project) == counts_before

        receipt_row = project.db.execute(
            "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        assert receipt_row["run_id"] is None
        receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
        read = next(
            item.ref for item in receipt.inputs if item.ref["kind"] == "list_table_read"
        )
        input_ref = read["source"]
        assert input_ref["kind"] == "column"
        assert input_ref["sheet_id"] == sheet_id
        assert input_ref["column_id"] == column_id
        evidence_kinds = {item.ref["kind"] for item in receipt.evidence}
        assert read["source_row_ids"] == source_row_ids
        assert read["item_count"] == 3
        assert "lineage_parent_rows" in evidence_kinds
    finally:
        project.close()


def test_derive_column_source_rejects_declared_projection() -> None:
    """The column source infers columns/item_schema; declaring them is an error."""
    from frisket.actions.system import validate_root_action

    action = _derive_column_action(sheet_id=1, column_id=4)
    action["params"]["columns"] = [
        {"name": "vendor", "path": "$.vendor", "type": "text"}
    ]
    validation = validate_root_action(action)
    assert validation.ok is False
    assert validation.error is not None


def _seed_and_ids(tmp_path: Path, project_path: Path) -> tuple[int, int, list[int]]:
    seed_path = _write_spec(tmp_path, "seed.json", _seed_pdf_tables_action())
    seed = run_cli(
        "action",
        "run",
        "--project",
        str(project_path),
        "--project-id",
        "project-pdf-tables",
        str(seed_path),
    )
    assert seed.returncode == 0, seed.stderr
    project = Project(project_path)
    try:
        sheet_id = int(
            project.db.execute("SELECT id FROM sheets WHERE name='Filings'").fetchone()[
                "id"
            ]
        )
        column_id = int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='pdf_tables'",
                (sheet_id,),
            ).fetchone()["id"]
        )
        source_row_ids = [
            int(row["id"])
            for row in project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=? ORDER BY position",
                (sheet_id,),
            ).fetchall()
        ]
    finally:
        project.close()
    return sheet_id, column_id, source_row_ids


def test_derive_column_source_include_columns_projects_only_listed_keys(
    tmp_path: Path,
) -> None:
    """The picker excludes the 8 pdf_tables metadata keys by passing
    include_columns; the child sheet materializes ONLY the listed real columns,
    in the requested order, dropping metadata from the key union."""
    from frisket.contracts.action import ActionResult
    from frisket.actions.system import validate_root_action

    project_path = tmp_path / "pdf-tables.frisket"
    project = Project.create(project_path, name="PDF Tables")
    project.close()
    sheet_id, column_id, source_row_ids = _seed_and_ids(tmp_path, project_path)

    action = _derive_column_action(
        sheet_id=sheet_id,
        column_id=column_id,
        target_sheet_name="Bid tables",
        include_columns=["vendor", "amount"],
    )
    validation = validate_root_action(action)
    assert validation.ok is True, validation.model_dump()

    spec_path = _write_spec(tmp_path, "derive.json", action)
    run = run_cli(
        "action",
        "run",
        "--project",
        str(project_path),
        "--project-id",
        "project-pdf-tables",
        str(spec_path),
    )
    assert run.returncode == 0, run.stderr
    result = ActionResult.model_validate(json.loads(run.stdout))
    assert result.status == "completed"

    child_sheet_id = next(
        output.sheet_id for output in result.outputs if output.kind == "sheet"
    )
    project = Project(project_path)
    try:
        columns = [
            row["name"]
            for row in project.db.execute(
                "SELECT name FROM columns WHERE sheet_id=? ORDER BY position",
                (child_sheet_id,),
            ).fetchall()
        ]
        # Metadata keys (source_row_id, table_index, raw_cells_json, …) excluded.
        assert columns == ["vendor", "amount"]

        col_ids = {
            row["name"]: int(row["id"])
            for row in project.db.execute(
                "SELECT id, name FROM columns WHERE sheet_id=?",
                (child_sheet_id,),
            ).fetchall()
        }
        vendors = project.get_values(child_sheet_id, col_ids["vendor"])
        # All three rows across both table_index groups materialize.
        assert list(vendors.values()) == ["Acme", "Globex", "Initech"]

        rows = project.db.execute(
            "SELECT parent_row_id FROM rows WHERE sheet_id=? ORDER BY position",
            (child_sheet_id,),
        ).fetchall()
        assert [int(row["parent_row_id"]) for row in rows] == [
            source_row_ids[0],
            source_row_ids[0],
            source_row_ids[0],
        ]
    finally:
        project.close()


def test_derive_column_source_include_columns_unknown_key_fails_loudly(
    tmp_path: Path,
) -> None:
    """An include_columns entry absent from the list-item union is a loud
    execution error, never a silent all-null column."""
    from frisket.contracts.action import ActionResult

    project_path = tmp_path / "pdf-tables-bad.frisket"
    project = Project.create(project_path, name="PDF Tables Bad")
    project.close()
    sheet_id, column_id, _ = _seed_and_ids(tmp_path, project_path)

    action = _derive_column_action(
        sheet_id=sheet_id,
        column_id=column_id,
        include_columns=["vendor", "not_a_real_column"],
    )
    spec_path = _write_spec(tmp_path, "derive_bad.json", action)
    run = run_cli(
        "action",
        "run",
        "--project",
        str(project_path),
        "--project-id",
        "project-pdf-tables",
        str(spec_path),
    )
    # A failed action still returns 0 from the CLI with a failed ActionResult.
    result = ActionResult.model_validate(json.loads(run.stdout))
    assert result.status != "completed"
    assert result.errors
    assert result.errors[0].code == "invalid_item_schema"
    assert result.errors[0].details["unknown"] == ["not_a_real_column"]


def test_derive_column_source_homogeneous_file_envelopes_materialize_attachment_file(
    tmp_path: Path,
) -> None:
    """Exact file envelopes stay intact when a JSON list becomes child rows."""
    from frisket.contracts.action import ActionResult

    attachments = [
        {
            "blob": "a3f1c9e8d4b2a7c6e5f0b9d8a1c4e7f2b6d3c8a5e0f1b4d7c2a9e6f3b8d5c1a4",
            "mime": "application/pdf",
            "filename": "annual-report.pdf",
        },
        {
            "blob": "b4e2d8a7c1f6b3e9d5a0c4f8b2e7d1a6c9f3b5e0d4a8c2f7b1e6d9a3c5f0b4e2",
            "mime": "application/pdf",
            "filename": "appendix.pdf",
        },
        {
            "blob": "c5a9e3d7b1f4c8a2e6d0b5f9c3a7e1d4b8f2c6a0e5d9b3f7c1a4e8d2b6f0c5a9",
            "mime": "image/jpeg",
            "filename": "site-photo.jpg",
        },
    ]
    seed_action = _seed_import_action()
    seed_action["params"]["columns"][1]["name"] = "attachments"
    seed_action["params"]["rows"] = [
        {"title": "First email", "attachments": attachments[:2]},
        {"title": "Second email", "attachments": attachments[2:]},
    ]
    seed_action["idempotency_key"] = "attachment_envelopes_seed@sha256:v1"

    project_path = tmp_path / "attachment-envelopes.frisket"
    project = Project.create(project_path, name="Attachment Envelopes")
    project.close()
    seed_path = _write_spec(tmp_path, "attachment_seed.json", seed_action)
    seed = run_cli(
        "action",
        "run",
        "--project",
        str(project_path),
        "--project-id",
        "project-attachment-envelopes",
        str(seed_path),
    )
    assert seed.returncode == 0, seed.stderr

    project = Project(project_path)
    try:
        sheet_id = int(
            project.db.execute("SELECT id FROM sheets WHERE name='Filings'").fetchone()[
                "id"
            ]
        )
        column_id = int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='attachments'",
                (sheet_id,),
            ).fetchone()["id"]
        )
        source_row_ids = [
            int(row["id"])
            for row in project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
            ).fetchall()
        ]
    finally:
        project.close()

    spec_path = _write_spec(
        tmp_path,
        "derive_attachments.json",
        _derive_column_action(
            sheet_id=sheet_id,
            column_id=column_id,
            target_sheet_name="Attachments",
            idempotency_key="derive_attachments@sha256:v1",
        ),
    )
    run = run_cli(
        "action",
        "run",
        "--project",
        str(project_path),
        "--project-id",
        "project-attachment-envelopes",
        str(spec_path),
    )
    assert run.returncode == 0, run.stderr
    result = ActionResult.model_validate(json.loads(run.stdout))
    assert result.status == "completed", result.errors
    child_sheet_id = next(
        output.sheet_id for output in result.outputs if output.kind == "sheet"
    )

    project = Project(project_path)
    try:
        columns = project.db.execute(
            "SELECT id, name, type FROM columns WHERE sheet_id=? ORDER BY position",
            (child_sheet_id,),
        ).fetchall()
        assert [(row["name"], row["type"]) for row in columns] == [
            ("attachment", "file")
        ]
        child_rows = project.db.execute(
            "SELECT id, parent_row_id FROM rows WHERE sheet_id=? ORDER BY position",
            (child_sheet_id,),
        ).fetchall()
        assert [int(row["parent_row_id"]) for row in child_rows] == [
            source_row_ids[0],
            source_row_ids[0],
            source_row_ids[1],
        ]
        attachment_values = project.get_values(child_sheet_id, int(columns[0]["id"]))
        assert [attachment_values[int(row["id"])] for row in child_rows] == attachments
    finally:
        project.close()

    projected_spec_path = _write_spec(
        tmp_path,
        "derive_attachment_filenames.json",
        _derive_column_action(
            sheet_id=sheet_id,
            column_id=column_id,
            target_sheet_name="Attachment filenames",
            idempotency_key="derive_attachment_filenames@sha256:v1",
            include_columns=["filename"],
        ),
    )
    projected_run = run_cli(
        "action",
        "run",
        "--project",
        str(project_path),
        "--project-id",
        "project-attachment-envelopes",
        str(projected_spec_path),
    )
    assert projected_run.returncode == 0, projected_run.stderr
    projected_result = ActionResult.model_validate(json.loads(projected_run.stdout))
    assert projected_result.status == "completed", projected_result.errors
    projected_sheet_id = next(
        output.sheet_id for output in projected_result.outputs if output.kind == "sheet"
    )

    project = Project(project_path)
    try:
        columns = project.db.execute(
            "SELECT id, name, type FROM columns WHERE sheet_id=? ORDER BY position",
            (projected_sheet_id,),
        ).fetchall()
        assert [(row["name"], row["type"]) for row in columns] == [("filename", "text")]
        child_rows = project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position",
            (projected_sheet_id,),
        ).fetchall()
        filenames = project.get_values(projected_sheet_id, int(columns[0]["id"]))
        assert [filenames[int(row["id"])] for row in child_rows] == [
            attachment["filename"] for attachment in attachments
        ]
    finally:
        project.close()


def test_derive_column_source_noncanonical_objects_keep_generic_key_union(
    tmp_path: Path,
) -> None:
    """Near-match envelopes do not turn an otherwise mixed object list into files."""
    from frisket.contracts.action import ActionResult

    list_items = [
        {
            "blob": "d6b0f4c8a2e5d9b3f7c1a6e0d4b8f2c5a9e3d7b1f4c8a2e6d0b5f9c3a7e1d4b8",
            "mime": "application/pdf",
            "filename": "canonical-looking.pdf",
        },
        {
            "blob": "e7c1a5f9d3b6e0c4a8f2d5b9e3c7a1f4d8b2e6c0a5f9d3b7e1c4a8f2d6b0e5c9",
            "mime": "application/pdf",
            "filename": "annotated.pdf",
            "note": "keep this metadata column",
        },
        {"subject": "No attachment in this message", "unread": True},
    ]
    seed_action = _seed_import_action()
    seed_action["params"]["columns"][1]["name"] = "items"
    seed_action["params"]["rows"] = [{"title": "Inbox", "items": list_items}]
    seed_action["idempotency_key"] = "mixed_attachment_seed@sha256:v1"

    project_path = tmp_path / "mixed-attachment-envelopes.frisket"
    project = Project.create(project_path, name="Mixed Attachment Envelopes")
    project.close()
    seed_path = _write_spec(tmp_path, "mixed_attachment_seed.json", seed_action)
    seed = run_cli(
        "action",
        "run",
        "--project",
        str(project_path),
        "--project-id",
        "project-mixed-attachment-envelopes",
        str(seed_path),
    )
    assert seed.returncode == 0, seed.stderr

    project = Project(project_path)
    try:
        sheet_id = int(
            project.db.execute("SELECT id FROM sheets WHERE name='Filings'").fetchone()[
                "id"
            ]
        )
        column_id = int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='items'", (sheet_id,)
            ).fetchone()["id"]
        )
    finally:
        project.close()

    spec_path = _write_spec(
        tmp_path,
        "derive_mixed_attachments.json",
        _derive_column_action(
            sheet_id=sheet_id,
            column_id=column_id,
            target_sheet_name="Mixed items",
            idempotency_key="derive_mixed_attachments@sha256:v1",
        ),
    )
    run = run_cli(
        "action",
        "run",
        "--project",
        str(project_path),
        "--project-id",
        "project-mixed-attachment-envelopes",
        str(spec_path),
    )
    assert run.returncode == 0, run.stderr
    result = ActionResult.model_validate(json.loads(run.stdout))
    assert result.status == "completed", result.errors
    child_sheet_id = next(
        output.sheet_id for output in result.outputs if output.kind == "sheet"
    )

    project = Project(project_path)
    try:
        columns = project.db.execute(
            "SELECT id, name, type FROM columns WHERE sheet_id=? ORDER BY position",
            (child_sheet_id,),
        ).fetchall()
        assert [(row["name"], row["type"]) for row in columns] == [
            ("blob", "text"),
            ("mime", "text"),
            ("filename", "text"),
            ("note", "text"),
            ("subject", "text"),
            ("unread", "boolean"),
        ]
        child_rows = project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (child_sheet_id,)
        ).fetchall()
        values_by_name = {
            row["name"]: project.get_values(child_sheet_id, int(row["id"]))
            for row in columns
        }
        assert [
            {
                name: values[int(child_row["id"])]
                for name, values in values_by_name.items()
            }
            for child_row in child_rows
        ] == [
            {
                **list_items[0],
                "note": None,
                "subject": None,
                "unread": None,
            },
            {
                **list_items[1],
                "subject": None,
                "unread": None,
            },
            {
                "blob": None,
                "mime": None,
                "filename": None,
                "note": None,
                **list_items[2],
            },
        ]
    finally:
        project.close()
