"""Transport failures and accounting failures have different retry semantics."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from frisket.ai.llm import LLMError, LLMRequest, LLMResponse, ModelRouter


class Policy:
    def __init__(self, *, admission_error=None, accounting_error=None):
        self.admission_error = admission_error
        self.accounting_error = accounting_error
        self.admitted = []
        self.responses = []
        self.failed = []

    def prepare(self, request, source):
        return request

    def before_live(self, request, source):
        if self.admission_error:
            raise self.admission_error
        self.admitted.append((request, source))

    def after_live(self, request, response, source):
        self.responses.append(response)
        if self.accounting_error:
            raise self.accounting_error

    def failed_live(self, request, error, source):
        self.failed.append(error)

    def admit_external_service(self, **request):
        if self.admission_error:
            raise self.admission_error
        self.admitted.append(request)
        return "embedding-purchase"

    def finish_service(self, operation_id, **facts):
        self.responses.append(facts)
        if self.accounting_error:
            raise self.accounting_error

    def fail_service(self, operation_id, error):
        self.failed.append(error)


@asynccontextmanager
async def router(policy):
    instance = ModelRouter(
        keys={"openai": "fixture"},
        key_sources={"openai": "platform_key"},
        use_env_keys=False,
        cache_mode="off",
        model_call_policy=policy,
        max_retries=2,
    )
    try:
        yield instance
    finally:
        await instance.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [RuntimeError("adapter bug"), asyncio.CancelledError()]
)
async def test_unexpected_transport_failure_marks_the_purchase_once(error):
    policy = Policy()
    async with router(policy) as model_router:

        async def complete(request, client):
            raise error

        model_router._adapters["openai"] = SimpleNamespace(complete=complete)
        with pytest.raises(type(error)):
            await model_router.complete(
                LLMRequest(model="openai/gpt-5-mini", messages=[])
            )
    assert len(policy.admitted) == 1
    assert policy.failed == [error]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["admission", "accounting"])
@pytest.mark.parametrize(
    "error",
    [
        LLMError("accounting unavailable", retryable=True),
        httpx.ReadError("accounting unavailable"),
    ],
)
async def test_policy_failure_does_not_repurchase(stage, error):
    policy = Policy(**{f"{stage}_error": error})
    calls = []
    async with router(policy) as model_router:

        async def complete(request, client):
            calls.append(request)
            return LLMResponse(
                content="known result",
                data=None,
                tokens_in=10,
                tokens_out=3,
                cost=0.1,
                model=request.model,
            )

        model_router._adapters["openai"] = SimpleNamespace(complete=complete)
        with pytest.raises(type(error)):
            await model_router.complete(
                LLMRequest(model="openai/gpt-5-mini", messages=[])
            )
    assert len(calls) == (stage == "accounting")
    assert policy.failed == []
    if stage == "accounting":
        assert policy.responses[0].content == "known result"


@pytest.mark.asyncio
@pytest.mark.parametrize("vector_only", [True, False])
async def test_embedding_batch_is_one_purchase_with_usage_and_known_vectors(
    vector_only,
):
    policy = Policy()
    calls = []
    async with router(policy) as model_router:

        async def embed_with_meta(texts, model, client):
            calls.append(list(texts))
            return [[1.0, 0.0], [0.0, 1.0]], {
                "usage": {"prompt_tokens": 100},
                "actual_model_id": model,
            }

        model_router._adapters["openai"] = SimpleNamespace(
            embed_with_meta=embed_with_meta
        )
        method = model_router.embed if vector_only else model_router.embed_batch
        response = await method(["one", "two"])
    assert calls == [["one", "two"]]
    assert len(policy.admitted) == len(policy.responses) == 1
    assert policy.admitted[0]["prepared"]["input"] == ["one", "two"]
    assert policy.admitted[0]["credential_source"] == "platform_key"
    assert policy.responses[0]["result"]["vectors"] == [[1.0, 0.0], [0.0, 1.0]]
    assert policy.responses[0]["usage"] == {"prompt_tokens": 100}
    assert policy.responses[0]["provider_cost_micro"] == 2
    assert (response if vector_only else response["vectors"]) == [
        [1.0, 0.0],
        [0.0, 1.0],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [RuntimeError("adapter bug"), asyncio.CancelledError()]
)
async def test_embedding_failure_marks_one_purchase(error):
    policy = Policy()
    async with router(policy) as model_router:

        async def embed_with_meta(texts, model, client):
            raise error

        model_router._adapters["openai"] = SimpleNamespace(
            embed_with_meta=embed_with_meta
        )
        with pytest.raises(type(error)):
            await model_router.embed(["one"])
    assert len(policy.admitted) == 1
    assert policy.failed == [error]
