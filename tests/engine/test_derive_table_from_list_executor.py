from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from executor_harness import CatalogEntry, ExecutorCase, Gate, case_env
from frisket.engine.store import Project


def _seed_import_action() -> dict[str, Any]:
    return {
        "action_id": "import.rows",
        "scope": {"kind": "project"},
        "sheet_name": "Transcripts",
        "params": {
            "columns": [
                {"name": "title", "type": "text"},
                {"name": "transcript", "type": "text"},
            ],
            "rows": [
                {
                    "title": "Episode 1",
                    "transcript": "Alice founded Newsroom Labs in Brooklyn",
                },
                {
                    "title": "Episode 2",
                    "transcript": "Bob joined Civic Data in Queens",
                },
            ],
            "source": {
                "kind": "inline",
                "label": "derive seed",
                "fingerprint": "sha256:derive-seed",
            },
        },
        "idempotency_key": "derive_seed@sha256:v1",
    }


def _map_named_results_action(sheet_id: int) -> dict[str, Any]:
    code = "\n".join(
        [
            "words = row['transcript'].split()",
            "person = words[0]",
            "result = {",
            "    'entities': [",
            "        {",
            "            'name': person,",
            "            'title': 'speaker',",
            "            'ignored': row['title'],",
            "        }",
            "    ],",
            "    'debug': {'first_word': person, 'input_title': row['title']},",
            "}",
        ]
    )
    return {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "input_columns": ["title", "transcript"],
            "code": code,
            "return_schema": {
                "type": "object",
                "required": ["entities", "debug"],
                "properties": {
                    "entities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["name", "title"],
                            "properties": {
                                "name": {"type": "string"},
                                "title": {"type": "string"},
                            },
                        },
                    },
                    "debug": {"type": "object"},
                },
            },
            "output_routes": [
                {
                    "name": "entities",
                    "path": "$.entities",
                    "target": {
                        "kind": "named_result",
                        "schema": "entity_list",
                        "may_feed": ["derive.table_from_list"],
                    },
                },
                {
                    "name": "debug_payload",
                    "path": "$.debug",
                    "target": {
                        "kind": "named_result",
                        "schema": "debug_object",
                        "may_feed": ["derive.table_from_list"],
                    },
                },
            ],
        },
        "idempotency_key": "derive_source_map@sha256:v1",
    }


def _derive_action(
    seeded: dict[str, Any],
    *,
    route: str = "entities",
    schema: str = "entity_list",
    column_id: int | None = None,
    target_sheet_name: str = "Entities",
    idempotency_key: str = "derive_entities@sha256:v1",
    columns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "action_id": "derive.table_from_list",
        "scope": {"kind": "project"},
        "sheet_name": target_sheet_name,
        "params": {
            "source": {
                "kind": "named_result",
                "sheet_id": seeded["sheet_id"],
                "column_id": (
                    column_id if column_id is not None else seeded["entities_column_id"]
                ),
                "run_id": seeded["run_id"],
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
            "columns": columns
            or [
                {"name": "name", "path": "$.name", "type": "text"},
                {"name": "title", "path": "$.title", "type": "text"},
            ],
        },
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    from frisket.engine.executor import run_action_spec

    seed = run_action_spec(
        project, _seed_import_action(), project_id="project-derive-table"
    )
    assert seed.status == "completed", seed.errors
    sheet_id = int(
        project.db.execute("SELECT id FROM sheets WHERE name='Transcripts'").fetchone()[
            "id"
        ]
    )
    mapped = run_action_spec(
        project, _map_named_results_action(sheet_id), project_id="project-derive-table"
    )
    assert mapped.status == "completed", mapped.errors
    assert mapped.run_id is not None
    columns = {
        row["name"]: int(row["id"])
        for row in project.db.execute(
            "SELECT id, name FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchall()
    }
    return {
        "sheet_id": sheet_id,
        "run_id": mapped.run_id,
        "entities_column_id": columns["__result_entities"],
        "debug_column_id": columns["__result_debug_payload"],
        "source_row_ids": [
            int(row["id"])
            for row in project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
            ).fetchall()
        ],
    }


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _derive_action(seeded)


def _duplicate_sheet_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _derive_action(seeded, idempotency_key="derive_entities@sha256:duplicate")


def _missing_column_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _derive_action(
        seeded,
        column_id=999999,
        target_sheet_name="Bad Source",
        idempotency_key="derive_entities@sha256:bad-source",
    )


def _object_source_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # The debug route resolves, but its payload is an object, not a list.
    return _derive_action(
        seeded,
        column_id=seeded["debug_column_id"],
        route="debug_payload",
        schema="debug_object",
        target_sheet_name="Debug Rows",
        idempotency_key="derive_debug@sha256:not-list",
    )


def _bad_projection_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _derive_action(
        seeded,
        target_sheet_name="Bad Projection",
        idempotency_key="derive_entities@sha256:bad-projection",
        columns=[{"name": "name", "path": "$.name", "type": "integer"}],
    )


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    assert result.op_ids == [3]
    assert {output.kind for output in result.outputs} == {"sheet", "column", "rows"}
    child_sheet_id = next(
        output.sheet_id for output in result.outputs if output.kind == "sheet"
    )
    child_row_ids = next(
        output.row_ids for output in result.outputs if output.kind == "rows"
    )
    assert len(child_row_ids) == 2

    child = project.db.execute(
        "SELECT * FROM sheets WHERE id=?", (child_sheet_id,)
    ).fetchone()
    assert child["name"] == "Entities"
    assert child["parent_sheet_id"] == seeded["sheet_id"]
    assert child["parent_op_id"] == 3
    columns = {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
            (child_sheet_id,),
        ).fetchall()
    }
    assert list(columns) == ["name", "title"]
    assert columns["name"]["type"] == "text"
    assert columns["name"]["ai_generated"] == 1
    assert columns["title"]["ai_generated"] == 1
    rows = project.db.execute(
        "SELECT * FROM rows WHERE sheet_id=? ORDER BY position", (child_sheet_id,)
    ).fetchall()
    assert [int(row["id"]) for row in rows] == child_row_ids
    assert [int(row["parent_row_id"]) for row in rows] == seeded["source_row_ids"]
    names = project.get_values(child_sheet_id, int(columns["name"]["id"]))
    titles = project.get_values(child_sheet_id, int(columns["title"]["id"]))
    assert list(names.values()) == ["Alice", "Bob"]
    assert list(titles.values()) == ["speaker", "speaker"]
    # keys outside the declared projection never materialize
    assert "ignored" not in columns

    op = project.db.execute("SELECT * FROM ops WHERE id=3").fetchone()
    assert op["kind"] == "derive.table_from_list"
    undo_info = json.loads(op["undo_info"])
    assert undo_info["created_sheets"] == [child_sheet_id]
    assert undo_info["created_rows"] == child_row_ids
    assert set(undo_info["created_columns"]) == {
        int(columns["name"]["id"]),
        int(columns["title"]["id"]),
    }

    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row["run_id"] is None
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "derive.table_from_list"
    assert receipt.op_ids == [3]
    read = next(
        item.ref for item in receipt.inputs if item.ref["kind"] == "list_table_read"
    )
    input_ref = read["source"]
    assert input_ref["sheet_id"] == seeded["sheet_id"]
    assert input_ref["column_id"] == seeded["entities_column_id"]
    assert input_ref["run_id"] == seeded["run_id"]
    assert input_ref["route"] == "entities"
    assert input_ref["schema"] == "entity_list"
    assert read["source_row_ids"] == seeded["source_row_ids"]
    assert read["item_count"] == 2
    source_receipt = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (read["source_receipt_id"],)
    ).fetchone()
    source_ref = next(
        item.ref
        for item in Receipt.model_validate_json(source_receipt["body"]).outputs
        if item.ref.get("kind") == "named_result"
        and item.ref.get("route") == "entities"
    )
    assert source_ref["source_action_kind"] == "map.python"
    assert source_ref["schema_json"]["type"] == "array"
    assert source_ref["item_schema"] == json.loads(op["spec"])["params"]["item_schema"]
    assert {"materialized_sheet", "materialized_rows"} <= {
        item.ref["kind"] for item in receipt.outputs
    }
    assert {"lineage_parent_rows"} <= {item.ref["kind"] for item in receipt.evidence}


CASES = [
    ExecutorCase(
        kind="derive.table_from_list",
        request_style="typed",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            side_effects=frozenset(
                {
                    "create_sheet",
                    "create_columns",
                    "create_rows",
                    "write_op",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_params",
                    "duplicate_sheet_name",
                    "idempotency_conflict",
                }
            ),
            cost_policy_kind="none",
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate(
                "invalid_input_ref_missing_column",
                _missing_column_action,
                "invalid_input_ref",
            ),
            Gate(
                "invalid_input_ref_object_source",
                _object_source_action,
                "invalid_input_ref",
            ),
            Gate(
                "invalid_item_schema_projection",
                _bad_projection_action,
                "invalid_item_schema",
            ),
            Gate(
                "duplicate_sheet_name",
                _duplicate_sheet_action,
                "duplicate_sheet_name",
                after_primary_run=True,
            ),
        ),
        expect_counts={
            "sheets": 1,
            "columns": 2,
            "rows": 2,
            "runs": 0,
            "results": 0,
            "ops": 1,
            "receipts": 1,
        },
        check_state=_check_state,
    )
]


@pytest.mark.parametrize("hidden", [False, True])
def test_declared_column_visibility_replays_until_changed(
    tmp_path, monkeypatch, hidden
):
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        request = _derive_action(
            env.seeded,
            columns=[
                {"name": "name", "path": "$.name", "type": "text"},
                {"name": "title", "path": "$.title", "type": "text", "hidden": hidden},
            ],
        )
        request["output_names"] = {"title": "Role"}
        first = env.run(request)
        assert first.status == "completed", first.errors
        ref = next(
            output.ref for output in first.outputs if output.ref.get("name") == "Role"
        )
        assert ref["hidden"] is hidden
        replay = env.run(request)
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id

        env.project.db.execute(
            "UPDATE columns SET hidden=? WHERE id=?", (not hidden, ref["column_id"])
        )
        env.project.db.commit()
        stale = env.run(request)
        assert stale.status == "failed"
        assert stale.errors[0].code == "stale_replay"


def test_child_sheet_replay_rejects_stale_materialized_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing a materialized child cell invalidates the recorded derivation:
    replaying the same spec is refused rather than silently diverging."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        child_sheet_id = next(
            output.sheet_id for output in first.outputs if output.kind == "sheet"
        )
        name_col = env.project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='name'",
            (child_sheet_id,),
        ).fetchone()
        first_child_row = env.project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position LIMIT 1",
            (child_sheet_id,),
        ).fetchone()
        env.project.apply_edits(
            [
                {
                    "row_id": int(first_child_row["id"]),
                    "column_id": int(name_col["id"]),
                    "value": "Mallory",
                }
            ]
        )
        replay = env.run_primary()
        assert replay.status == "failed"
        assert replay.errors[0].code == "stale_replay"


def test_empty_named_result_retains_parent_schema_and_replays_without_source_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.contracts.action import Receipt

    original = _map_named_results_action

    def empty_action(sheet_id):
        action = original(sheet_id)
        action["params"]["code"] = "result = {'entities': [], 'debug': {}}"
        return action

    monkeypatch.setitem(globals(), "_map_named_results_action", empty_action)
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        request = _derive_action(env.seeded)
        request["output_names"] = {"name": "Person"}
        first = env.run(request)
        assert first.status == "completed", first.errors
        child_sheet_id = next(
            output.sheet_id for output in first.outputs if output.kind == "sheet"
        )
        child = env.project.db.execute(
            "SELECT parent_sheet_id, parent_op_id, hidden FROM sheets WHERE id=?",
            (child_sheet_id,),
        ).fetchone()
        assert tuple(child) == (env.seeded["sheet_id"], first.op_ids[0], 0)
        columns = [
            tuple(column)
            for column in env.project.db.execute(
                "SELECT name, type, ai_generated FROM columns "
                "WHERE sheet_id=? ORDER BY position",
                (child_sheet_id,),
            )
        ]
        assert columns == [("Person", "text", 1), ("title", "text", 1)]
        assert env.project.row_count(child_sheet_id) == 0
        receipt_body = env.project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (first.receipt_id,)
        ).fetchone()["body"]
        receipt = Receipt.model_validate_json(receipt_body)
        read = next(
            item.ref for item in receipt.inputs if item.ref["kind"] == "list_table_read"
        )
        assert read["item_count"] == 0
        assert read["source_row_ids"] == env.seeded["source_row_ids"]

        # Discard GC removes immutable source results through their owning
        # generation; the already materialized child remains applied.
        env.project.db.execute(
            "UPDATE ops SET status='discarded' WHERE id="
            "(SELECT op_id FROM runs WHERE id=?)",
            (env.seeded["run_id"],),
        )
        env.project.db.commit()
        assert env.project.compact(vacuum=False)["results_pruned"] > 0
        assert (
            env.project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=? AND column_id=?",
                (env.seeded["run_id"], env.seeded["entities_column_id"]),
            ).fetchone()[0]
            == 0
        )
        before = env.counts()
        replay = env.run(request)
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert env.counts() == before
        assert (
            env.project.db.execute(
                "SELECT parent_sheet_id FROM sheets WHERE id=?", (child_sheet_id,)
            ).fetchone()[0]
            == env.seeded["sheet_id"]
        )
