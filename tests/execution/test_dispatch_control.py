"""Cancellation is checked at each dispatch, never by discarding a return."""

import asyncio
from contextlib import closing
from types import SimpleNamespace

import httpx
import pytest

from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.types import LLMRequest
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.changes import ChangeStore
from frisket.execution.dispatch_control import (
    STANDALONE,
    RunDispatch,
    dispatch_for_context,
)
from frisket.ops._sidecar import sidecar_post
from tests.engine.test_result_generation_store import _start_claimed_run
from tests.ai.test_structured_wire_failures import UsagePolicy


@pytest.fixture
def owned(tmp_path):
    with closing(Project.create(tmp_path / "dispatch.frisket")) as project:
        sheet = project.add_sheet("data")
        output = project.add_column(sheet, "answer", ai_generated=True)
        rows = project.add_rows(sheet, [{}], {})
        run = _start_claimed_run(
            project,
            sheet_id=sheet,
            output_column_id=output,
            row_ids=rows,
            label="dispatch",
        )
        with project.db:
            project.db.execute("BEGIN IMMEDIATE")
            ChangeStore(project).start(run.op_id)
        control = RunDispatch(
            project,
            run.run_id,
            run.authority.writer_attempt_id,
            claim_token=run.authority.claim_token,
        )
        control.check()
        yield project, run, control


def request(**kwargs):
    return LLMRequest(
        model="openai/gpt-4.1-mini",
        messages=[{"role": "user", "content": "hi"}],
        **kwargs,
    )


def response(text="done"):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        },
    )


@pytest.mark.asyncio
async def test_cancel_during_provider_failure_prevents_retry(owned):
    project, run, control = owned
    calls = []

    def respond(req):
        calls.append(req)
        RunResultStore(project).request_cancel(run.run_id)
        return httpx.Response(429, json={"error": {"message": "busy"}})

    router = ModelRouter(keys={"openai": "test"}, use_env_keys=False, max_retries=2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        router._client = client
        with pytest.raises(asyncio.CancelledError):
            await router.for_execution(control).complete(request())
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_return_after_cancel_retains_usage_and_response(owned):
    project, run, control = owned
    policy = UsagePolicy()

    def respond(req):
        RunResultStore(project).request_cancel(run.run_id)
        return response()

    router = ModelRouter(
        keys={"openai": "test"}, use_env_keys=False, model_call_policy=policy
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        router._client = client
        result = await router.for_execution(control).complete(request())
    assert result.content == "done"
    assert policy.responses == [result]
    assert result.tokens_out == 2


@pytest.mark.asyncio
async def test_structured_repair_keeps_dispatch_owner(owned):
    project, run, control = owned
    calls = []

    def respond(req):
        calls.append(req)
        RunResultStore(project).request_cancel(run.run_id)
        return response("not json")

    router = ModelRouter(keys={"openai": "test"}, use_env_keys=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        router._client = client
        with pytest.raises(asyncio.CancelledError):
            await router.for_execution(control).complete(
                request(
                    schema={
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                    }
                )
            )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_bound_runs_do_not_mutate_shared_router(owned):
    project, run, control = owned
    output = project.add_column(1, "other", ai_generated=True)
    other = _start_claimed_run(
        project, sheet_id=1, output_column_id=output, row_ids=[1], label="other"
    )
    other_control = RunDispatch(
        project,
        other.run_id,
        other.authority.writer_attempt_id,
        claim_token=other.authority.claim_token,
    )
    router = ModelRouter(keys={"openai": "test"}, use_env_keys=False)
    one, two = router.for_execution(control), router.for_execution(other_control)
    RunResultStore(project).request_cancel(run.run_id)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response())
    ) as client:
        router._client = client
        results = await asyncio.gather(
            one.complete(request()), two.complete(request()), return_exceptions=True
        )
        standalone = await router.for_execution(STANDALONE).complete(request())
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1].content == standalone.content == "done"
    assert one.for_execution(control) is one
    with pytest.raises(ValueError):
        one.for_execution(other_control)


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE execution_attempts SET state='halted'",
        "UPDATE ops SET publication_closed=1",
        "UPDATE runs SET current_attempt_id=NULL",
    ],
)
def test_dispatch_rechecks_persisted_authority(owned, mutation):
    project, run, control = owned
    project.db.execute(mutation)
    project.db.commit()
    with pytest.raises(asyncio.CancelledError):
        control.check()


def test_managed_context_cannot_silently_use_standalone_router(owned):
    project, run, _ = owned
    with pytest.raises(RuntimeError, match="execution attempt"):
        dispatch_for_context(
            SimpleNamespace(project=project, extras={"run_id": run.run_id})
        )
    with pytest.raises(RuntimeError, match="cancellation owner"):
        dispatch_for_context(SimpleNamespace(project=project, extras={"preview": True}))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 429])
async def test_sidecar_checks_before_retry_but_preserves_successful_return(
    owned, status
):
    project, run, control = owned
    calls = []

    def respond(req):
        calls.append(req)
        RunResultStore(project).request_cancel(run.run_id)
        return httpx.Response(status, json={"pages": [], "usage": {"pages": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        operation = sidecar_post(
            SimpleNamespace(http=client),
            "/ocr",
            connection=SimpleNamespace(
                base_url="https://test",
                token="test",
                timeout_seconds=None,
                connect_timeout_seconds=None,
            ),
            dispatch=control,
        )
        if status == 429:
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            assert (await operation)["usage"] == {"pages": 1}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_dispatch_reads_current_thread_connection_and_claim(owned):
    from frisket.engine.store.output_claims import OutputColumnClaimStore

    project, run, control = owned
    await asyncio.to_thread(control.check)
    OutputColumnClaimStore(project).release(claim_token=run.authority.claim_token)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.to_thread(control.check)


@pytest.mark.asyncio
async def test_unbound_router_cannot_silently_dispatch_standalone():
    from frisket.execution.dispatch_control import STANDALONE

    calls = []

    def respond(req):
        calls.append(req)
        return response()

    router = ModelRouter(keys={"openai": "test"}, use_env_keys=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        router._client = client
        for method, args in (
            (router.complete, (request(),)),
            (router.complete_transport, (request(),)),
            (router.embed, (["test"],)),
            (router.embed_batch, (["test"],)),
        ):
            with pytest.raises(TypeError, match="_execution"):
                await method(*args)
        assert not calls
        assert (
            await router.for_execution(STANDALONE).complete(request())
        ).content == "done"
        assert len(calls) == 1


@pytest.mark.parametrize("injected", [False, True])
def test_embedding_refresh_checks_run_cancellation_before_provider(
    tmp_path, monkeypatch, injected
):
    from frisket.engine.executor import ExecutorDeps, run_action_spec
    from frisket.ai.embeddings import EmbeddingGateway
    from frisket.engine.executor.action_families import embeddings
    from tests.engine.test_embedding_index_actions import (
        _create_action,
        _refresh_action,
    )

    calls = []
    with closing(Project.create(tmp_path / "embedding-dispatch.frisket")) as project:
        sheet = project.add_sheet("data")
        column = project.add_column(sheet, "headline")
        project.add_rows(sheet, [{"headline": "hello"}], {"headline": column})
        router = ModelRouter(keys={"openai": "test"}, use_env_keys=False)

        class Adapter:
            base_url = "https://example.invalid"

            async def embed_with_meta(self, *args):
                calls.append(args)
                pytest.fail("Cancelled work reached the provider")

        router._adapters["openai"] = Adapter()
        created = run_action_spec(
            project,
            _create_action(sheet, provider="openai", policy={"allow_remote": True}),
            project_id="test",
            router=router,
        )
        assert created.status == "completed", created.errors
        index_id = created.outputs[0].ref["index_id"]
        mint = embeddings.mint_direct_effect_attempt

        def cancel_before_dispatch(current, **kwargs):
            commitment = mint(current, **kwargs)
            RunResultStore(current).request_cancel(kwargs["run_id"])
            return commitment

        monkeypatch.setattr(
            embeddings, "mint_direct_effect_attempt", cancel_before_dispatch
        )
        with pytest.raises(asyncio.CancelledError):
            run_action_spec(
                project,
                _refresh_action(index_id),
                project_id="test",
                router=router,
                deps=ExecutorDeps(embedding_gateway=EmbeddingGateway(router=router))
                if injected
                else None,
            )
        assert not calls
        assert not project.db.in_transaction
        [run] = project.db.execute("SELECT status FROM runs").fetchall()
        assert run["status"] == "cancelled"
        [attempt] = project.db.execute(
            "SELECT state FROM execution_attempts"
        ).fetchall()
        assert attempt["state"] == "halted"
