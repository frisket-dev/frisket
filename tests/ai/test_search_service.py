from __future__ import annotations

import httpx
import pytest

from frisket.ai.research.search import SearchProviderError, SearchService


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_auto_prefers_exa_and_preserves_reported_dollars() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-api-key")
        seen["payload"] = __import__("json").loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Record",
                        "url": "https://example.test/record",
                        "publishedDate": "2026-09-01",
                    }
                ],
                "costDollars": {"total": 0.007},
            },
        )

    async with _client(handler) as client:
        result = await SearchService(
            preference="auto",
            effective_keys={"tavily": "tvly-secret", "exa": "exa-secret"},
            http=client,
        ).search("public records", max_results=3)

    assert result.provider == "exa"
    assert result.results[0].url == "https://example.test/record"
    assert result.results[0].published_at == "2026-09-01"
    assert result.usage.provider_reported_cost_usd == 0.007
    assert result.usage.provider_cost_usd == 0.007
    assert result.usage.units == ()
    assert seen == {
        "url": "https://api.exa.ai/search",
        "key": "exa-secret",
        "payload": {"query": "public records", "numResults": 3, "type": "auto"},
    }


@pytest.mark.asyncio
async def test_exa_missing_reported_cost_settles_against_captured_quote() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    async with _client(handler) as client:
        service = SearchService(
            preference="exa", effective_keys={"exa": "exa-secret"}, http=client
        )
        quote = service.quote(max_results=6)
        result = await service.search("records", max_results=6, quote=quote)

    assert result.usage.provider_reported_cost_usd is None
    assert result.usage.provider_cost_usd == 0.007
    assert result.usage.cost_source == "configured_catalog"
    assert result.usage.quote is quote


@pytest.mark.asyncio
async def test_tavily_basic_search_keeps_credits_separate_from_dollars() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tvly-secret"
        body = __import__("json").loads(request.content)
        assert body == {
            "query": "court filing",
            "topic": "general",
            "search_depth": "basic",
            "max_results": 2,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "include_published_date": True,
            "auto_parameters": False,
            "include_usage": True,
        }
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Filing",
                        "url": "https://example.test/filing",
                        "content": "An excerpt",
                        "published_date": "2026-08-02",
                    }
                ],
                "usage": {"credits": 1},
            },
        )

    async with _client(handler) as client:
        result = await SearchService(
            preference="tavily",
            effective_keys={"tavily": "tvly-secret"},
            http=client,
        ).search("court filing", max_results=2)

    assert result.results[0].excerpt == "An excerpt"
    assert result.usage.provider_reported_cost_usd is None
    assert result.usage.provider_cost_usd == 0.008
    assert result.usage.quote is not None
    assert result.usage.quote.pricing_key == "tavily.search.credit"
    assert [(unit.name, unit.quantity) for unit in result.usage.units] == [
        ("credits", 1)
    ]


@pytest.mark.asyncio
async def test_explicit_paid_provider_never_falls_back_or_retries() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"detail": "quota with secret canary"})

    async with _client(handler) as client:
        service = SearchService(
            preference="exa",
            effective_keys={"exa": "secret-canary", "tavily": "other"},
            http=client,
        )
        with pytest.raises(SearchProviderError) as failure:
            await service.search("private-query-canary")

    assert failure.value.code == "quota"
    assert failure.value.provider == "exa"
    assert calls == 1
    assert "canary" not in str(failure.value)


def test_explicit_paid_provider_requires_its_effective_key() -> None:
    with pytest.raises(SearchProviderError) as failure:
        SearchService(preference="tavily", effective_keys={"exa": "different-provider"})
    assert failure.value.code == "missing_credentials"
    assert failure.value.provider == "tavily"


@pytest.mark.asyncio
async def test_auto_without_paid_keys_uses_ddgs_and_normalizes_results() -> None:
    calls: list[tuple[str, int]] = []

    class FakeDDGS:
        def __init__(self, *, timeout: float):
            assert timeout == 7.0

        def text(self, query: str, *, max_results: int):
            calls.append((query, max_results))
            return [
                {
                    "title": "Public page",
                    "href": "https://example.test/page",
                    "body": "Snippet",
                    "date": "2026-07-01",
                }
            ]

    result = await SearchService(
        preference="auto", effective_keys={}, ddgs_factory=FakeDDGS
    ).search("query", max_results=4, timeout=7.0)

    assert calls == [("query", 4)]
    assert result.provider == "ddgs"
    assert result.results[0].excerpt == "Snippet"
    assert result.usage.provider_reported_cost_usd is None
    assert result.usage.provider_cost_usd == 0.0
    assert result.usage.cost_source == "free"


def test_paid_quote_is_bounded_and_uses_canonical_pricing() -> None:
    exa = SearchService(preference="exa", effective_keys={"exa": "key"})
    quote = exa.quote(max_results=10)
    assert quote.pricing_key == "exa.search.request"
    assert quote.unit == "request"
    assert quote.unit_price_usd == "0.007"
    assert quote.estimated_cost_usd == 0.007
    with pytest.raises(ValueError, match="between 1 and 10"):
        exa.quote(max_results=11)

    ddgs = SearchService(preference="ddgs", effective_keys={})
    assert ddgs.quote(max_results=20).estimated_cost_usd == 0.0
