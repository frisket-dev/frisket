"""FtM upload admission uses normal action dispatch and request-owned sources."""

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.actions.entity_package_types import ImportedEntityDataset
from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.contracts.action import ActionError, ActionIdentity, ActionResult
from frisket.engine.executor import BoundLocalFile, ExecutorDeps
from frisket.engine.store.receipts import ReceiptStore
from frisket.server.app import create_app
from frisket.server.services import action_runs
from frisket.server.services.action_runs import ActionRunResponse, ActionRunService


def test_upload_route_forwards_verified_borrowed_source_and_preserves_refusal(
    tmp_path, monkeypatch
):
    requests = []
    content = b'{"id":"person","schema":"Person","properties":{"name":["Ada"]}}'

    def observe(self, project_id, body, *, request_context, admitted_sources):
        assert request_context.path_params["pid"] == project_id
        assert body["action_id"] == "frisket.ftm.ftm_import"
        assert body["scope"] == {"kind": "project"}
        assert body["params"]["dataset_name"] == "Case"
        path = body["params"]["source_path"]
        assert not Path(path).exists()
        assert set(admitted_sources) == {path}
        admitted = admitted_sources[path]
        assert not admitted.stream.closed
        assert admitted.stream.tell() == 0
        assert admitted.stream.read() == content
        assert admitted.sha256 == "sha256:" + hashlib.sha256(content).hexdigest()
        requests.append((body, admitted.stream))
        return ActionRunResponse(
            status_code=400,
            payload=ActionResult(
                action=ActionIdentity(
                    action_id=body["action_id"], kind=body["action_id"]
                ),
                project_id=project_id,
                status="failed",
                errors=[ActionError(code="not_enabled", message="Enable the plugin")],
            ).model_dump(mode="json"),
        )

    monkeypatch.setattr(ActionRunService, "run_action", observe)
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post("/api/projects", json={"name": "Upload"}).json()["id"]
    for filename in ("case.jsonl", "renamed.jsonl"):
        response = client.post(
            f"/api/projects/{project_id}/import/followthemoney",
            files={"file": (filename, content, "application/x-ndjson")},
            data={"dataset_name": "Case"},
        )
        assert response.status_code == 400, response.text
        assert response.json()["errors"][0]["code"] == "not_enabled"
        assert requests[-1][1].closed
    assert requests[0][0] == requests[1][0]
    assert not (tmp_path / "workspace" / ".v1_import_uploads").exists()
    operation = client.get("/openapi.json").json()["paths"][
        "/api/projects/{pid}/import/followthemoney"
    ]["post"]
    assert operation["requestBody"]["content"].keys() == {"multipart/form-data"}
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ActionResult"
    }


def test_normal_action_service_preserves_request_dependencies_and_admitted_sources(
    tmp_path, monkeypatch
):
    def cancelled():
        return False

    calls = []

    def factory(project_id, request):
        calls.append((project_id, request))
        return ExecutorDeps(cancelled=cancelled)

    app = create_app(tmp_path / "workspace", executor_deps_factory=factory)
    client = TestClient(app)
    project_id = client.post("/api/projects", json={"name": "Dependencies"}).json()[
        "id"
    ]
    workspace = app.state.workspace
    original = action_runs.run_action_spec
    stream = io.BytesIO(b"admitted")
    sources = {"admission-label": BoundLocalFile(stream, "sha256:observed")}

    def observe(project, body, **kwargs):
        assert kwargs["deps"].local_file_sources is sources
        assert kwargs["deps"].cancelled is cancelled
        return original(project, body, **kwargs)

    monkeypatch.setattr(action_runs, "run_action_spec", observe)
    context = object()
    response = ActionRunService(workspace).run_action(
        project_id,
        {
            "action_id": "import.rows",
            "scope": {"kind": "project"},
            "sheet_name": "Rows",
            "params": {
                "columns": [{"name": "name", "type": "text"}],
                "rows": [{"name": "Ada"}],
            },
            "idempotency_key": "admitted-source-deps",
        },
        request_context=context,
        admitted_sources=sources,
    )
    assert response.status_code == 200, response.payload
    assert response.payload["status"] == "completed"
    assert calls == [(project_id, context)]
    assert not stream.closed


def test_disabled_ftm_upload_does_not_publish(tmp_path):
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post("/api/projects", json={"name": "Disabled"}).json()["id"]
    response = client.post(
        f"/api/projects/{project_id}/import/followthemoney",
        files={"file": ("entities.jsonl", b"{}", "application/json")},
    )
    assert response.status_code == 400, response.text
    assert response.json()["status"] == "failed"
    project = client.app.state.workspace.get(project_id)
    for table in ("sheets", "rows", "columns", "receipts"):
        assert project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_installed_ftm_http_upload_replay_and_downloadable_native_export(tmp_path):
    pytest.importorskip("followthemoney")
    root = Path(__file__).resolve().parents[2]
    plugin_root = root / "src/frisket/authoring/bundled_plugins/frisket.ftm"
    # rule19: the real installed manifest supplies consent grants for HTTP execution.
    manifest = json.loads((plugin_root / "plugin.json").read_text())
    content = (root / "tests/goldens/ftm_bundled_plugin/case.ftm.json").read_bytes()
    _reset_default_registry_for_tests()
    try:
        with TestClient(create_app(tmp_path / "workspace")) as client:
            pid = client.post("/api/projects", json={"name": "FtM HTTP"}).json()["id"]
            plugin_url = f"/api/projects/{pid}/workbench/plugins/frisket.ftm"
            installed = client.post(
                f"{plugin_url}/install-local",
                json={
                    "source": {"kind": "localPath", "value": str(plugin_root)},
                    "arbitraryPackageLoadAllowed": False,
                },
            )
            assert installed.status_code == 200, installed.text
            activated = client.post(
                f"{plugin_url}/activate",
                json={
                    "receiptId": installed.json()["receiptId"],
                    "trustAcknowledged": True,
                    "permissionsAccepted": manifest["requires"]["capabilities"],
                    "arbitraryPackageLoadAllowed": False,
                },
            )
            assert activated.status_code == 200, activated.text
            backend = client.post(
                f"{plugin_url}/backend/activate",
                json={
                    "trustAcknowledged": True,
                    "arbitraryPackageLoadAllowed": False,
                    "executableHandlersAllowed": True,
                },
            )
            assert backend.status_code == 200, backend.text

            url = f"/api/projects/{pid}/import/followthemoney"
            uploaded = client.post(
                url,
                files={"file": ("case.jsonl", content, "application/x-ndjson")},
                data={"dataset_name": "HTTP case"},
            )
            assert uploaded.status_code == 200, uploaded.text
            imported = uploaded.json()
            assert imported["status"] == "completed", imported
            dataset = ImportedEntityDataset.model_validate(imported["value"])
            assert dataset.dataset_name == "HTTP case"
            assert len(dataset.sheets) == 4
            assert {sheet.kind for sheet in dataset.sheets} == {
                "entity",
                "relationship",
                "unsupported",
            }
            assert {item["sheet_id"] for item in imported["outputs"]} == {
                sheet.sheet_id for sheet in dataset.sheets
            }
            project = client.app.state.workspace.get(pid)
            receipt = ReceiptStore(project).parsed_by_id(imported["receipt_id"])
            source = next(
                item.ref
                for item in receipt.inputs
                if item.ref["kind"] == "local_file_read"
            )
            assert source["sha256"] == "sha256:" + hashlib.sha256(content).hexdigest()
            assert not (tmp_path / "workspace" / ".v1_import_uploads").exists()
            raw_entities = []
            for sheet in dataset.sheets:
                raw_entities.extend(
                    project.get_values(
                        sheet.sheet_id, sheet.column_ids["_ftm_raw_json"]
                    ).values()
                )
            assert sorted(raw_entities, key=lambda row: row["id"]) == sorted(
                (
                    entity
                    for line in content.splitlines()
                    if (entity := json.loads(line))["id"] != "person-missing-name"
                ),
                key=lambda row: row["id"],
            )
            # The golden's nameless Person is refused by the real SDK validator.
            assert any(item.get("line_number") == 4 for item in dataset.diagnostics)
            replay = client.post(
                url,
                files={"file": ("renamed.jsonl", content, "application/x-ndjson")},
                data={"dataset_name": "HTTP case"},
            )
            assert replay.status_code == 200, replay.text
            assert replay.json() == imported
            assert len(project.sheets()) == 4

            rowsets, mappings = [], []
            for sheet in dataset.sheets:
                if sheet.kind == "unsupported":
                    continue
                rowset = {"kind": "sheet", "sheet_id": sheet.sheet_id}
                rowsets.append(rowset)
                mappings.append(
                    {
                        "rowset": rowset,
                        "schema": sheet.schema_name,
                        "id_policy": {"kind": "row_ref"},
                        "properties": {
                            name: {"column": name}
                            for name in sheet.column_ids
                            if not name.startswith("_ftm_")
                        },
                    }
                )
            export_request = {
                "action_id": "frisket.ftm.ftm_export",
                "scope": {"kind": "project"},
                "params": {
                    "rowsets": rowsets,
                    "mappings": mappings,
                    "filename": "http-case.zip",
                    "validate_entities": True,
                },
                "idempotency_key": "http-export",
            }
            exported = client.post(
                f"/api/projects/{pid}/actions/v1/run", json=export_request
            )
            assert exported.status_code == 200, exported.text
            result = exported.json()
            assert result["status"] == "completed", result
            package = result["value"]
            assert package["filename"] == "http-case.zip"
            assert package["entity_count"] == 3
            assert ReceiptStore(project).parsed_by_id(result["receipt_id"]).exports == [
                package
            ]
            downloaded = client.get(f"/api/projects/{pid}/blobs/{package['blob_hash']}")
            assert downloaded.status_code == 200, downloaded.text
            assert downloaded.headers["content-type"] == "application/zip"
            assert (
                hashlib.sha256(downloaded.content).hexdigest() == package["blob_hash"]
            )
            with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
                entities = [
                    json.loads(line)
                    for line in archive.read("entities.ftm.jsonl").splitlines()
                ]
                assert {entity["schema"] for entity in entities} == {
                    "Person",
                    "Company",
                    "Membership",
                }
                assert json.loads(archive.read("manifest.json"))["validate"] is True
                assert json.loads(archive.read("source_refs.json"))["entry_count"] > 0
            export_replay = client.post(
                f"/api/projects/{pid}/actions/v1/run", json=export_request
            )
            assert export_replay.status_code == 200, export_replay.text
            assert export_replay.json() == result
            other = client.post("/api/projects", json={"name": "Other"}).json()["id"]
            assert (
                client.get(
                    f"/api/projects/{other}/blobs/{package['blob_hash']}"
                ).status_code
                == 404
            )
    finally:
        _reset_default_registry_for_tests()
