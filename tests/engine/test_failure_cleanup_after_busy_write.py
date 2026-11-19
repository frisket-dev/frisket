"""Failure handlers clean up after a write refused by a busy database.

A plain statement refused with ``database is locked`` leaves the implicit
transaction that preceded it open on the project connection. Each handler
here must roll that failed work back, still perform its cleanup write, and
surface the original failure, leaving no transaction open.
"""

from __future__ import annotations

import importlib
import sqlite3
from collections.abc import Callable
from typing import Any

import pytest

from executor_harness import case_env
from frisket.engine.executor import (
    ExecutorDeps,
    group_summary_runtime,
    run_action_spec,
)
from frisket.engine.store import Project
from test_embedding_effect_checkpoint import project as project
from test_embedding_lifecycle_replay_hardening import (
    FakeProbeGateway,
    _OpenRouterRouter,
    _create_action,
)


def refuse_while_locked(project: Project, write: Callable[[], Any]) -> None:
    """Run ``write`` while another connection holds the write lock."""
    project.db.execute("PRAGMA busy_timeout=50")
    holder = sqlite3.connect(project.db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            write()
    finally:
        holder.rollback()
        holder.close()
        project.db.execute("PRAGMA busy_timeout=10000")
    assert project.db.in_transaction


def test_embedding_probe_releases_its_reservation_after_a_busy_change(
    project, monkeypatch
):
    original = Project.append_change

    def busy_probe_change(self, kind, *args, **kwargs):
        if kind == "embedding.index_create":
            refuse_while_locked(self, lambda: original(self, kind, *args, **kwargs))
            raise sqlite3.OperationalError("database is locked")
        return original(self, kind, *args, **kwargs)

    monkeypatch.setattr(Project, "append_change", busy_probe_change)
    gateway = FakeProbeGateway(dim=1024)

    result = run_action_spec(
        project,
        _create_action(
            project._sheet_id,
            key="busy-probe",
            provider="openrouter",
            model="openai/text-embedding-3-large",
            provider_policy={"allow_remote": True},
        ),
        project_id="p",
        deps=ExecutorDeps(embedding_gateway=gateway, router=_OpenRouterRouter()),
    )

    assert result.status == "failed"
    assert result.errors[0].code == "project_write_failed"
    assert not gateway.calls
    assert not project.db.in_transaction
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_group_summary_releases_its_reservation_after_a_busy_write(
    tmp_path, monkeypatch
):
    module = importlib.import_module("tests.engine.test_reduce_group_summary_executor")
    with case_env(module.CASES[0], tmp_path, monkeypatch) as env:
        action = env.case.make_action(env.seeded)
        sheet_id = int(env.project.db.execute("SELECT id FROM sheets").fetchone()[0])

        async def busy_completion(*_args: Any, **_kwargs: Any) -> Any:
            refuse_while_locked(
                env.project,
                lambda: env.project.db.execute(
                    "UPDATE sheets SET name=name WHERE id=?", (sheet_id,)
                ),
            )
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(
            group_summary_runtime, "_complete_reduce_group_summaries", busy_completion
        )

        result = env.run(action)

        assert result.status == "failed"
        assert result.errors[0].code == "model_run_failed"
        assert not env.project.db.in_transaction
        assert (
            env.project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE idempotency_key=?",
                (action["idempotency_key"],),
            ).fetchone()[0]
            == 0
        )
