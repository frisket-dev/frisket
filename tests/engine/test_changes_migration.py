"""Dedicated tests for the data-preserving ops -> changes project schema migration."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from frisket.engine.executor import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.feeds import FeedStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY
from frisket.engine.store.transactions import write_transaction
from frisket.upgrades.project._tip import CURRENT_CHAIN_TIP_DIGEST
from frisket.upgrades.project.changes_migration import (
    CHANGES_FROM_DIGEST,
    CHANGES_TO_DIGEST,
)
from historical_schema import restore_pre_changes_schema


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
