"""Tests for the data-preserving feeds schema upgrade step."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from frisket.engine.store import Project
from frisket.engine.store.feeds import FeedStore
from frisket.engine.store.schema import SCHEMA_DIGEST
from frisket.alerts.triggers import feed_poll_materialization_from_receipt
from frisket.feeds.health import build_feed_health
from frisket.upgrades.project.feeds_migration import (
    FEEDS_FROM_DIGEST,
    FEEDS_TO_DIGEST,
)
from historical_schema import restore_pre_feeds_tables


def _build_pre_feeds_bundle(bundle_path: Path) -> dict[str, int | str]:
    project = Project.create(bundle_path, name="Feeds Migration Test")
    sheet_id = project.add_sheet("Notices")
    col_id = project.add_column(sheet_id, "title", type="text")
    row_ids = project.add_rows(sheet_id, [{"title": "Alpha"}], {"title": col_id})
    op_id = int(project.db.execute("SELECT MAX(id) FROM ops").fetchone()[0])
    channel = project.create_notification_channel(
        kind="email",
        name="Ops",
        config={"to": "ops@example.com"},
        secret_ref="env:RESEND_API_KEY",
    )
    channel_id = int(channel["id"])
    project.close()

    db = sqlite3.connect(bundle_path / "project.db")
    try:
        db.execute("PRAGMA foreign_keys = OFF")
        restore_pre_feeds_tables(db)
        db.execute(
            "INSERT INTO sources "
            "(id, name, kind, url, config, sheet_id, schedule, enabled, cursor, "
            "last_checked_at, last_status, new_rows_total, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                7,
                "Public Notices",
                "rss",
                "https://feeds.example/notices.xml?token=SECRET",
                json.dumps({"merge_strategy": "append-new"}),
                sheet_id,
                "@hourly",
                1,
                "cursor-v1",
                "2026-10-01 12:00:00",
                "ok",
                4,
                "2026-09-01 00:00:00",
            ),
        )
        poll_receipt_id = "rcpt_legacy_source_poll"
        poll_receipt_body = {
            "schema_version": "frisket.action_receipt.v1",
            "receipt_id": poll_receipt_id,
            "project_id": "legacy-proj",
            "action_id": "act_legacy_poll",
            "action_kind": "source.poll",
            "idempotency_key": "legacy-poll@1",
            "params_hash": "hash-poll",
            "status": "completed",
            "inputs": [
                {
                    "name": "source",
                    "ref": {
                        "kind": "source_poll_source",
                        "source_id": 7,
                        "source_kind": "rss",
                        "name": "Public Notices",
                    },
                },
                {
                    "name": "request",
                    "ref": {"kind": "source_poll_request", "source_id": 7},
                },
            ],
            "outputs": [
                {
                    "name": "source",
                    "ref": {
                        "kind": "source_poll_source",
                        "source_id": 7,
                        "source_kind": "rss",
                        "name": "Public Notices",
                    },
                },
                {
                    "name": "source_run",
                    "ref": {
                        "kind": "source_poll_run",
                        "source_id": 7,
                        "source_run_id": 11,
                        "sheet_id": sheet_id,
                        "op_id": op_id,
                        "status": "ok",
                        "materialized_rows": 1,
                        "new_rows": 1,
                    },
                },
                {
                    "name": "sheet",
                    "ref": {"kind": "source_sheet", "sheet_id": sheet_id},
                },
                {
                    "name": "rows",
                    "ref": {
                        "kind": "source_poll_rows",
                        "sheet_id": sheet_id,
                        "op_id": op_id,
                        "row_ids": row_ids,
                    },
                },
            ],
            "records": [
                {
                    "name": "cursor",
                    "ref": {"kind": "source_poll_cursor", "source_id": 7},
                }
            ],
            "errors": [],
        }
        db.execute(
            "INSERT INTO receipts "
            "(id, action_kind, action_id, idempotency_key, params_hash, status, body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                poll_receipt_id,
                "source.poll",
                "act_legacy_poll",
                "legacy-poll@1",
                "hash-poll",
                "completed",
                json.dumps(poll_receipt_body),
            ),
        )
        failed_receipt_id = "rcpt_legacy_source_fail"
        failed_receipt_body = {
            "schema_version": "frisket.action_receipt.v1",
            "receipt_id": failed_receipt_id,
            "project_id": "legacy-proj",
            "action_id": "act_legacy_fail",
            "action_kind": "source.check",
            "idempotency_key": "legacy-check@1",
            "params_hash": "hash-check",
            "status": "failed",
            "inputs": [
                {
                    "name": "source",
                    "ref": {"kind": "source_check_source", "source_id": 7},
                }
            ],
            "outputs": [],
            "evidence": [
                {
                    "name": "cursor",
                    "ref": {"kind": "source_check_cursor", "source_id": 7},
                }
            ],
            "errors": [
                {
                    "code": "unsupported_source_kind",
                    "action_kind": "source.check",
                    "message": "RSS feeds use feed.poll",
                    "details": {"source_id": 7},
                }
            ],
        }
        db.execute(
            "INSERT INTO receipts "
            "(id, action_kind, action_id, idempotency_key, params_hash, status, body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                failed_receipt_id,
                "source.check",
                "act_legacy_fail",
                "legacy-check@1",
                "hash-check",
                "failed",
                json.dumps(failed_receipt_body),
            ),
        )
        db.execute(
            "INSERT INTO receipts "
            "(id, action_kind, action_id, idempotency_key, params_hash, status, body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "rcpt_unrelated",
                "map.template",
                "act_unrelated",
                "unrelated@1",
                "hash-unrelated",
                "completed",
                json.dumps({"action_kind": "map.template", "outputs": []}),
            ),
        )
        db.execute(
            "INSERT INTO source_runs "
            "(id, source_id, op_id, receipt_id, status, new_rows, skipped_rows, "
            "changed_rows, revisions, error, cursor_before, cursor_after, "
            "duration_ms, warning_count, cost_micro, summary_json, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                11,
                7,
                op_id,
                poll_receipt_id,
                "ok",
                1,
                2,
                0,
                0,
                None,
                "cursor-v0",
                "cursor-v1",
                125,
                0,
                500,
                json.dumps({"materialized_rows": 1}),
                "2026-10-01 11:59:00",
                "2026-10-01 12:00:00",
            ),
        )
        db.execute(
            "INSERT INTO notification_items "
            "(id, dedupe_key, severity, title, summary, source_kind, source_ref, deep_link, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                "source:7:run:11:failed",
                "warning",
                "Feed failed",
                "Temporary outage",
                "source",
                json.dumps({"source_id": 7, "source_run_id": 11, "status": "failed"}),
                json.dumps({"kind": "source", "source_id": 7, "sheet_id": sheet_id}),
                json.dumps({"status": "failed"}),
            ),
        )
        db.execute(
            "INSERT INTO notification_items "
            "(id, dedupe_key, severity, title, summary, source_kind, source_ref, deep_link, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                2,
                "alert:9:run:1",
                "info",
                "Alert matched",
                "New row",
                "alert",
                json.dumps({"alert_id": 9}),
                json.dumps({"kind": "alert", "alert_id": 9}),
                json.dumps({}),
            ),
        )
        db.execute(
            "INSERT INTO notification_routes "
            "(id, name, channel_id, owner_ref, source_kind, source_ref_match_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                1,
                "Feed route",
                channel_id,
                "local",
                "source",
                json.dumps({"source_id": 7}),
            ),
        )
        db.execute(
            "INSERT INTO notification_routes "
            "(id, name, channel_id, owner_ref, source_kind, source_ref_match_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                2,
                "Alert route",
                channel_id,
                "local",
                "alert",
                json.dumps({"alert_id": 9}),
            ),
        )
        db.execute(
            "UPDATE meta SET value=? WHERE key='schema_digest'",
            (FEEDS_FROM_DIGEST,),
        )
        db.commit()
    finally:
        db.close()

    return {
        "feed_id": 7,
        "feed_check_id": 11,
        "sheet_id": sheet_id,
        "op_id": op_id,
        "poll_receipt_id": poll_receipt_id,
        "failed_receipt_id": failed_receipt_id,
    }


def test_feeds_migration_preserves_data_and_rewrites_receipts_and_notifications(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "legacy-feeds.frisket"
    seeded = _build_pre_feeds_bundle(bundle_path)

    project = Project(bundle_path)
    try:
        digest = project.db.execute(
            "SELECT value FROM meta WHERE key='schema_digest'"
        ).fetchone()[0]
        assert digest == SCHEMA_DIGEST == FEEDS_TO_DIGEST
        assert project.db.execute("PRAGMA foreign_key_check").fetchall() == []

        tables = {
            row[0]
            for row in project.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "feeds" in tables
        assert "feed_checks" in tables
        assert "sources" not in tables
        assert "source_runs" not in tables

        indexes = {
            row[0]
            for row in project.db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert {
            "idx_feeds_sheet",
            "idx_feed_checks_feed_started",
            "idx_feed_checks_receipt",
        } <= indexes

        store = FeedStore(project)
        feed = store.get_feed(int(seeded["feed_id"]))
        assert feed is not None
        assert feed["name"] == "Public Notices"
        assert feed["kind"] == "rss"
        assert feed["sheet_id"] == seeded["sheet_id"]
        assert feed["cursor"] == "cursor-v1"
        assert feed["new_rows_total"] == 4

        checks = store.feed_checks_page(int(seeded["feed_id"]))
        assert len(checks) == 1
        assert checks[0]["id"] == seeded["feed_check_id"]
        assert checks[0]["feed_id"] == seeded["feed_id"]
        assert checks[0]["receipt_id"] == seeded["poll_receipt_id"]
        assert checks[0]["new_rows"] == 1
        assert checks[0]["skipped_rows"] == 2
        assert checks[0]["cost_micro"] == 500

        # Migrated poll receipt is readable by feed_poll_materialization_from_receipt
        poll_row = project.db.execute(
            "SELECT action_kind, body FROM receipts WHERE id=?",
            (seeded["poll_receipt_id"],),
        ).fetchone()
        assert poll_row["action_kind"] == "feed.poll"
        poll_body = json.loads(poll_row["body"])
        assert poll_body["action_kind"] == "feed.poll"
        assert [io["name"] for io in poll_body["outputs"]] == [
            "feed",
            "feed_check",
            "sheet",
            "rows",
        ]
        assert poll_body["outputs"][1]["ref"]["kind"] == "feed_poll_check"
        assert poll_body["outputs"][1]["ref"]["feed_id"] == seeded["feed_id"]
        assert (
            poll_body["outputs"][1]["ref"]["feed_check_id"] == seeded["feed_check_id"]
        )
        assert poll_body["records"] == [
            {
                "name": "cursor",
                "ref": {"kind": "feed_poll_cursor", "feed_id": seeded["feed_id"]},
            }
        ]
        materialized = feed_poll_materialization_from_receipt(
            project,
            project_id="legacy-proj",
            receipt_id=str(seeded["poll_receipt_id"]),
        )
        assert materialized is not None
        assert materialized["kind"] == "feed_poll_materialized"
        assert materialized["feed_id"] == seeded["feed_id"]
        assert materialized["feed_check_id"] == seeded["feed_check_id"]
        assert materialized["sheet_id"] == seeded["sheet_id"]

        # Failed receipt error code + details rewritten; unrelated receipt untouched
        failed_row = project.db.execute(
            "SELECT action_kind, body FROM receipts WHERE id=?",
            (seeded["failed_receipt_id"],),
        ).fetchone()
        assert failed_row["action_kind"] == "feed.check"
        failed_body = json.loads(failed_row["body"])
        assert failed_body["errors"][0]["code"] == "unsupported_feed_kind"
        assert failed_body["errors"][0]["action_kind"] == "feed.check"
        assert failed_body["errors"][0]["details"] == {"feed_id": seeded["feed_id"]}
        assert failed_body["evidence"] == [
            {
                "name": "cursor",
                "ref": {"kind": "feed_check_cursor", "feed_id": seeded["feed_id"]},
            }
        ]

        unrelated = project.db.execute(
            "SELECT action_kind FROM receipts WHERE id='rcpt_unrelated'"
        ).fetchone()
        assert unrelated["action_kind"] == "map.template"

        # Notifications + routes rewritten for feed, untouched for alert
        feed_notif = project.db.execute(
            "SELECT source_kind, dedupe_key, source_ref, deep_link "
            "FROM notification_items WHERE id=1"
        ).fetchone()
        assert feed_notif["source_kind"] == "feed"
        assert feed_notif["dedupe_key"] == "feed:7:run:11:failed"
        assert json.loads(feed_notif["source_ref"]) == {
            "feed_id": 7,
            "feed_check_id": 11,
            "status": "failed",
        }
        assert json.loads(feed_notif["deep_link"]) == {
            "kind": "feed",
            "feed_id": 7,
            "sheet_id": seeded["sheet_id"],
        }
        alert_notif = project.db.execute(
            "SELECT source_kind, dedupe_key FROM notification_items WHERE id=2"
        ).fetchone()
        assert alert_notif["source_kind"] == "alert"
        assert alert_notif["dedupe_key"] == "alert:9:run:1"

        feed_route = project.db.execute(
            "SELECT source_kind, source_ref_match_json FROM notification_routes WHERE id=1"
        ).fetchone()
        assert feed_route["source_kind"] == "feed"
        assert json.loads(feed_route["source_ref_match_json"]) == {"feed_id": 7}
        alert_route = project.db.execute(
            "SELECT source_kind, source_ref_match_json FROM notification_routes WHERE id=2"
        ).fetchone()
        assert alert_route["source_kind"] == "alert"
        assert json.loads(alert_route["source_ref_match_json"]) == {"alert_id": 9}

        # Feed health reads cleanly from the migrated bundle
        health = build_feed_health(project, int(seeded["feed_id"]))
        assert health["schema_version"] == "frisket.feed_health.v1"
        assert health["feed"]["id"] == seeded["feed_id"]
        assert health["feed"]["url"] == "https://feeds.example/notices.xml"
        assert health["runs_page"]["total"] == 1
        assert health["runs"][0]["id"] == seeded["feed_check_id"]
    finally:
        project.close()
