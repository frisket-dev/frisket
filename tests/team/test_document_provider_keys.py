"""Organization document setup and queued execution use the same credentials."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter
from starlette.routing import Mount

from frisket.ai.llm import ModelRouter
from frisket.credentials import resolve_credential_for_use
from frisket.engine.jobs import Worker
from frisket.engine.jobs.ports import (
    JobHandlerContext,
    TrustedJobOrgUnavailable,
    WorkerPorts,
)
from frisket.engine.jobs.runs import resolve_run_action_credentials
from frisket.engine.store.media_blobs import media_cell, owned_media_metadata_document
from frisket.engine.store.runs import RunResultStore
from frisket.execution.credential_use import CredentialOwner, CredentialUseContext
from frisket.team.app import TeamConfig, create_team_app
from frisket.team.document_credentials import OrganizationDocumentCredentials
from tests.team_setup_helpers import claim_server
from tests.ops.test_opendocrouter import ENGINE, response


@pytest.fixture(autouse=True)
def close_test_control_pool(tmp_path):
    from frisket.team.control_plane import _ENGINES

    yield
    # Successful parametrizations can reuse pytest's temporary path. Close
    # this test's cached SQLite handle before pytest removes its database.
    engine = _ENGINES.pop(f"sqlite:///{tmp_path / 'control.db'}", None)
    if engine is not None:
        engine.dispose()


@pytest.mark.parametrize(
    "provider,engine", [("datalab", "datalab"), ("opendocrouter", ENGINE)]
)
@pytest.mark.parametrize("standalone", [False, True])
def test_org_key_setup_selector_and_queued_ocr(
    tmp_path, monkeypatch, provider, engine, standalone
):
    async def validate(_provider, _key):
        return True

    config = TeamConfig(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Desk",
        magic_link_enabled=False,
    )
    app = create_team_app(config, validate_provider_key=validate)
    owner = TestClient(app)
    claim_server(app, client=owner, workspace_name="Desk")
    core = next(r.app for r in app.routes if isinstance(r, Mount) and r.path == "")
    workspace = core.state.workspace
    calls = []
    expected_key = "org-document-key"
    fixtures = Path(__file__).parents[1] / "fixtures/datalab"

    def handler(request):
        calls.append(request)
        if provider == "opendocrouter":
            assert request.headers["authorization"] == f"Bearer {expected_key}"
            return httpx.Response(200, json=response())
        assert request.headers["x-api-key"] == expected_key
        is_ocr = (
            b'name="word_bboxes"' in request.content
            if request.method == "POST"
            else request.url.path.endswith("LHGvzW1QzTVi9wRglrtZ2Q")
        )
        kind = "ocr" if is_ocr else "convert"
        phase = "submit" if request.method == "POST" else "complete"
        return httpx.Response(
            200, json=json.loads((fixtures / f"{kind}_{phase}.json").read_text())
        )

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ModelRouter, "client", property(lambda self: transport))
    # Environment credentials must not substitute for this organization's key.
    monkeypatch.setenv("DATALAB_API_KEY", "deployment-secret")
    monkeypatch.setenv("OPEN_DOC_ROUTER_API_KEY", "deployment-secret")
    pid = owner.post("/api/projects", json={"name": "Scans"}).json()["id"]
    project = workspace.get(pid)
    sheet = project.add_sheet("Scans")
    col = project.add_column(sheet, "image", type="image")
    data = io.BytesIO()
    Image.new("RGB", (100, 200), "white").save(data, format="PNG")
    blob = project.add_blob(
        data.getvalue(),
        filename="scan.png",
        mime="image/png",
        metadata=owned_media_metadata_document(
            probe={"kind": "image", "width": 100, "height": 200}
        ),
    )
    doc_col = project.add_column(sheet, "document", type="file")
    pdf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    writer.write(pdf)
    doc_blob = project.add_blob(
        pdf.getvalue(),
        filename="scan.pdf",
        mime="application/pdf",
        metadata=owned_media_metadata_document(probe={"kind": "pdf", "pages": 1}),
    )
    project.add_rows(
        sheet,
        [
            {
                "image": media_cell(blob, filename="scan.png", mime="image/png"),
                "document": media_cell(
                    doc_blob, filename="scan.pdf", mime="application/pdf"
                ),
            }
        ],
        {"image": col, "document": doc_col},
    )

    def choice():
        result = owner.post(
            f"/api/projects/{pid}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "action",
                    "action_id": "media.ocr",
                    "field": "engine",
                    "params": {},
                },
            },
        )
        assert result.status_code == 200, result.text
        return next(
            c
            for g in result.json()["groups"]
            for c in g["choices"]
            if c["authored_selection"].get("engine") == engine
        )

    missing = choice()
    assert missing["status"] == "needs_setup", missing
    org_scope = next(
        s for s in missing["setup"]["scopes"] if s["scope"] == "organization"
    )
    assert org_scope["can_mutate"]
    checked = owner.post(
        "/api/org/keys/validate", json={"provider": provider, "key": "org-document-key"}
    )
    assert checked.status_code == 200 and checked.json()["ok"], checked.text
    saved = owner.post(
        "/api/org/keys",
        json={
            "provider": provider,
            "key": "org-document-key",
            "validation_token": checked.json()["validation_token"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert "org-document-key" not in saved.text
    assert any(k["provider"] == provider for k in owner.get("/api/org/keys").json()), (
        owner.get("/api/org/keys").json()
    )
    from frisket.execution.provider import ExecutionCompositionContext

    composed = workspace.execution_composition_for(
        project, workspace.router_for(project), ExecutionCompositionContext.direct()
    )
    assert (
        composed.credential_use_context.credential_resolver.keys.get(provider)
        == "org-document-key"
    )
    for identity in (TrustedJobOrgUnavailable.JOB_ROW_HAS_NO_ORG, 999):
        with pytest.raises(ValueError):
            workspace.execution_composition_for(
                project,
                workspace.router_for(project),
                replace(
                    ExecutionCompositionContext.direct(), trusted_job_org_id=identity
                ),
            )
    ready = choice()
    assert ready["status"] == "ready", ready
    if not standalone:
        from tests.deterministic_time import controlled_time

        preview_request = {
            "action_id": "media.to_markdown",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "document", "engine": engine},
            "idempotency_key": "org-convert-preview",
        }
        endpoint = f"/api/projects/{pid}/actions/v1/preview"
        started = owner.post(endpoint, json=preview_request)
        if started.status_code == 402:
            preview_request["confirmation"] = started.json()["error"]["details"][
                "promise_set_hash"
            ]
            started = owner.post(endpoint, json=preview_request)
        assert started.status_code == 202, started.text
        preview_id = started.json()["preview_id"]
        payload = None

        def finished():
            nonlocal payload
            polled = owner.get(f"{endpoint}/{preview_id}")
            assert polled.status_code == 200, polled.text
            payload = polled.json()
            return payload["status"] != "running"

        with controlled_time(timeout=10) as clock:
            clock.wait_until(
                finished, message="Organization conversion preview did not finish"
            )
        assert payload["status"] == "done", payload
        expected_markdown = "Acme" if provider == "datalab" else "# Page 1"
        assert expected_markdown in json.dumps(payload["result"]["rows"]), payload

    request = {
        "action_id": "media.ocr",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"source": "image", "engine": engine},
        "output_names": {"text": "text", "blocks": "boxes"},
        "idempotency_key": "org-ocr",
    }
    result = owner.post(f"/api/projects/{pid}/actions/v1/run", json=request)
    if result.status_code == 402:
        request["confirmation"] = result.json()["errors"][0]["details"][
            "promise_set_hash"
        ]
        result = owner.post(f"/api/projects/{pid}/actions/v1/run", json=request)
    assert result.status_code == 200, result.text
    job_id = result.json()["job_id"]
    assert "org-document-key" not in repr(workspace.queue.get(job_id).payload)
    # Rotation between admission and execution must use the current org key.
    expected_key = "rotated-org-document-key"
    checked = owner.post(
        "/api/org/keys/validate", json={"provider": provider, "key": expected_key}
    )
    assert checked.status_code == 200, checked.text
    saved = owner.post(
        "/api/org/keys",
        json={
            "provider": provider,
            "key": expected_key,
            "validation_token": checked.json()["validation_token"],
        },
    )
    assert saved.status_code == 200, saved.text
    registry = workspace.registry
    if standalone:
        from frisket.engine.jobs.worker import HandlerRegistry
        from frisket.engine.jobs.runs import register_project_run_handler
        from frisket.team.document_credentials import TeamOrgDocumentCredentialPort
        from frisket.team.secret_box import TeamSecretBox

        registry = HandlerRegistry()
        register_project_run_handler(
            registry,
            workspace_root=workspace.root,
            control_database_url=config.database_url,
            worker_ports=WorkerPorts(
                action_credential_port=TeamOrgDocumentCredentialPort(
                    TeamSecretBox(config).decrypt
                )
            ),
        )
    worker = Worker(workspace.queue, registry, poll_interval=0.01)
    for _ in range(20):
        job = workspace.queue.get(job_id)
        if job.status in {"done", "failed"}:
            break
        assert worker.run_once()
    job = workspace.queue.get(job_id)
    assert job.status == "done", job
    run_id = result.json()["run_id"]
    facts = RunResultStore(project).model_calls(run_id)
    assert facts and all(f["credential_source"] == "org_byok" for f in facts), facts
    text_column = next(c for c in project.columns(sheet) if c["name"] == "text")
    expected_text = "Hello Datalab" if provider == "datalab" else "Page 1"
    assert all(
        expected_text in value
        for value in project.get_values(sheet, text_column["id"]).values()
    )
    assert calls
    assert owner.delete(f"/api/org/keys/{provider}").status_code == 200
    assert choice()["status"] == "needs_setup"
    asyncio.run(transport.aclose())


@pytest.mark.parametrize(
    "name,provider",
    [("DATALAB_API_KEY", "datalab"), ("OPEN_DOC_ROUTER_API_KEY", "opendocrouter")],
)
def test_document_resolution_keeps_owner_and_project_precedence(
    monkeypatch, name, provider
):
    monkeypatch.setenv(name, "env-key")
    values = {}
    project = SimpleNamespace(
        provider_model_keys=lambda: values, secret_plaintext=lambda _: None
    )
    resolver = OrganizationDocumentCredentials(7, {provider: "org-key"})
    context = CredentialUseContext(credential_resolver=resolver)
    resolved = resolve_credential_for_use(project, name, context=context)
    assert resolved.value == "org-key" and resolved.source == "org_byok"
    assert resolved.owner == CredentialOwner.organization(7)
    values[provider] = "project-key"
    assert (
        resolve_credential_for_use(project, name, context=context).source
        == "project_key"
    )
    values.clear()
    project.secret_plaintext = lambda _: "legacy-project-key"
    assert (
        resolve_credential_for_use(project, name, context=context).value
        == "legacy-project-key"
    )
    project.secret_plaintext = lambda _: None
    empty = CredentialUseContext(
        credential_resolver=OrganizationDocumentCredentials(7, {})
    )
    assert resolve_credential_for_use(project, name, context=empty) is None
    assert "org-key" not in repr(resolver)


def test_worker_action_port_uses_only_trusted_org_identity():
    seen = []

    class Port:
        def action_credential_resolver(self, *, org_id, control_database_url):
            seen.append(org_id)
            return OrganizationDocumentCredentials(org_id, {})

    ports = WorkerPorts(action_credential_port=Port())
    resolver = resolve_run_action_credentials(
        handler_context=JobHandlerContext.from_claimed_job(trusted_org_id=17),
        ports=ports,
        control_database_url="sqlite:///control",
    )
    assert resolver.org_id == 17 and seen == [17]
    with pytest.raises(ValueError, match="trusted job identity"):
        resolve_run_action_credentials(
            handler_context=JobHandlerContext.without_job_row(),
            ports=ports,
            control_database_url="sqlite:///control",
        )


@pytest.mark.parametrize("reachable,ok", [(True, True), (True, False), (False, False)])
def test_team_key_validation_preserves_reachability(monkeypatch, reachable, ok):
    from frisket.server import provider_config
    from frisket.team.auth_runtime import validate_provider_credential

    monkeypatch.setattr(
        provider_config,
        "probe_provider",
        lambda *a, **kw: {"reachable": reachable, "ok": ok},
    )
    if reachable:
        assert asyncio.run(validate_provider_credential("datalab", "key")) is ok
    else:
        with pytest.raises(RuntimeError, match="could not be reached"):
            asyncio.run(validate_provider_credential("datalab", "key"))
