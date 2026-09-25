"""Typed Docling execution through the optional Solo-managed model server."""

from __future__ import annotations

from contextlib import closing

import pytest

from frisket.ai.llm import ModelRouter
from frisket.contracts.action import Receipt
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.executor import document_convert as document_convert_module
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import RouteStore
from frisket.engine.store.media_blobs import media_cell
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.execution.promises import Promise


@pytest.fixture
def project(tmp_path):
    with closing(
        Project.create(tmp_path / "managed-docling.frisket", name="docs")
    ) as p:
        yield p


def _seed_pdf(project: Project) -> tuple[int, int]:
    sheet_id = project.add_sheet("data")
    column_id = project.add_column(sheet_id, "doc", type="file")
    digest = project.add_blob(
        b"%PDF-1.4 managed docling test",
        filename="scan.pdf",
        mime="application/pdf",
    )
    [row_id] = project.add_rows(
        sheet_id,
        [{"doc": media_cell(digest, mime="application/pdf", filename="scan.pdf")}],
        {"doc": column_id},
    )
    return sheet_id, row_id


def _run(project: Project, sheet_id: int, composition):
    return run_action_spec(
        project,
        {
            "action_id": "media.to_markdown",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"source": "doc", "engine": "docling"},
            "output_names": {"markdown": "converted"},
            "idempotency_key": "managed-docling-action",
        },
        project_id="managed-docling-test",
        router=ModelRouter(cache=None, cache_mode="off"),
        deps=ExecutorDeps(execution_composition=composition),
    )


def _composition(project: Project, *, include_managed: bool = True):
    router = ModelRouter(cache=None, cache_mode="off")
    return open_execution_composition(
        project,
        router,
        ExecutionCompositionContext.direct(),
        include_managed_local_models=include_managed,
    )


def test_typed_docling_uses_managed_local_route_without_consent_or_fee(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRISKET_LOCAL_MODELS_URL", "http://127.0.0.1:42117")
    monkeypatch.setenv("FRISKET_LOCAL_MODELS_TOKEN", "managed-token")
    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)
    monkeypatch.setattr("frisket.runtime.model_install.is_installed", lambda: True)
    captured = {}

    async def fake_post(
        ctx, route, *, files=None, data=None, connection=None, **kwargs
    ):
        captured.update(route=route, data=data, connection=connection)
        return {"documents": [{"markdown": "# Local result", "ocr_used": [True]}]}

    monkeypatch.setattr(document_convert_module, "sidecar_post", fake_post)
    sheet_id, row_id = _seed_pdf(project)

    result = _run(project, sheet_id, _composition(project))

    assert result.status == "completed", result.errors
    columns = {column["name"]: column["id"] for column in project.columns(sheet_id)}
    assert (
        project.get_values(sheet_id, columns["converted"])[row_id] == "# Local result"
    )
    assert project.get_values(sheet_id, columns["ocr_used"])[row_id] == [True]
    assert captured["route"] == "/to-markdown"
    assert captured["data"]["engine"] == "docling"
    assert captured["connection"].base_url == "http://127.0.0.1:42117"
    assert captured["connection"].token == "managed-token"

    route, promise_set = RouteStore.for_run(project, result.run_id).head()
    assert route.target_snapshot == {
        "target_id": "local-models",
        "capability": "document.convert",
        "transport": "sidecar.convert",
        "run_scoped": False,
    }
    assert route.egress_class == "none"
    assert route.cost_posture == "operator_borne"
    assert RouteStore.for_run(project, result.run_id).consents() == []
    promises = [Promise.from_row(row) for row in promise_set.promises]
    assert not any(promise.field == "cost" for promise in promises)
    receipt_body = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()[0]
    receipt = Receipt.model_validate_json(receipt_body)
    assert all(use.get("cost_actual") in {None, 0.0} for use in receipt.provider_use)


@pytest.mark.parametrize("managed_installed", [False, True])
def test_configured_external_docling_is_not_replaced_by_managed_runtime(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
    managed_installed: bool,
) -> None:
    monkeypatch.setenv("FRISKET_LOCAL_MODELS_URL", "http://127.0.0.1:42117")
    monkeypatch.setenv("FRISKET_LOCAL_MODELS_TOKEN", "managed-token")
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://gateway.example:9000")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "gateway-token")
    monkeypatch.setattr(
        "frisket.runtime.model_install.is_installed", lambda: managed_installed
    )
    captured = {}

    async def fake_post(
        ctx, route, *, files=None, data=None, connection=None, **kwargs
    ):
        captured["connection"] = connection
        return {"documents": [{"markdown": "# Gateway result", "ocr_used": []}]}

    monkeypatch.setattr(document_convert_module, "sidecar_post", fake_post)
    sheet_id, _row_id = _seed_pdf(project)

    result = _run(project, sheet_id, _composition(project))

    assert result.status == "completed", result.errors
    route, _promise_set = RouteStore.for_run(project, result.run_id).head()
    assert route.target_snapshot["target_id"] == "models-gateway"
    assert route.egress_class == "operator_lan"
    assert captured["connection"].base_url == "http://gateway.example:9000"
    assert captured["connection"].token == "gateway-token"
