from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from executor_harness import CatalogEntry, ExecutorCase, Gate, case_env
from frisket.engine.store import Project
from tests.action_test_helpers import run_typed_map_request, typed_map_request

_COUNT_TABLES = ("sheets", "columns", "rows", "runs", "results", "ops", "receipts")


def _run_seed_action(project: Project, action: dict[str, Any]) -> Any:
    if "action_id" in action:
        result = run_typed_map_request(
            project, action, project_id="project-export-work-log"
        )
        assert result.status == "completed", result.errors
        return result

    from frisket.engine.executor import run_action_spec

    result = run_action_spec(project, action, project_id="project-export-work-log")
    assert result.status == "completed", result.errors
    return result


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
                "label": "export seed",
                "fingerprint": "sha256:export-seed",
            },
        },
        "idempotency_key": "export_seed@sha256:v1",
    }


def _map_named_results_action(sheet_id: int) -> dict[str, Any]:
    code = "\n".join(
        [
            "words = row['transcript'].split()",
            "person = words[0]",
            "result = {",
            "    'entities': [{'name': person, 'title': 'speaker'}],",
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
                "required": ["entities"],
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
            ],
        },
        "idempotency_key": "export_source_map@sha256:v1",
    }


def _derive_action(*, sheet_id: int, column_id: int, run_id: int) -> dict[str, Any]:
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
        "idempotency_key": "export_derive_entities@sha256:v1",
    }


def _export_action(
    path: Path,
    *,
    key: str = "export_work_log@sha256:v1",
) -> dict[str, Any]:
    return {
        "action_id": "export.work_log",
        "scope": {"kind": "project"},
        "params": {
            "destination": {"kind": "local_file", "path": str(path)},
            "include_receipts": True,
        },
        "idempotency_key": key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    """A work log worth exporting: import -> map to named results -> derive a
    table -> undo -> redo, so the artifact covers every operation family."""
    from executor_harness import operation_action

    _run_seed_action(project, _seed_import_action())
    sheet_id = int(
        project.db.execute("SELECT id FROM sheets WHERE name='Transcripts'").fetchone()[
            "id"
        ]
    )
    mapped = _run_seed_action(project, _map_named_results_action(sheet_id))
    entities_column_id = int(
        project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='__result_entities'",
            (sheet_id,),
        ).fetchone()["id"]
    )
    derived = _run_seed_action(
        project,
        _derive_action(
            sheet_id=sheet_id,
            column_id=entities_column_id,
            run_id=mapped.run_id,
        ),
    )
    derive_op_id = derived.op_ids[0]
    _run_seed_action(
        project,
        operation_action(
            "operation.undo",
            key="export_undo_derive@sha256:v1",
            expected_op_id=derive_op_id,
        ),
    )
    _run_seed_action(
        project,
        operation_action(
            "operation.redo",
            key="export_redo_derive@sha256:v1",
            expected_op_id=derive_op_id,
        ),
    )
    out_dir = tmp_path / "artifacts"
    out_dir.mkdir()
    return {"dir": tmp_path, "out": out_dir / "work-log.md"}


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _export_action(seeded["out"])


def _invalid_destination_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _export_action(
        seeded["dir"] / "missing-parent" / "work-log.md",
        key="export_invalid_destination@sha256:v1",
    )


def _conflicting_destination_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same idempotency key as the primary export, different destination.
    return _export_action(seeded["dir"] / "other.md")


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    assert result.op_ids == []
    output_path: Path = seeded["out"]
    export_output = next(output for output in result.outputs if output.kind == "export")
    assert export_output.ref["kind"] == "export_artifact"
    assert export_output.ref["format"] == "markdown"
    assert export_output.ref["path"] == str(output_path)
    assert export_output.ref["byte_count"] == output_path.stat().st_size
    assert export_output.ref["sha256"].startswith("sha256:")

    text = output_path.read_text(encoding="utf-8")
    assert text.startswith("# Work log:")
    assert "| Transcripts | 2 | 2 |" in text
    assert "| Entities | 2 | 2 |" in text
    for kind in (
        "import.rows",
        "map.python",
        "derive.table_from_list",
        "operation.undo",
        "operation.redo",
        "export.work_log",
    ):
        assert kind in text
    assert "lineage_parent_rows" in text
    assert "operation_status_transition" in text

    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "export.work_log"
    assert receipt.outputs[0].ref["kind"] == "export_artifact"
    assert receipt.exports[0]["path"] == str(output_path)
    assert {item.ref["kind"] for item in receipt.evidence} >= {
        "exported_receipts",
        "exported_operations",
        "exported_sheets",
    }


CASES = [
    ExecutorCase(
        kind="export.work_log",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:read", "project:write"),
            side_effects=frozenset(
                {
                    "read_project_state",
                    "write_export_artifact",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_export_destination",
                    "export_artifact_missing",
                    "export_artifact_mismatch",
                    "idempotency_conflict",
                    "project_write_failed",
                }
            ),
            cost_policy_kind="none",
        ),
        seed=_seed,
        make_action=_make_action,
        request_style="typed",
        gates=(
            Gate(
                "invalid_export_destination",
                _invalid_destination_action,
                "invalid_export_destination",
                no_writes=False,  # The callable host records the failed invocation.
            ),
            Gate(
                "idempotency_conflict",
                _conflicting_destination_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
        ),
        expect_counts={table: 0 for table in _COUNT_TABLES} | {"receipts": 1},
        check_state=_check_state,
    )
]


def _regex_extract_action(sheet_id: int) -> dict[str, Any]:
    return typed_map_request(
        "map.regex_extract",
        sheet_id,
        params={"input_columns": ["transcript"], "pattern": r"[A-Z][a-z]+"},
        output_names={"extracted": "speaker"},
        idempotency_key="export_work_log_regex@sha256:v1",
    )


def _row_delete_action(sheet_id: int, row_ids: list[int]) -> dict[str, Any]:
    return {
        "action_id": "row.delete",
        "scope": {"kind": "project"},
        "params": {"sheet_id": sheet_id, "row_ids": row_ids},
        "idempotency_key": "export_work_log_delete@sha256:v1",
    }


def test_work_log_payload_omits_deleted_rows_and_hidden_columns(tmp_path: Path) -> None:
    """The work log reads what the sheet reads.

    A deleted row (``rows.hidden``) and a plumbing column (``columns.hidden``,
    e.g. map.python's ``__result_entities``) are invisible on the normal read
    path, so an exported-and-re-emitted work log must not resurrect either.
    """
    from frisket.server.exports.work_log import build_work_log_payload

    project = Project.create(tmp_path / "work-log-hidden.frisket", name="Hidden")
    try:
        _run_seed_action(project, _seed_import_action())
        sheet_id = int(
            project.db.execute(
                "SELECT id FROM sheets WHERE name='Transcripts'"
            ).fetchone()["id"]
        )
        _run_seed_action(project, _regex_extract_action(sheet_id))
        _run_seed_action(project, _map_named_results_action(sheet_id))

        hidden_column = project.db.execute(
            "SELECT hidden FROM columns WHERE sheet_id=? AND name='__result_entities'",
            (sheet_id,),
        ).fetchone()
        assert hidden_column is not None and hidden_column["hidden"] == 1

        deleted_row_id = int(
            project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
            ).fetchall()[0]["id"]
        )
        _run_seed_action(project, _row_delete_action(sheet_id, [deleted_row_id]))
        assert project.row_count(sheet_id) == 1

        payload = build_work_log_payload(
            project, "project-export-work-log", include_receipts=False
        )
        runs = {run["action_kind"]: run for run in payload["runs"]}

        assert runs["map.python"]["outputs"] == []
        assert runs["map.python"]["examples"] == []
        assert runs["map.regex_extract"]["outputs"] == ["speaker"]
        surviving = runs["map.regex_extract"]["examples"]
        assert [example["value"] for example in surviving] == ["Bob"]
        # The op-log label is a separate surface (project.history() shows it
        # too); the run rows are the ones that dumped cell values.
        assert "Alice" not in json.dumps(payload)
        assert "__result_entities" not in json.dumps(payload["runs"])
    finally:
        project.close()


def test_export_work_log_artifact_trust_on_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay re-verifies the artifact bytes: identical bytes replay clean,
    tampered bytes fail as a mismatch, a deleted file fails as missing."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        sha = next(o for o in first.outputs if o.kind == "export").ref["sha256"]
        after = env.counts()

        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert (
            next(o for o in replay.outputs if o.kind == "export").ref["sha256"] == sha
        )

        output_path: Path = env.seeded["out"]
        output_path.write_text("tampered\n", encoding="utf-8")
        tampered = env.run_primary()
        assert tampered.status == "failed"
        assert tampered.errors[0].code == "export_artifact_mismatch"
        assert env.counts() == after

        output_path.unlink()
        missing = env.run_primary()
        assert missing.status == "failed"
        assert missing.errors[0].code == "export_artifact_missing"
        assert env.counts() == after
