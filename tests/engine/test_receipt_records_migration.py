"""Data-preserving upgrade for receipt record keys and Python route targets."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from frisket.engine.receipt_index import work_log_receipt_summaries
from frisket.engine.store import Project
from frisket.engine.store.project_blobs import _retained_payload_hashes
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.schema import (
    BundleSchemaMismatch,
    RECEIPT_RECORDS_META_KEY,
    RECEIPT_RECORDS_VERSION,
    SCHEMA_DIGEST,
    SCHEMA_DIGEST_META_KEY,
)
from frisket.upgrades.project.citations_migration import CITATIONS_TO_DIGEST
from frisket.upgrades.project.receipt_records_migration import (
    RECEIPT_RECORDS_TO_DIGEST,
)


def _downgrade_to_citations_digest(db: sqlite3.Connection) -> None:
    db.execute("BEGIN")
    try:
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
    assert SCHEMA_DIGEST == RECEIPT_RECORDS_TO_DIGEST
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
