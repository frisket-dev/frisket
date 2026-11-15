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
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY

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
from frisket.engine.store.value_codec import decode_stored_value
from frisket.execution.attempt import StaleAttemptWriter
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
        model_call_policy=LOCAL_PURCHASE_POLICY,
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
    # the terminal receipt transaction has not committed.
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
    assert op_spec.pop("execution_sources") == {
        "sheet_ids": [project._sheet_id],
        "column_ids": [column["id"] for column in project.columns(project._sheet_id)],
    }
    request = bound.request.model_dump(mode="json", exclude={"confirmation"})
    assert {key: op_spec[key] for key in request} == request
    assert (
        typed_action_for_request({key: op_spec[key] for key in request}).params
        == bound.params
    )
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
    assert checkpoints == []  # Each result retired its checkpoint atomically.
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM sheets WHERE name='Beat Summaries'"
        ).fetchone()[0]
        == 1
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        == 2
    )
    # Returning from the interrupted invocation releases its dispatch owner,
    # while the run and paid facts remain recoverable immediately.
    assert attempt["state"] == "halted"
    assert (
        project.db.execute(
            "SELECT current_attempt_id FROM runs WHERE id=?", (run_id,)
        ).fetchone()[0]
        is None
    )

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
    result_rows = project.db.execute(
        "SELECT value_kind, value FROM results WHERE run_id=?", (run_id,)
    ).fetchall()
    assert {row["value_kind"] for row in result_rows} == {"text"}
    values = {
        decode_stored_value(row["value_kind"], row["value"]) for row in result_rows
    }
    assert values == {
        "Accountability stories share contracting risk.",
        "Infrastructure stories share service disruption risk.",
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
    assert closed["state"] == "halted"
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
    assert run["current_attempt_id"] is None
    attempt = project.db.execute(
        "SELECT state FROM execution_attempts WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert attempt is not None and attempt["state"] == "halted"

    checkpoints = project.db.execute(
        "SELECT state, authorized_attempt_id FROM effect_checkpoints ORDER BY id"
    ).fetchall()
    assert checkpoints == []
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
    assert reservation["run_id"] == run_id
    assert reservation["status"] == "running"
    reservation_body = Receipt.model_validate_json(str(reservation["body"]))
    assert reservation_body.run_id == run_id
    assert reservation_body.status == "running"

    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM sheets WHERE name='Beat Summaries'"
        ).fetchone()[0]
        == 1
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        == 2
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        == 1
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
    plans = list(reduces._reduce_group_summary_unit_plans(params, resolved))
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
        model_call_policy=LOCAL_PURCHASE_POLICY,
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


@pytest.mark.skipif(
    __import__("os").name != "posix", reason="SIGKILL process-death contract"
)
def test_group_refresh_recovers_committed_state_after_sigkill(project, tmp_path):
    import signal
    import subprocess
    import sys
    from pathlib import Path
    from contextlib import closing

    project.set_provider_key(
        provider="anthropic",
        encrypted=encrypt_secret("sk-ant-test"),
        hint=key_hint("sk-ant-test"),
        spend_cap_micro=100_000,
    )
    router = _project_key_router(_CountingSummaryAdapter())
    original = _run(project, _action(project._sheet_id, key="before-kill"), router)
    assert original.status == "completed", original.errors
    sheet_id = original.outputs[0].sheet_id
    path = project.path
    project.close()
    child = tmp_path / "kill-group.py"
    child.write_text("""
import os,signal,sys
from pathlib import Path
sys.path[:0]=[sys.argv[3],str(Path(sys.argv[3]).parent)]
from test_reduce_effect_checkpoint import _CountingSummaryAdapter,_project_key_router,_run
from frisket.engine.store import Project
from frisket.engine.executor.group_summary_publication import GroupPublication
p=Project(Path(sys.argv[1]))
original=GroupPublication.publish
count=0
def publish(self,value):
 global count
 count+=1
 if count == 2:
  os.kill(os.getpid(),signal.SIGKILL)
 return original(self,value)
GroupPublication.publish=publish
_run(p,{'action_id':'sheet.refresh','scope':{'kind':'project'},'params':{'sheet_id':int(sys.argv[2])},'idempotency_key':'killed-refresh'},_project_key_router(_CountingSummaryAdapter()))
raise RuntimeError('death hook was not reached')
""")
    child_run = subprocess.run(
        [
            sys.executable,  # subprocess-boundary: real SIGKILL after a committed batch
            str(child),
            str(path),
            str(sheet_id),
            str(Path(__file__).resolve().parent),
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert child_run.returncode == -signal.SIGKILL, child_run.stdout + child_run.stderr
    with closing(Project(path)) as reopened:
        receipt = reopened.db.execute(
            "SELECT * FROM receipts WHERE idempotency_key='killed-refresh'"
        ).fetchone()
        run_id = receipt["run_id"]
        assert receipt["status"] == "running"
        assert (
            reopened.db.execute(
                "SELECT state FROM execution_attempts WHERE run_id=? ORDER BY seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()[0]
            == "dispatching"
        )
        assert (
            reopened.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 1
        )
        assert (
            reopened.db.execute(
                "SELECT COUNT(*) FROM effect_checkpoints c JOIN execution_attempts a ON a.id=c.authorized_attempt_id WHERE a.run_id=? AND c.state='returned'",
                (run_id,),
            ).fetchone()[0]
            == 1
        )
        reopened.db.execute(
            "UPDATE execution_attempts SET created_at='2000-01-01' WHERE run_id=?",
            (run_id,),
        )
        reopened.db.execute(
            "UPDATE output_column_claims SET lease_expires_at='2000-01-01' WHERE run_id=?",
            (run_id,),
        )
        reopened.db.execute(
            "UPDATE sheets SET refresh_lease_expires_at='2000-01-01' WHERE id=?",
            (sheet_id,),
        )
        reopened.db.commit()
        # Read-only ownership inspection does not mutate or abandon an attempt.
        assert (
            reduces._recoverable_group_run(reopened, receipt, receipt["params_hash"])
            is None
        )
        assert (
            reopened.db.execute(
                "SELECT state FROM execution_attempts WHERE run_id=? ORDER BY seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()[0]
            == "dispatching"
        )
        provider = _CountingSummaryAdapter()
        recovered = _run(
            reopened,
            {
                "action_id": "sheet.refresh",
                "scope": {"kind": "project"},
                "params": {"sheet_id": sheet_id},
                "idempotency_key": "killed-refresh",
            },
            _project_key_router(provider),
        )
        assert recovered.status == "completed", recovered.errors
        assert recovered.run_id == run_id
        assert recovered.outputs[0].sheet_id == sheet_id
        assert provider.requests == []
        assert (
            reopened.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 2
        )
        assert reopened.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 2


@pytest.mark.parametrize("terminal_error", [False, True])
def test_group_retry_keeps_every_paid_wire_in_checkpoint_and_replays_once(
    project, monkeypatch, terminal_error
):
    from frisket.ai.llm import LLMError

    project.set_provider_key(
        provider="anthropic",
        encrypted=encrypt_secret("sk-ant-test"),
        hint=key_hint("sk-ant-test"),
        spend_cap_micro=500_000,
    )

    class RetryAdapter(_CountingSummaryAdapter):
        async def complete(self, req, client, **kwargs):
            response = await super().complete(req, client, **kwargs)
            odd = len(self.requests) % 2 == 1
            if odd or terminal_error:
                response.tokens_in, response.tokens_out = (11, 3) if odd else (19, 5)
                response.cost = 0.002 if odd else 0.004
                error = LLMError("returned malformed output", retryable=odd)
                error.wire_calls = [response]
                raise error
            return response

    adapter = RetryAdapter()
    router = _project_key_router(adapter)
    router.max_retries = 1
    action = _action(project._sheet_id, key=f"retry-paid-{terminal_error}")
    original = reduces._write_reduce_group_summary_result
    crashes = [True]

    def crash_once(*args, **kwargs):
        if crashes.pop() if crashes else False:
            raise RuntimeError("after retry checkpoints")
        return original(*args, **kwargs)

    monkeypatch.setattr(reduces, "_write_reduce_group_summary_result", crash_once)
    first = _run(project, action, router)
    assert first.status == "failed"
    assert len(adapter.requests) == 4
    run_id = project.db.execute("SELECT MAX(id) FROM runs").fetchone()[0]
    calls = RunResultStore(project).model_calls(run_id)
    assert len(calls) == len({call["id"] for call in calls}) == 4
    expected = 0.012 if terminal_error else 0.016
    assert sum(call["provider_cost_usd"] for call in calls) == pytest.approx(expected)
    # Incremental publication has already retired each returned checkpoint;
    # the injected failure happened before the final receipt, not row writes.
    assert project.db.execute("SELECT state FROM effect_checkpoints").fetchall() == []
    recovered = _run(project, action, router)
    assert recovered.status == ("failed" if terminal_error else "completed"), (
        recovered.errors
    )
    assert len(adapter.requests) == 4
    assert len(RunResultStore(project).model_calls(run_id)) == 4
    assert project.db.execute(
        "SELECT cost_actual FROM runs WHERE id=?", (run_id,)
    ).fetchone()[0] == pytest.approx(expected)


def test_group_known_retry_usage_survives_unknown_final_request_without_rebuy(project):
    from frisket.ai.llm import LLMError

    class Adapter(_CountingSummaryAdapter):
        async def complete(self, req, client, **kwargs):
            response = await super().complete(req, client, **kwargs)
            error = LLMError(
                "unresolved response",
                retryable=len(self.requests) == 1,
                post_egress_ambiguous=len(self.requests) > 1,
            )
            if len(self.requests) == 1:
                response.cost = 0.002
                error.wire_calls = [response]
            raise error

    provider = Adapter()
    router = _project_key_router(provider)
    router.max_retries = 1
    action = _action(project._sheet_id, key="known-then-unknown")
    first = _run(project, action, router)
    assert first.status == "failed"
    assert any(
        error.code == "external_effect_reconciliation_required"
        for error in first.errors
    ), first.errors
    run_id = project.db.execute("SELECT MAX(id) FROM runs").fetchone()[0]
    calls = RunResultStore(project).model_calls(run_id)
    assert len(calls) == 1 and calls[0]["provider_cost_usd"] == pytest.approx(0.002)
    assert [
        row[0] for row in project.db.execute("SELECT state FROM effect_checkpoints")
    ] == ["reserved"]
    again = _run(project, action, router)
    assert again.status == "failed"
    assert len(provider.requests) == 2
    assert len(RunResultStore(project).model_calls(run_id)) == 1
