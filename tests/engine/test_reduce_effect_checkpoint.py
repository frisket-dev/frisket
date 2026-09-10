"""Crash-window regressions for the paid reduce.group_summary effect loop.

Hardening spec Lane 2.3: ``_complete_reduce_group_summaries`` used to buy N
per-group provider responses and persist nothing until the result transaction
after the loop — process death re-bought every group on retry, and the facts
carried NULL ``attempt_id`` (settlement-invisible).  These tests pin the port
onto the shared effect-checkpoint store: spend and facts are durable the
moment each group's response returns, a retry replays without re-calling, an
ambiguous reservation refuses by name, and free/local work writes nothing.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from executor_harness import run_action_with_confirmation
from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.ai.llm.adapters import OpenAICompatAdapter
from frisket.contracts.action import Receipt
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor import group_summary_runtime as reduces
from frisket.engine.executor.group_summary_action import prepare_group_summary_action
from frisket.engine.store import Project
from frisket.engine.store.effect_checkpoints import EffectCheckpointStore
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt import (
    StaleAttemptWriter,
    abandon_stale_dispatching_attempts,
)
from frisket.team.security.secrets import encrypt_secret, key_hint

PROJECT_ID = "project-reduce-effects"


class _CountingSummaryAdapter(OpenAICompatAdapter):
    def __init__(self) -> None:
        super().__init__("test", "http://127.0.0.1:11434/v1")
        self.requests: list[LLMRequest] = []

    async def complete(  # noqa: ANN001
        self, req: LLMRequest, client, *, cost_model: str | None = None
    ) -> LLMResponse:
        del client, cost_model
        self.requests.append(req)
        prompt = str(req.messages[-1]["content"])
        summary = (
            "Accountability stories share contracting risk."
            if "accountability" in prompt
            else "Infrastructure stories share service disruption risk."
        )
        return LLMResponse(
            content=summary,
            data=None,
            tokens_in=83,
            tokens_out=17,
            cost=0.006,
            model=req.model,
        )


@pytest.fixture()
def project(tmp_path):
    p = Project.create(tmp_path / "reduce-effects.frisket", name="reduce-effects")
    sheet_id = p.add_sheet("Feature Tour")
    columns = {
        "story": p.add_column(sheet_id, "story", type="text"),
        "beat": p.add_column(sheet_id, "beat", type="text"),
    }
    p.add_rows(
        sheet_id,
        [
            {
                "story": "City hall awarded a no-bid software contract",
                "beat": "accountability",
            },
            {
                "story": "Audit found duplicate vendor payments",
                "beat": "accountability",
            },
            {"story": "Water main repairs closed two blocks", "beat": "infrastructure"},
        ],
        columns,
    )
    p._sheet_id = sheet_id  # type: ignore[attr-defined]
    try:
        yield p
    finally:
        p.close()


def _action(
    sheet_id: int,
    *,
    model: str = "anthropic/claude-haiku-4-5",
    key: str = "reduce_effects@sha256:stable",
) -> dict[str, Any]:
    return {
        "action_id": "reduce.group_summary",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "sheet_name": "Beat Summaries",
        "params": {
            "source": ["story"],
            "group_by": "beat",
            "model": model,
            "instruction": "Summarize common reporting risks for each beat.",
        },
        "idempotency_key": key,
    }


def _project_key_router(provider: _CountingSummaryAdapter) -> ModelRouter:
    router = ModelRouter(
        keys={"anthropic": "sk-ant-test"},
        key_sources={"anthropic": "project_key"},
        cache=None,
        cache_mode="off",
    )
    router._adapters["anthropic"] = provider  # noqa: SLF001
    return router


def _run(project: Project, action: dict[str, Any], router: ModelRouter):
    return run_action_with_confirmation(
        project, action, project_id=PROJECT_ID, router=router
    )


def test_returned_reduce_checkpoint_accounts_spend_before_result_commit(
    project, monkeypatch
) -> None:
    """The reduce twin of the MapRunner regression: crash after the provider
    returned, before the result transaction — spend and facts are already
    durable under the authorizing attempt, and the retry materializes the
    summary sheet without calling the provider again."""

    # The cap leaves headroom: the pre-flight spend-cap gate runs before the
    # replay-only retry, and an exactly-exhausted key would refuse the resume
    # itself.  Cap VISIBILITY of the crashed spend is asserted directly below.
    project.set_provider_key(
        provider="anthropic",
        encrypted=encrypt_secret("sk-ant-test"),
        hint=key_hint("sk-ant-test"),
        spend_cap_micro=50_000,
    )
    provider = _CountingSummaryAdapter()
    router = _project_key_router(provider)
    action = _action(project._sheet_id)

    real_write = reduces._write_reduce_group_summary_result
    crashes = [True]

    def crash_once_before_result_commit(*args, **kwargs):
        if crashes[0]:
            crashes[0] = False
            raise RuntimeError("injected crash after accounted provider returns")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(
        reduces, "_write_reduce_group_summary_result", crash_once_before_result_commit
    )

    crashed = _run(project, action, router)
    assert crashed.status == "failed"
    assert "injected crash" in crashed.errors[0].message

    # Both per-group provider invoices are durable truth already: facts under
    # the authorizing attempt, project-key cap spend, run cost — even though
    # no summary sheet, result cell, or receipt committed.
    assert len(provider.requests) == 2
    run_id = int(project.db.execute("SELECT MAX(id) FROM runs").fetchone()[0])
    # A crash leaves the pre-egress op visible. It must retain the same public
    # typed request as the final op, not nest the host's private physical plan
    # inside Params (which cannot be rebound as a group-summary request).
    op_spec = json.loads(
        project.db.execute(
            "SELECT o.spec FROM ops o JOIN runs r ON r.op_id=o.id WHERE r.id=?",
            (run_id,),
        ).fetchone()[0]
    )
    bound = typed_action_for_request(action)
    assert op_spec == bound.request.model_dump(mode="json", exclude={"confirmation"})
    assert typed_action_for_request(op_spec).params == bound.params
    attempt = project.db.execute(
        "SELECT id, state FROM execution_attempts WHERE run_id=?", (run_id,)
    ).fetchone()
    assert attempt is not None
    calls = project.db.execute(
        "SELECT attempt_id, provider_cost_usd, credential_source "
        "FROM model_calls WHERE run_id=?",
        (run_id,),
    ).fetchall()
    assert len(calls) == 2
    assert {call["attempt_id"] for call in calls} == {attempt["id"]}
    assert {call["provider_cost_usd"] for call in calls} == {0.006}
    assert {call["credential_source"] for call in calls} == {"project_key"}
    # The crashed spend is already visible to the project-key cap: another
    # launch cannot treat these returned checkpoints as free headroom.
    assert project.provider_spend_state("anthropic").spent_micro == 12_000
    run = project.db.execute(
        "SELECT cost_actual FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert run["cost_actual"] == pytest.approx(0.012)
    checkpoints = project.db.execute("SELECT state FROM effect_checkpoints").fetchall()
    assert [row["state"] for row in checkpoints] == ["returned", "returned"]
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM sheets WHERE name='Beat Summaries'"
        ).fetchone()[0]
        == 0
    )
    # The writer stays live through the result/checkpoint transaction. This
    # assertion is the regression fence for the old pre-commit `effected`
    # transition.
    assert attempt["state"] == "dispatching"
    project.db.execute(
        "UPDATE execution_attempts SET created_at=datetime('now', '-7 hours') "
        "WHERE id=?",
        (attempt["id"],),
    )
    project.db.commit()
    assert abandon_stale_dispatching_attempts(project, run_id) == 1

    # Retry the same logical action: the durable responses replay, the sheet
    # materializes, and the provider is NOT called again.
    resumed = _run(project, action, router)
    assert resumed.status == "completed", resumed.errors
    assert len(provider.requests) == 2
    assert resumed.run_id == run_id
    summary_sheet = project.db.execute(
        "SELECT id FROM sheets WHERE name='Beat Summaries' AND hidden=0"
    ).fetchone()
    assert summary_sheet is not None
    values = {
        row["value"]
        for row in project.db.execute(
            "SELECT value FROM results WHERE run_id=?", (run_id,)
        ).fetchall()
    }
    assert values == {
        '"Accountability stories share contracting risk."',
        '"Infrastructure stories share service disruption risk."',
    }
    # No double count: the run's cost stays exactly the two accrued invoices,
    # the facts still belong to the original authorizing attempt, and the
    # checkpoints retired with the result commit.
    run = project.db.execute(
        "SELECT cost_actual, status FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert run["status"] == "completed"
    assert run["cost_actual"] == pytest.approx(0.012)
    assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 2
    assert (
        project.db.execute("SELECT COUNT(*) FROM effect_checkpoints").fetchone()[0] == 0
    )
    closed = project.db.execute(
        "SELECT state FROM execution_attempts WHERE id=?", (attempt["id"],)
    ).fetchone()
    assert closed["state"] == "abandoned"
    replacement = project.db.execute(
        "SELECT id, state FROM execution_attempts WHERE run_id=? "
        "ORDER BY seq DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    assert replacement["id"] != attempt["id"]
    assert replacement["state"] == "effected"
    # One op, one run: the pre-egress accounting envelope IS the final op/run.
    assert resumed.op_ids == [
        int(
            project.db.execute(
                "SELECT op_id FROM runs WHERE id=?", (run_id,)
            ).fetchone()[0]
        )
    ]


def test_reduce_terminalizer_joins_materialization_transaction(
    project, monkeypatch
) -> None:
    """A failure after the kernel writes still rolls back the caller's txn."""

    project.set_provider_key(
        provider="anthropic",
        encrypted=encrypt_secret("sk-ant-test"),
        hint=key_hint("sk-ant-test"),
        spend_cap_micro=50_000,
    )
    provider = _CountingSummaryAdapter()
    router = _project_key_router(provider)
    action = _action(
        project._sheet_id,
        key="reduce_effects@sha256:terminal-transaction-join",
    )

    real_terminalize = reduces.terminalize_project_run

    def cut_after_join(*args, **kwargs):
        assert project.db.in_transaction
        assert kwargs["commit"] is False
        terminalization = real_terminalize(*args, **kwargs)
        assert terminalization.disposition == "terminalized"
        assert terminalization.receipt_disposition == "updated"

        run_id = int(kwargs["run_id"])
        run = project.db.execute(
            "SELECT status, finished_at, current_attempt_id FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert run is not None
        assert run["status"] == "completed"
        assert run["finished_at"] is not None
        assert run["current_attempt_id"] is None
        attempt = project.db.execute(
            "SELECT state FROM execution_attempts WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert attempt is not None and attempt["state"] == "effected"
        assert (
            project.db.execute("SELECT COUNT(*) FROM effect_checkpoints").fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 2
        )
        raise StaleAttemptWriter(
            "injected after joined reduce terminalization, before outer commit"
        )

    monkeypatch.setattr(reduces, "terminalize_project_run", cut_after_join)

    result = _run(project, action, router)
    assert result.status == "failed"
    assert result.errors[0].code == "stale_attempt_writer"
    assert "injected after joined reduce terminalization" in result.errors[0].message
    assert len(provider.requests) == 2

    run_id = int(project.db.execute("SELECT MAX(id) FROM runs").fetchone()[0])
    run = project.db.execute(
        "SELECT status, finished_at, current_attempt_id FROM runs WHERE id=?",
        (run_id,),
    ).fetchone()
    assert run is not None
    assert run["status"] == "running"
    assert run["finished_at"] is None
    attempt_id = str(run["current_attempt_id"])
    attempt = project.db.execute(
        "SELECT state FROM execution_attempts WHERE id=?",
        (attempt_id,),
    ).fetchone()
    assert attempt is not None and attempt["state"] == "dispatching"

    checkpoints = project.db.execute(
        "SELECT state, authorized_attempt_id FROM effect_checkpoints ORDER BY id"
    ).fetchall()
    assert [(row["state"], row["authorized_attempt_id"]) for row in checkpoints] == [
        ("returned", attempt_id),
        ("returned", attempt_id),
    ]
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM model_calls WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        == 2
    )

    reservation = project.db.execute(
        "SELECT run_id, status, body FROM receipts WHERE idempotency_key=?",
        (action["idempotency_key"],),
    ).fetchone()
    assert reservation is not None
    assert reservation["run_id"] is None
    assert reservation["status"] == "running"
    reservation_body = Receipt.model_validate_json(str(reservation["body"]))
    assert reservation_body.run_id is None
    assert reservation_body.status == "running"

    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM sheets WHERE name='Beat Summaries'"
        ).fetchone()[0]
        == 0
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        == 0
    )


def test_reserved_reduce_checkpoint_refuses_resume(project) -> None:
    """A ``reserved`` unit means a provider may have been reached with no
    durable response: resume refuses by name and never calls out."""

    provider = _CountingSummaryAdapter()
    router = _project_key_router(provider)
    action = _action(project._sheet_id, key="reduce_effects@sha256:reserved")

    prepared = prepare_group_summary_action(project, typed_action_for_request(action))
    params = prepared.operation
    resolved = prepared.resolved
    assert isinstance(resolved, dict)
    plans = reduces._reduce_group_summary_unit_plans(params, resolved)
    group_key = reduces._reduce_group_summary_effect_group_key(params)
    store = EffectCheckpointStore(project.db)
    assert store.reserve(
        "cp-ambiguous-reduce-unit",
        family="reduce_group_summary",
        group_key=group_key,
        unit_key=plans[0]["unit_key"],
        action_kind="reduce.group_summary",
        identity=plans[0]["identity"],
    )

    result = _run(project, action, router)
    assert result.status == "failed"
    assert result.errors[0].code == "external_effect_reconciliation_required"
    assert provider.requests == []
    # The ambiguous reservation is untouched: still an explicit
    # possible-effect reconciliation record.
    ambiguous = store.get("cp-ambiguous-reduce-unit")
    assert ambiguous is not None and ambiguous["state"] == "reserved"


def test_free_local_reduce_effect_makes_no_checkpoints(project) -> None:
    """A local-provider reduce spends nothing and must produce zero durable
    checkpoint writes, while its run-scoped result still uses a claimed
    execution attempt."""

    provider = _CountingSummaryAdapter()
    router = ModelRouter(
        keys={},
        cache=None,
        cache_mode="off",
        local_endpoints=(
            LocalModelEndpointConfig(
                endpoint_id="test-local",
                display_name="Test local",
                origin="http://127.0.0.1:11434",
                source="local_file",
            ),
        ),
    )
    router._adapters["ollama"]._adapters["test-local"] = provider  # type: ignore[attr-defined]  # noqa: SLF001
    action = _action(
        project._sheet_id,
        model="ollama/@test-local/llama3",
        key="reduce_effects@sha256:free-local",
    )

    result = _run(project, action, router)
    assert result.status == "completed", result.errors
    assert len(provider.requests) == 2
    assert (
        project.db.execute("SELECT COUNT(*) FROM effect_checkpoints").fetchone()[0] == 0
    )
    run_id = int(result.run_id)
    attempt = project.db.execute(
        "SELECT run_id, state FROM execution_attempts"
    ).fetchone()
    assert attempt is not None
    assert attempt["run_id"] == run_id
    assert attempt["state"] == "effected"
    assert RunResultStore(project).model_calls(run_id)
