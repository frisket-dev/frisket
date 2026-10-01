from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from frisket.engine.store import Project
from frisket.server import provider_config
from frisket.server.mcp import LocalBackend


class _CompletedBackfill:
    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return {
            "status": "completed",
            "run_id": 17,
            "receipt_id": "receipt-search-provider",
            "outputs": [
                {
                    "kind": "run_backfill",
                    "ref": {
                        "kind": "run_backfill",
                        "run_id": 17,
                        "requested_row_ids": [],
                        "filled_row_ids": [],
                        "filled": 0,
                    },
                }
            ],
            "errors": [],
        }


@pytest.mark.parametrize("provider", ["exa", "tavily"])
def test_local_mcp_backfill_uses_the_workspace_search_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    Project.create(root / "demo.frisket", name="Demo").close()
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    provider_config.save_local_provider_key(root, provider, f"{provider}-test-key")
    provider_config.save_search_provider(root, provider)

    selected: list[str] = []

    def run_action_spec(_project: Project, _action: dict[str, Any], **kwargs: Any):
        factory = kwargs["deps"].search_service_factory
        assert factory is not None
        selected.append(factory().provider)
        return _CompletedBackfill()

    monkeypatch.setattr("frisket.engine.executor.run_action_spec", run_action_spec)
    backend = LocalBackend(root)
    try:
        result = asyncio.run(backend.backfill_run("demo", 1, "sources"))
        assert result["status"] == "completed"
        assert selected == [provider]
    finally:
        backend.ws.get("demo").close()
