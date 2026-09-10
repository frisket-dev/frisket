"""LLM layer tests. Failure mechanics use chaos fault injection (never
success-path mocks); success paths live in test_llm_live.py via the real
response cache."""

import asyncio
import os

import httpx
import pytest

from frisket.ai.llm import (
    CacheMiss,
    ChaosConfig,
    ChaosMiddleware,
    DEFAULT_MAX_OUTPUT_TOKENS,
    LLMError,
    LLMRequest,
    LLMResponse,
    ModelRouter,
    ResponseCache,
    request_key,
)
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.ai.llm.adapters import _malformed_response_error, _raise_for_status
from frisket.testing import (
    CANONICAL_LLM_CACHE_PATH,
    REFRESH_CACHE_ENV,
    TEST_CACHE_ENV,
    ensure_isolated_llm_cache,
    llm_cache_path,
)


def req(text="hello", model="anthropic/claude-haiku-4-5", **kw):
    return LLMRequest(model=model, messages=[{"role": "user", "content": text}], **kw)


class TestCacheKeys:
    def test_default_output_limit_is_cache_identity(self):
        default_request = req()
        assert default_request.max_tokens == DEFAULT_MAX_OUTPUT_TOKENS
        assert request_key(default_request) != request_key(req(max_tokens=1_024))

    def test_row_inputs_in_key(self):
        # row 2's response can never be returned for row 1
        assert request_key(req("row one text")) != request_key(req("row two text"))

    def test_prompt_change_busts(self):
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        a = request_key(req("same", schema=schema))
        b = request_key(
            req(
                "same",
                schema={"type": "object", "properties": {"y": {"type": "string"}}},
            )
        )
        assert a != b

    def test_model_and_params_bust(self):
        assert request_key(req(model="anthropic/claude-haiku-4-5")) != request_key(
            req(model="gemini/gemini-2.5-flash")
        )
        assert request_key(req(temperature=0.0)) != request_key(req(temperature=0.7))

    def test_recipe_version_busts(self):
        assert request_key(req(), recipe_version="1") != request_key(
            req(), recipe_version="2"
        )

    def test_same_request_same_key(self):
        assert request_key(req("stable")) == request_key(req("stable"))

    def test_reasoning_policy_busts_only_when_set(self):
        plain = req("stable")
        assert request_key(plain) == request_key(req("stable", reasoning_policy=None))
        assert request_key(plain) != request_key(
            req("stable", reasoning_policy="disabled")
        )
        assert request_key(req("stable", reasoning_policy="disabled")) != request_key(
            req("stable", reasoning_policy="low_exclude")
        )


class TestCacheStore:
    def test_roundtrip_marks_cached(self, tmp_path):
        cache = ResponseCache(tmp_path / "c.db")
        r = LLMResponse(
            content="hi",
            data=None,
            tokens_in=5,
            tokens_out=2,
            cost=0.0001,
            model="anthropic/claude-haiku-4-5",
        )
        cache.put("k1", r)
        hit = cache.get("k1")
        assert hit is not None and hit.cached is True and hit.content == "hi"
        assert cache.get("nope") is None

    def test_output_limit_fact_roundtrips(self, tmp_path):
        cache = ResponseCache(tmp_path / "c.db")
        limited = LLMResponse(
            content='{"name":"Ada"}',
            data={"name": "Ada"},
            tokens_in=5,
            tokens_out=2,
            cost=0.0001,
            model="openai/gpt-5-mini",
            output_limited=True,
        )
        cache.put("limited", limited)
        hit = cache.get("limited")
        assert hit is not None and hit.output_limited is True

    def test_no_secrets_in_cache_file(self, tmp_path):
        """Cache stores response bodies only — never request headers/keys."""
        cache = ResponseCache(tmp_path / "c.db")
        r = LLMResponse(
            content="x", data=None, tokens_in=1, tokens_out=1, cost=0, model="m"
        )
        cache.put("k", r)
        cache.close()
        raw = (tmp_path / "c.db").read_bytes()
        for needle in (b"sk-ant-", b"Bearer", b"Authorization", b"x-api-key"):
            assert needle not in raw

    def test_test_cache_uses_temp_copy_by_default(self, monkeypatch):
        monkeypatch.delenv(TEST_CACHE_ENV, raising=False)
        monkeypatch.delenv(REFRESH_CACHE_ENV, raising=False)

        isolated = ensure_isolated_llm_cache(run_id=f"unit-{os.getpid()}")

        assert isolated != CANONICAL_LLM_CACHE_PATH
        assert isolated.exists()
        assert isolated.read_bytes()[:32] == CANONICAL_LLM_CACHE_PATH.read_bytes()[:32]
        assert os.environ[TEST_CACHE_ENV] == str(isolated)
        assert llm_cache_path() == isolated

    def test_cache_refresh_mode_uses_committed_fixture(self, monkeypatch):
        monkeypatch.delenv(TEST_CACHE_ENV, raising=False)
        monkeypatch.setenv(REFRESH_CACHE_ENV, "1")

        assert ensure_isolated_llm_cache() == CANONICAL_LLM_CACHE_PATH
        assert llm_cache_path() == CANONICAL_LLM_CACHE_PATH


class TestChaosDeterminism:
    def test_same_seed_same_fault_sequence(self):
        async def faults(seed):
            mw = ChaosMiddleware(
                ChaosConfig(
                    seed=seed,
                    enabled=True,
                    fail_rate=0.3,
                    rate_limit_rate=0.2,
                    malformed_json_rate=0.2,
                )
            )
            seq = []
            for _ in range(30):
                try:
                    out = await mw.intercept(req(schema={"type": "object"}))
                    seq.append("ok" if out is None else "fab")
                except LLMError as e:
                    seq.append(f"err{e.status}")
            return seq

        a = asyncio.run(faults(42))
        b = asyncio.run(faults(42))
        c = asyncio.run(faults(43))
        assert a == b
        assert a != c  # different seed, different weather

    def test_storm_triggers_after_n(self):
        async def run():
            mw = ChaosMiddleware(
                ChaosConfig(
                    seed=1, enabled=True, rate_limit_storm_after=3, storm_length=2
                )
            )
            outcomes = []
            for _ in range(7):
                try:
                    await mw.intercept(req())
                    outcomes.append("ok")
                except LLMError as e:
                    outcomes.append(e.status)
            return outcomes

        outcomes = asyncio.run(run())
        assert outcomes == ["ok", "ok", "ok", 429, 429, "ok", "ok"]

    def test_disabled_chaos_never_intercepts(self):
        async def run():
            mw = ChaosMiddleware(ChaosConfig(seed=1, enabled=False, fail_rate=1.0))
            return [await mw.intercept(req()) for _ in range(5)]

        assert asyncio.run(run()) == [None] * 5


class TestRouterFailureMechanics:
    def test_hostile_malformed_response_error_stringification_fails_closed(self):
        secret = "checkpoint1b-hostile-malformed-response-secret"

        class HostileDecodeError(ValueError):
            def __str__(self) -> str:
                raise RuntimeError(secret)

        safe = _malformed_response_error("completion", HostileDecodeError(), secret)
        assert str(safe) == (
            "malformed completion response: [UNPRINTABLE HostileDecodeError]"
        )
        assert secret not in str(safe)
        assert safe.retryable is True

    def test_hostile_transport_error_stringification_fails_closed(self):
        secret = "checkpoint1b-hostile-http-error-secret"

        class HostileConnectError(httpx.ConnectError):
            def __str__(self) -> str:
                raise RuntimeError(secret)

        error = HostileConnectError("unused")
        safe = ModelRouter._safe_transport_error(error, (secret,))  # noqa: SLF001
        assert str(safe) == "transport error: [UNPRINTABLE HostileConnectError]"
        assert secret not in str(safe)
        assert safe.retryable is True
        assert safe.transport_kind == "connect"

    def test_hostile_llm_error_stringification_fails_closed_and_preserves_metadata(
        self,
    ):
        secret = "checkpoint1b-hostile-llm-error-secret"

        class HostileLLMError(LLMError):
            def __str__(self) -> str:
                raise RuntimeError(secret)

        error = HostileLLMError(
            "unused",
            status=429,
            retryable=True,
            provider_code="rate_limited",
            transport_kind="connect",
        )
        safe = ModelRouter._safe_llm_error(error, (secret,))  # noqa: SLF001
        assert secret not in str(safe)
        assert str(safe) == "[UNPRINTABLE HostileLLMError]"
        assert safe.status == 429
        assert safe.retryable is True
        assert safe.provider_code == "rate_limited"
        assert safe.transport_kind == "connect"

    def test_legacy_llm_error_without_transport_field_remains_compatible(self):
        class LegacyLLMError(LLMError):
            def __init__(self):
                Exception.__init__(self, "legacy failure")
                self.status = 503
                self.retryable = True
                self.provider_code = "service_unavailable"

        safe = ModelRouter._safe_llm_error(LegacyLLMError(), ())  # noqa: SLF001
        assert str(safe) == "legacy failure"
        assert safe.status == 503
        assert safe.retryable is True
        assert safe.provider_code == "service_unavailable"
        assert safe.transport_kind is None

    def test_status_body_redacts_before_truncation_and_provider_code_cannot_echo_key(
        self,
    ):
        secret = "checkpoint1b-byte495-secret"
        response = httpx.Response(
            500,
            content=(b"x" * 495) + secret.encode(),
        )
        with pytest.raises(LLMError) as excinfo:
            _raise_for_status(response, secret_values=(secret,))
        assert secret not in str(excinfo.value)

        keyed_code = httpx.Response(
            401,
            json={"error": {"code": f"prefix_{secret}", "message": "denied"}},
        )
        with pytest.raises(LLMError) as keyed:
            _raise_for_status(keyed_code, secret_values=(secret,))
        assert keyed.value.provider_code == "provider_error"

    def test_provider_and_router_errors_redact_only_the_adapter_key(self):
        secret = "checkpoint1b-provider-secret"
        response = httpx.Response(
            429,
            json={"error": {"code": "invalid_code!", "message": secret}},
        )
        with pytest.raises(LLMError) as provider_error:
            _raise_for_status(response, secret_values=(secret,))
        assert secret not in str(provider_error.value)
        assert provider_error.value.status == 429
        assert provider_error.value.retryable is True
        assert provider_error.value.provider_code == "provider_error"

        class EchoingAdapter:
            api_key = secret

            async def complete(self, req, client):  # noqa: ANN001
                raise LLMError(
                    f"provider echoed {secret}",
                    status=429,
                    retryable=True,
                    provider_code=f"prefix_{secret}",
                )

        async def run() -> None:
            router = ModelRouter(
                keys={"anthropic": secret}, cache=None, cache_mode="off", max_retries=0
            )
            router._adapters["anthropic"] = EchoingAdapter()  # noqa: SLF001
            trace: list[dict] = []
            with pytest.raises(LLMError) as router_error:
                await router.complete(req(), trace=trace)
            assert secret not in str(router_error.value)
            assert router_error.value.status == 429
            assert router_error.value.retryable is True
            assert router_error.value.provider_code == "provider_error"
            assert secret not in str(trace)
            assert secret not in str(router.health_snapshot())
            await router.aclose()

        asyncio.run(run())

    def test_all_failures_exhaust_retries(self):
        async def run():
            router = ModelRouter(
                keys={"anthropic": "test-key-not-used"},
                chaos=ChaosConfig(seed=7, enabled=True, fail_rate=1.0),
                max_retries=2,
            )
            with pytest.raises(LLMError) as ei:
                await router.complete(req())
            assert ei.value.status == 500
            # 1 initial + 2 retries, all intercepted before any network call
            assert router.chaos.calls == 3
            await router.aclose()

        asyncio.run(run())

    def test_401_not_retried(self):
        async def run():
            router = ModelRouter(
                keys={"anthropic": "test-key-not-used"},
                chaos=ChaosConfig(seed=7, enabled=True, auth_fail_rate=1.0),
                max_retries=3,
            )
            with pytest.raises(LLMError) as ei:
                await router.complete(req())
            assert ei.value.status == 401
            assert router.chaos.calls == 1
            await router.aclose()

        asyncio.run(run())

    def test_unknown_provider_clear_error(self):
        async def run():
            router = ModelRouter(keys={"anthropic": "k"})
            with pytest.raises(LLMError, match="no adapter"):
                await router.complete(req(model="hologram/quantum-9000"))
            await router.aclose()

        asyncio.run(run())

    def test_structured_outer_failure_is_redacted_with_adapter_key(self):
        secret = "checkpoint1b-structured-secret"

        class InvalidStructuredAdapter:
            api_key = secret

            async def complete(self, req, client):  # noqa: ANN001
                return LLMResponse(
                    content=secret,
                    data={"wrong": secret},
                    tokens_in=0,
                    tokens_out=0,
                    cost=0,
                    model=req.model,
                )

        async def run() -> None:
            router = ModelRouter(
                keys={"anthropic": secret}, cache=None, cache_mode="off", max_retries=0
            )
            router._adapters["anthropic"] = InvalidStructuredAdapter()  # noqa: SLF001
            with pytest.raises(LLMError) as excinfo:
                await router.complete(
                    req(schema={"type": "object", "required": ["wanted"]})
                )
            assert secret not in str(excinfo.value)
            assert secret not in (excinfo.value.raw_text or "")
            await router.aclose()

        asyncio.run(run())

    def test_embedding_and_probe_errors_redact_only_the_selected_key(self):
        secret = "checkpoint1b-embed-probe-secret"

        class FailingEmbeddingAdapter:
            api_key = secret
            base_url = "https://example.invalid/v1"

            async def embed(self, texts, model, client):  # noqa: ANN001
                raise LLMError(f"embedding rejected {secret}")

        class ProbeClient:
            async def get(self, url, timeout):  # noqa: ANN001
                raise RuntimeError(f"probe rejected {secret}")

        async def run() -> None:
            router = ModelRouter(
                keys={"openai": secret},
                cache=None,
                cache_mode="off",
                use_env_keys=False,
            )
            adapter = FailingEmbeddingAdapter()
            router._adapters["openai"] = adapter  # noqa: SLF001
            with pytest.raises(LLMError) as embed_error:
                await router.embed(["hello"])
            assert secret not in str(embed_error.value)
            router._client = ProbeClient()  # type: ignore[assignment]  # noqa: SLF001
            probe = await router.probe_providers()
            assert secret not in probe["openai"]["error"]

        asyncio.run(run())

    def test_success_response_usage_counts_must_be_non_negative_integers(self):
        secret = "checkpoint1b-usage-count-secret"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": secret, "completion_tokens": 1},
                },
            )

        async def run() -> None:
            router = ModelRouter(
                keys={"openai": secret},
                cache=None,
                cache_mode="off",
                max_retries=0,
                use_env_keys=False,
            )
            router._client = httpx.AsyncClient(  # noqa: SLF001
                transport=httpx.MockTransport(handler)
            )
            with pytest.raises(LLMError) as excinfo:
                await router.complete(req(model="openai/unpriced-test-model"))
            assert secret not in str(excinfo.value)
            assert "prompt_tokens is not a non-negative integer" in str(excinfo.value)
            await router.aclose()

        asyncio.run(run())

    def test_replay_strict_miss_fails_loudly(self, tmp_path):
        async def run():
            router = ModelRouter(
                keys={"anthropic": "k"},
                cache=ResponseCache(tmp_path / "c.db"),
                cache_mode="replay_strict",
            )
            with pytest.raises(CacheMiss, match="FRISKET_CACHE_REFRESH=1"):
                await router.complete(req("never seen"))
            await router.aclose()

        asyncio.run(run())

    def test_cache_hit_bypasses_chaos_and_network(self, tmp_path):
        async def run():
            cache = ResponseCache(tmp_path / "c.db")
            r = req("cached question")
            cache.put(
                request_key(r),
                LLMResponse(
                    content="cached answer",
                    data=None,
                    tokens_in=1,
                    tokens_out=1,
                    cost=0,
                    model=r.model,
                ),
            )
            router = ModelRouter(
                keys={"anthropic": "k"},
                cache=cache,
                cache_mode="replay",
                chaos=ChaosConfig(seed=1, enabled=True, fail_rate=1.0),
            )
            out = await router.complete(r)
            assert out.cached and out.content == "cached answer"
            assert router.chaos.calls == 0
            await router.aclose()

        asyncio.run(run())


class TestLocalEndpointThreading:
    """ModelRouter consumes one explicit plural endpoint authority."""

    def test_bare_router_has_no_synthetic_local_endpoint(self):
        router = ModelRouter(cache=None, use_env_keys=False, local_endpoints=())
        assert router.local_endpoints == ()
        assert "ollama" not in router.providers()

    def test_exact_endpoint_carries_the_selected_bearer(self):
        endpoint = LocalModelEndpointConfig(
            endpoint_id="heavy",
            display_name="Heavy server",
            origin="https://llm.heavy.internal",
            inference_token="secret-bearer",
            edge_auth=True,
            source="env",
        )
        router = ModelRouter(
            cache=None,
            use_env_keys=False,
            local_endpoints=(endpoint,),
        )
        selected, adapter, bare_model = router.resolve_local_model(
            "ollama/@heavy/qwen3:8b"
        )
        assert selected is endpoint
        assert adapter.api_key == "secret-bearer"
        assert adapter.base_url == "https://llm.heavy.internal/v1"
        assert bare_model == "qwen3:8b"

    def test_bare_router_does_not_read_env_endpoint_authority(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_URL", "https://llm.heavy.internal")
        monkeypatch.setenv("FRISKET_LLM_TOKEN", "secret-bearer")
        router = ModelRouter(cache=None)
        assert router.local_endpoints == ()
        assert "ollama" not in router.providers()

    def test_unqualified_local_model_is_rejected(self):
        endpoint = LocalModelEndpointConfig(
            endpoint_id="studio",
            display_name="Studio",
            origin="http://127.0.0.1:1234",
            source="local_file",
        )
        router = ModelRouter(use_env_keys=False, local_endpoints=(endpoint,))
        with pytest.raises(LLMError, match="ollama/@"):
            router.resolve_local_model("ollama/qwen3:8b")


class TestRoutedProviderStamp:
    """``LLMResponse.provider`` — the routed provider, minted once.

    Fourteen downstream sites used to re-parse the model string to recover
    the provider, in five mutually incompatible spellings (``or "unknown"``,
    ``or "remote"``, ``(provider or model)``, ``... if "/" in model else
    "unknown"``, and the router's own unguarded ``[1]`` index). The router
    knows the answer -- it just picked the adapter with it -- so it stamps it
    beside ``credential_source`` and receipts read the field.
    """

    class _Stub:
        api_key = "k"

        async def complete(self, request, client):
            return LLMResponse(
                content="ok",
                data=None,
                tokens_in=1,
                tokens_out=1,
                cost=0.01,
                model=request.model,
            )

        async def embed(self, texts, model, client):
            return [[0.0] for _ in texts]

    def _router(self, **kw):
        kw.setdefault("cache", None)
        router = ModelRouter(keys={"anthropic": "k"}, use_env_keys=False, **kw)
        router._adapters["anthropic"] = self._Stub()  # noqa: SLF001
        return router

    def test_receipt_provider_is_the_provider_the_router_selected(self):
        router = self._router(cache_mode="off")
        resp = asyncio.run(router.complete(req()))
        assert resp.provider == "anthropic"
        assert resp.credential_source == router.credential_source_for("anthropic")

    def test_a_replayed_response_is_stamped_too(self, tmp_path):
        cache = ResponseCache(tmp_path / "c.db")
        router = self._router(cache=cache, cache_mode="replay")
        first = asyncio.run(router.complete(req()))
        assert first.cached is False and first.provider == "anthropic"
        # A cassette written before the field existed carries no provider;
        # the replay path stamps it rather than trusting the stored body.
        stored = cache.get(request_key(req()))
        stored.provider = "unknown"
        stored.cost = None
        stored.cost_source = "pricing_data"
        cache.put(request_key(req()), stored)
        replayed = asyncio.run(router.complete(req()))
        assert replayed.cached is True
        assert replayed.provider == "anthropic"
        assert replayed.credential_source == "cache"
        assert replayed.cost is None
        assert replayed.cost_source == "unknown"

    def test_an_unqualified_model_id_never_raises_indexerror(self):
        """A bare id is unroutable, but it must fail by NAME, not by index.

        ``embed``/``embed_batch`` indexed ``split("/", 1)[1]`` unguarded, so a
        bare id raised a bare IndexError instead of the router's own honest
        "no adapter for provider ..." message.
        """
        router = self._router(cache_mode="off")
        with pytest.raises(LLMError, match="no adapter for provider 'claude-haiku"):
            asyncio.run(router.complete(req(model="claude-haiku-4-5")))
        router._adapters["bare-model-id"] = self._Stub()  # noqa: SLF001
        assert asyncio.run(router.embed(["a"], model="bare-model-id")) == [[0.0]]
