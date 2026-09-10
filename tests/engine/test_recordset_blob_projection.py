from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import media_cell, owned_media_metadata_document


PROJECT_ID = "project-recordset-blob-projection"


def _seed_project(project_path: Path) -> tuple[Project, int, int, int, str]:
    project = Project.create(project_path, name="Blob Projection")
    sheet_id = project.add_sheet("Assets")
    image_column_id = project.add_column(sheet_id, "image", type="image")
    digest = project.add_blob(
        b"fake image bytes",
        filename="source.jpg",
        mime="image/jpeg",
        metadata=owned_media_metadata_document(probe={"kind": "image"}),
    )
    row_id = project.add_rows(
        sheet_id,
        [
            {
                "image": media_cell(
                    digest,
                    mime="image/jpeg",
                    filename="source.jpg",
                )
            }
        ],
        {"image": image_column_id},
    )[0]
    return project, sheet_id, image_column_id, row_id, digest


def _map_python_action(sheet_id: int) -> dict[str, Any]:
    return {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "input_columns": ["image"],
            "code": "result = {'crops': [{'crop': row['image'], 'label': 'source'}]}",
            "return_schema": {
                "type": "object",
                "required": ["crops"],
                "properties": {
                    "crops": {
                        "type": "array",
                        "items": _crop_item_schema(),
                    }
                },
            },
            "output_routes": [
                {
                    "name": "crops",
                    "path": "$.crops",
                    "target": {
                        "kind": "named_result",
                        "schema": "crop_list",
                        "may_feed": ["derive.table_from_list"],
                    },
                }
            ],
        },
        "idempotency_key": "blob_projection@sha256:map",
    }


def _derive_action(*, sheet_id: int, column_id: int, run_id: int) -> dict[str, Any]:
    return {
        "action_id": "derive.table_from_list",
        "scope": {"kind": "project"},
        "sheet_name": "Crops",
        "params": {
            "source": {
                "kind": "named_result",
                "sheet_id": sheet_id,
                "column_id": column_id,
                "run_id": run_id,
                "route": "crops",
                "schema": "crop_list",
            },
            "item_schema": _crop_item_schema(),
            "columns": [
                {"name": "crop", "path": "$.crop", "type": "image"},
                {"name": "label", "path": "$.label", "type": "text"},
            ],
        },
        "idempotency_key": "blob_projection@sha256:derive",
    }


def _crop_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["crop", "label"],
        "properties": {
            "crop": {
                "type": "object",
                "required": ["blob"],
                "properties": {
                    "blob": {"type": "string"},
                    "mime": {"type": "string"},
                    "filename": {"type": "string"},
                },
            },
            "label": {"type": "string"},
        },
    }


def test_derive_table_from_list_projects_blob_envelopes_to_typed_media_cells(
    tmp_path: Path,
) -> None:
    project, sheet_id, _image_column_id, source_row_id, digest = _seed_project(
        tmp_path / "recordset-blob-projection.frisket"
    )
    try:
        mapped = run_action_spec(
            project,
            _map_python_action(sheet_id),
            project_id=PROJECT_ID,
        )
        assert mapped.status == "completed", mapped.errors
        assert mapped.run_id is not None
        named_output = next(
            output for output in mapped.outputs if output.kind == "named_result"
        )
        assert named_output.ref["item_schema"] == _crop_item_schema()
        with project.materialize_blob(digest) as path:
            assert path.exists()

        derived = run_action_spec(
            project,
            _derive_action(
                sheet_id=sheet_id,
                column_id=int(named_output.column_id or named_output.ref["column_id"]),
                run_id=mapped.run_id,
            ),
            project_id=PROJECT_ID,
        )
        assert derived.status == "completed", derived.errors
        child_sheet_id = next(
            output.sheet_id for output in derived.outputs if output.kind == "sheet"
        )
        assert child_sheet_id is not None
        columns = {
            row["name"]: row
            for row in project.db.execute(
                "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
                (child_sheet_id,),
            ).fetchall()
        }
        assert list(columns) == ["crop", "label"]
        assert columns["crop"]["type"] == "image"
        assert "blob_id" not in columns
        row = project.db.execute(
            "SELECT * FROM rows WHERE sheet_id=?", (child_sheet_id,)
        ).fetchone()
        assert row is not None
        assert int(row["parent_row_id"]) == source_row_id
        crop_values = project.get_values(child_sheet_id, int(columns["crop"]["id"]))
        crop = next(iter(crop_values.values()))
        assert crop == {
            "blob": digest,
            "mime": "image/jpeg",
            "filename": "source.jpg",
        }
        labels = project.get_values(child_sheet_id, int(columns["label"]["id"]))
        assert list(labels.values()) == ["source"]

        receipt_row = project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (derived.receipt_id,)
        ).fetchone()
        assert receipt_row is not None
        receipt = json.loads(receipt_row["body"])
        source_ref = next(
            item["ref"]
            for item in receipt["inputs"]
            if item["ref"]["kind"] == "list_table_read"
        )
        source_receipt = project.db.execute(
            "SELECT action_kind FROM receipts WHERE id=?",
            (source_ref["source_receipt_id"],),
        ).fetchone()
        assert source_receipt["action_kind"] == "map.python"
        assert source_ref["source_receipt_id"] == mapped.receipt_id
    finally:
        project.close()
