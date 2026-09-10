"""Runtime contract for the neutral model-call policy boundary."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from frisket.ai.llm import LLMError, LLMRequest, LLMResponse, ModelRouter, ResponseCache
from frisket.ai.llm.cache import request_key
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.engine.jobs.runs import _router_for
from frisket.engine.store import Project
from frisket.server.workspace import Workspace


class _RecordingPolicy:
    def __init__(
        self,
        events: list[tuple],
        *,
        prepare_marker: str = "",
        refuse_before_live: bool = False,
        fail_after_live: bool = False,
    ) -> None:
        self.events = events
        self.prepare_marker = prepare_marker
        self.refuse_before_live = refuse_before_live
        self.fail_after_live = fail_after_live

    def prepare(self, request: LLMRequest, credential_source: str) -> LLMRequest:
        self.events.append(("prepare", credential_source, request.params.get("marker")))
        if not self.prepare_marker:
            return request
        return replace(
            request, params={**request.params, "marker": self.prepare_marker}
        )

    def before_live(self, request: LLMRequest, credential_source: str) -> None:
        self.events.append(
            ("before_live", credential_source, request.params.get("marker"))
        )
        if self.refuse_before_live:
            raise RuntimeError("policy refused live call")

    def after_live(
        self,
        request: LLMRequest,
        response: LLMResponse,
        credential_source: str,
    ) -> None:
        self.events.append(
            (
                "after_live",
                credential_source,
                response.provider,
                response.credential_source,
                response.tokens_in,
                response.tokens_out,
                response.cost,
            )
        )
        if self.fail_after_live:
            raise RuntimeError("policy could not record live response")


class _Adapter:
    def __init__(self, events: list[tuple], *, fail_first: bool = False) -> None:
        self.events = events
        self.calls = 0
        self.fail_first = fail_first

    async def complete(self, request: LLMRequest, _client: object) -> LLMResponse:
        self.calls += 1
        self.events.append(("adapter", self.calls, request.params.get("marker")))
        if self.fail_first and self.calls == 1:
            raise LLMError("rate limited", status=429, retryable=True)
        return LLMResponse(
            content="ok",
            data=None,
            tokens_in=7,
            tokens_out=3,
            cost=0.004,
            model=request.model,
        )


class _RecordingCache(ResponseCache):
    def __init__(self, path, events: list[tuple]) -> None:  # noqa: ANN001
        super().__init__(path)
        self.events = events

    def put(self, key: str, response: LLMResponse) -> None:
        self.events.append(("cache_put", key))
        super().put(key, response)


def _request() -> LLMRequest:
    return LLMRequest(
        model="openai/test",
        messages=[{"role": "user", "content": "hello"}],
    )


def _router(
    *,
    policy: _RecordingPolicy,
    cache: ResponseCache | None = None,
    cache_mode: str = "off",
    max_retries: int = 0,
) -> ModelRouter:
    return ModelRouter(
        keys={"openai": "test-key"},
        key_sources={"openai": "platform_key"},
        cache=cache,
        cache_mode=cache_mode,  # type: ignore[arg-type]
        max_retries=max_retries,
        use_env_keys=False,
        model_call_policy=policy,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_mode", ["replay", "replay_strict"])
async def test_prepare_changes_cache_identity_and_cache_hits_skip_live_callbacks(
    tmp_path, cache_mode: str
) -> None:
    events: list[tuple] = []
    policy = _RecordingPolicy(events, prepare_marker="policy-v1")
    cache = ResponseCache(tmp_path / "responses.db")
    request = _request()
    prepared = replace(request, params={"marker": "policy-v1"})
    cache.put(
        request_key(prepared),
        LLMResponse("from-cache", None, 2, 1, 0.001, prepared.model),
    )
    router = _router(policy=policy, cache=cache, cache_mode=cache_mode)

    try:
        response = await router.complete_transport(request)
    finally:
        await router.aclose()
        cache.close()

    assert response.cached is True
    assert response.content == "from-cache"
    assert events == [("prepare", "platform_key", None)]


@pytest.mark.asyncio
async def test_live_policy_order_exposes_stamped_provenance_before_cache_write(
    tmp_path,
) -> None:
    events: list[tuple] = []
    policy = _RecordingPolicy(events, prepare_marker="policy-v1")
    cache = _RecordingCache(tmp_path / "responses.db", events)
    router = _router(policy=policy, cache=cache, cache_mode="replay")
    adapter = _Adapter(events)
    router._adapters["openai"] = adapter  # noqa: SLF001 - hermetic wire double

    try:
        response = await router.complete_transport(_request())
    finally:
        await router.aclose()
        cache.close()

    assert response.credential_source == "platform_key"
    assert events[0] == ("prepare", "platform_key", None)
    assert events[1] == ("before_live", "platform_key", "policy-v1")
    assert events[2] == ("adapter", 1, "policy-v1")
    assert events[3] == (
        "after_live",
        "platform_key",
        "openai",
        "platform_key",
        7,
        3,
        0.004,
    )
    assert events[4][0] == "cache_put"


@pytest.mark.asyncio
async def test_before_live_refusal_prevents_the_adapter_attempt() -> None:
    events: list[tuple] = []
    router = _router(
        policy=_RecordingPolicy(events, refuse_before_live=True), max_retries=1
    )
    adapter = _Adapter(events)
    router._adapters["openai"] = adapter  # noqa: SLF001 - hermetic wire double

    try:
        with pytest.raises(RuntimeError, match="policy refused live call"):
            await router.complete_transport(_request())
    finally:
        await router.aclose()

    assert adapter.calls == 0
    assert events == [
        ("prepare", "platform_key", None),
        ("before_live", "platform_key", None),
    ]


@pytest.mark.asyncio
async def test_after_live_failure_propagates_and_prevents_cache_write(tmp_path) -> None:
    events: list[tuple] = []
    cache = _RecordingCache(tmp_path / "responses.db", events)
    router = _router(
        policy=_RecordingPolicy(events, fail_after_live=True),
        cache=cache,
        cache_mode="replay",
    )
    adapter = _Adapter(events)
    router._adapters["openai"] = adapter  # noqa: SLF001 - hermetic wire double

    try:
        with pytest.raises(RuntimeError, match="policy could not record live response"):
            await router.complete_transport(_request())
        cached_count = cache.count()
    finally:
        await router.aclose()
        cache.close()

    assert adapter.calls == 1
    assert cached_count == 0
    assert not any(event[0] == "cache_put" for event in events)


@pytest.mark.asyncio
async def test_before_live_runs_for_each_real_retry_but_after_live_runs_once(
    monkeypatch,
) -> None:
    events: list[tuple] = []
    policy = _RecordingPolicy(events)
    router = _router(policy=policy, max_retries=1)
    adapter = _Adapter(events, fail_first=True)
    router._adapters["openai"] = adapter  # noqa: SLF001 - hermetic wire double

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("frisket.ai.llm.router.asyncio.sleep", no_sleep)
    try:
        await router.complete_transport(_request())
    finally:
        await router.aclose()

    assert events == [
        ("prepare", "platform_key", None),
        ("before_live", "platform_key", None),
        ("adapter", 1, None),
        ("before_live", "platform_key", None),
        ("adapter", 2, None),
        ("after_live", "platform_key", "openai", "platform_key", 7, 3, 0.004),
    ]


def test_project_key_overlay_reconstruction_retains_policy_and_provenance(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple] = []
    policy = _RecordingPolicy(events)
    project = Project.create(tmp_path / "project.frisket", name="policy")
    base = _router(policy=policy)
    workspace = Workspace(tmp_path / "workspace", router=base)
    monkeypatch.setattr(
        project,
        "provider_model_keys",
        lambda: {"openai": "project-key"},
    )
    try:
        composed = workspace.router_for(project)
        adapter = _Adapter(events)
        composed._adapters["openai"] = adapter  # noqa: SLF001 - hermetic wire double

        asyncio.run(composed.complete_transport(_request()))
    finally:
        asyncio.run(base.aclose())
        if "composed" in locals():
            asyncio.run(composed.aclose())
        project.close()

    assert events == [
        ("prepare", "project_key", None),
        ("before_live", "project_key", None),
        ("adapter", 1, None),
        ("after_live", "project_key", "openai", "project_key", 7, 3, 0.004),
    ]


def test_queued_local_endpoint_overlay_retains_the_identical_policy(tmp_path) -> None:
    policy = _RecordingPolicy([])
    original_endpoint = LocalModelEndpointConfig(
        endpoint_id="old",
        display_name="Old endpoint",
        origin="https://old.example.test",
        inference_token="old-token",
        edge_auth=False,
        source="test",
    )
    replacement_endpoint = LocalModelEndpointConfig(
        endpoint_id="new",
        display_name="New endpoint",
        origin="https://new.example.test",
        inference_token="new-token",
        edge_auth=False,
        source="test",
    )
    project = Project.create(tmp_path / "queued.frisket", name="queued")
    base = ModelRouter(
        keys={"openai": "test-key"},
        key_sources={"openai": "platform_key"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
        local_endpoints=(original_endpoint,),
        model_call_policy=policy,
    )
    try:
        rebuilt = _router_for(
            project,
            base,
            local_endpoints=(replacement_endpoint,),
        )
    finally:
        project.close()

    assert rebuilt is not base
    assert rebuilt.local_endpoints == (replacement_endpoint,)
    assert rebuilt.model_call_policy is policy


@pytest.mark.asyncio
async def test_router_without_a_policy_keeps_existing_transport_behavior() -> None:
    events: list[tuple] = []
    router = ModelRouter(
        keys={"openai": "test-key"},
        key_sources={"openai": "platform_key"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
    )
    adapter = _Adapter(events)
    router._adapters["openai"] = adapter  # noqa: SLF001 - hermetic wire double
    try:
        response = await router.complete_transport(_request())
    finally:
        await router.aclose()

    assert response.content == "ok"
    assert response.credential_source == "platform_key"
    assert events == [("adapter", 1, None)]
