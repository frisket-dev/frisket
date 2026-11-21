"""Failure handlers clean up after work that failed with writes pending.

Failed work can leave its implicit transaction open with earlier successful
writes in it. Each handler here must roll that failed work back, still
perform its cleanup write, and surface the original failure, leaving no
transaction open.
"""

from __future__ import annotations

import importlib
import sqlite3
from typing import Any

from executor_harness import case_env
from frisket.engine.executor import (
    ExecutorDeps,
    group_summary_runtime,
    run_action_spec,
)
from frisket.engine.executor.action_families import embeddings
from frisket.engine.store import Project
from frisket.execution.attempt import AttemptClaimRefused
from test_embedding_effect_checkpoint import project as project
from test_embedding_lifecycle_replay_hardening import (
    FakeProbeGateway,
    _OpenRouterRouter,
    _create_action,
)


def fail_with_a_pending_write(project: Project) -> None:
    """Write without committing, then fail as a later statement would."""
    project.db.execute("UPDATE sheets SET name='half-finished'")
    assert project.db.in_transaction
    raise sqlite3.OperationalError("database is locked")


def _probe_action(project, key: str) -> dict[str, Any]:
    return _create_action(
        project._sheet_id,
        key=key,
        provider="openrouter",
        model="openai/text-embedding-3-large",
        provider_policy={"allow_remote": True},
    )


def _confirmed_probe_action(project, key: str, *, gateway: Any) -> dict[str, Any]:
    action = _probe_action(project, key)
    challenge = run_action_spec(
        project,
        action,
        project_id="p",
        deps=ExecutorDeps(embedding_gateway=gateway, router=_OpenRouterRouter()),
    )
    assert challenge.status == "needs_confirmation", challenge.errors
    assert not gateway.calls
    return {
        **action,
        "confirmation": challenge.errors[0].details["promise_set_hash"],
    }


def test_embedding_probe_releases_its_reservation_after_failed_change(
    project, monkeypatch
):
    original = Project.append_change

    def failing_probe_change(self, kind, *args, **kwargs):
        if kind == "embedding.index_create":
            fail_with_a_pending_write(self)
        return original(self, kind, *args, **kwargs)

    monkeypatch.setattr(Project, "append_change", failing_probe_change)
    gateway = FakeProbeGateway(dim=1024)
    action = _confirmed_probe_action(project, "failed-probe-change", gateway=gateway)

    result = run_action_spec(
        project,
        action,
        project_id="p",
        deps=ExecutorDeps(embedding_gateway=gateway, router=_OpenRouterRouter()),
    )

    assert result.status == "failed"
    assert result.errors[0].code == "project_write_failed"
    assert not gateway.calls
    assert not project.db.in_transaction
    assert project.db.execute("SELECT name FROM sheets").fetchone()[0] == "data"
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_group_summary_releases_its_reservation_after_failed_work(
    tmp_path, monkeypatch
):
    module = importlib.import_module("tests.engine.test_reduce_group_summary_executor")
    with case_env(module.CASES[0], tmp_path, monkeypatch) as env:
        action = env.case.make_action(env.seeded)
        names = [row[0] for row in env.project.db.execute("SELECT name FROM sheets")]

        async def failing_completion(*_args: Any, **_kwargs: Any) -> Any:
            fail_with_a_pending_write(env.project)

        monkeypatch.setattr(
            group_summary_runtime,
            "_complete_reduce_group_summaries",
            failing_completion,
        )

        result = env.run(action)

        assert result.status == "failed"
        assert result.errors[0].code == "model_run_failed"
        assert not env.project.db.in_transaction
        assert [
            row[0] for row in env.project.db.execute("SELECT name FROM sheets")
        ] == names
        assert (
            env.project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE idempotency_key=?",
                (action["idempotency_key"],),
            ).fetchone()[0]
            == 0
        )


def test_embedding_probe_closes_its_run_after_refused_work_left_writes_pending(
    project, monkeypatch
):
    def refuse_after_a_pending_write(project, **_kwargs):
        project.db.execute("UPDATE sheets SET name='half-finished'")
        raise AttemptClaimRefused("claim_lost", "attempt claim refused")

    monkeypatch.setattr(
        embeddings, "mint_direct_effect_attempt", refuse_after_a_pending_write
    )
    gateway = FakeProbeGateway(dim=1024)
    action = _confirmed_probe_action(project, "refused-probe", gateway=gateway)

    result = run_action_spec(
        project,
        action,
        project_id="p",
        deps=ExecutorDeps(embedding_gateway=gateway, router=_OpenRouterRouter()),
    )

    assert result.status == "failed"
    assert result.errors[0].code == "project_write_failed"
    assert not gateway.calls
    assert not project.db.in_transaction
    assert project.db.execute("SELECT name FROM sheets").fetchone()[0] == "data"
    assert [row[0] for row in project.db.execute("SELECT status FROM runs")] == [
        "failed"
    ]
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
