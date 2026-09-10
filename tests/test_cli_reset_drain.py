"""PC-9 stop-the-world reset-drain preflight (base component)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import sqlalchemy as sa

from frisket.engine.jobs.queue import (
    QUEUE_DB_NAME,
    SqlAlchemyJobQueue,
    jobs_table,
    open_queue,
)
from frisket.engine.store import Project
from frisket.engine.store.effect_checkpoints import EffectCheckpointStore
from frisket.engine.store.output_claims import OutputColumnClaimStore


def _seed_nonterminal_attempt(project: Project) -> None:
    sheet_id = project.add_sheet("Reset drain")
    op_id = project.db.execute(
        "INSERT INTO ops (kind, spec) VALUES ('classify', '{}')"
    ).lastrowid
    run_id = project.db.execute(
        "INSERT INTO runs (op_id, sheet_id, action_kind) VALUES (?, ?, 'map.classify')",
        (op_id, sheet_id),
    ).lastrowid
    project.db.execute(
        "INSERT INTO execution_attempts "
        "(id, run_id, seq, state, action_identity_hash, scope_json, created_at) "
        "VALUES ('attempt_reset_drain', ?, 1, 'admitted', 'sha256:test', '[]', "
        "'2026-07-31T00:00:00+00:00')",
        (run_id,),
    )
    project.db.commit()


def test_base_reset_drain_is_clean_for_empty_queue_and_nested_fresh_bundle(
    tmp_path: Path,
) -> None:
    from frisket.operability.reset_drain import inspect_base_reset_drain

    queue_workspace = tmp_path / "queue"
    queue = open_queue(workspace=queue_workspace)
    queue.close()

    projects_root = tmp_path / "projects"
    project = Project.create(projects_root / "7" / "demo.frisket", name="demo")
    project.close()
    entries_before = sorted(
        path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")
    )

    report = inspect_base_reset_drain(
        queue_locator=queue_workspace / QUEUE_DB_NAME,
        projects_root=projects_root,
    )

    assert report["schema_version"] == "frisket.reset_drain_report.v1"
    assert report["scope"] == "base"
    assert report["ready"] is True
    assert report["hit_count"] == 0
    assert report["errors"] == []
    assert report["queue"] == {
        "jobs": {"total": 0, "by_status": {}},
        "handler_authorities": 0,
    }
    assert report["bundles"]["scanned"] == 1
    assert (
        sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
        == entries_before
    )


def test_base_reset_drain_reports_every_hit_and_authority_independent_of_status(
    tmp_path: Path,
) -> None:
    from frisket.operability.reset_drain import inspect_base_reset_drain

    queue_workspace = tmp_path / "queue"
    queue = open_queue(workspace=queue_workspace)
    assert isinstance(queue, SqlAlchemyJobQueue)
    job_id = queue.enqueue("echo", {"value": "queued work"})
    claimed = queue.claim("reset-drain-test-worker")
    assert claimed is not None and claimed.id == job_id
    # Deliberately make job status terminal without acknowledging the handler
    # authority. The preflight must inspect both tables independently.
    with queue.engine.begin() as connection:
        connection.execute(
            jobs_table.update()
            .where(jobs_table.c.id == job_id)
            .values(status="done", finished_at=sa.func.now())
        )
    queue.close()

    projects_root = tmp_path / "projects"
    project = Project.create(projects_root / "9" / "busy.frisket", name="busy")
    sheet_id = project.add_sheet("Claims")
    claims, conflict = OutputColumnClaimStore(project).acquire(
        sheet_id=sheet_id,
        output_names=["generated"],
        action_kind="map.classify",
    )
    assert len(claims) == 1 and conflict is None
    OutputColumnClaimStore(project).release(
        claim_token=str(claims[0]["claim_token"]), status="released"
    )
    assert EffectCheckpointStore(project.db).reserve(
        "checkpoint_reset_drain",
        family="model_call",
        group_key="map.classify",
        unit_key="row:1",
        action_kind="map.classify",
        identity="sha256:test",
    )
    _seed_nonterminal_attempt(project)
    project.close()

    report = inspect_base_reset_drain(
        queue_locator=queue_workspace / QUEUE_DB_NAME,
        projects_root=projects_root,
    )

    assert report["ready"] is False
    assert report["hit_count"] == 5
    assert report["queue"] == {
        "jobs": {"total": 1, "by_status": {"done": 1}},
        "handler_authorities": 1,
    }
    assert report["bundles"]["output_claims"] == 1
    assert report["bundles"]["effect_checkpoints"] == 1
    assert report["bundles"]["nonterminal_execution_attempts"] == 1
    assert report["bundles"]["items"] == [
        {
            "bundle": "9/busy.frisket",
            "output_claims": {"total": 1, "by_status": {"released": 1}},
            "effect_checkpoints": {"total": 1, "by_state": {"reserved": 1}},
            "nonterminal_execution_attempts": {
                "total": 1,
                "by_state": {"admitted": 1},
            },
        }
    ]


def test_reset_drain_command_emits_json_and_exits_nonzero_on_a_hit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from frisket.cli.reset_drain import reset_drain

    queue_workspace = tmp_path / "queue"
    queue = open_queue(workspace=queue_workspace)
    queue.enqueue("echo", {})
    queue.close()
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    monkeypatch.setenv(
        "FRISKET_RUN_QUEUE_DATABASE_URL",
        str(queue_workspace / QUEUE_DB_NAME),
    )
    monkeypatch.setenv("FRISKET_PROJECTS_ROOT", str(projects_root))

    assert reset_drain([]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "frisket.reset_drain_report.v1"
    assert report["ready"] is False
    assert report["queue"]["jobs"]["total"] == 1


def test_reset_drain_refuses_a_bundle_missing_its_database(tmp_path: Path) -> None:
    from frisket.operability.reset_drain import inspect_base_reset_drain

    queue_workspace = tmp_path / "queue"
    queue = open_queue(workspace=queue_workspace)
    queue.close()
    projects_root = tmp_path / "projects"
    (projects_root / "incomplete.frisket").mkdir(parents=True)

    report = inspect_base_reset_drain(
        queue_locator=queue_workspace / QUEUE_DB_NAME,
        projects_root=projects_root,
    )

    assert report["ready"] is False
    assert report["bundles"]["scanned"] == 1
    assert report["errors"] == [
        {
            "scope": "bundle",
            "code": "bundle_inspection_failed",
            "bundle": "incomplete.frisket",
        }
    ]


def test_reset_drain_refuses_a_symlinked_projects_subtree(tmp_path: Path) -> None:
    from frisket.operability.reset_drain import inspect_base_reset_drain

    queue_workspace = tmp_path / "queue"
    queue = open_queue(workspace=queue_workspace)
    queue.close()
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    outside = tmp_path / "outside"
    project = Project.create(outside / "busy.frisket", name="busy")
    sheet_id = project.add_sheet("Claims")
    OutputColumnClaimStore(project).acquire(
        sheet_id=sheet_id,
        output_names=["generated"],
        action_kind="map.classify",
    )
    project.close()
    (projects_root / "mounted").symlink_to(outside, target_is_directory=True)
    sealed = tmp_path / "sealed"
    sealed_target = sealed / "target"
    sealed_target.mkdir(parents=True)
    (projects_root / "unreadable-mount").symlink_to(
        sealed_target, target_is_directory=True
    )
    sealed.chmod(0)

    try:
        report = inspect_base_reset_drain(
            queue_locator=queue_workspace / QUEUE_DB_NAME,
            projects_root=projects_root,
        )
    finally:
        sealed.chmod(0o755)

    assert report["ready"] is False
    assert report["bundles"]["scanned"] == 0
    assert report["errors"] == [
        {
            "scope": "bundles",
            "code": "bundle_traversal_failed",
            "path": "mounted",
        },
        {
            "scope": "bundles",
            "code": "bundle_traversal_failed",
            "path": "unreadable-mount",
        },
    ]


def test_reset_drain_refuses_a_sqlite_rollback_journal(tmp_path: Path) -> None:
    from frisket.operability.reset_drain import inspect_base_reset_drain

    queue_workspace = tmp_path / "queue"
    queue = open_queue(workspace=queue_workspace)
    queue.close()
    queue_database = queue_workspace / QUEUE_DB_NAME
    Path(f"{queue_database}-journal").touch()
    projects_root = tmp_path / "projects"
    projects_root.mkdir()

    report = inspect_base_reset_drain(
        queue_locator=queue_database,
        projects_root=projects_root,
    )

    assert report["ready"] is False
    assert report["errors"] == [{"scope": "queue", "code": "queue_inspection_failed"}]


def test_reset_drain_checks_sidecars_at_a_symlinked_database_target(
    tmp_path: Path,
) -> None:
    from frisket.operability.reset_drain import inspect_base_reset_drain

    queue_workspace = tmp_path / "queue"
    queue = open_queue(workspace=queue_workspace)
    queue.close()
    authority = Project.create(tmp_path / "authority.frisket", name="authority")
    authority.close()
    authority_database = tmp_path / "authority.frisket" / "project.db"
    Path(f"{authority_database}-journal").touch()
    projects_root = tmp_path / "projects"
    linked_bundle = projects_root / "linked.frisket"
    linked_bundle.mkdir(parents=True)
    (linked_bundle / "project.db").symlink_to(authority_database)

    report = inspect_base_reset_drain(
        queue_locator=queue_workspace / QUEUE_DB_NAME,
        projects_root=projects_root,
    )

    assert report["ready"] is False
    assert report["errors"] == [
        {
            "scope": "bundle",
            "code": "bundle_inspection_failed",
            "bundle": "linked.frisket",
        }
    ]


def test_reset_drain_counts_a_null_attempt_state_as_nonterminal(tmp_path: Path) -> None:
    from frisket.operability.reset_drain import inspect_base_reset_drain

    queue_workspace = tmp_path / "queue"
    queue = open_queue(workspace=queue_workspace)
    queue.close()
    projects_root = tmp_path / "projects"
    bundle = projects_root / "malformed.frisket"
    bundle.mkdir(parents=True)
    connection = sqlite3.connect(bundle / "project.db")
    try:
        connection.execute("CREATE TABLE output_column_claims (status TEXT)")
        connection.execute("CREATE TABLE effect_checkpoints (state TEXT)")
        connection.execute("CREATE TABLE execution_attempts (state TEXT)")
        connection.execute("INSERT INTO execution_attempts VALUES (NULL)")
        connection.commit()
    finally:
        connection.close()

    report = inspect_base_reset_drain(
        queue_locator=queue_workspace / QUEUE_DB_NAME,
        projects_root=projects_root,
    )

    assert report["ready"] is False
    assert report["hit_count"] == 1
    assert report["bundles"]["items"][0]["nonterminal_execution_attempts"] == {
        "total": 1,
        "by_state": {"<null>": 1},
    }


def test_reset_drain_normalizes_postgres_and_starts_a_read_only_transaction(
    monkeypatch,
) -> None:
    from frisket.operability import reset_drain as drain_module

    events: list[str] = []
    connection = MagicMock()
    connection.begin.return_value.__enter__.return_value = None
    status_result = MagicMock()
    status_result.all.return_value = [("done", 2)]
    authority_result = MagicMock()
    authority_result.scalar_one.return_value = 3

    def _execute(statement):
        rendered = str(statement)
        events.append(rendered)
        return status_result if "FROM jobs" in rendered else authority_result

    connection.exec_driver_sql.side_effect = lambda statement: events.append(statement)
    connection.execute.side_effect = _execute
    engine = MagicMock()
    engine.dialect.name = "postgresql"
    engine.connect.return_value.__enter__.return_value = connection
    received_urls: list[sa.engine.URL] = []

    def _create_engine(url, *, future):
        assert future is True
        received_urls.append(url)
        return engine

    monkeypatch.setattr(drain_module.sa, "create_engine", _create_engine)

    assert drain_module._queue_counts(
        "postgresql://operator:secret@db.invalid/frisket_run_queue"
    ) == ({"done": 2}, 3)
    assert received_urls[0].drivername == "postgresql+psycopg"
    assert events[0] == "SET TRANSACTION READ ONLY"
    assert "FROM jobs" in events[1]
    assert "FROM job_handler_authorities" in events[2]
    engine.dispose.assert_called_once_with()


def test_reset_drain_parser_error_is_sanitized_json(capsys) -> None:
    from frisket.cli.reset_drain import reset_drain

    secret = "postgresql+psycopg://operator:pc9-super-secret@db.invalid/queue"
    assert reset_drain([secret]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "pc9-super-secret" not in captured.out
    assert json.loads(captured.out)["errors"] == [
        {"scope": "configuration", "code": "invalid_arguments"}
    ]
