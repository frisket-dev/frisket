from __future__ import annotations
from contextlib import closing

import sqlite3
import threading

import pytest

from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.engine.store.project_qa_research import (
    ProjectQAResearchBudgetExceeded,
    ProjectQAResearchConflictError,
    ProjectQAResearchStore,
    ProjectQAResearchTurnLimitReached,
    ProjectQAResearchUnknownCost,
)
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY
from frisket.upgrades.project.consent_estimate_migration import (
    CONSENT_ESTIMATE_TO_DIGEST,
)
from tests.engine.test_bundle_schema_fence import _restore_legacy_authorities


OLD_SCHEMA_DIGEST = "frisket.schema.v1:caa3ac7c8aaa66153dd8e2cad5950942"


def _research(make_project):
    project = make_project("Research")
    qa = ProjectQAStore(project)
    thread = qa.create_thread(title="Investigate")
    turn = qa.submit_turn(thread["id"], request_id="request-1", question="Why?")
    store = ProjectQAResearchStore(project)
    research = store.create(
        turn_id=turn["id"],
        actor="owner",
        budget_micros=100,
        currency="USD",
        write_mode="ask_each",
        max_turns=None,
    )
    return project, store, research


def test_create_is_idempotent_and_lifecycle_updates_use_revision_cas(
    make_project,
) -> None:
    project, store, research = _research(make_project)
    replay = store.create(
        turn_id=research["turn_id"],
        actor="owner",
        budget_micros=100,
        currency="USD",
        write_mode="ask_each",
        max_turns=None,
    )
    assert replay == research
    with pytest.raises(ProjectQAResearchConflictError, match="different"):
        store.create(
            turn_id=research["turn_id"],
            actor="other",
            budget_micros=100,
            currency="USD",
            write_mode="ask_each",
            max_turns=None,
        )

    store.checkpoint(research["id"], [{"role": "assistant", "content": "partial"}])
    store.add_output_grant(research["id"], "receipt-8")
    pending = {"id": "approval-1", "operation_id": "write-1", "kind": "action"}
    paused = store.publish_pause(research["id"], pending)
    assert paused["revision"] == 4
    assert paused["state"] == "paused"
    assert paused["saved_messages"][0]["content"] == "partial"
    assert paused["pending_pause"] == pending
    assert paused["output_grants"] == ["receipt-8"]
    assert paused["turn_count"] == 0
    with pytest.raises(ProjectQAResearchConflictError, match="revision"):
        store.resume(
            research["id"],
            pause_id="approval-1",
            expected_revision=1,
            decision="approve",
            actor="owner",
        )


def test_admission_is_exact_idempotent_and_settlement_replaces_reservation(
    make_project,
) -> None:
    project, store, research = _research(make_project)
    admitted = store.admit_operation(
        research["id"],
        operation_id="call-1",
        payload_identity="sha256:one",
        operation_kind="model",
        estimate_micros=70,
        metadata={"provider": "example"},
    )
    assert admitted["actual_micros"] is None
    assert store.get(research["id"])["turn_count"] == 1
    assert (
        store.admit_operation(
            research["id"],
            operation_id="call-1",
            payload_identity="sha256:one",
            operation_kind="model",
            estimate_micros=70,
            metadata={"provider": "example"},
        )
        == admitted
    )
    with pytest.raises(ProjectQAResearchConflictError, match="payload"):
        store.admit_operation(
            research["id"],
            operation_id="call-1",
            payload_identity="sha256:other",
            operation_kind="model",
            estimate_micros=70,
        )
    with pytest.raises(ProjectQAResearchUnknownCost):
        store.admit_operation(
            research["id"],
            operation_id="unknown",
            payload_identity="sha256:unknown",
            operation_kind="search",
            estimate_micros=None,
        )
    with pytest.raises(ProjectQAResearchBudgetExceeded) as exc:
        store.admit_operation(
            research["id"],
            operation_id="call-2",
            payload_identity="sha256:two",
            operation_kind="action",
            estimate_micros=31,
        )
    assert (exc.value.needed_micros, exc.value.remaining_micros) == (31, 30)

    settled = store.settle_operation(
        research["id"],
        operation_id="call-1",
        payload_identity="sha256:one",
        actual_micros=40,
    )
    assert settled["actual_micros"] == 40
    assert (
        store.settle_operation(
            research["id"],
            operation_id="call-1",
            payload_identity="sha256:one",
            actual_micros=40,
        )
        == settled
    )
    with pytest.raises(ProjectQAResearchConflictError, match="settled"):
        store.settle_operation(
            research["id"],
            operation_id="call-1",
            payload_identity="sha256:one",
            actual_micros=41,
        )
    assert store.budget_summary(research["id"]) == {
        "budget_micros": 100,
        "currency": "USD",
        "reserved_micros": 0,
        "settled_micros": 40,
        "committed_micros": 40,
        "remaining_micros": 60,
    }
    assert store.get_operation(research["id"], "call-1")["metadata"] == {
        "provider": "example"
    }

    store.publish_pause(
        research["id"],
        {"id": "limit", "operation_id": "call-3", "kind": "turn_limit"},
    )
    limited = store.resume(
        research["id"],
        pause_id="limit",
        expected_revision=2,
        decision="continue",
        actor="owner",
        max_turns=1,
    )
    assert limited["turn_count"] == 1
    with pytest.raises(ProjectQAResearchTurnLimitReached):
        store.admit_operation(
            research["id"],
            operation_id="call-3",
            payload_identity="sha256:three",
            operation_kind="model",
            estimate_micros=1,
        )


def test_two_connections_cannot_overcommit_one_budget(make_project) -> None:
    project, _store, research = _research(make_project)
    path = project.path
    project.close()
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def admit(operation_id: str) -> None:
        with closing(Project(path)) as opened:
            try:
                barrier.wait()
                ProjectQAResearchStore(opened).admit_operation(
                    research["id"],
                    operation_id=operation_id,
                    payload_identity=f"sha256:{operation_id}",
                    operation_kind="model",
                    estimate_micros=60,
                )
                outcomes.append("admitted")
            except ProjectQAResearchBudgetExceeded:
                outcomes.append("refused")

    threads = [
        threading.Thread(  # realtime: real concurrent SQLite admission; no clock assertions
            target=admit, args=(f"op-{index}",)
        )
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["admitted", "refused"]


def test_research_schema_migration_preserves_existing_project_data(
    make_project,
) -> None:
    project, _store, research_row = _research(make_project)
    path = project.path
    project.db.execute("INSERT INTO sheets (name) VALUES ('Existing')")
    project.db.commit()
    project.close()

    with sqlite3.connect(path / "project.db") as db:
        db.execute("PRAGMA foreign_keys=OFF")
        _restore_legacy_authorities(db)
        db.execute("DROP TABLE import_sessions")
        db.execute("DROP TABLE project_qa_research_operations")
        db.execute("DROP TABLE project_qa_research_runs")
        db.execute("ALTER TABLE project_qa_threads DROP COLUMN research_json")
        db.execute("ALTER TABLE project_qa_turns DROP COLUMN research_json")
        db.execute(
            "UPDATE meta SET value=? WHERE key=?",
            (OLD_SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY),
        )

    with closing(Project(path)) as reopened:
        assert reopened.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
        assert (
            reopened.db.execute("SELECT name FROM sheets").fetchone()[0] == "Existing"
        )
        qa = ProjectQAStore(reopened)
        turn = qa.get_turn(research_row["turn_id"])
        assert turn["question"] == "Why?"
        assert qa.get_thread(turn["thread_id"])["title"] == "Investigate"
        assert reopened.db.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {
            row[0]
            for row in reopened.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "project_qa_research_runs" in tables
        assert "project_qa_research_operations" in tables


def test_research_pause_column_migration_preserves_pending_pause(
    make_project,
) -> None:
    project, store, research_row = _research(make_project)
    pending = {
        "id": "pause-upgrade-1",
        "operation_id": "write-upgrade-1",
        "kind": "action",
        "title": "Add generated column",
    }
    paused = store.publish_pause(research_row["id"], pending)
    path = project.path
    project.db.execute("BEGIN")
    project.db.execute(
        "ALTER TABLE project_qa_research_runs "
        "RENAME COLUMN pending_pause_json TO pending_approval_json"
    )
    project.db.execute(
        "UPDATE meta SET value=? WHERE key=?",
        (CONSENT_ESTIMATE_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
    )
    project.db.commit()
    row = project.db.execute(
        "SELECT pending_approval_json FROM project_qa_research_runs WHERE id=?",
        (research_row["id"],),
    ).fetchone()
    assert row is not None and "pause-upgrade-1" in row[0]
    project.close()

    with closing(Project(path)) as reopened:
        assert reopened.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
        columns = {
            row[1]
            for row in reopened.db.execute(
                "PRAGMA table_info(project_qa_research_runs)"
            )
        }
        assert "pending_pause_json" in columns
        assert "pending_approval_json" not in columns
        upgraded_store = ProjectQAResearchStore(reopened)
        loaded = upgraded_store.get(research_row["id"])
        assert loaded["state"] == "paused"
        assert loaded["pending_pause"] == pending
        resumed = upgraded_store.resume(
            research_row["id"],
            pause_id="pause-upgrade-1",
            expected_revision=paused["revision"],
            decision="approve",
            actor="owner",
        )
        assert resumed["state"] == "running"
        assert resumed["pending_pause"] is None
        assert reopened.db.execute("PRAGMA foreign_key_check").fetchall() == []
