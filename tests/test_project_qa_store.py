from __future__ import annotations

import sqlite3

import pytest

from frisket.engine.store import Project
from frisket.engine.store.project_qa import (
    ProjectQAActiveTurnError,
    ProjectQAConflictError,
    ProjectQAStore,
)
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY


def _store(tmp_path) -> tuple[Project, ProjectQAStore]:
    project = Project.create(tmp_path / "ask.frisket", name="Ask")
    return project, ProjectQAStore(project)


def test_threads_keep_project_scope_preferences_creator_and_revision(tmp_path) -> None:
    project, store = _store(tmp_path)
    try:
        thread = store.create_thread(
            title="Investigate",
            scope={"kind": "project"},
            model=None,
            created_by="reporter",
        )
        assert thread == {
            "id": thread["id"],
            "title": "Investigate",
            "scope": {"kind": "project"},
            "model": None,
            "web": False,
            "suggest_actions": True,
            "revision": 1,
            "created_by": "reporter",
            "created_at": thread["created_at"],
            "updated_at": thread["updated_at"],
        }

        changed = store.update_thread(
            thread["id"],
            expected_revision=1,
            scope={
                "kind": "sources",
                "sources": [{"kind": "rows", "sheet_id": 4, "row_ids": [8, 9]}],
            },
            model="openrouter/test",
            web=True,
            suggest_actions=False,
        )
        assert changed["revision"] == 2
        assert changed["scope"]["sources"][0]["row_ids"] == [8, 9]
        reset = store.update_thread(
            thread["id"], expected_revision=2, scope=None, model=None
        )
        assert reset["scope"] == {"kind": "project"}
        assert reset["model"] is None
        with pytest.raises(ProjectQAConflictError, match="revision"):
            store.update_thread(thread["id"], expected_revision=1, title="stale")
    finally:
        project.close()


def test_scope_normalizes_overlaps_and_bounds_unique_selected_rows(tmp_path) -> None:
    project, store = _store(tmp_path)
    try:
        thread = store.create_thread(
            title="Scoped",
            scope={
                "kind": "sources",
                "sources": [
                    {"kind": "rows", "sheet_id": 2, "row_ids": [3, 3, 4]},
                    {"kind": "rows", "sheet_id": 2, "row_ids": [4, 5]},
                    {"kind": "file", "sheet_id": 2, "row_id": 3, "column_id": 7},
                    {"kind": "sheet", "sheet_id": 2},
                    {"kind": "file", "sheet_id": 2, "row_id": 3, "column_id": 7},
                ],
            },
        )
        assert thread["scope"] == {
            "kind": "sources",
            "sources": [
                {"kind": "file", "sheet_id": 2, "row_id": 3, "column_id": 7},
                {"kind": "sheet", "sheet_id": 2},
            ],
        }
    finally:
        project.close()


def test_submit_is_idempotent_freezes_turn_and_enforces_single_active_turn(
    tmp_path,
) -> None:
    project, store = _store(tmp_path)
    try:
        thread = store.create_thread(title="Ask")
        params = {
            "request_id": "request-1",
            "question": "What changed?",
            "scope": {"kind": "sources", "sources": [{"kind": "sheet", "sheet_id": 3}]},
            "model": "openrouter/test",
            "web": True,
            "suggest_actions": False,
            "submitted_by": "reporter",
        }
        turn = store.submit_turn(thread["id"], **params)
        assert turn["status"] == "running"
        assert turn["scope"] == params["scope"]
        assert turn["web"] is True
        assert store.submit_turn(thread["id"], **params) == turn
        assert store.get_active_turn(thread["id"])["id"] == turn["id"]
        with pytest.raises(ProjectQAConflictError, match="different content"):
            store.submit_turn(thread["id"], **{**params, "question": "Other"})
        with pytest.raises(ProjectQAActiveTurnError):
            store.submit_turn(thread["id"], **{**params, "request_id": "request-2"})

        store.update_thread(thread["id"], expected_revision=1, web=False)
        assert store.get_turn(turn["id"])["web"] is True
        assert store.events(thread["id"])["events"] == [
            {
                "thread_id": thread["id"],
                "turn_id": turn["id"],
                "seq": 1,
                "kind": "question",
                "payload": {"question": "What changed?"},
                "created_at": store.events(thread["id"])["events"][0]["created_at"],
            }
        ]
    finally:
        project.close()


def test_events_citations_stop_terminalize_and_reconcile_only_unowned_turns(
    tmp_path,
) -> None:
    project, store = _store(tmp_path)
    try:
        thread = store.create_thread(title="Ask")
        first = store.submit_turn(thread["id"], request_id="one", question="One")
        progress = store.append_event(
            first["id"], kind="assistant", payload={"text": "Looking"}
        )
        citation = store.add_citation(
            first["id"],
            label="Sheet / row 7",
            source_kind="row",
            locator={"sheet_id": 1, "row_id": 7},
            excerpt="Observed value",
            metadata={"truncated": False},
        )
        assert progress["seq"] == 2
        assert store.get_citation(citation["id"])["locator"] == {
            "sheet_id": 1,
            "row_id": 7,
        }
        assert store.request_stop(first["id"])["status"] == "stopping"
        assert store.request_stop(first["id"])["status"] == "stopping"
        with pytest.raises(ProjectQAConflictError, match="stopping"):
            store.append_event(first["id"], kind="assistant", payload={"text": "late"})
        assert (
            store.append_event(first["id"], kind="usage", payload={"calls": 1})["kind"]
            == "usage"
        )
        with pytest.raises(ProjectQAConflictError, match="terminal"):
            store.add_citation(
                first["id"],
                label="late",
                source_kind="row",
                locator={"sheet_id": 1, "row_id": 7},
            )
        terminal = store.finish_turn(first["id"], status="stopped")
        assert terminal["status"] == "stopped"
        assert store.get_active_turn(thread["id"]) is None
        events = store.events(thread["id"], after=1, limit=1)
        assert [event["seq"] for event in events["events"]] == [2]
        assert events["cursor"] == 2
        assert events["has_more"] is True
        older = store.events(thread["id"], before=4, limit=2)
        assert [event["seq"] for event in older["events"]] == [2, 3]
        assert older["has_more"] is True
        assert store.request_stop(first["id"])["status"] == "stopped"
        with pytest.raises(ProjectQAConflictError, match="terminal"):
            store.append_event(first["id"], kind="answer", payload={"text": "late"})

        second = store.submit_turn(thread["id"], request_id="two", question="Two")
        other = store.create_thread(title="Other")
        third = store.submit_turn(other["id"], request_id="three", question="Three")
        reconciled = store.reconcile_abandoned_turns(live_turn_ids={second["id"]})
        assert reconciled == [third["id"]]
        assert store.get_turn(second["id"])["status"] == "running"
        assert store.get_turn(third["id"])["status"] == "interrupted"
    finally:
        project.close()


def test_schema_migration_preserves_existing_bundle_and_stamps_new_digest(
    tmp_path,
) -> None:
    project, _store_instance = _store(tmp_path)
    path = project.path
    project.db.execute("INSERT INTO sheets (name) VALUES ('Existing')")
    project.db.commit()
    project.close()

    with sqlite3.connect(path / "project.db") as db:
        db.execute("DROP TABLE project_qa_citations")
        db.execute("DROP TABLE project_qa_events")
        db.execute("DROP TABLE project_qa_turns")
        db.execute("DROP TABLE project_qa_threads")
        db.execute(
            "UPDATE meta SET value=? WHERE key=?",
            (
                "frisket.schema.v1:8120b7fe7102b3570ff0c8326bd62fa2",
                SCHEMA_DIGEST_META_KEY,
            ),
        )

    reopened = Project(path)
    try:
        assert reopened.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
        assert (
            reopened.db.execute("SELECT name FROM sheets").fetchone()[0] == "Existing"
        )
        assert reopened.db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        reopened.close()
