"""Data-preserving upgrade for receipt record keys and Python route targets."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from typing import Any

import pytest

from frisket.actions.python_types import ReceiptRecordTarget
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.map_rows_action import _typed_replay_error
from frisket.engine.receipt_index import work_log_receipt_summaries
from frisket.engine.store import Project
from frisket.engine.store.action_submissions import record_submission
from frisket.engine.store.project_blobs import _retained_payload_hashes
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.schema import (
    BundleSchemaMismatch,
    RECEIPT_RECORDS_META_KEY,
    RECEIPT_RECORDS_VERSION,
    SCHEMA_DIGEST,
    SCHEMA_DIGEST_META_KEY,
)
from frisket.server.services.action_history import editable_action_draft
from frisket.upgrades.project._tip import CURRENT_CHAIN_TIP_DIGEST
from frisket.upgrades.project.citations_migration import CITATIONS_TO_DIGEST
from tests.engine.test_generated_columns_migration import (
    _downgrade_generated_columns_schema,
)


def _downgrade_to_citations_digest(db: sqlite3.Connection) -> None:
    db.execute("BEGIN")
    try:
        _downgrade_generated_columns_schema(db)
        db.execute("DELETE FROM meta WHERE key=?", (RECEIPT_RECORDS_META_KEY,))
        db.execute(
            "UPDATE meta SET value=? WHERE key=?",
            (CITATIONS_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def test_fresh_bundle_stamps_receipt_records_format(make_project) -> None:
    project = make_project("fresh-receipt-records")
    assert project.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
    assert SCHEMA_DIGEST == CURRENT_CHAIN_TIP_DIGEST
    assert project.get_meta(RECEIPT_RECORDS_META_KEY) == RECEIPT_RECORDS_VERSION


def test_receipt_records_upgrade_rewrites_receipt_bodies_and_python_routes(
    make_project,
) -> None:
    project = make_project("receipt-records-upgrade")
    sheet_id = project.add_sheet("Documents")
    hidden_col_id = project.add_column(
        sheet_id,
        "__evidence_debug",
        type="json",
        hidden=True,
    )
    digest = "a" * 64

    op_id = int(
        project.db.execute(
            "INSERT INTO ops (kind, label, spec, status) VALUES (?, ?, ?, 'applied')",
            (
                "map.python",
                "Python route",
                json.dumps(
                    {
                        "params": {
                            "output_routes": [
                                {
                                    "name": "debug",
                                    "path": "$",
                                    "target": {
                                        "kind": "receipt_evidence",
                                        "retention": "pinned",
                                    },
                                }
                            ]
                        }
                    }
                ),
            ),
        ).lastrowid
        or 0
    )
    run_id = int(
        project.db.execute(
            "INSERT INTO runs (op_id, sheet_id, action_kind, params, status) "
            "VALUES (?, ?, 'map.python', ?, 'completed')",
            (
                op_id,
                sheet_id,
                json.dumps(
                    {
                        "output_routes": [
                            {
                                "name": "debug",
                                "path": "$",
                                "target": {
                                    "kind": "receipt_evidence",
                                    "retention": "pinned",
                                },
                            }
                        ]
                    }
                ),
            ),
        ).lastrowid
        or 0
    )

    legacy_python_body = {
        "schema_version": "frisket.receipt.v1",
        "receipt_id": "rcpt_python",
        "project_id": "proj_1",
        "action_id": "act_1",
        "action_kind": "map.python",
        "run_id": run_id,
        "op_ids": [op_id],
        "idempotency_key": "k-python",
        "params_hash": "sha256:abc",
        "status": "completed",
        "inputs": [],
        "outputs": [],
        "value": None,
        "provider_use": [],
        "evidence": [
            {
                "ref": {
                    "kind": "map_python_receipt_evidence",
                    "route": "debug",
                    "path": "$",
                    "sheet_id": sheet_id,
                    "column_id": hidden_col_id,
                    "retention": "pinned",
                    "op_id": op_id,
                    "run_id": run_id,
                },
                "retention": "pinned",
            },
            {
                "ref": {
                    "kind": "typed_action_request",
                    "params": {
                        "output_routes": [
                            {
                                "name": "debug",
                                "path": "$",
                                "target": {
                                    "kind": "receipt_evidence",
                                    "retention": "pinned",
                                },
                            }
                        ]
                    },
                },
                "retention": "compactable",
            },
            {
                "ref": {
                    "kind": "row_file_output",
                    "primary": {"blob_hash": digest},
                    "supplemental": [],
                },
                "retention": "pinned",
            },
        ],
        "trace_ref": None,
        "review": None,
        "exports": [],
        "warnings": [],
        "errors": [],
    }
    legacy_export_body = {
        "schema_version": "frisket.receipt.v1",
        "receipt_id": "rcpt_export",
        "project_id": "proj_1",
        "action_id": "act_2",
        "action_kind": "export.work_log",
        "run_id": None,
        "op_ids": [],
        "idempotency_key": None,
        "params_hash": "sha256:def",
        "status": "completed",
        "inputs": [],
        "outputs": [],
        "value": None,
        "provider_use": [],
        "evidence": [
            {
                "ref": {
                    "kind": "exported_receipts",
                    "receipts": [
                        {
                            "receipt_id": "rcpt_python",
                            "action_kind": "map.python",
                            "outputs": [],
                            "evidence": ["map_python_receipt_evidence"],
                        }
                    ],
                },
                "retention": "pinned",
            }
        ],
        "trace_ref": None,
        "review": None,
        "exports": [],
        "warnings": [],
        "errors": [],
    }

    project.db.execute(
        "INSERT INTO receipts "
        "(id, run_id, action_kind, action_id, idempotency_key, params_hash, status, body) "
        "VALUES (?, ?, ?, ?, ?, ?, 'completed', ?)",
        (
            "rcpt_python",
            run_id,
            "map.python",
            "act_1",
            "k-python",
            "sha256:abc",
            json.dumps(legacy_python_body),
        ),
    )
    project.db.execute(
        "INSERT INTO receipts "
        "(id, run_id, action_kind, action_id, idempotency_key, params_hash, status, body) "
        "VALUES (?, NULL, ?, ?, NULL, ?, 'completed', ?)",
        (
            "rcpt_export",
            "export.work_log",
            "act_2",
            "sha256:def",
            json.dumps(legacy_export_body),
        ),
    )
    project.db.execute(
        "INSERT INTO receipts "
        "(id, run_id, action_kind, action_id, idempotency_key, params_hash, status, body) "
        "VALUES ('rcpt_purged', NULL, 'import.rows', 'act_3', NULL, 'sha256:ghi', 'completed', '{}')"
    )
    project.db.commit()

    _downgrade_to_citations_digest(project.db)
    bundle_path = project.path
    project.close()

    with closing(Project(bundle_path)) as upgraded:
        assert upgraded.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
        assert upgraded.get_meta(RECEIPT_RECORDS_META_KEY) == RECEIPT_RECORDS_VERSION

        parsed_python = ReceiptStore(upgraded).require_parsed("rcpt_python")
        assert [item.ref["kind"] for item in parsed_python.records] == [
            "map_python_receipt_record",
            "typed_action_request",
            "row_file_output",
        ]
        assert parsed_python.records[0].retention == "pinned"
        assert (
            parsed_python.records[1].ref["params"]["output_routes"][0]["target"]["kind"]
            == "receipt_record"
        )

        parsed_export = ReceiptStore(upgraded).require_parsed("rcpt_export")
        exported_entry = parsed_export.records[0].ref["receipts"][0]
        assert "evidence" not in exported_entry
        assert exported_entry["records"] == ["map_python_receipt_evidence"]

        purged_raw = ReceiptStore(upgraded).body_by_id("rcpt_purged")
        assert purged_raw == "{}"

        col_name = upgraded.db.execute(
            "SELECT name FROM columns WHERE id=?", (hidden_col_id,)
        ).fetchone()[0]
        assert col_name == "__record_debug"

        run_params = json.loads(
            upgraded.db.execute(
                "SELECT params FROM runs WHERE id=?", (run_id,)
            ).fetchone()[0]
        )
        assert run_params["output_routes"][0]["target"]["kind"] == "receipt_record"

        op_spec = json.loads(
            upgraded.db.execute("SELECT spec FROM ops WHERE id=?", (op_id,)).fetchone()[
                0
            ]
        )
        assert (
            op_spec["params"]["output_routes"][0]["target"]["kind"] == "receipt_record"
        )

        assert digest in set(_retained_payload_hashes(upgraded.db))

        summaries = {item["id"]: item for item in work_log_receipt_summaries(upgraded)}
        assert summaries["rcpt_python"]["records"] == [
            "map_python_receipt_record",
            "row_file_output",
            "typed_action_request",
        ]


def _map_python_request(sheet_id: int, *, key: str) -> dict[str, Any]:
    return {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "input_columns": ["title"],
            "code": (
                "result = {'length': len(str(row['title'])), "
                "'debug': {'len': len(str(row['title']))}}"
            ),
            "return_schema": {
                "type": "object",
                "required": ["length", "debug"],
                "properties": {
                    "length": {"type": "integer"},
                    "debug": {"type": "object"},
                },
            },
            "output_routes": [
                {
                    "name": "length",
                    "path": "$.length",
                    "target": {"kind": "column", "type": "integer"},
                },
                {
                    "name": "debug",
                    "path": "$.debug",
                    "target": {"kind": "receipt_record", "retention": "pinned"},
                },
            ],
        },
        "idempotency_key": key,
    }


def _downgrade_map_python_to_legacy(
    project: Project,
    *,
    receipt_id: str,
    run_id: int,
    op_id: int,
    hidden_col_id: int,
) -> None:
    db = project.db
    db.execute(
        "UPDATE columns SET name='__evidence_debug' WHERE id=?",
        (hidden_col_id,),
    )
    db.execute(
        "UPDATE run_review_fields SET column_name='__evidence_debug' "
        "WHERE column_id=? AND column_name='__record_debug'",
        (hidden_col_id,),
    )
    db.execute(
        "UPDATE generated_column_claims SET output_name='__evidence_debug' "
        "WHERE column_id=? AND output_name='__record_debug'",
        (hidden_col_id,),
    )

    raw_body = db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()[0]
    body = json.loads(raw_body)
    records = body.pop("records")
    for item in records:
        ref = item.get("ref", {})
        if ref.get("kind") == "map_python_receipt_record":
            ref["kind"] = "map_python_receipt_evidence"
        elif (
            ref.get("kind") == "typed_hidden_output"
            and ref.get("column_id") == hidden_col_id
        ):
            ref["name"] = "__evidence_debug"
        elif ref.get("kind") == "typed_action_request":
            for route in ref.get("params", {}).get("output_routes", []):
                if route.get("target", {}).get("kind") == "receipt_record":
                    route["target"]["kind"] = "receipt_evidence"
    body["evidence"] = records
    db.execute(
        "UPDATE receipts SET body=? WHERE id=?",
        (json.dumps(body, sort_keys=True), receipt_id),
    )

    for table, column, row_id in (("runs", "params", run_id), ("ops", "spec", op_id)):
        raw = db.execute(
            f"SELECT {column} FROM {table} WHERE id=?", (row_id,)
        ).fetchone()[0]
        rewritten = raw.replace('"receipt_record"', '"receipt_evidence"').replace(
            '"__record_debug"', '"__evidence_debug"'
        )
        db.execute(
            f"UPDATE {table} SET {column}=? WHERE id=?",
            (rewritten, row_id),
        )
    db.commit()


def test_migrated_map_python_run_keeps_hidden_output_and_replay_valid(
    make_project,
) -> None:
    project = make_project("map-python-replay-upgrade")
    sheet_id = project.add_sheet("Items")
    title_col = project.add_column(sheet_id, "title", type="text")
    project.add_rows(
        sheet_id, [{"title": "Alpha"}, {"title": "Beta"}], {"title": title_col}
    )

    request = _map_python_request(sheet_id, key="map_python_upgrade@sha256:v1")
    result = run_action_spec(project, request, project_id="proj-replay")
    assert result.status == "completed", result.errors
    assert result.receipt_id is not None and result.run_id is not None
    op_id = result.op_ids[0]
    hidden_col_id = int(
        project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='__record_debug'",
            (sheet_id,),
        ).fetchone()[0]
    )

    _downgrade_map_python_to_legacy(
        project,
        receipt_id=result.receipt_id,
        run_id=result.run_id,
        op_id=op_id,
        hidden_col_id=hidden_col_id,
    )
    _downgrade_to_citations_digest(project.db)
    bundle_path = project.path
    project.close()

    with closing(Project(bundle_path)) as upgraded:
        assert upgraded.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
        col_name = upgraded.db.execute(
            "SELECT name FROM columns WHERE id=?", (hidden_col_id,)
        ).fetchone()[0]
        assert col_name == "__record_debug"

        migrated = ReceiptStore(upgraded).require_parsed(result.receipt_id)
        hidden_ref = next(
            item.ref
            for item in migrated.records
            if item.ref.get("kind") == "typed_hidden_output"
        )
        assert hidden_ref["column_id"] == hidden_col_id
        assert hidden_ref["name"] == "__record_debug"

        review_rows = upgraded.db.execute(
            "SELECT column_name FROM run_review_fields WHERE column_id=?",
            (hidden_col_id,),
        ).fetchall()
        assert all(row[0] == "__record_debug" for row in review_rows)
        claim_rows = upgraded.db.execute(
            "SELECT output_name FROM generated_column_claims WHERE column_id=?",
            (hidden_col_id,),
        ).fetchall()
        assert all(row[0] == "__record_debug" for row in claim_rows)

        run_params = json.loads(
            upgraded.db.execute(
                "SELECT params FROM runs WHERE id=?", (result.run_id,)
            ).fetchone()[0]
        )
        assert run_params["output_names"]["debug"] == "__record_debug"
        if "output_target_preconditions" in run_params:
            assert "__evidence_debug" not in run_params["output_target_preconditions"]
            assert "__record_debug" in run_params["output_target_preconditions"]

        op_spec = json.loads(
            upgraded.db.execute("SELECT spec FROM ops WHERE id=?", (op_id,)).fetchone()[
                0
            ]
        )
        assert op_spec["output_names"]["debug"] == "__record_debug"
        if "output_target_preconditions" in op_spec:
            assert "__evidence_debug" not in op_spec["output_target_preconditions"]
            assert "__record_debug" in op_spec["output_target_preconditions"]

        bound = typed_action_for_request(request)
        assert _typed_replay_error(upgraded, bound)(migrated) is None


def test_migrated_map_python_name_conflict_keeps_column_and_receipt_aligned(
    make_project,
) -> None:
    project = make_project("map-python-conflict-upgrade")
    sheet_id = project.add_sheet("Items")
    title_col = project.add_column(sheet_id, "title", type="text")
    project.add_rows(sheet_id, [{"title": "Alpha"}], {"title": title_col})

    request = _map_python_request(sheet_id, key="map_python_conflict@sha256:v1")
    result = run_action_spec(project, request, project_id="proj-conflict")
    assert result.status == "completed", result.errors
    assert result.receipt_id is not None and result.run_id is not None
    op_id = result.op_ids[0]
    hidden_col_id = int(
        project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='__record_debug'",
            (sheet_id,),
        ).fetchone()[0]
    )

    _downgrade_map_python_to_legacy(
        project,
        receipt_id=result.receipt_id,
        run_id=result.run_id,
        op_id=op_id,
        hidden_col_id=hidden_col_id,
    )
    # Create an active conflicting column with the target name before upgrade.
    project.add_column(sheet_id, "__record_debug", type="text")
    _downgrade_to_citations_digest(project.db)
    bundle_path = project.path
    project.close()

    with closing(Project(bundle_path)) as upgraded:
        assert upgraded.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
        col_name = upgraded.db.execute(
            "SELECT name FROM columns WHERE id=?", (hidden_col_id,)
        ).fetchone()[0]
        assert col_name == "__evidence_debug"

        migrated = ReceiptStore(upgraded).require_parsed(result.receipt_id)
        hidden_ref = next(
            item.ref
            for item in migrated.records
            if item.ref.get("kind") == "typed_hidden_output"
        )
        assert hidden_ref["column_id"] == hidden_col_id
        assert hidden_ref["name"] == "__evidence_debug"

        run_params = json.loads(
            upgraded.db.execute(
                "SELECT params FROM runs WHERE id=?", (result.run_id,)
            ).fetchone()[0]
        )
        assert run_params["output_names"]["debug"] == "__evidence_debug"

        bound = typed_action_for_request(request)
        assert _typed_replay_error(upgraded, bound)(migrated) is None


def test_receipt_records_upgrade_rewrites_action_submissions_drafts(
    make_project,
) -> None:
    project = make_project("action-submissions-upgrade")
    sheet_id = project.add_sheet("Items")
    legacy_draft = {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "input_columns": ["title"],
            "code": "result = {'debug': row['title']}",
            "return_schema": {
                "type": "object",
                "required": ["debug"],
                "properties": {"debug": {"type": "string"}},
            },
            "output_routes": [
                {
                    "name": "debug",
                    "path": "$.debug",
                    "target": {"kind": "receipt_evidence", "retention": "pinned"},
                }
            ],
        },
        "output_names": {},
    }
    sub_id = record_submission(project.db, legacy_draft, request_key="req-python-1")
    # Also insert a colliding row with the already-migrated hash on the same request_key
    # so UNIQUE(request_key, draft_hash) collision safety is exercised directly.
    migrated_draft = json.loads(
        json.dumps(legacy_draft).replace('"receipt_evidence"', '"receipt_record"')
    )
    existing_new_id = record_submission(
        project.db, migrated_draft, request_key="req-python-conflict"
    )
    conflict_legacy_id = record_submission(
        project.db, legacy_draft, request_key="req-python-conflict"
    )

    _downgrade_to_citations_digest(project.db)
    bundle_path = project.path
    project.close()

    with closing(Project(bundle_path)) as upgraded:
        assert upgraded.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
        row = upgraded.db.execute(
            "SELECT draft_hash, draft_json FROM action_submissions WHERE id=?",
            (sub_id,),
        ).fetchone()
        assert row is not None
        expected_encoded = json.dumps(
            migrated_draft, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        expected_hash = hashlib.sha256(expected_encoded.encode()).hexdigest()
        assert row[0] == expected_hash
        restored = json.loads(row[1])
        assert restored == migrated_draft
        restored_draft = editable_action_draft(restored)
        assert restored_draft.action_id == "map.python"
        assert restored_draft.params == migrated_draft["params"]
        bound = typed_action_for_request(
            {**restored, "idempotency_key": "restored-submission@sha256:v1"}
        )
        assert isinstance(bound.params.output_routes[0].target, ReceiptRecordTarget)

        # Colliding submission row is also updated in draft_json without IntegrityError.
        conflict_row = upgraded.db.execute(
            "SELECT draft_json FROM action_submissions WHERE id=?",
            (conflict_legacy_id,),
        ).fetchone()
        assert conflict_row is not None
        assert json.loads(conflict_row[0]) == migrated_draft
        assert (
            upgraded.db.execute(
                "SELECT id FROM action_submissions WHERE id=?", (existing_new_id,)
            ).fetchone()
            is not None
        )


def test_receipt_records_upgrade_fails_closed_on_unknown_digest(make_project) -> None:
    project = make_project("receipt-records-unknown-digest")
    project.db.execute(
        "UPDATE meta SET value=? WHERE key=?",
        ("frisket.schema.v1:00000000000000000000000000000000", SCHEMA_DIGEST_META_KEY),
    )
    project.db.commit()
    bundle_path = project.path
    project.close()

    with pytest.raises(BundleSchemaMismatch):
        Project(bundle_path)
