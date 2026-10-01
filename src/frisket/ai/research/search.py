"""One bounded web-search service with normalized results and accounting."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

import httpx

SearchProvider = Literal["exa", "tavily", "ddgs"]
SearchPreference = Literal["auto", "exa", "tavily", "ddgs"]
SearchErrorCode = Literal[
    "missing_credentials",
    "auth",
    "quota",
    "timeout",
    "provider_error",
]

_PAID_PROVIDERS = frozenset({"exa", "tavily"})
_EXCERPT_CHARS = 1_000
_PAID_MAX_RESULTS = 10


@dataclass(frozen=True)
class SearchUsageUnit:
    name: str
    quantity: int | float


@dataclass(frozen=True)
class SearchUsage:
    provider: SearchProvider
    service: str
    request_count: int
    provider_reported_cost_usd: float | None
    provider_cost_usd: float | None
    cost_source: Literal["provider_reported", "configured_catalog", "free", "unknown"]
    units: tuple[SearchUsageUnit, ...] = ()
    quote: SearchQuote | None = None


@dataclass(frozen=True)
class SearchQuote:
    provider: SearchProvider
    max_results: int
    pricing_key: str | None
    pricing_label: str
    unit: str
    unit_price_usd: str | None
    estimated_cost_usd: float | None
    cost_source: Literal["configured_catalog", "free_public_api", "unknown"]


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    excerpt: str
    retrieved_at: str
    published_at: str | None
    provider: SearchProvider


@dataclass(frozen=True)
class SearchResponse:
    provider: SearchProvider
    results: tuple[SearchHit, ...]
    usage: SearchUsage


class SearchProviderError(RuntimeError):
    """A safe, actionable provider failure with no response or query text."""

    def __init__(
        self,
        provider: SearchProvider,
        code: SearchErrorCode,
        message: str,
        *,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.status = status


def normalize_search_preference(value: str | None) -> SearchPreference:
    clean = (value or "auto").strip().lower()
    if clean not in {"auto", "exa", "tavily", "ddgs"}:
        raise ValueError(f"unsupported search provider: {value}")
    return clean  # type: ignore[return-value]


def select_search_provider(
    preference: str | None,
    effective_keys: Mapping[str, str],
) -> SearchProvider:
    """Resolve a provider from caller-owned effective credentials only."""
    selected = normalize_search_preference(preference)
    if selected == "auto":
        for provider in ("exa", "tavily"):
            if (effective_keys.get(provider) or "").strip():
                return provider
        return "ddgs"
    if selected in _PAID_PROVIDERS and not (effective_keys.get(selected) or "").strip():
        label = selected.capitalize()
        raise SearchProviderError(
            selected,
            "missing_credentials",
            f"{label} search requires a configured API key.",
        )
    return selected


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _text(value: Any, *, limit: int | None = None) -> str:
    text = value if isinstance(value, str) else ""
    return text if limit is None else text[:limit]


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or not math.isfinite(value):
        return None
    return value


def _http_error(provider: SearchProvider, status: int) -> SearchProviderError:
    label = provider.capitalize()
    if status in {401, 403}:
        return SearchProviderError(
            provider,
            "auth",
            f"{label} rejected the configured API key.",
            status=status,
        )
    if status in {402, 429, 432}:
        return SearchProviderError(
            provider,
            "quota",
            f"{label} search quota or billing limit was reached.",
            status=status,
        )
    return SearchProviderError(
        provider,
        "provider_error",
        f"{label} search failed (HTTP {status}).",
        status=status,
    )


class SearchService:
    """Select one provider and make exactly one provider call per search."""

    def __init__(
        self,
        *,
        preference: SearchPreference | str = "auto",
        effective_keys: Mapping[str, str],
        http: httpx.AsyncClient | None = None,
        ddgs_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._keys = {
            provider: value.strip()
            for provider, value in effective_keys.items()
            if provider in _PAID_PROVIDERS and isinstance(value, str) and value.strip()
        }
        self._provider = select_search_provider(preference, self._keys)
        self._http = http
        self._ddgs_factory = ddgs_factory

    @property
    def provider(self) -> SearchProvider:
        return self._provider

    def quote(self, *, max_results: int = 6) -> SearchQuote:
        """Capture the rate identity used for admission and later settlement."""
        self._validate_result_limit(max_results)
        if self._provider == "ddgs":
            return SearchQuote(
                provider="ddgs",
                max_results=max_results,
                pricing_key=None,
                pricing_label="DDGS public search",
                unit="request",
                unit_price_usd="0",
                estimated_cost_usd=0.0,
                cost_source="free_public_api",
            )
        from frisket.ai.external_pricing import (
            EXA_SEARCH_REQUEST,
            TAVILY_SEARCH_CREDIT,
            estimate_external_cost,
            external_pricing_entry,
        )

        pricing_key = (
            EXA_SEARCH_REQUEST if self._provider == "exa" else TAVILY_SEARCH_CREDIT
        )
        entry = external_pricing_entry(pricing_key)
        estimate = estimate_external_cost(pricing_key, 1)
        return SearchQuote(
            provider=self._provider,
            max_results=max_results,
            pricing_key=pricing_key,
            pricing_label=str(entry["label"]),
            unit=str(entry["unit"]),
            unit_price_usd=entry["unit_price_usd_string"],
            estimated_cost_usd=estimate["cost"],
            cost_source=entry["cost_source"],
        )

    def _validate_result_limit(self, max_results: int) -> None:
        maximum = _PAID_MAX_RESULTS if self._provider in _PAID_PROVIDERS else 20
        if type(max_results) is not int or not 1 <= max_results <= maximum:
            raise ValueError(
                f"{self._provider.capitalize()} search result limit must be "
                f"between 1 and {maximum}"
            )

    async def search(
        self,
        query: str,
        *,
        max_results: int = 6,
        timeout: float = 20.0,
        quote: SearchQuote | None = None,
    ) -> SearchResponse:
        if quote is None:
            quote = self.quote(max_results=max_results)
        elif quote.provider != self._provider or quote.max_results != max_results:
            raise ValueError("search quote does not match provider and result limit")
        else:
            self._validate_result_limit(max_results)
        query = query.strip() if isinstance(query, str) else ""
        if not query:
            return SearchResponse(
                provider=self._provider,
                results=(),
                usage=SearchUsage(
                    provider=self._provider,
                    service=f"{self._provider}.search",
                    request_count=0,
                    provider_reported_cost_usd=None,
                    provider_cost_usd=0.0,
                    cost_source="free",
                    quote=quote,
                ),
            )
        if self._provider == "exa":
            return await self._search_exa(query, max_results, timeout, quote)
        if self._provider == "tavily":
            return await self._search_tavily(query, max_results, timeout, quote)
        return await self._search_ddgs(query, max_results, timeout, quote)

    async def _post(
        self,
        provider: SearchProvider,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        own_client = self._http is None
        client = self._http or httpx.AsyncClient()
        try:
            try:
                response = await client.post(
                    url, headers=dict(headers), json=dict(payload), timeout=timeout
                )
            except httpx.TimeoutException as exc:
                raise SearchProviderError(
                    provider, "timeout", f"{provider.capitalize()} search timed out."
                ) from exc
            except httpx.HTTPError as exc:
                raise SearchProviderError(
                    provider,
                    "provider_error",
                    f"{provider.capitalize()} search is unavailable.",
                ) from exc
            if response.status_code != 200:
                raise _http_error(provider, response.status_code)
            try:
                body = response.json()
            except ValueError as exc:
                raise SearchProviderError(
                    provider,
                    "provider_error",
                    f"{provider.capitalize()} returned an invalid search response.",
                ) from exc
            if not isinstance(body, dict):
                raise SearchProviderError(
                    provider,
                    "provider_error",
                    f"{provider.capitalize()} returned an invalid search response.",
                )
            return body
        finally:
            if own_client:
                await client.aclose()

    async def _search_exa(
        self,
        query: str,
        max_results: int,
        timeout: float,
        quote: SearchQuote,
    ) -> SearchResponse:
        body = await self._post(
            "exa",
            "https://api.exa.ai/search",
            headers={"x-api-key": self._keys["exa"]},
            payload={"query": query, "numResults": max_results, "type": "auto"},
            timeout=timeout,
        )
        results = body.get("results")
        if not isinstance(results, list):
            raise SearchProviderError(
                "exa", "provider_error", "Exa returned an invalid search response."
            )
        retrieved_at = _now()
        hits = tuple(
            SearchHit(
                title=_text(item.get("title")),
                url=_text(item.get("url")),
                excerpt=_text(
                    item.get("summary")
                    or item.get("text")
                    or " ".join(item.get("highlights") or []),
                    limit=_EXCERPT_CHARS,
                ),
                retrieved_at=retrieved_at,
                published_at=_text(item.get("publishedDate")) or None,
                provider="exa",
            )
            for item in results[:max_results]
            if isinstance(item, dict)
        )
        cost = None
        cost_dollars = body.get("costDollars")
        if isinstance(cost_dollars, dict):
            cost = _number(cost_dollars.get("total"))
        provider_cost = cost if cost is not None else quote.estimated_cost_usd
        return SearchResponse(
            provider="exa",
            results=hits,
            usage=SearchUsage(
                provider="exa",
                service="exa.search",
                request_count=1,
                provider_reported_cost_usd=cost,
                provider_cost_usd=provider_cost,
                cost_source=(
                    "provider_reported"
                    if cost is not None
                    else "configured_catalog"
                    if provider_cost is not None
                    else "unknown"
                ),
                quote=quote,
            ),
        )

    async def _search_tavily(
        self,
        query: str,
        max_results: int,
        timeout: float,
        quote: SearchQuote,
    ) -> SearchResponse:
        body = await self._post(
            "tavily",
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {self._keys['tavily']}"},
            payload={
                "query": query,
                "topic": "general",
                "search_depth": "basic",
                "max_results": max_results,
                "include_answer": False,
                "include_raw_content": False,
                "include_images": False,
                "include_published_date": True,
                "auto_parameters": False,
                "include_usage": True,
            },
            timeout=timeout,
        )
        results = body.get("results")
        if not isinstance(results, list):
            raise SearchProviderError(
                "tavily",
                "provider_error",
                "Tavily returned an invalid search response.",
            )
        retrieved_at = _now()
        hits = tuple(
            SearchHit(
                title=_text(item.get("title")),
                url=_text(item.get("url")),
                excerpt=_text(item.get("content"), limit=_EXCERPT_CHARS),
                retrieved_at=retrieved_at,
                published_at=_text(item.get("published_date")) or None,
                provider="tavily",
            )
            for item in results[:max_results]
            if isinstance(item, dict)
        )
        units: tuple[SearchUsageUnit, ...] = ()
        usage = body.get("usage")
        if isinstance(usage, dict):
            credits = _number(usage.get("credits"))
            if credits is not None:
                units = (SearchUsageUnit(name="credits", quantity=credits),)
        provider_cost = _rated_cost(quote, units[0].quantity) if units else None
        return SearchResponse(
            provider="tavily",
            results=hits,
            usage=SearchUsage(
                provider="tavily",
                service="tavily.search",
                request_count=1,
                provider_reported_cost_usd=None,
                provider_cost_usd=provider_cost,
                cost_source="configured_catalog"
                if provider_cost is not None
                else "unknown",
                units=units,
                quote=quote,
            ),
        )

    async def _search_ddgs(
        self,
        query: str,
        max_results: int,
        timeout: float,
        quote: SearchQuote,
    ) -> SearchResponse:
        if self._ddgs_factory is None:
            from ddgs import DDGS

            factory = DDGS
        else:
            factory = self._ddgs_factory
        try:
            try:
                ddgs = factory(timeout=timeout)
            except TypeError:
                # Older DDGS versions and existing host-injected adapters did
                # not accept constructor options; their call is still bounded
                # by the admitted task lifecycle.
                ddgs = factory()
            results = await asyncio.to_thread(
                lambda: ddgs.text(query, max_results=max_results)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — DDGS raises plain exceptions
            raise SearchProviderError(
                "ddgs", "provider_error", "DDGS search is unavailable."
            ) from exc
        retrieved_at = _now()
        hits = tuple(
            SearchHit(
                title=_text(item.get("title")),
                url=_text(item.get("href")),
                excerpt=_text(item.get("body"), limit=_EXCERPT_CHARS),
                retrieved_at=retrieved_at,
                published_at=_text(item.get("date")) or None,
                provider="ddgs",
            )
            for item in (results or [])[:max_results]
            if isinstance(item, dict)
        )
        return SearchResponse(
            provider="ddgs",
            results=hits,
            usage=SearchUsage(
                provider="ddgs",
                service="ddgs.text",
                request_count=1,
                provider_reported_cost_usd=None,
                provider_cost_usd=0.0,
                cost_source="free",
                quote=quote,
            ),
        )


def _rated_cost(quote: SearchQuote, quantity: int | float) -> float | None:
    """Rate actual provider units against the pre-call captured quote."""
    if quote.unit_price_usd is None:
        return None
    return float(Decimal(quote.unit_price_usd) * Decimal(str(quantity)))
