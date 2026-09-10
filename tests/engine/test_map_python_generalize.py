"""map.python generalized onto the reserved-maprunner skeleton (2026-06-27).

Asserts the NEW contract that the redesign establishes:
  (a) column + named_result routes write columns, and the named_result feeds
      derive.table_from_list (the generic Provenance.named_result_refs axis);
  (b) a receipt_evidence route now produces a HIDDEN column + a LIGHTWEIGHT receipt
      ref (column_id + path + retention), NOT per-row value blobs in the receipt body;
  (c) reservation-based idempotency — a duplicate while the first run is still RESERVED
      yields idempotency_in_progress; and build_reserved_spec(PYTHON_OP) carries no
      resolve_override / write_override (the generic skeleton owns them).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from frisket.contracts.action import Receipt
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project


PROJECT_ID = "project-map-python-generalize"


def _seed(project_path: Path) -> tuple[Project, int, list[int]]:
    project = Project.create(project_path, name="Map Python Generalize")
    sheet_id = project.add_sheet("Transcripts")
    columns = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "transcript": project.add_column(sheet_id, "transcript", type="text"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "Episode 1",
                "transcript": "Alice founded Newsroom Labs in Brooklyn",
            },
            {"title": "Episode 2", "transcript": "Bob joined Civic Data in Queens"},
        ],
        columns,
    )
    return project, sheet_id, row_ids


def _map_python_action(sheet_id: int, *, key: str) -> dict[str, Any]:
    code = "\n".join(
        [
            "words = row['transcript'].split()",
            "person = words[0]",
            "result = {",
            "    'word_count': len(words),",
            "    'entities': [{'name': person, 'kind': 'person'}],",
            "    'debug': {'first_word': person, 'title': row['title']},",
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
                "required": ["word_count", "entities", "debug"],
                "properties": {
                    "word_count": {"type": "integer"},
                    "entities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["name", "kind"],
                            "properties": {
                                "name": {"type": "string"},
                                "kind": {"type": "string"},
                            },
                        },
                    },
                    "debug": {"type": "object"},
                },
            },
            "output_routes": [
                {
                    "name": "word_count",
                    "path": "$.word_count",
                    "target": {
                        "kind": "column",
                        "type": "integer",
                    },
                },
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
                    "name": "debug",
                    "path": "$.debug",
                    "target": {"kind": "receipt_evidence", "retention": "compactable"},
                },
            ],
        },
        "idempotency_key": key,
    }


def _derive_action(
    *, sheet_id: int, column_id: int, run_id: int, key: str
) -> dict[str, Any]:
    return {
        "action_id": "derive.table_from_list",
        "scope": {"kind": "project"},
        "sheet_name": "Entities",
        "params": {
            "source": {
                "kind": "named_result",
                "sheet_id": sheet_id,
                "column_id": column_id,
                "run_id": run_id,
                "route": "entities",
                "schema": "entity_list",
            },
            "item_schema": {
                "type": "object",
                "required": ["name", "kind"],
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string"},
                },
            },
            "columns": [
                {"name": "name", "path": "$.name", "type": "text"},
                {"name": "kind", "path": "$.kind", "type": "text"},
            ],
        },
        "idempotency_key": key,
    }


def _receipt(project: Project, receipt_id: str) -> Receipt:
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    assert row is not None
    return Receipt.model_validate(json.loads(row["body"]))


def test_column_and_named_result_routes_and_table_from_list(tmp_path: Path) -> None:
    project, sheet_id, row_ids = _seed(tmp_path / "a.frisket")
    try:
        result = run_action_spec(
            project,
            _map_python_action(sheet_id, key="map_python@sha256:a"),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors
        # column target -> visible column output; named_result -> named_result output.
        assert {o.kind for o in result.outputs} == {"column", "named_result"}
        assert {o.name for o in result.outputs} == {"word_count", "entities"}

        word_col = project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? AND name='word_count'", (sheet_id,)
        ).fetchone()
        assert word_col["hidden"] == 0
        assert word_col["type"] == "integer"
        assert list(project.get_values(sheet_id, int(word_col["id"])).values()) == [
            6,
            6,
        ]

        # named_result -> hidden __result_entities column holding the per-row lists.
        named_col = project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? AND name='__result_entities'",
            (sheet_id,),
        ).fetchone()
        assert named_col["hidden"] == 1
        assert named_col["type"] == "json"
        assert list(project.get_values(sheet_id, int(named_col["id"])).values())[0] == [
            {"name": "Alice", "kind": "person"}
        ]

        # the named_result feeds derive.table_from_list.
        derived = run_action_spec(
            project,
            _derive_action(
                sheet_id=sheet_id,
                column_id=int(named_col["id"]),
                run_id=result.run_id,
                key="derive@sha256:a",
            ),
            project_id=PROJECT_ID,
        )
        assert derived.status == "completed", derived.errors
        entities_sheet = project.db.execute(
            "SELECT id FROM sheets WHERE name='Entities'"
        ).fetchone()
        assert entities_sheet is not None
        n_rows = project.db.execute(
            "SELECT COUNT(*) FROM rows WHERE sheet_id=? AND hidden=0",
            (int(entities_sheet["id"]),),
        ).fetchone()[0]
        assert n_rows == 2
    finally:
        project.close()


def test_receipt_evidence_route_is_hidden_column_plus_lightweight_ref(
    tmp_path: Path,
) -> None:
    project, sheet_id, _ = _seed(tmp_path / "b.frisket")
    try:
        result = run_action_spec(
            project,
            _map_python_action(sheet_id, key="map_python@sha256:b"),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors

        # receipt_evidence -> a HIDDEN __evidence_debug column holding the per-row values.
        evidence_col = project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? AND name='__evidence_debug'",
            (sheet_id,),
        ).fetchone()
        assert evidence_col is not None
        assert evidence_col["hidden"] == 1
        first = list(project.get_values(sheet_id, int(evidence_col["id"])).values())[0]
        assert first == {"first_word": "Alice", "title": "Episode 1"}
        # the evidence route is NOT projected as a user-facing output.
        assert "debug" not in {o.name for o in result.outputs}

        receipt = _receipt(project, result.receipt_id)
        ev = [
            item.ref
            for item in receipt.evidence
            if item.ref["kind"] == "map_python_receipt_evidence"
        ]
        assert len(ev) == 1
        ref = ev[0]
        # LIGHTWEIGHT: a column_id + path + retention pointer, NOT per-row value blobs.
        assert ref["route"] == "debug"
        assert ref["path"] == "$.debug"
        assert ref["column_id"] == int(evidence_col["id"])
        assert ref["retention"] == "compactable"
        assert "values" not in ref
    finally:
        project.close()


def test_reservation_based_idempotency_in_progress(tmp_path: Path) -> None:
    project, sheet_id, _ = _seed(tmp_path / "c.frisket")
    try:
        # Simulate a still-running first action by leaving a 'running' reservation in
        # place (the reserved skeleton writes one before the sandbox runs), then issue a
        # duplicate with the same key.
        from frisket.engine.executor.action_reservations import _receipt_for_idempotency

        first = run_action_spec(
            project,
            _map_python_action(sheet_id, key="map_python@sha256:c"),
            project_id=PROJECT_ID,
        )
        assert first.status == "completed", first.errors

        # Force the stored receipt back into the running state to model an in-flight run.
        existing = _receipt_for_idempotency(project, "map_python@sha256:c")
        assert existing is not None
        project.db.execute(
            "UPDATE receipts SET status='running' WHERE id=?", (existing["id"],)
        )
        project.db.commit()

        dup = run_action_spec(
            project,
            _map_python_action(sheet_id, key="map_python@sha256:c"),
            project_id=PROJECT_ID,
        )
        assert dup.status == "failed"
        assert dup.errors[0].code == "idempotency_in_progress"
    finally:
        project.close()


def test_reserved_spec_has_no_resolve_or_write_override() -> None:
    from frisket.actions.python import PYTHON
    from frisket.actions.core import MapRows
    from frisket.actions.registry import ACTION_REGISTRY

    assert isinstance(PYTHON.run, MapRows)
    assert ACTION_REGISTRY.get("map.python").definition is PYTHON
