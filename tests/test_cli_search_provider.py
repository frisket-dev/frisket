from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from frisket.cli.action import action as cli_action
from frisket.engine.store import Project
from frisket.server import provider_config


class _CompletedAction:
    status = "completed"
    errors: tuple[()] = ()

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return {
            "status": self.status,
            "run_id": None,
            "receipt_id": "receipt-cli-search-provider",
            "outputs": [],
            "errors": [],
        }


def _search_action(sheet_id: int) -> dict[str, Any]:
    return {
        "action_id": "research.web_search",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"query": {"text": "Background on {{story}}"}, "max_results": 1},
        "output_names": {"search_results": "sources"},
        "idempotency_key": "cli-search-provider",
    }


def _backfill_action(sheet_id: int) -> dict[str, Any]:
    return {
        "action_id": "run.backfill",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"column": "sources"},
        "idempotency_key": "cli-search-provider-backfill",
    }


@pytest.mark.parametrize("provider", ["exa", "tavily"])
def test_cli_action_and_backfill_share_the_bundle_parent_search_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider: str,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    project_path = root / "demo.frisket"
    project = Project.create(project_path, name="Demo")
    sheet_id = project.add_sheet("Stories")
    project.close()
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    provider_config.save_local_provider_key(root, provider, f"{provider}-test-key")
    provider_config.save_search_provider(root, provider)

    selected: list[str] = []

    def run_action_spec(_project: Project, _action: dict[str, Any], **kwargs: Any):
        factory = kwargs["deps"].search_service_factory
        assert factory is not None
        selected.append(factory().provider)
        return _CompletedAction()

    monkeypatch.setattr("frisket.engine.executor.run_action_spec", run_action_spec)
    for name, request in (
        ("search.json", _search_action(sheet_id)),
        ("backfill.json", _backfill_action(sheet_id)),
    ):
        request_path = tmp_path / name
        request_path.write_text(json.dumps(request), encoding="utf-8")
        assert (
            cli_action(
                [
                    "run",
                    "--project",
                    str(project_path),
                    "--project-id",
                    "demo",
                    str(request_path),
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["status"] == "completed"

    assert selected == [provider, provider]
