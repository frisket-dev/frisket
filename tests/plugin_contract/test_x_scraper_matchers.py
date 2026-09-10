"""frisket.x reference scraper end-to-end (url-classification-plugin-matchers-v1,
matcher contract). Marked ``plugin_contract`` — deselected from the default gate by
pyproject addopts; run explicitly::

    uv run pytest -q -m plugin_contract tests/plugin_contract/test_x_scraper_matchers.py

The full arc the ask names ("write custom scrapers based on urls, e.g.
twitter/x … allow plugins to participate"):

1. Load — the frisket.x manifest validates with its ``matchers`` surface.
2. Register — backend activation registers x.post (scraper) + x.profile
   (collection) host-side at ``source="plugin"``.
3. Classify — ``x.com/nasa/status/123`` -> scraper; ``x.com/nasa`` -> collection.
   A first-party youtube URL is untouched (no execution-trust elevation).
4. Execute — ``x.scrape_post`` runs in the plugin SUBPROCESS under its declared
   capability (distinct pid, plugin identity), writing the ``post`` json cell +
   receipt; a run missing the capability fails closed.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.server.app import create_app
from frisket.features.url_classification import classify_url
from frisket.authoring.workbench.plugin_runtime_capabilities import (
    enabled_workbench_plugin_ids,
)
from frisket.features.url_classification.plugin_matchers import (
    unregister_plugin_matchers,
)

pytestmark = pytest.mark.plugin_contract

TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"
PLUGIN_ID = "frisket.x"
FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "local_plugins"
    / "frisket_x_scraper"
)


@pytest.fixture(autouse=True)
def reset_registry() -> None:
    _reset_default_registry_for_tests()
    unregister_plugin_matchers(PLUGIN_ID)
    yield
    unregister_plugin_matchers(PLUGIN_ID)
    _reset_default_registry_for_tests()


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _sheet_data(client: TestClient, project_id: str, sheet_id: int) -> dict[str, Any]:
    response = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data?offset=0&limit=50"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _columns_by_name(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(column["name"]): column for column in data["columns"]}


def _cell_values(data: dict[str, Any], *, column_name: str) -> dict[int, Any]:
    columns = _columns_by_name(data)
    column_id = int(columns[column_name]["id"])
    return {int(row["id"]): row["cells"].get(str(column_id)) for row in data["rows"]}


def _project_with_post_urls(client: TestClient) -> tuple[str, int, list[int]]:
    project_id = client.post(
        "/api/projects", json={"name": "frisket.x scraper contract"}
    ).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={
            "file": (
                "posts.csv",
                "url\nhttps://x.com/nasa/status/123\nhttps://twitter.com/esa/status/456\n",
                "text/csv",
            )
        },
    )
    assert imported.status_code == 200, imported.text
    sheet_id = int(imported.json()["sheet_id"])
    data = _sheet_data(client, project_id, sheet_id)
    row_ids = [int(row["id"]) for row in data["rows"]]
    assert len(row_ids) == 2
    return project_id, sheet_id, row_ids


def _install_activate_backend(
    client: TestClient, project_id: str, *, plugin_root: Path
) -> dict[str, Any]:
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(plugin_root)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = str(installed.json()["receiptId"])

    activated = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activated.status_code == 200, activated.text

    backend = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/backend/activate",
        json={
            "trustAcknowledged": True,
            "arbitraryPackageLoadAllowed": False,
            "executableHandlersAllowed": True,
        },
    )
    assert backend.status_code == 200, backend.text
    return backend.json()


def _run_scrape(
    client: TestClient,
    project_id: str,
    *,
    sheet_id: int,
    key: str,
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={
            "schema_version": "frisket.action.v2",
            "kind": "x.scrape_post",
            "capabilities": capabilities
            if capabilities is not None
            else ["project:write", TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "params": {"sheet_id": sheet_id, "url": "url"},
            "idempotency_key": key,
        },
    )
    result = response.json()
    result["_http_status"] = response.status_code
    return result


def test_x_scraper_matchers_end_to_end(tmp_path: Path) -> None:
    client = _client(tmp_path)
    plugin_root = tmp_path / "packages" / "frisket_x_scraper"
    shutil.copytree(FIXTURE_ROOT, plugin_root)

    # (1) Before activation the plugin's domain is unclaimed: the first-party
    #     page fallback owns it (no plugin matcher registered yet).
    pre = classify_url("https://x.com/nasa/status/123")
    assert pre is not None and pre.matcher_id == "firstparty.page"

    project_id, sheet_id, row_ids = _project_with_post_urls(client)

    # (2) Activation registers both matchers host-side.
    backend = _install_activate_backend(client, project_id, plugin_root=plugin_root)
    assert backend["registeredBackendContributions"]["matchers"] == [
        "x.post",
        "x.profile",
    ]

    # (3) Classification routes x.com/twitter.com to the plugin; youtube stays
    #     first-party (no hijack, no execution-trust elevation).
    post = classify_url(
        "https://x.com/nasa/status/123",
        enabled_plugin_ids=enabled_workbench_plugin_ids(
            client.app.state.workspace.get(project_id)
        ),
    )
    assert post is not None
    assert (post.kind, post.provider, post.source) == ("scraper", "x", "plugin")
    assert post.origin_plugin_id == PLUGIN_ID
    assert post.handler.action_kind == "x.scrape_post"

    other_project = _project_with_post_urls(client)[0]
    isolated = classify_url(
        "https://x.com/nasa/status/123",
        enabled_plugin_ids=enabled_workbench_plugin_ids(
            client.app.state.workspace.get(other_project)
        ),
    )
    assert isolated is not None and isolated.matcher_id == "firstparty.page"

    profile = classify_url("https://twitter.com/nasa", enabled_plugin_ids={PLUGIN_ID})
    assert profile is not None
    assert profile.kind == "collection"
    assert profile.handler.action_kind == "derive.collection_expand"
    assert profile.expansion is not None
    assert profile.expansion.enumerator == "x.list_profile_posts"

    youtube = classify_url("https://www.youtube.com/watch?v=abc123")
    assert youtube is not None and youtube.source == "first_party"

    # (4a) A run missing the declared capability fails closed (no execution).
    missing = _run_scrape(
        client,
        project_id,
        sheet_id=sheet_id,
        key="x-scrape-missing-cap",
        capabilities=["project:write"],
    )
    assert missing["status"] == "failed"
    assert missing["errors"][0]["code"] == "plugin_capability_required"

    # (4b) With the capability, x.scrape_post runs in the plugin subprocess.
    result = _run_scrape(client, project_id, sheet_id=sheet_id, key="x-scrape-ok")
    assert result["_http_status"] == 200, result
    assert result["status"] == "completed", result
    assert result["receipt_id"]

    data = _sheet_data(client, project_id, sheet_id)
    columns = _columns_by_name(data)
    assert columns["post"]["type"] == "json"
    assert columns["post"]["ai_generated"] is True
    posts = _cell_values(data, column_name="post")
    first = posts[row_ids[0]]
    if isinstance(first, str):
        first = json.loads(first)
    assert first["provider"] == "x"
    assert first["handle"] == "nasa"
    assert first["status_id"] == "123"
    # In-subprocess execution posture: the handler ran under the plugin identity
    # in a DISTINCT process (subprocess_runner), not the host test process.
    assert first["scraped_by_plugin_id"] == PLUGIN_ID
    assert first["scraped_by_handler_key"] == "frisket.x:scrape_post"
    assert int(first["scraper_pid"]) != os.getpid()
