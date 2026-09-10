from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Any

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.store import Project
from runner_test_helpers import run_action_with_exact_confirmation
from tests.engine.extract_typed_chain_helpers import typed_extract_request


PROJECT_ID = "project-vision-extract-chain"
WORKFLOW_ID = "vision-image-extract-chain"


class _VisionAdapter:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.requests.append(req)
        return LLMResponse(
            content=json.dumps(self.reply),
            data=dict(self.reply),
            tokens_in=96,
            tokens_out=44,
            cost=0.008,
            model=req.model,
        )


def _router(reply: dict[str, Any]) -> tuple[ModelRouter, _VisionAdapter]:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    adapter = _VisionAdapter(reply)
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return router, adapter


def _png_header(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", width, height)


def _import_files_action(*paths: Path) -> dict[str, Any]:
    return {
        "action_id": "import.files",
        "scope": {"kind": "project"},
        "sheet_name": "Contact cards",
        "params": {
            "files": [
                {
                    "path": str(path),
                    "filename": path.name,
                    "mime": "image/png",
                }
                for path in paths
            ],
        },
        "idempotency_key": "vision_chain/import_files@sha256:cards",
    }


def _map_extract_action(sheet_id: int) -> dict[str, Any]:
    return typed_extract_request(
        sheet_id,
        source=["filename", "media"],
        context="Rows are public-domain contact-card images.",
        instruction="Extract every visible person with a name and title.",
        fields=[
            {
                "name": "people",
                "type": "list",
                "description": "People visible in the image.",
                "items": {
                    "type": "object",
                    "required": ["name", "title"],
                    "properties": {
                        "name": {"type": "string"},
                        "title": {"type": "string"},
                    },
                },
            }
        ],
        idempotency_key="vision_chain/map_extract_people@sha256:v1",
    )


def _derive_people_action(
    *,
    sheet_id: int,
    column_id: int,
    run_id: int,
    route: str = "people",
    schema: str = "people_list",
    target_sheet_name: str = "People",
    idempotency_key: str = "vision_chain/derive_people@sha256:v1",
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
            "item_schema": {
                "type": "object",
                "required": ["name", "title"],
                "properties": {
                    "name": {"type": "string"},
                    "title": {"type": "string"},
                },
            },
            "columns": [
                {"name": "name", "path": "$.name", "type": "text"},
                {"name": "title", "path": "$.title", "type": "text"},
            ],
        },
        "idempotency_key": idempotency_key,
    }


def _receipt_refs(project: Project, receipt_id: str) -> tuple[dict[str, Any], ...]:
    from frisket.contracts.action import Receipt

    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?",
        (receipt_id,),
    ).fetchone()
    assert row is not None
    receipt = Receipt.model_validate(json.loads(row["body"]))
    return tuple(
        [item.ref for item in receipt.inputs]
        + [item.ref for item in receipt.outputs]
        + [item.ref for item in receipt.evidence]
    )


def _counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "sheets",
            "columns",
            "rows",
            "runs",
            "results",
            "model_calls",
            "ops",
            "receipts",
        )
    }


def test_vision_image_extract_chain_derives_people_from_map_extract_list(
    tmp_path: Path,
) -> None:
    from frisket.actions.system import validate_root_action
    from frisket.contracts.action import Receipt

    image_paths = [
        tmp_path / "contact-card-1.png",
        tmp_path / "contact-card-2.png",
    ]
    for index, image_path in enumerate(image_paths, start=1):
        image_path.write_bytes(_png_header(20 + index, 13))
    project_path = tmp_path / "vision-chain.frisket"
    project = Project.create(project_path, name="Vision chain")
    try:
        imported = run_action_with_exact_confirmation(
            project,
            _import_files_action(*image_paths),
            project_id=PROJECT_ID,
        )
        assert imported.status == "completed"
        sheet_id = next(
            output.sheet_id for output in imported.outputs if output.kind == "sheet"
        )
        assert sheet_id is not None
        source_row_ids = [
            int(row["id"])
            for row in project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=? ORDER BY position",
                (sheet_id,),
            ).fetchall()
        ]
        assert len(source_row_ids) == len(image_paths)

        router, adapter = _router(
            {
                "people": [
                    {"name": "Ada Lovelace", "title": "mathematician"},
                    {"name": "Grace Hopper", "title": "computer scientist"},
                ]
            }
        )
        extract_action = _map_extract_action(sheet_id)
        assert validate_root_action(extract_action).ok is True
        extracted = run_action_with_exact_confirmation(
            project,
            extract_action,
            project_id=PROJECT_ID,
            router=router,
        )
        assert extracted.status == "completed"
        assert extracted.run_id is not None
        assert extracted.receipt_id is not None
        assert len(adapter.requests) == len(image_paths)
        assert all(
            any(part.get("type") == "image" for part in request.messages[1]["content"])
            for request in adapter.requests
        )
        people_column = next(
            output
            for output in extracted.outputs
            if output.kind == "column" and output.name == "people"
        )
        assert people_column.column_id is not None
        named_output = next(
            output
            for output in extracted.outputs
            if output.kind == "named_result" and output.name == "people"
        )
        assert named_output.ref["schema"] == "people_list"
        assert named_output.ref["may_feed"] == ["derive.table_from_list"]

        extract_refs = _receipt_refs(project, extracted.receipt_id)
        extract_ref_kinds = {ref["kind"] for ref in extract_refs}
        assert {"model_rows_input_column", "named_result"} <= extract_ref_kinds
        # The image column is recorded as the run's media input over every
        # source row (the typed receipt records the column, not each blob).
        media_input = next(
            ref
            for ref in extract_refs
            if ref["kind"] == "model_rows_input_column" and ref["name"] == "media"
        )
        assert media_input["type"] == "image"
        assert media_input["row_ids"] == source_row_ids
        media_column_id = int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='media'",
                (sheet_id,),
            ).fetchone()["id"]
        )
        assert media_input["column_id"] == media_column_id
        # Content-addressed ``map_extract_blob_input`` provenance (one
        # hash/filename ref per blob) no longer exists in typed receipts —
        # product metadata drop, reported. Resolve the receipt's column x
        # row_ids reference to the seeded cells and pin those blobs instead.
        media_values = project.get_values(
            sheet_id, media_input["column_id"], row_ids=media_input["row_ids"]
        )
        referenced_cells = [media_values[row_id] for row_id in media_input["row_ids"]]
        assert [cell["filename"] for cell in referenced_cells] == [
            image_path.name for image_path in image_paths
        ]
        assert [cell["blob"] for cell in referenced_cells] == [
            hashlib.sha256(image_path.read_bytes()).hexdigest()
            for image_path in image_paths
        ]

        before_extract_replay = _counts(project)
        replayed_extract = run_action_with_exact_confirmation(
            project,
            extract_action,
            project_id=PROJECT_ID,
            router=router,
        )
        assert replayed_extract.status == "completed"
        assert replayed_extract.receipt_id == extracted.receipt_id
        assert _counts(project) == before_extract_replay
        assert len(adapter.requests) == len(image_paths)
        assert any(
            output.kind == "named_result" and output.name == "people"
            for output in replayed_extract.outputs
        )

        derive_action = _derive_people_action(
            sheet_id=sheet_id,
            column_id=people_column.column_id,
            run_id=extracted.run_id,
        )
        from frisket.actions.system import validate_root_action

        assert validate_root_action(derive_action).ok is True
        derived = run_action_with_exact_confirmation(
            project,
            derive_action,
            project_id=PROJECT_ID,
        )
        assert derived.status == "completed"
        assert derived.receipt_id is not None
        child_sheet_id = next(
            output.sheet_id for output in derived.outputs if output.kind == "sheet"
        )
        assert child_sheet_id is not None
        child_row_ids = next(
            output.row_ids for output in derived.outputs if output.kind == "rows"
        )
        assert len(child_row_ids) == 4

        child = project.db.execute(
            "SELECT * FROM sheets WHERE id=?", (child_sheet_id,)
        ).fetchone()
        assert child is not None
        assert child["parent_sheet_id"] == sheet_id
        columns = {
            row["name"]: row
            for row in project.db.execute(
                "SELECT * FROM columns WHERE sheet_id=?",
                (child_sheet_id,),
            ).fetchall()
        }
        assert list(columns) == ["name", "title"]
        assert columns["name"]["ai_generated"] == 1
        names = project.get_values(child_sheet_id, int(columns["name"]["id"]))
        titles = project.get_values(child_sheet_id, int(columns["title"]["id"]))
        assert list(names.values()) == [
            "Ada Lovelace",
            "Grace Hopper",
            "Ada Lovelace",
            "Grace Hopper",
        ]
        assert list(titles.values()) == [
            "mathematician",
            "computer scientist",
            "mathematician",
            "computer scientist",
        ]
        parent_rows = [
            int(row["parent_row_id"])
            for row in project.db.execute(
                "SELECT parent_row_id FROM rows WHERE sheet_id=? ORDER BY position",
                (child_sheet_id,),
            ).fetchall()
        ]
        assert parent_rows == [
            row_id for row_id in people_column.row_ids for _ in range(2)
        ]

        derive_receipt_row = project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (derived.receipt_id,)
        ).fetchone()
        assert derive_receipt_row is not None
        derive_receipt = Receipt.model_validate(json.loads(derive_receipt_row["body"]))
        source_ref = next(
            item.ref
            for item in derive_receipt.inputs
            if item.ref["kind"] == "list_table_read"
        )
        assert source_ref["source_receipt_id"] == extracted.receipt_id
        source_receipt = project.db.execute(
            "SELECT action_kind FROM receipts WHERE id=?",
            (source_ref["source_receipt_id"],),
        ).fetchone()
        assert source_receipt["action_kind"] == "map.extract"
        assert source_ref["source"]["route"] == "people"

        before_replay = _counts(project)
        replay = run_action_with_exact_confirmation(
            project,
            derive_action,
            project_id=PROJECT_ID,
        )
        assert replay.status == "completed"
        assert replay.receipt_id == derived.receipt_id
        assert _counts(project) == before_replay

        bad_source = _derive_people_action(
            sheet_id=sheet_id,
            column_id=people_column.column_id,
            run_id=extracted.run_id,
            route="faces",
            schema="faces_list",
            target_sheet_name="Faces",
            idempotency_key="vision_chain/derive_bad_source@sha256:v1",
        )
        before_bad_source = _counts(project)
        bad = run_action_with_exact_confirmation(
            project,
            bad_source,
            project_id=PROJECT_ID,
        )
        assert bad.status == "failed"
        assert bad.errors[0].code == "invalid_input_ref"
        assert _counts(project) == before_bad_source
    finally:
        project.close()
