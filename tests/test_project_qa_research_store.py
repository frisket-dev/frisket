from __future__ import annotations

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
from tests.engine.test_bundle_schema_fence import _restore_legacy_authorities


OLD_SCHEMA_DIGEST = "frisket.schema.v1:caa3ac7c8aaa66153dd8e2cad5950942"


def _research(tmp_path):
    project = Project.create(tmp_path / "research.frisket", name="Research")
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


def test_create_is_idempotent_and_lifecycle_updates_use_revision_cas(tmp_path) -> None:
    project, store, research = _research(tmp_path)
    try:
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

        paused = store.update(
            research["id"],
            expected_revision=1,
            state="paused",
            saved_messages=[{"role": "assistant", "content": "partial"}],
            pending_approval={"operation_id": "write-1"},
            output_grants=[{"column_id": 8}],
        )
        assert paused["revision"] == 2
        assert paused["state"] == "paused"
        assert paused["saved_messages"][0]["content"] == "partial"
        assert paused["pending_approval"] == {"operation_id": "write-1"}
        assert paused["output_grants"] == [{"column_id": 8}]
        assert paused["turn_count"] == 0
        with pytest.raises(ProjectQAResearchConflictError, match="revision"):
            store.update(research["id"], expected_revision=1, state="running")
    finally:
        project.close()


def test_admission_is_exact_idempotent_and_settlement_replaces_reservation(
    tmp_path,
) -> None:
    project, store, research = _research(tmp_path)
    try:
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

        limited = store.update(research["id"], expected_revision=1, max_turns=1)
        assert limited["turn_count"] == 1
        with pytest.raises(ProjectQAResearchTurnLimitReached):
            store.admit_operation(
                research["id"],
                operation_id="call-3",
                payload_identity="sha256:three",
                operation_kind="model",
                estimate_micros=1,
            )
    finally:
        project.close()


def test_two_connections_cannot_overcommit_one_budget(tmp_path) -> None:
    project, _store, research = _research(tmp_path)
    path = project.path
    project.close()
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def admit(operation_id: str) -> None:
        opened = Project(path)
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
        finally:
            opened.close()

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


def test_research_schema_migration_preserves_existing_project_data(tmp_path) -> None:
    project, _store, research_row = _research(tmp_path)
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

    reopened = Project(path)
    try:
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
    finally:
        reopened.close()
