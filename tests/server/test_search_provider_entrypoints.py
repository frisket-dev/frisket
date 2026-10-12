from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.ai.research.search import (
    SearchHit,
    SearchResponse,
    SearchService,
    SearchUsage,
    SearchUsageUnit,
)
from frisket.server import provider_config
from frisket.server.app import create_app
from frisket.server.mcp import LocalBackend
from http_test_helpers import (
    drain_queue,
    post_row_add_as_v1_action,
    post_v1_action_with_exact_confirmation,
)


def _web_search_spec(sheet_id: int) -> dict:
    return {
        "action_id": "research.web_search",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"query": {"text": "Background on {{story}}"}, "max_results": 1},
        "output_names": {"search_results": "sources"},
        "idempotency_key": "search-provider-entrypoint",
    }


@pytest.mark.parametrize("provider", ["exa", "tavily"])
def test_local_mcp_backfill_uses_the_workspace_search_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    root = tmp_path / "workspace"
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    calls: list[tuple[str, str]] = []

    async def fake_search(
        service: SearchService,
        query: str,
        *,
        max_results: int = 6,
        timeout: float = 20.0,
        quote=None,
    ) -> SearchResponse:
        del timeout
        quote = quote or service.quote(max_results=max_results)
        calls.append((service.provider, query))
        return SearchResponse(
            provider=service.provider,
            results=(
                SearchHit(
                    title="Result",
                    url="https://example.test/result",
                    excerpt="Evidence",
                    retrieved_at=datetime.now(UTC).isoformat(),
                    published_at=None,
                    provider=service.provider,
                ),
            ),
            usage=SearchUsage(
                provider=service.provider,
                service=f"{service.provider}.search",
                request_count=1,
                provider_reported_cost_usd=None,
                provider_cost_usd=quote.estimated_cost_usd,
                cost_source="configured_catalog",
                units=(SearchUsageUnit(name=quote.unit, quantity=1),),
                quote=quote,
            ),
        )

    monkeypatch.setattr(SearchService, "search", fake_search)
    with TestClient(create_app(root)) as client:
        provider_config.save_local_provider_key(root, provider, f"{provider}-test-key")
        provider_config.save_search_provider(root, provider)
        project_id = client.post("/api/projects", json={"name": "Demo"}).json()["id"]
        imported = client.post(
            f"/api/projects/{project_id}/import/csv",
            files={"file": ("stories.csv", "story\nfirst story\n", "text/csv")},
        )
        assert imported.status_code == 200, imported.text
        sheet_id = int(imported.json()["sheet_id"])
        source = post_v1_action_with_exact_confirmation(
            client, project_id, _web_search_spec(sheet_id)
        )
        assert source.status_code == 200, source.text
        drain_queue(client)
        added = post_row_add_as_v1_action(
            client, project_id, sheet_id, {"story": "late story"}
        )
        assert added.status_code == 200, added.text

    backend = LocalBackend(root)
    try:
        challenge = asyncio.run(backend.backfill_run(project_id, sheet_id, "sources"))
        assert challenge["status"] == "needs_confirmation"
        result = asyncio.run(
            backend.backfill_run(
                project_id,
                sheet_id,
                "sources",
                confirmed=True,
                consented_promise_set_hash=challenge["promise_set_hash"],
            )
        )
        assert result["status"] == "completed"
        assert result["filled"] == 1
        assert calls == [
            (provider, "Background on first story"),
            (provider, "Background on late story"),
        ]
    finally:
        backend.ws.get(project_id).close()
        backend.ws.queue.close()
