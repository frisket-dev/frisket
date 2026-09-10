from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from frisket.contracts.action import ActionResult, Receipt
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project


PROJECT_ID = "project-recordset-contract"


def _seed_project(project_path: Path) -> tuple[Project, int, list[int]]:
    project = Project.create(project_path, name="Recordset Contract")
    sheet_id = project.add_sheet("Notes")
    columns = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "body": project.add_column(sheet_id, "body", type="text"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {"title": "One", "body": "Alice founded Newsroom Labs."},
            {"title": "Two", "body": "Bob joined Civic Data."},
        ],
        columns,
    )
    return project, sheet_id, row_ids


def _map_python_action(sheet_id: int, *, key: str) -> dict[str, Any]:
    return {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "input_columns": ["title", "body"],
            "code": "\n".join(
                [
                    "name = row['body'].split()[0]",
                    "result = {'entities': [{'name': name, 'kind': 'person'}]}",
                ]
            ),
            "return_schema": _map_return_schema(),
            "output_routes": [
                {
                    "name": "entities",
                    "path": "$.entities",
                    "target": {
                        "kind": "named_result",
                        "schema": "entity_list",
                        "may_feed": ["derive.table_from_list"],
                    },
                }
            ],
        },
        "idempotency_key": key,
    }


def _map_return_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["entities"],
        "properties": {
            "entities": {
                "type": "array",
                "items": _entity_item_schema(),
            }
        },
    }


def _entity_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["name", "kind"],
        "properties": {
            "name": {"type": "string"},
            "kind": {"type": "string"},
        },
    }


def _derive_action(
    *,
    sheet_id: int,
    column_id: int,
    run_id: int,
    key: str,
    target_sheet_name: str,
    item_schema: dict[str, Any] | None = None,
    route: str = "entities",
    schema: str = "entity_list",
) -> dict[str, Any]:
    return {
        "action_id": "derive.table_from_list",
        "scope": {"kind": "project"},
        "sheet_name": target_sheet_name,
        "params": {
            "source": {
                "kind": "named_result",
                "sheet_id": sheet_id,
                "column_id": column_id,
                "run_id": run_id,
                "route": route,
                "schema": schema,
            },
            "item_schema": item_schema or _entity_item_schema(),
            "columns": [
                {"name": "name", "path": "$.name", "type": "text"},
                {"name": "kind", "path": "$.kind", "type": "text"},
            ],
        },
        "idempotency_key": key,
    }


def _receipt(project: Project, receipt_id: str) -> Receipt:
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?",
        (receipt_id,),
    ).fetchone()
    assert row is not None
    return Receipt.model_validate(json.loads(row["body"]))


def _store_receipt(project: Project, receipt: Receipt) -> None:
    project.db.execute(
        "UPDATE receipts SET action_kind=?, body=? WHERE id=?",
        (
            receipt.action_kind,
            json.dumps(receipt.model_dump(mode="json"), sort_keys=True),
            receipt.receipt_id,
        ),
    )
    project.db.commit()


def _source_column_id(project: Project, sheet_id: int) -> int:
    row = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name='__result_entities'",
        (sheet_id,),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def _prepare_source(
    tmp_path: Path,
) -> tuple[Project, int, list[int], ActionResult, int, Receipt]:
    project, sheet_id, row_ids = _seed_project(tmp_path / "recordset-contract.frisket")
    mapped = run_action_spec(
        project,
        _map_python_action(sheet_id, key="map_python@sha256:recordset-contract"),
        project_id=PROJECT_ID,
    )
    assert mapped.status == "completed", mapped.errors
    assert mapped.run_id is not None
    assert mapped.receipt_id is not None
    column_id = _source_column_id(project, sheet_id)
    return (
        project,
        sheet_id,
        row_ids,
        mapped,
        column_id,
        _receipt(project, mapped.receipt_id),
    )


def test_derive_table_from_list_uses_registered_named_result_capabilities(
    tmp_path: Path,
) -> None:
    project, sheet_id, row_ids, mapped, column_id, receipt = _prepare_source(tmp_path)
    try:
        output_ref = next(
            output.ref
            for output in receipt.outputs
            if output.ref["kind"] == "named_result"
        )
        assert output_ref["source_action_kind"] == "map.python"
        assert output_ref["sheet_id"] == sheet_id
        assert output_ref["column_id"] == column_id
        assert output_ref["run_id"] == mapped.run_id
        assert output_ref["route"] == "entities"
        assert output_ref["schema"] == "entity_list"
        assert (
            output_ref["schema_json"] == _map_return_schema()["properties"]["entities"]
        )
        assert output_ref["item_schema"] == _entity_item_schema()
        assert output_ref["row_ids"] == row_ids
        assert output_ref["may_feed"] == ["derive.table_from_list"]

        result = run_action_spec(
            project,
            _derive_action(
                sheet_id=sheet_id,
                column_id=column_id,
                run_id=mapped.run_id,
                key="derive@sha256:registered",
                target_sheet_name="Entities",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors
        before_mutations = _table_counts(project)

        mismatch_schema = deepcopy(_entity_item_schema())
        mismatch_schema["properties"]["kind"] = {"type": "integer"}
        schema_mismatch = run_action_spec(
            project,
            _derive_action(
                sheet_id=sheet_id,
                column_id=column_id,
                run_id=mapped.run_id,
                key="derive@sha256:schema-mismatch",
                target_sheet_name="Schema Mismatch",
                item_schema=mismatch_schema,
            ),
            project_id=PROJECT_ID,
        )
        assert schema_mismatch.status == "failed"
        assert schema_mismatch.errors[0].code == "invalid_item_schema"
        assert _table_counts(project) == before_mutations

        stale_route = run_action_spec(
            project,
            _derive_action(
                sheet_id=sheet_id,
                column_id=column_id,
                run_id=mapped.run_id,
                key="derive@sha256:stale-route",
                target_sheet_name="Stale Route",
                route="people",
            ),
            project_id=PROJECT_ID,
        )
        assert stale_route.status == "failed"
        assert stale_route.errors[0].code == "invalid_input_ref"
        assert _table_counts(project) == before_mutations

        spoofed = receipt.model_copy(deep=True)
        spoofed.action_kind = "map.fake"
        spoofed.outputs[0].ref["source_action_kind"] = "map.fake"
        _store_receipt(project, spoofed)
        unregistered = run_action_spec(
            project,
            _derive_action(
                sheet_id=sheet_id,
                column_id=column_id,
                run_id=mapped.run_id,
                key="derive@sha256:unregistered",
                target_sheet_name="Unregistered",
            ),
            project_id=PROJECT_ID,
        )
        assert unregistered.status == "failed"
        assert unregistered.errors[0].code == "invalid_input_ref"
        assert _table_counts(project) == before_mutations

        legacy = receipt.model_copy(deep=True)
        for field in ("source_action_kind", "schema_json", "item_schema", "row_ids"):
            legacy.outputs[0].ref.pop(field, None)
        _store_receipt(project, legacy)
        legacy_result = run_action_spec(
            project,
            _derive_action(
                sheet_id=sheet_id,
                column_id=column_id,
                run_id=mapped.run_id,
                key="derive@sha256:legacy-ref",
                target_sheet_name="Legacy",
            ),
            project_id=PROJECT_ID,
        )
        assert legacy_result.status == "failed"
        assert legacy_result.errors[0].code == "invalid_input_ref"
        assert _table_counts(project) == before_mutations
    finally:
        project.close()


def _table_counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("sheets", "columns", "rows", "runs", "results", "ops", "receipts")
    }
