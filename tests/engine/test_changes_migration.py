"""Dedicated tests for the data-preserving ops -> changes project schema migration."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from frisket.contracts.action import Receipt
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.store import Project
from frisket.engine.store.changes import start_change
from frisket.engine.store.consumed_sources import (
    _descriptors,
    _hash,
    _members,
    _records,
    authority_revisions,
    capture_fingerprint,
    row_input_state,
    verify_bundle,
)
from frisket.engine.store.feeds import FeedStore
from frisket.engine.store.effect_checkpoints import EffectCheckpointStore
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY
from frisket.engine.store.staleness import compute_sync_states
from frisket.engine.store.transactions import write_transaction
from frisket.server.services.project_qa_citations import resolve_citation
from frisket.server.services.project_qa_sources import read_source_text
from frisket.upgrades.project._tip import CURRENT_CHAIN_TIP_DIGEST
from frisket.upgrades.project.changes_migration import (
    CHANGES_FROM_DIGEST,
    CHANGES_TO_DIGEST,
    _rewrite_locator,
    _rewrite_receipt_body,
    _rewrite_submission_draft,
)
from tests.engine.historical_schema import restore_pre_changes_schema


@pytest.mark.parametrize("case", ["reserved", "returned", "provider_error"])
def test_upgrade_retains_embedding_refresh_checkpoint_accounting_owner(
    tmp_path: Path, case: str
) -> None:
    bundle_path = tmp_path / "embedding-refresh.frisket"
    with closing(Project.create(bundle_path, name="Embedding refresh")) as seed:
        sheet_id = seed.add_sheet("Records")
        change_id = seed.append_change("embedding.index_refresh", {})
        run_id = RunResultStore(seed).start_run(
            change_id, sheet_id, "embedding.index_refresh", total_rows=1
        )
    # The pre-cutover producer stores accounting ownership in effect, for
    # both the reservation and the durable provider response/error.
    effect = {"op_id": change_id, "run_id": run_id, "attempt_id": "attempt-1"}
    payload = {"effect": effect}
    if case == "returned":
        payload.update(
            {
                "result": {
                    "provider_id": "openai",
                    "provider_kind": "platform_api",
                    "actual_model_id": "text-embedding-3-small",
                    "vectors": [[0.1, 0.2]],
                    "usage": {"input_count": 1},
                    "extension": {"op_id": 81},
                },
                "provider_fact": {
                    "id": f"embedding_refresh_{run_id}_batch",
                    "provider": "openai",
                    "provider_cost_usd": 0.01,
                    "extension": {"op_id": 82},
                },
            }
        )
    elif case == "provider_error":
        payload["error"] = {
            "code": "embedding_provider_error",
            "message": "Unavailable",
        }
    before = json.loads(json.dumps(payload))
    state = "reserved" if case == "reserved" else "returned"
    accounting_persisted = int(case == "returned")
    raw = sqlite3.connect(bundle_path / "project.db")
    try:
        restore_pre_changes_schema(raw)
        raw.execute(
            "INSERT INTO effect_checkpoints "
            "(id,family,group_key,unit_key,action_kind,identity,state,payload,accounting_persisted) "
            "VALUES ('refresh','embedding_index_refresh','index:1','batch:1',"
            "'embedding.index_refresh','identity',?,?,?)",
            (state, json.dumps(payload), accounting_persisted),
        )
        raw.commit()
    finally:
        raw.close()
    with closing(Project(bundle_path)) as upgraded:
        checkpoint = EffectCheckpointStore(upgraded.db).find_unit(
            family="embedding_index_refresh", group_key="index:1", unit_key="batch:1"
        )
        assert checkpoint is not None
        saved = checkpoint["payload"]
        assert saved["effect"] == {
            "change_id": change_id,
            "run_id": run_id,
            "attempt_id": "attempt-1",
        }
        assert checkpoint["state"] == state
        assert checkpoint["accounting_persisted"] == accounting_persisted
        original_run = RunResultStore(upgraded).get_run(saved["effect"]["run_id"])
        assert original_run is not None
        assert original_run["change_id"] == saved["effect"]["change_id"]
        assert {key: value for key, value in saved.items() if key != "effect"} == {
            key: value for key, value in before.items() if key != "effect"
        }


def test_upgrade_rewrites_exported_metadata_without_entering_exported_user_values(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "export-metadata.frisket"
    with closing(Project.create(bundle_path, name="Export metadata")):
        pass
    user_schema = {"type": "object", "properties": {"op_id": {"type": "integer"}}}
    nested = {
        "receipt_id": "prior-python",
        "action_kind": "convert.python",
        "outputs": [
            {
                "name": "values",
                "ref": {
                    "kind": "named_result",
                    "op_id": 5,
                    "schema": user_schema,
                },
            }
        ],
        "records": [
            {
                "ref": {
                    "kind": "typed_action_request",
                    "op_id": 5,
                    "action_id": "convert.python",
                    "params": {"return_schema": user_schema},
                }
            }
        ],
    }
    receipt = Receipt(
        receipt_id="work-log",
        project_id="project",
        action_id="export-action",
        action_kind="export.work_log",
        status="completed",
    ).model_dump(mode="json")
    receipt["records"] = [
        {
            "ref": {
                "kind": "exported_operations",
                "operations": [
                    {"op_id": 5, "kind": "convert.python", "status": "applied"},
                ],
            }
        },
        {"ref": {"kind": "exported_receipts", "receipts": [nested]}},
    ]
    raw = sqlite3.connect(bundle_path / "project.db")
    try:
        restore_pre_changes_schema(raw)
        raw.execute(
            "INSERT INTO receipts(id,action_kind,status,body) "
            "VALUES ('work-log','export.work_log','completed',?)",
            (json.dumps(receipt),),
        )
        raw.commit()
    finally:
        raw.close()
    with closing(Project(bundle_path)) as upgraded:
        saved = ReceiptStore(upgraded).require_parsed("work-log")
        assert saved.records[0].ref["operations"] == [
            {"change_id": 5, "kind": "convert.python", "status": "applied"}
        ]
        exported = saved.records[1].ref["receipts"][0]
        assert exported["outputs"][0]["ref"]["change_id"] == 5
        assert exported["outputs"][0]["ref"]["schema"] == user_schema
        request = exported["records"][0]["ref"]
        assert request["change_id"] == 5
        assert request["params"] == {"return_schema": user_schema}


def test_locator_upgrade_rewrites_source_identity_without_entering_user_data() -> None:
    opaque = {"op_id": 9, "nested": [{"op_id": 10}]}
    locator = {
        "value_ref": {"op_id": 7},
        "source_version": {"op_id": 7, "base_producer_id": 8},
        "prepared_value_ref": {"op_id": 11},
        "extension": opaque,
    }
    rewritten, changed = _rewrite_locator(locator, {"op_id": "change_id"})
    assert changed
    assert rewritten == {
        "value_ref": {"change_id": 7},
        "source_version": {"change_id": 7, "base_producer_id": 8},
        "prepared_value_ref": {"change_id": 11},
        "extension": opaque,
    }


def test_receipt_upgrade_visits_every_section_and_reconstructs_result() -> None:
    before = {
        "receipt_id": "undo-receipt",
        "project_id": "project",
        "action_id": "undo-action",
        "action_kind": "operation.undo",
        "status": "completed",
        "op_ids": [7],
        "inputs": [
            {
                "name": "target_operation",
                "ref": {"kind": "target_operation", "op_id": 7},
            }
        ],
        "outputs": [
            {
                "name": "undo.7",
                "ref": {"kind": "operation_status_transition", "op_id": 7},
            }
        ],
        "records": [
            {"ref": {"kind": "operation_cursor", "op_id": 7}},
            {"ref": {"kind": "operation_undo_info", "op_id": 7}},
            {"ref": {"kind": "operation_snapshot", "op_id": 7}},
        ],
    }
    raw, changed = _rewrite_receipt_body(json.dumps(before))
    assert changed
    receipt = Receipt.model_validate_json(raw)
    assert receipt.inputs[0].ref == {"kind": "target_change", "change_id": 7}
    assert [item.ref["kind"] for item in receipt.records] == [
        "change_cursor",
        "change_undo_info",
        "change_snapshot",
    ]
    assert all(item.ref["change_id"] == 7 for item in receipt.records)
    result = _result_from_receipt(receipt)
    assert result.outputs[0].kind == "change"
    assert result.outputs[0].ref == {"kind": "change_status_transition", "change_id": 7}


@pytest.mark.parametrize(
    "action_id, params",
    [
        (
            "convert.python",
            {
                "return_schema": {
                    "type": "object",
                    "properties": {"op_id": {"type": "integer"}},
                    "required": ["op_id"],
                    "additionalProperties": False,
                }
            },
        ),
        (
            "import.rows",
            {"rows": [{"op_id": 7, "nested": {"copied_op_id": 8}}]},
        ),
        (
            "custom.action",
            {"op_id": 7, "extension": {"expected_op_id": 8}},
        ),
    ],
)
def test_action_drafts_preserve_opaque_params(
    action_id: str, params: dict[str, object]
) -> None:
    draft = {"action_id": action_id, "params": params}
    before = json.dumps(draft, sort_keys=True)
    assert not _rewrite_submission_draft(draft)
    assert json.dumps(draft, sort_keys=True) == before


@pytest.mark.parametrize(
    "action_id, old_key, new_key",
    [
        ("operation.undo", "expected_op_id", "expected_change_id"),
        ("operation.restore", "op_id", "change_id"),
        ("review.decision", "copied_op_id", "copied_change_id"),
    ],
)
def test_action_drafts_rewrite_only_declared_metadata(
    action_id: str, old_key: str, new_key: str
) -> None:
    opaque = {"op_id": 8, "copied_op_id": 9, "nested": [{"expected_op_id": 10}]}
    draft = {
        "action_id": action_id,
        "params": {old_key: 7, "extension": opaque},
    }
    assert _rewrite_submission_draft(draft)
    assert draft["params"] == {new_key: 7, "extension": opaque}


def test_upgrade_preserves_user_json_in_specs_requests_and_receipts(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "opaque-params.frisket"
    with closing(Project.create(bundle_path, name="Opaque parameters")) as seed:
        sheet_id = seed.add_sheet("Items")
        change_id = seed.append_change("convert.python", {})
        run_id = RunResultStore(seed).start_run(
            change_id, sheet_id, "convert.python", total_rows=0
        )
    user_schema = {
        "type": "object",
        "properties": {"op_id": {"type": "integer"}},
        "required": ["op_id"],
        "additionalProperties": False,
    }
    params = {
        "return_schema": user_schema,
        "extension": {"op_id": 17, "nested": [{"op_ids": [18]}]},
    }
    request = {"action_id": "convert.python", "params": params}
    spec = {"action_kind": "convert.python", "op_id": change_id, "params": params}
    draft_json = _canonical_json(request)
    draft_hash = hashlib.sha256(draft_json.encode()).hexdigest()
    receipt = Receipt(
        receipt_id="opaque-receipt",
        project_id="project",
        action_id="python-action",
        action_kind="convert.python",
        status="completed",
        value={"op_id": 19},
    ).model_dump(mode="json")
    receipt["records"] = [
        {
            "ref": {
                "kind": "typed_action_request",
                "op_id": change_id,
                **request,
                "request": request,
            }
        }
    ]
    raw = sqlite3.connect(bundle_path / "project.db")
    try:
        restore_pre_changes_schema(raw)
        raw.execute("UPDATE ops SET spec=? WHERE id=?", (json.dumps(spec), change_id))
        raw.execute("UPDATE runs SET params=? WHERE id=?", (json.dumps(spec), run_id))
        raw.execute(
            "INSERT INTO action_submissions "
            "(id,request_key,draft_hash,action_id,draft_json) VALUES (101,'opaque',?,'convert.python',?)",
            (draft_hash, draft_json),
        )
        raw.execute(
            "INSERT INTO receipts(id,action_kind,body,status) VALUES ('opaque-receipt','convert.python',?,'completed')",
            (json.dumps(receipt),),
        )
        raw.commit()
    finally:
        raw.close()
    with closing(Project(bundle_path)) as upgraded:
        for table, field, identity in (
            ("changes", "spec", change_id),
            ("runs", "params", run_id),
        ):
            saved = json.loads(
                upgraded.db.execute(
                    f"SELECT {field} FROM {table} WHERE id=?", (identity,)
                ).fetchone()[0]
            )
            assert saved["change_id"] == change_id
            assert saved["params"] == params
        submission = upgraded.db.execute(
            "SELECT draft_json,draft_hash FROM action_submissions WHERE id=101"
        ).fetchone()
        assert tuple(submission) == (draft_json, draft_hash)
        saved_receipt = ReceiptStore(upgraded).require_parsed("opaque-receipt")
        assert saved_receipt.value == {"op_id": 19}
        ref = saved_receipt.records[0].ref
        assert ref["change_id"] == change_id
        assert ref["params"] == params
        assert ref["request"] == request


def _seed_rich_bundle(bundle_path: Path) -> dict[str, int | str]:
    with closing(Project.create(bundle_path, name="Changes Migration Test")) as seed:
        sheet_id = seed.add_sheet("Documents")
        title_col = seed.add_column(sheet_id, "title", type="text")
        row_ids = seed.add_rows(
            sheet_id,
            [{"title": "Alpha"}, {"title": "Beta"}],
            {"title": title_col},
        )
        first_change_id = seed.change_cursor
        assert first_change_id > 0

        # Manual edit so edits + current_cells + undo/redo are exercised.
        edit_res = run_action_spec(
            seed,
            {
                "action_id": "cell.edit",
                "scope": {"kind": "project"},
                "params": {
                    "edits": [
                        {
                            "row_id": row_ids[0],
                            "column_id": title_col,
                            "value": "Alpha Edited",
                        }
                    ]
                },
                "idempotency_key": "changes-mig-edit@1",
            },
            project_id="proj-changes-mig",
        )
        assert edit_res.status == "completed", edit_res.errors
        edit_change_id = edit_res.change_ids[0]
        edit_receipt_id = edit_res.receipt_id
        assert edit_receipt_id is not None

        # Undo + redo so a change.undo receipt exists too.
        undo_res = run_action_spec(
            seed,
            {
                "action_id": "change.undo",
                "scope": {"kind": "project"},
                "params": {"expected_change_id": edit_change_id},
                "idempotency_key": "changes-mig-undo@1",
            },
            project_id="proj-changes-mig",
        )
        assert undo_res.status == "completed", undo_res.errors
        undo_receipt_id = undo_res.receipt_id
        assert undo_receipt_id is not None

        redo_res = run_action_spec(
            seed,
            {
                "action_id": "change.redo",
                "scope": {"kind": "project"},
                "params": {"expected_change_id": edit_change_id},
                "idempotency_key": "changes-mig-redo@1",
            },
            project_id="proj-changes-mig",
        )
        assert redo_res.status == "completed", redo_res.errors

        # Seed a view, an alert + alert_check, and a feed + feed_check.
        view_id = seed.add_view(
            "All rows",
            {
                "selection": {
                    "scope": {"kind": "sheet", "sheet_id": sheet_id},
                    "filters": [],
                }
            },
        )
        alert_id = seed.add_alert(name="Doc alert", view_id=view_id)
        with seed.db:
            seed.db.execute(
                "UPDATE alerts SET last_evaluated_change=? WHERE id=?",
                (edit_change_id, alert_id),
            )
            seed.db.execute(
                "INSERT INTO alert_checks "
                "(id, alert_id, status, change_cursor_before, change_cursor_after, matched_rows, new_rows) "
                "VALUES (41, ?, 'ok', ?, ?, 2, 1)",
                (alert_id, first_change_id, edit_change_id),
            )
        feed_store = FeedStore(seed)
        with write_transaction(seed.db):
            feed_id = feed_store.add_feed(
                name="Notices",
                kind="rss",
                url="https://example.test/feed.xml",
                sheet_id=sheet_id,
            )
            feed_check_id = feed_store.record_feed_check(
                feed_id,
                change_id=first_change_id,
                status="ok",
                new_rows=2,
            )
            seed.db.execute(
                "UPDATE feed_checks SET summary_json=? WHERE id=?",
                (
                    json.dumps({"change_id": first_change_id, "new_rows": 2}),
                    feed_check_id,
                ),
            )
        with seed.db:
            seed.db.execute(
                "INSERT INTO citations "
                "(id, stable_id, subject_kind, subject_ref_json, sheet_id, row_id, column_id, change_id) "
                "VALUES (91, 'cit-mig-1', 'cell_value', ?, ?, ?, ?, ?)",
                (
                    json.dumps(
                        {
                            "kind": "manual_edit",
                            "change_id": edit_change_id,
                            "row_id": row_ids[0],
                            "column_id": title_col,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    sheet_id,
                    row_ids[0],
                    title_col,
                    edit_change_id,
                ),
            )
            seed.db.execute(
                "INSERT INTO text_surfaces "
                "(id, stable_id, surface_kind, text_sheet_id, text_row_id, text_column_id, "
                "value_ref_json, content_hash, offset_unit) "
                "VALUES (81, 'surf-mig-1', 'cell', ?, ?, ?, ?, ?, 'unicode_codepoint')",
                (
                    sheet_id,
                    row_ids[0],
                    title_col,
                    json.dumps(
                        {"kind": "manual_edit", "change_id": edit_change_id},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "sha256:" + "a" * 64,
                ),
            )

    # Downgrade to exact FEEDS_TO_DIGEST pre-changes schema.
    raw = sqlite3.connect(bundle_path / "project.db")
    try:
        restore_pre_changes_schema(raw)
        assert (
            raw.execute(
                "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
            ).fetchone()[0]
            == CHANGES_FROM_DIGEST
        )
        assert (
            raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ops'"
            ).fetchone()
            is not None
        )
        assert (
            raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='changes'"
            ).fetchone()
            is None
        )
        assert (
            raw.execute("SELECT value FROM meta WHERE key='op_cursor'").fetchone()
            is not None
        )
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        raw.close()

    return {
        "sheet_id": sheet_id,
        "title_col": title_col,
        "row_id": row_ids[0],
        "first_change_id": first_change_id,
        "edit_change_id": edit_change_id,
        "edit_receipt_id": edit_receipt_id,
        "undo_receipt_id": undo_receipt_id,
        "alert_id": alert_id,
        "feed_id": feed_id,
        "feed_check_id": feed_check_id,
    }


def test_changes_migration_preserves_schema_fks_receipts_cursors_and_undo_redo(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "changes-migration.frisket"
    fx = _seed_rich_bundle(bundle_path)

    with closing(Project(bundle_path)) as upgraded:
        assert (
            upgraded.get_meta(SCHEMA_DIGEST_META_KEY)
            == CURRENT_CHAIN_TIP_DIGEST
            == CHANGES_TO_DIGEST
            == SCHEMA_DIGEST
        )
        assert upgraded.db.execute("PRAGMA foreign_key_check").fetchall() == []

        # Table renames and zero stale ops table/meta key.
        tables = {
            r[0]
            for r in upgraded.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "changes" in tables
        assert "ops" not in tables
        assert upgraded.get_meta("op_cursor") is None
        assert upgraded.change_cursor == fx["edit_change_id"]

        # Verify column renames across tables & FK references point to changes(id).
        for tbl, expected_col in (
            ("sheets", "parent_change_id"),
            ("sheets", "last_verified_change_cursor"),
            ("base_cell_producers", "change_id"),
            ("runs", "change_id"),
            ("import_sessions", "change_id"),
            ("generated_column_claims", "change_id"),
            ("prepared_page_versions", "producing_change_id"),
            ("prepared_content_sets", "producing_change_id"),
            ("edits", "change_id"),
            ("current_cells", "origin_change_id"),
            ("citations", "change_id"),
            ("feed_checks", "change_id"),
            ("views", "change_id"),
            ("alerts", "last_evaluated_change"),
            ("alert_checks", "change_cursor_before"),
            ("alert_checks", "change_cursor_after"),
            ("cell_baseline", "origin_change_id"),
            ("materialized_row_sources", "change_id"),
            ("document_text_manual_pins", "edit_change_id"),
            ("copied_result_outcomes", "change_id"),
            ("consumed_source_bundles", "owner_change_id"),
            ("column_descriptor_changes", "change_id"),
            ("structural_changes", "change_id"),
            ("result_judgment_changes", "change_id"),
        ):
            cols = {r[1] for r in upgraded.db.execute(f"PRAGMA table_info({tbl})")}
            assert expected_col in cols, (tbl, expected_col, cols)

        # Receipts migrated: change_ids + change.undo action_kind.
        edit_receipt = ReceiptStore(upgraded).require_parsed(str(fx["edit_receipt_id"]))
        assert edit_receipt.change_ids == [fx["edit_change_id"]]
        raw_edit_body = ReceiptStore(upgraded).body_by_id(str(fx["edit_receipt_id"]))
        assert raw_edit_body is not None and '"op_ids"' not in raw_edit_body

        undo_receipt = ReceiptStore(upgraded).require_parsed(str(fx["undo_receipt_id"]))
        assert undo_receipt.action_kind == "change.undo"
        assert undo_receipt.change_ids == [fx["edit_change_id"]]

        # Alert & feed cursors preserved.
        alert = upgraded.get_alert(int(fx["alert_id"]))
        assert alert is not None
        assert alert["last_evaluated_change"] == fx["edit_change_id"]
        alert_check = upgraded.get_alert_check(41)
        assert alert_check is not None
        assert alert_check["change_cursor_before"] == fx["first_change_id"]
        assert alert_check["change_cursor_after"] == fx["edit_change_id"]

        checks = FeedStore(upgraded).feed_checks_page(int(fx["feed_id"]))
        assert len(checks) == 1
        assert checks[0]["change_id"] == fx["first_change_id"]
        assert (
            json.loads(checks[0]["summary_json"])["change_id"] == fx["first_change_id"]
        )

        # Citation subject_ref_json & text_surfaces.value_ref_json migrated and trigger restored.
        cit_ref = json.loads(
            upgraded.db.execute(
                "SELECT subject_ref_json FROM citations WHERE id=91"
            ).fetchone()[0]
        )
        assert cit_ref["change_id"] == fx["edit_change_id"]
        assert "op_id" not in cit_ref

        surf_ref = json.loads(
            upgraded.db.execute(
                "SELECT value_ref_json FROM text_surfaces WHERE id=81"
            ).fetchone()[0]
        )
        assert surf_ref["change_id"] == fx["edit_change_id"]
        assert "op_id" not in surf_ref
        assert (
            upgraded.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_text_surfaces_no_update'"
            ).fetchone()
            is not None
        )

        # Undo/redo history continues to work seamlessly after migration.
        def _title() -> object:
            return upgraded.db.execute(
                "SELECT value FROM current_cell_values WHERE row_id=? AND column_id=?",
                (int(fx["row_id"]), int(fx["title_col"])),
            ).fetchone()[0]

        assert _title() == "Alpha Edited"
        undone_id = upgraded.undo()
        assert undone_id == fx["edit_change_id"]
        assert _title() == "Alpha"
        redone_id = upgraded.redo()
        assert redone_id == fx["edit_change_id"]
        assert _title() == "Alpha Edited"

        # Idempotent replay on the edit action succeeds without creating a duplicate change.
        count_before = upgraded.history_total()
        replayed = run_action_spec(
            upgraded,
            {
                "action_id": "cell.edit",
                "scope": {"kind": "project"},
                "params": {
                    "edits": [
                        {
                            "row_id": fx["row_id"],
                            "column_id": fx["title_col"],
                            "value": "Alpha Edited",
                        }
                    ]
                },
                "idempotency_key": "changes-mig-edit@1",
            },
            project_id="proj-changes-mig",
        )
        assert replayed.status == "completed", replayed.errors
        assert replayed.receipt_id == fx["edit_receipt_id"]
        assert upgraded.history_total() == count_before

    # Reopening an already-migrated bundle is a clean no-op.
    with closing(Project(bundle_path)) as reopened:
        assert reopened.get_meta(SCHEMA_DIGEST_META_KEY) == CHANGES_TO_DIGEST
        assert reopened.db.execute("PRAGMA foreign_key_check").fetchall() == []


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def test_consumed_source_bundles_freshness_and_shared_inputs_survive_upgrade(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "consumed-sources-migration.frisket"
    with closing(Project.create(bundle_path, name="Freshness Migration")) as seed:
        src_sheet = seed.add_sheet("Sources")
        src_col = seed.add_column(src_sheet, "title", type="text")
        src_rows = seed.add_rows(
            src_sheet, [{"title": "Alpha"}, {"title": "Beta"}], {"title": src_col}
        )
        derived_change = seed.append_change("derive.sheet", {"kind": "derive"})
        derived_sheet = seed.add_sheet("Derived")
        with write_transaction(seed.db):
            start_change(seed.db, derived_change)
            seed.db.execute(
                "UPDATE sheets SET parent_sheet_id=?, parent_change_id=? WHERE id=?",
                (src_sheet, derived_change, derived_sheet),
            )
        run_change = seed.append_change("map.extract", {"kind": "map"})
        run_id = RunResultStore(seed).start_run(
            run_change, src_sheet, "map.extract", row_ids=[src_rows[0]], total_rows=1
        )
        descriptors = _descriptors(seed, [src_col])
        revisions = authority_revisions(seed, [src_col])
        members = _members(seed, src_sheet, src_rows)
        _, live_refs = seed.get_values_with_refs(
            src_sheet, src_col, row_ids=src_rows, include_validity=True
        )
        _, row_live_refs = seed.get_values_with_refs(
            src_sheet, src_col, row_ids=[src_rows[0]], include_validity=False
        )
        legacy_scope = {
            "sheet_id": src_sheet,
            "column_ids": [src_col],
            "intent": "whole",
            "cohort": None,
            "max_row_id": max(src_rows),
            "partitions": [
                {
                    "lo": 1,
                    "hi": max(src_rows),
                    "fingerprint": _hash(
                        _records(
                            {"sheet_id": src_sheet, "column_ids": [src_col]},
                            members,
                            descriptors,
                            {src_col: live_refs},
                            legacy_v1=True,
                        )
                    ),
                    "count": len(src_rows),
                    "member_count": len(members),
                }
            ],
        }
        v2_part_fp = _hash(
            _records(
                {"sheet_id": src_sheet, "column_ids": [src_col]},
                members,
                descriptors,
                {src_col: live_refs},
                legacy_v1=False,
            )
        )
        assert legacy_scope["partitions"][0]["fingerprint"] != v2_part_fp

        legacy_agg_capture = {
            "schema_version": 1,
            "kind": "aggregate",
            "scopes": [legacy_scope],
            "revisions": revisions,
            "coherent": True,
            "fingerprint": _hash([legacy_scope]),
        }
        legacy_shared_capture = {
            "schema_version": 1,
            "kind": "aggregate",
            "scopes": [legacy_scope],
            "revisions": revisions,
            "coherent": True,
            "fingerprint": _hash([legacy_scope]),
            "shared_meta": {"op_id": run_change},
        }
        old_shared_fp = capture_fingerprint(legacy_shared_capture)

        legacy_row_ref = dict(row_live_refs[src_rows[0]])
        legacy_row_ref["op_id"] = legacy_row_ref.pop("change_id")
        legacy_row_capture = {
            "schema_version": 1,
            "kind": "row",
            "sheet_id": src_sheet,
            "row_id": src_rows[0],
            "inputs": [
                {
                    "role": "title",
                    "column_id": src_col,
                    "descriptor": descriptors[str(src_col)],
                    "ref": legacy_row_ref,
                }
            ],
            "shared_inputs": [old_shared_fp],
            "revisions": revisions,
            "coherent": True,
        }

    raw = sqlite3.connect(bundle_path / "project.db")
    try:
        restore_pre_changes_schema(raw)
        agg_fp = capture_fingerprint(legacy_agg_capture)
        agg_id = int(
            raw.execute(
                "INSERT INTO consumed_source_bundles "
                "(capture_key, owner_op_id, run_id, row_id, output_sheet_id, kind, "
                "input_fingerprint, capture_json, state, checked_revisions_json) "
                "VALUES (?, ?, NULL, NULL, ?, 'aggregate', ?, ?, 'unknown', '{}')",
                (
                    f"{derived_change}:None:None:{derived_sheet}",
                    derived_change,
                    derived_sheet,
                    agg_fp,
                    _canonical_json(legacy_agg_capture),
                ),
            ).lastrowid
            or 0
        )
        raw.execute(
            "INSERT INTO consumed_source_columns(bundle_id, column_id) VALUES (?, ?)",
            (agg_id, src_col),
        )
        shared_id = int(
            raw.execute(
                "INSERT INTO consumed_source_bundles "
                "(capture_key, owner_op_id, run_id, row_id, output_sheet_id, kind, "
                "input_fingerprint, capture_json, state, checked_revisions_json) "
                "VALUES (?, ?, ?, NULL, ?, 'aggregate', ?, ?, 'unknown', '{}')",
                (
                    f"{run_change}:{run_id}:None:{src_sheet}:shared:{old_shared_fp}",
                    run_change,
                    run_id,
                    src_sheet,
                    old_shared_fp,
                    _canonical_json(legacy_shared_capture),
                ),
            ).lastrowid
            or 0
        )
        raw.execute(
            "INSERT INTO consumed_source_columns(bundle_id, column_id) VALUES (?, ?)",
            (shared_id, src_col),
        )
        row_fp = capture_fingerprint(legacy_row_capture)
        row_bundle_id = int(
            raw.execute(
                "INSERT INTO consumed_source_bundles "
                "(capture_key, owner_op_id, run_id, row_id, output_sheet_id, kind, "
                "input_fingerprint, capture_json, state, checked_revisions_json) "
                "VALUES (?, ?, ?, ?, ?, 'row', ?, ?, 'unknown', '{}')",
                (
                    f"{run_change}:{run_id}:{src_rows[0]}:{src_sheet}",
                    run_change,
                    run_id,
                    src_rows[0],
                    src_sheet,
                    row_fp,
                    _canonical_json(legacy_row_capture),
                ),
            ).lastrowid
            or 0
        )
        raw.execute(
            "INSERT INTO consumed_source_columns(bundle_id, column_id) VALUES (?, ?)",
            (row_bundle_id, src_col),
        )
        raw.execute(
            "INSERT INTO effect_checkpoints "
            "(id, family, group_key, unit_key, action_kind, identity, state, payload) "
            "VALUES ('chk-shared-1', 'row_effect', ?, '1', 'map.extract', 'id-1', 'returned', ?)",
            (
                f"run:{run_id}",
                json.dumps(
                    {
                        "op_id": {
                            "value": {"op_id": run_change},
                            "__consumed_sources": legacy_row_capture,
                        },
                    }
                ),
            ),
        )
        raw.commit()
    finally:
        raw.close()

    with closing(Project(bundle_path)) as upgraded:
        assert upgraded.db.execute("PRAGMA foreign_key_check").fetchall() == []
        # True pre-PR-10B v1 aggregate partition hashes verify as fresh / synced.
        assert verify_bundle(upgraded, agg_id) == "fresh"
        sync = compute_sync_states(upgraded, sheet_ids=[derived_sheet])
        assert sync[derived_sheet]["sync_state"] == "synced"

        # Aggregate hashes and opaque extension data retain their identities.
        shared_row = upgraded.db.execute(
            "SELECT capture_key, input_fingerprint, capture_json "
            "FROM consumed_source_bundles WHERE id=?",
            (shared_id,),
        ).fetchone()
        new_shared_fp = str(shared_row["input_fingerprint"])
        assert new_shared_fp == old_shared_fp
        assert str(shared_row["capture_key"]).endswith(f":shared:{new_shared_fp}")
        assert json.loads(shared_row["capture_json"])["shared_meta"] == {
            "op_id": run_change
        }

        row_bundle = upgraded.db.execute(
            "SELECT input_fingerprint, capture_json FROM consumed_source_bundles WHERE id=?",
            (row_bundle_id,),
        ).fetchone()
        parsed_row_cap = json.loads(row_bundle["capture_json"])
        assert parsed_row_cap["shared_inputs"] == [new_shared_fp]
        assert '"op_id"' not in str(row_bundle["capture_json"])
        assert row_bundle["input_fingerprint"] == capture_fingerprint(parsed_row_cap)
        assert verify_bundle(upgraded, row_bundle_id) == "fresh"
        assert row_input_state(upgraded, run_id, src_rows[0]) == "fresh"

        checkpoint = json.loads(
            upgraded.db.execute(
                "SELECT payload FROM effect_checkpoints WHERE id='chk-shared-1'"
            ).fetchone()[0]
        )
        assert list(checkpoint) == ["op_id"]
        assert checkpoint["op_id"]["value"] == {"op_id": run_change}
        checkpoint_capture = checkpoint["op_id"]["__consumed_sources"]
        assert checkpoint_capture["shared_inputs"] == [new_shared_fp]
        assert '"op_id"' not in json.dumps(checkpoint_capture)

        # Mutating a source cell turns v1 aggregate stale; undoing restores fresh/synced.
        upgraded.apply_edits(
            [{"row_id": src_rows[0], "column_id": src_col, "value": "Alpha Changed"}]
        )
        assert verify_bundle(upgraded, agg_id) == "stale"
        assert (
            compute_sync_states(upgraded, sheet_ids=[derived_sheet])[derived_sheet][
                "sync_state"
            ]
            == "stale"
        )
        upgraded.undo()
        assert verify_bundle(upgraded, agg_id) == "fresh"
        assert (
            compute_sync_states(upgraded, sheet_ids=[derived_sheet])[derived_sheet][
                "sync_state"
            ]
            == "synced"
        )


def test_project_qa_citations_and_events_resolve_current_after_upgrade(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "qa-citations-migration.frisket"
    with closing(Project.create(bundle_path, name="Ask Migration")) as seed:
        sheet_id = seed.add_sheet("Reports")
        col_id = seed.add_column(sheet_id, "summary", type="text")
        row_id = seed.add_rows(
            sheet_id, [{"summary": "Café résumé — clause 4"}], {"summary": col_id}
        )[0]
        edit_change = seed.apply_edits(
            [{"row_id": row_id, "column_id": col_id, "value": "Café résumé — edited"}]
        )
        source_info = read_source_text(seed, (sheet_id, row_id, col_id), limit=100)
        assert source_info["value_ref"]["change_id"] == edit_change
        cursor_now = seed.change_cursor

        qa = ProjectQAStore(seed)
        thread = qa.create_thread(title="Check citations", scope={"kind": "project"})
        turn = qa.submit_turn(
            thread["id"],
            request_id="req-qa-1",
            question="Summarize row 1",
            scope={"kind": "project"},
        )
        qa.append_event(
            turn["id"],
            kind="research_child",
            payload={
                "change_ids": [edit_change],
                "source_change_cursor": cursor_now,
                "note": "Café résumé",
            },
        )
        cell_cit = qa.add_citation(
            turn["id"],
            source_kind="cell",
            label="Reports · row 1 · summary",
            excerpt="Café résumé — edited",
            locator={
                "sheet_id": sheet_id,
                "row_id": row_id,
                "column_id": col_id,
                "value_ref": source_info["value_ref"],
                "source_version": source_info["version"],
                "note": "Café résumé",
            },
        )
        query_cit = qa.add_citation(
            turn["id"],
            source_kind="query",
            label="Reports query",
            excerpt="1 row",
            locator={
                "sheet_id": sheet_id,
                "scope": {"kind": "sheet", "sheet_id": sheet_id},
                "query": {
                    "schema_version": "frisket.query_spec.v1",
                    "kind": "sheet.filter",
                    "scope": {"kind": "sheet", "sheet_id": sheet_id},
                    "filter": {},
                },
                "source_change_cursor": cursor_now,
            },
        )
        cell_cit_id = str(cell_cit["id"])
        query_cit_id = str(query_cit["id"])

    raw = sqlite3.connect(bundle_path / "project.db")
    try:
        restore_pre_changes_schema(raw)
        raw_cell_loc = raw.execute(
            "SELECT locator_json FROM project_qa_citations WHERE id=?", (cell_cit_id,)
        ).fetchone()[0]
        assert '"op_id"' in raw_cell_loc
        raw_ev = raw.execute(
            "SELECT payload_json FROM project_qa_events WHERE kind='research_child'"
        ).fetchone()[0]
        assert '"op_ids"' in raw_ev and '"source_op_cursor"' in raw_ev
    finally:
        raw.close()

    with closing(Project(bundle_path)) as upgraded:
        resolved_cell = resolve_citation(upgraded, thread["id"], cell_cit_id)
        assert resolved_cell["status"] == "current", resolved_cell
        resolved_query = resolve_citation(upgraded, thread["id"], query_cit_id)
        assert resolved_query["status"] == "current", resolved_query

        migrated_cell_loc = upgraded.db.execute(
            "SELECT locator_json FROM project_qa_citations WHERE id=?", (cell_cit_id,)
        ).fetchone()[0]
        assert "Café résumé" in migrated_cell_loc
        assert '"op_id"' not in migrated_cell_loc
        assert '"change_id"' in migrated_cell_loc

        migrated_event = upgraded.db.execute(
            "SELECT payload_json FROM project_qa_events WHERE kind='research_child'"
        ).fetchone()[0]
        assert "Café résumé" in migrated_event
        parsed_event = json.loads(migrated_event)
        assert parsed_event["change_ids"] == [edit_change]
        assert parsed_event["source_change_cursor"] == cursor_now
        assert "op_ids" not in parsed_event and "source_op_cursor" not in parsed_event


def test_action_submissions_migration_draft_hash_and_collision_safety(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "submissions-migration.frisket"
    with closing(Project.create(bundle_path, name="Submissions Migration")) as seed:
        seed.add_sheet("Items")

    target_undo_draft = {
        "action_id": "change.undo",
        "params": {"expected_change_id": 5},
    }
    target_undo_json = _canonical_json(target_undo_draft)
    target_undo_hash = hashlib.sha256(target_undo_json.encode()).hexdigest()

    legacy_undo_draft = {
        "action_id": "operation.undo",
        "params": {"expected_op_id": 5},
    }
    legacy_undo_json = _canonical_json(legacy_undo_draft)
    legacy_undo_hash = hashlib.sha256(legacy_undo_json.encode()).hexdigest()

    legacy_redo_draft = {
        "action_id": "operation.redo",
        "params": {"expected_op_id": 6},
    }
    legacy_redo_json = _canonical_json(legacy_redo_draft)
    legacy_redo_hash = hashlib.sha256(legacy_redo_json.encode()).hexdigest()

    raw = sqlite3.connect(bundle_path / "project.db")
    try:
        restore_pre_changes_schema(raw)
        raw.execute(
            "INSERT INTO action_submissions "
            "(id, request_key, draft_hash, action_id, draft_json) "
            "VALUES (101, 'req-collide', ?, 'change.undo', ?)",
            (target_undo_hash, target_undo_json),
        )
        raw.execute(
            "INSERT INTO action_submissions "
            "(id, request_key, draft_hash, action_id, draft_json) "
            "VALUES (102, 'req-collide', ?, 'operation.undo', ?)",
            (legacy_undo_hash, legacy_undo_json),
        )
        raw.execute(
            "INSERT INTO action_submissions "
            "(id, request_key, draft_hash, action_id, draft_json) "
            "VALUES (103, 'req-clean', ?, 'operation.redo', ?)",
            (legacy_redo_hash, legacy_redo_json),
        )
        raw.commit()
    finally:
        raw.close()

    with closing(Project(bundle_path)) as upgraded:
        clean = upgraded.db.execute(
            "SELECT action_id, draft_hash, draft_json FROM action_submissions WHERE id=103"
        ).fetchone()
        assert clean["action_id"] == "change.redo"
        expected_clean_json = _canonical_json(
            {
                "action_id": "change.redo",
                "params": {"expected_change_id": 6},
            }
        )
        assert clean["draft_json"] == expected_clean_json
        assert (
            clean["draft_hash"]
            == hashlib.sha256(expected_clean_json.encode()).hexdigest()
        )

        collided = upgraded.db.execute(
            "SELECT action_id, draft_hash, draft_json FROM action_submissions WHERE id=102"
        ).fetchone()
        assert collided["action_id"] == "change.undo"
        assert collided["draft_json"] == target_undo_json
        assert collided["draft_hash"] == legacy_undo_hash


def test_notification_items_and_extended_spec_keys_migrate(tmp_path: Path) -> None:
    bundle_path = tmp_path / "notifications-specs-migration.frisket"
    with closing(Project.create(bundle_path, name="Notifications Migration")) as seed:
        sheet_id = seed.add_sheet("Records")
        change_id = seed.append_change("derive.join", {"kind": "join"})
        run_id = RunResultStore(seed).start_run(
            change_id, sheet_id, "derive.join", total_rows=0
        )

    raw = sqlite3.connect(bundle_path / "project.db")
    try:
        restore_pre_changes_schema(raw)
        raw.execute(
            "UPDATE ops SET spec=? WHERE id=?",
            (
                json.dumps(
                    {
                        "source_op_id": 1,
                        "parent_op_id": 1,
                        "op_ids": [1, change_id],
                        "source_op_ids": [1],
                    }
                ),
                change_id,
            ),
        )
        raw.execute(
            "UPDATE runs SET params=? WHERE id=?",
            (
                json.dumps(
                    {
                        "source_op_id": 1,
                        "parent_op_id": 1,
                        "op_ids": [change_id],
                        "source_op_ids": [1],
                    }
                ),
                run_id,
            ),
        )
        raw.execute(
            "INSERT INTO notification_items "
            "(id, source_kind, source_ref, dedupe_key, title, summary, severity, deep_link, payload) "
            "VALUES (101, 'run', ?, 'dedupe-1', 'Done', 'Summary', 'info', ?, ?)",
            (
                json.dumps({"op_id": change_id}),
                json.dumps({"op_id": change_id}),
                json.dumps({"op_id": change_id, "op_ids": [change_id]}),
            ),
        )
        raw.commit()
    finally:
        raw.close()

    with closing(Project(bundle_path)) as upgraded:
        spec = json.loads(
            upgraded.db.execute(
                "SELECT spec FROM changes WHERE id=?", (change_id,)
            ).fetchone()[0]
        )
        assert spec == {
            "source_change_id": 1,
            "parent_change_id": 1,
            "change_ids": [1, change_id],
            "source_change_ids": [1],
        }
        params = json.loads(
            upgraded.db.execute(
                "SELECT params FROM runs WHERE id=?", (run_id,)
            ).fetchone()[0]
        )
        assert params == {
            "source_change_id": 1,
            "parent_change_id": 1,
            "change_ids": [change_id],
            "source_change_ids": [1],
        }
        notif = upgraded.db.execute(
            "SELECT source_ref, deep_link, payload FROM notification_items WHERE id=101"
        ).fetchone()
        assert json.loads(notif["source_ref"]) == {"change_id": change_id}
        assert json.loads(notif["deep_link"]) == {"change_id": change_id}
        assert json.loads(notif["payload"]) == {
            "change_id": change_id,
            "change_ids": [change_id],
        }


def test_sqlite_sequence_high_water_preserved_with_deleted_tail_rows(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "sequence-highwater-migration.frisket"
    with closing(Project.create(bundle_path, name="Sequence Migration")) as seed:
        sheet_id = seed.add_sheet("Items")

    raw = sqlite3.connect(bundle_path / "project.db")
    try:
        restore_pre_changes_schema(raw)
        raw.execute(
            "INSERT INTO ops(id, kind, label, spec, status) VALUES (50, 'temp', 'Temp', '{}', 'applied')"
        )
        raw.execute(
            "INSERT INTO runs(id, op_id, sheet_id, action_kind, params, status) "
            "VALUES (75, 50, ?, 'map.python', '{}', 'completed')",
            (sheet_id,),
        )
        raw.execute("DELETE FROM runs WHERE id=75")
        raw.execute("DELETE FROM ops WHERE id=50")
        raw.commit()
    finally:
        raw.close()

    with closing(Project(bundle_path)) as upgraded:
        seqs = {
            str(r[0]): int(r[1])
            for r in upgraded.db.execute("SELECT name, seq FROM sqlite_sequence")
        }
        assert "ops" not in seqs
        assert seqs["changes"] >= 50
        assert seqs["runs"] >= 75
        next_change = upgraded.append_change("next.change", {})
        assert next_change > 50
