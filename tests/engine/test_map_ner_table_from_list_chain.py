from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from frisket.contracts.action import Receipt
from frisket.engine.executor import actions as executor_actions
from frisket.ai.llm import ModelRouter
from frisket.engine.store import Project
from tests.engine.extract_typed_chain_helpers import typed_ner_request


PROJECT_ID = "project-map-ner-chain"


class _SidecarResponse:
    status_code = 200
    text = "ok"

    def __init__(self, entities: list[dict[str, Any]]) -> None:
        self.entities = entities

    def json(self) -> dict[str, Any]:
        return {"results": [self.entities]}


class _SidecarHttp:
    is_closed = False

    async def post(self, url: str, **kwargs: Any) -> _SidecarResponse:
        text = str((kwargs.get("json") or {}).get("texts", [""])[0])
        if "Ada" in text:
            return _SidecarResponse(
                [
                    {
                        "text": "Ada Lovelace",
                        "label": "person",
                        "start": 0,
                        "end": 12,
                        "score": 0.98,
                    }
                ]
            )
        return _SidecarResponse(
            [
                {
                    "text": "Analytical Engine",
                    "label": "organization",
                    "start": 28,
                    "end": 45,
                    "score": 0.91,
                }
            ]
        )


def _router() -> ModelRouter:
    router = ModelRouter(cache=None, cache_mode="off")
    router._client = _SidecarHttp()  # noqa: SLF001
    return router


def _seed_project(project_path: Path) -> tuple[Project, int, list[int]]:
    project = Project.create(project_path, name="NER Chain")
    sheet_id = project.add_sheet("Texts")
    columns = {
        "body": project.add_column(sheet_id, "body", type="text"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {"body": "Ada Lovelace wrote notes."},
            {"body": "Babbage described the Analytical Engine."},
        ],
        columns,
    )
    return project, sheet_id, row_ids


def _map_ner_action(sheet_id: int) -> dict[str, Any]:
    return typed_ner_request(
        sheet_id,
        source=["body"],
        labels=["person", "organization"],
        threshold=0.5,
        engine="gliner",
        output_name="entities",
        idempotency_key="map_ner_chain@sha256:map",
    )


def _derive_action(*, sheet_id: int, column_id: int, run_id: int) -> dict[str, Any]:
    item_schema = _entity_item_schema()
    return {
        "action_id": "derive.table_from_list",
        "scope": {"kind": "project"},
        "sheet_name": "Entity Rows",
        "params": {
            "source": {
                "kind": "named_result",
                "sheet_id": sheet_id,
                "column_id": column_id,
                "run_id": run_id,
                "route": "entities",
                "schema": "entities_list",
            },
            "item_schema": item_schema,
            "columns": [
                {"name": "text", "path": "$.text", "type": "text"},
                {"name": "type", "path": "$.type", "type": "text"},
                {"name": "start", "path": "$.start", "type": "integer"},
                {"name": "end", "path": "$.end", "type": "integer"},
                {"name": "score", "path": "$.score", "type": "number"},
            ],
        },
        "idempotency_key": "map_ner_chain@sha256:derive",
    }


def _entity_item_schema() -> dict[str, Any]:
    """The STORED entity item contract, derived rather than re-typed.

    A hand copy is a drift trap: the named_result ref this file asserts on
    carries the item schema derived from the typed ``map.ner`` output field
    (the ``EntityMention`` model), so derive it from that one source the same
    way the receipt writer does.
    """
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.engine.executor.recordsets import _item_schema_for

    entities = next(
        field
        for field in ACTION_REGISTRY.get("map.ner").definition.run.output_fields
        if field.key == "entities"
    )
    return _item_schema_for(dict(entities.schema))


def _receipt(project: Project, receipt_id: str) -> Receipt:
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?",
        (receipt_id,),
    ).fetchone()
    assert row is not None
    return Receipt.model_validate(json.loads(row["body"]))


def _counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("sheets", "columns", "rows", "runs", "results", "ops", "receipts")
    }


def test_map_ner_named_result_feeds_derive_table_from_list(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")

    project, sheet_id, source_row_ids = _seed_project(
        tmp_path / "map-ner-chain.frisket"
    )
    try:
        mapped = executor_actions.run_action_spec(
            project,
            _map_ner_action(sheet_id),
            project_id=PROJECT_ID,
            router=_router(),
        )
        assert mapped.status == "completed", mapped.errors
        assert mapped.run_id is not None
        assert mapped.receipt_id is not None
        assert [(output.kind, output.name) for output in mapped.outputs] == [
            ("column", "entities"),
            ("named_result", "entities"),
        ]
        named_output = next(
            output for output in mapped.outputs if output.kind == "named_result"
        )
        assert named_output.ref["source_action_kind"] == "map.ner"
        assert named_output.ref["row_ids"] == source_row_ids
        assert named_output.ref["item_schema"] == _entity_item_schema()
        column_id = int(named_output.column_id or named_output.ref["column_id"])

        derived = executor_actions.run_action_spec(
            project,
            _derive_action(
                sheet_id=sheet_id, column_id=column_id, run_id=mapped.run_id
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
        assert list(columns) == ["text", "type", "start", "end", "score"]
        rows = project.db.execute(
            "SELECT * FROM rows WHERE sheet_id=? ORDER BY position",
            (child_sheet_id,),
        ).fetchall()
        assert [int(row["parent_row_id"]) for row in rows] == source_row_ids
        texts = project.get_values(child_sheet_id, int(columns["text"]["id"]))
        labels = project.get_values(child_sheet_id, int(columns["type"]["id"]))
        assert list(texts.values()) == ["Ada Lovelace", "Analytical Engine"]
        assert list(labels.values()) == ["person", "organization"]

        receipt = _receipt(project, derived.receipt_id or "")
        source_ref = next(
            ref.ref for ref in receipt.inputs if ref.ref["kind"] == "list_table_read"
        )
        assert (
            _receipt(project, source_ref["source_receipt_id"]).action_kind == "map.ner"
        )
        assert source_ref["source_receipt_id"] == mapped.receipt_id
        before_replay = _counts(project)
        replay = executor_actions.run_action_spec(
            project,
            _derive_action(
                sheet_id=sheet_id, column_id=column_id, run_id=mapped.run_id
            ),
            project_id=PROJECT_ID,
        )
        assert replay.status == "completed"
        assert replay.receipt_id == derived.receipt_id
        assert _counts(project) == before_replay
    finally:
        project.close()
