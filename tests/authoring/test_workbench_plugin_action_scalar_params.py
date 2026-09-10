from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.engine.jobs import Worker
from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.server.app import create_app


TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"
PLUGIN_ID = "demo.scalar_params"
FORMAT_ACTION_KIND = "demo.scalar_params.format_text"
COMBINE_ACTION_KIND = "demo.scalar_params.combine_columns"
TEMPLATE_ACTION_KIND = "demo.scalar_params.template_sources"
FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "local_plugins"
PLUGIN_ROOT = FIXTURE_ROOT / "demo_scalar_params"


@pytest.fixture(autouse=True)
def reset_registry() -> None:
    _reset_default_registry_for_tests()
    yield
    _reset_default_registry_for_tests()


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _project_with_rows(client: TestClient) -> tuple[str, int, list[int]]:
    project_id = client.post("/api/projects", json={"name": "Scalar params"}).json()[
        "id"
    ]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={
            "file": (
                "rows.csv",
                "text,extra,city,state\nalpha,one,Boston,MA\nbeta,two,Austin,TX\n",
                "text/csv",
            )
        },
    )
    assert imported.status_code == 200, imported.text
    sheet_id = int(imported.json()["sheet_id"])
    data = _sheet_data(client, project_id, sheet_id)
    return project_id, sheet_id, [int(row["id"]) for row in data["rows"]]


def _install_activate_backend(client: TestClient, project_id: str) -> None:
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(PLUGIN_ROOT)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = installed.json()["receiptId"]

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
    assert set(backend.json()["registeredRuntimeBindings"]["actions"]) == {
        FORMAT_ACTION_KIND,
        COMBINE_ACTION_KIND,
        TEMPLATE_ACTION_KIND,
    }


def _run_action(
    client: TestClient,
    project_id: str,
    *,
    kind: str,
    sheet_id: int,
    row_ids: list[int] | None = None,
    params: dict[str, Any],
    key: str,
    expected_status: int = 200,
) -> dict[str, Any]:
    """An installed Action is an ordinary Action, so it takes the ordinary
    request envelope: selection lives in `scope`, never in `params`."""
    scope: dict[str, Any] = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = row_ids
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={
            "action_id": kind,
            "scope": scope,
            "params": params,
            "idempotency_key": key,
        },
    )
    assert response.status_code == expected_status, response.text
    return response.json()


def _sheet_data(
    client: TestClient,
    project_id: str,
    sheet_id: int,
) -> dict[str, Any]:
    response = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data?offset=0&limit=20"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _cell_values(
    client: TestClient,
    project_id: str,
    sheet_id: int,
    column_name: str,
) -> dict[int, Any]:
    data = _sheet_data(client, project_id, sheet_id)
    column = next(item for item in data["columns"] if item["name"] == column_name)
    column_id = str(column["id"])
    return {int(row["id"]): row["cells"].get(column_id) for row in data["rows"]}


def _drain_queue(client: TestClient, *, worker_id: str = "scalar-param-worker") -> None:
    ws = client.app.state.workspace
    worker = Worker(ws.queue, ws.registry, worker_id=worker_id, poll_interval=0.01)
    while worker.run_once():
        pass


def _receipt(client: TestClient, project_id: str, receipt_id: str) -> dict[str, Any]:
    response = client.get(
        f"/api/projects/{project_id}/actions/v1/receipts/{receipt_id}"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _catalog_entry(
    client: TestClient,
    project_id: str,
    kind: str,
) -> dict[str, Any]:
    response = client.get(f"/api/projects/{project_id}/actions/v1/catalog")
    assert response.status_code == 200, response.text
    return next(item for item in response.json()["actions"] if item["kind"] == kind)


def test_scalar_params_reach_inline_child_without_control_fields(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, row_ids = _project_with_rows(client)
    _install_activate_backend(client, project_id)

    result = _run_action(
        client,
        project_id,
        kind=FORMAT_ACTION_KIND,
        sheet_id=sheet_id,
        row_ids=[row_ids[0]],
        params={
            "text": "text",
            "prefix": "pre",
            "uppercase": True,
            "repeat": 2,
        },
        key="scalar-inline",
    )

    assert result["status"] == "completed"
    values = _cell_values(client, project_id, sheet_id, "formatted")
    assert values[row_ids[0]] == "pre:ALPHA,ALPHA:control=none"
    assert values[row_ids[1]] is None


def test_catalog_keeps_scalar_params_out_of_source_requirements(tmp_path: Path) -> None:
    """Scalar knobs and source columns are declared in one Params model, and
    the catalog still separates them: only the source-shaped fields become
    source_requirements, while every field (scalar included) is described by
    the input schema, which is the single authority."""
    client = _client(tmp_path)
    project_id, _sheet_id, _row_ids = _project_with_rows(client)
    _install_activate_backend(client, project_id)

    format_entry = _catalog_entry(client, project_id, FORMAT_ACTION_KIND)
    assert sorted(format_entry["input_schema"]["properties"]) == [
        "prefix",
        "repeat",
        "text",
        "uppercase",
    ]
    assert format_entry["ui_hints"]["semantic_controls"] == {"text": "column"}
    assert format_entry["ui_hints"]["source_requirements"] == [
        {
            "id": "text",
            "param": "text",
            "label": "Text",
            "accepted_column_types": ["text"],
            "mode": "column",
            "min": 1,
        }
    ]

    combine_entry = _catalog_entry(client, project_id, COMBINE_ACTION_KIND)
    assert combine_entry["ui_hints"]["semantic_controls"] == {"sources": "columns"}
    assert combine_entry["ui_hints"]["source_requirements"][0] == {
        "id": "sources",
        "param": "sources",
        "label": "Sources",
        "accepted_column_types": ["text"],
        "mode": "columns",
        "min": 2,
        "max": 2,
    }
    assert combine_entry["input_schema"]["properties"]["sources"]["maxItems"] == 2

    template_entry = _catalog_entry(client, project_id, TEMPLATE_ACTION_KIND)
    assert list(template_entry["input_schema"]["properties"]) == ["template"]
    assert template_entry["ui_hints"]["semantic_controls"] == {"template": "template"}
    assert template_entry["ui_hints"]["source_requirements"] == [
        {
            "id": "template",
            "param": "template",
            "label": "Template",
            "mode": "template",
            "min": 0,
            "template_columns": "union",
        }
    ]


def test_many_and_template_source_columns_reach_child(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, row_ids = _project_with_rows(client)
    _install_activate_backend(client, project_id)

    combined = _run_action(
        client,
        project_id,
        kind=COMBINE_ACTION_KIND,
        sheet_id=sheet_id,
        row_ids=[row_ids[0]],
        params={
            "sources": ["text", "extra"],
            "separator": "|",
        },
        key="combine-columns",
    )
    assert combined["status"] == "completed"
    assert _cell_values(client, project_id, sheet_id, "combined")[row_ids[0]] == (
        "alpha|one"
    )
    # Provenance is the request's own params now, not a separate
    # plugin_action_source_bindings evidence fact: the selected columns ARE
    # the typed parameter, so the receipt records them with the action.
    combined_receipt = _receipt(client, project_id, str(combined["receipt_id"]))
    assert combined_receipt["action_kind"] == COMBINE_ACTION_KIND
    assert combined_receipt["status"] == "completed"

    templated = _run_action(
        client,
        project_id,
        kind=TEMPLATE_ACTION_KIND,
        sheet_id=sheet_id,
        row_ids=[row_ids[1]],
        params={
            "template": {"text": "{{city}}/{{state}}"},
        },
        key="template-sources",
    )
    assert templated["status"] == "completed"
    assert (
        _cell_values(client, project_id, sheet_id, "template_summary")[row_ids[1]]
        == "{{city}}/{{state}} -> city=Austin;state=TX"
    )
    # The template's own text names the columns it read, and the summary above
    # proves both reached the handler in declaration order.
    templated_receipt = _receipt(client, project_id, str(templated["receipt_id"]))
    assert templated_receipt["action_kind"] == TEMPLATE_ACTION_KIND
    assert templated_receipt["status"] == "completed"


def test_non_scalar_param_is_rejected_before_the_handler_runs(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, _row_ids = _project_with_rows(client)
    _install_activate_backend(client, project_id)

    result = _run_action(
        client,
        project_id,
        kind=FORMAT_ACTION_KIND,
        sheet_id=sheet_id,
        params={
            "text": "text",
            "prefix": {"unsafe": "not-scalar"},
        },
        key="bad-scalar-param",
        expected_status=400,
    )

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "invalid_params"
    assert result["errors"][0]["field"] == "params"
    assert result["errors"][0]["details"]["errors"][0]["loc"] == ["prefix"]
