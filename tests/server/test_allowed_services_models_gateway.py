"""Allowed services covers the models gateway that document and audio
engines use, judged by the gateway's actual origin."""

from __future__ import annotations

import pytest
from model_doubles import HttpClientRouter, RecordingSidecarHttp

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project
from frisket.service_access import (
    AllowedServices,
    ApprovedEndpoint,
    ApprovedEndpoints,
    write_approved_endpoints,
)

REMOTE_GATEWAY = "http://models.lan.example.test:9000"
LOOPBACK_GATEWAY = "http://127.0.0.1:9000"


def _run(project: Project, sheet: int, http: RecordingSidecarHttp):
    spec = {
        "action_id": "media.to_markdown",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"source": "doc", "engine": "docling"},
        "output_names": {"markdown": "markdown", "ocr_used": "markdown_ocr_used"},
        "idempotency_key": "allowed-services-gateway",
    }
    router = HttpClientRouter(http)
    result = run_action_spec(project, spec, project_id="t", router=router)
    if result.status == "needs_confirmation":
        spec = {**spec, "confirmation": result.errors[0].details["promise_set_hash"]}
        result = run_action_spec(project, spec, project_id="t", router=router)
    return result


@pytest.mark.parametrize(
    ("origin", "setting", "approve", "allowed"),
    [
        (REMOTE_GATEWAY, "this_machine_only", False, False),
        (REMOTE_GATEWAY, "approved_services", False, False),
        (REMOTE_GATEWAY, "approved_services", True, True),
        (REMOTE_GATEWAY, "this_machine_only", True, False),
        (REMOTE_GATEWAY, "all_services", False, True),
        (LOOPBACK_GATEWAY, "this_machine_only", False, True),
    ],
)
def test_models_gateway_is_judged_by_its_origin(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    setting: AllowedServices,
    approve: bool,
    allowed: bool,
) -> None:
    monkeypatch.setenv("FRISKET_MODELS_URL", origin)
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    if approve:
        write_approved_endpoints(
            tmp_path,
            ApprovedEndpoints(
                endpoints=(ApprovedEndpoint(host="models.lan.example.test"),)
            ),
        )
    project = Project.create(tmp_path / "t.frisket", name="t", allowed_services=setting)
    sheet = project.add_sheet("data")
    cols = {"doc": project.add_column(sheet, "doc", type="text")}
    project.add_rows(sheet, [{"doc": "<html><body>hi</body></html>"}], cols)
    http = RecordingSidecarHttp(
        {"documents": [{"markdown": "# Converted", "ocr_used": [False]}]}
    )

    result = _run(project, sheet, http)

    if allowed:
        assert result.status == "completed", result.errors
        assert http.captured["url"] == f"{origin}/to-markdown"
    else:
        assert result.status == "failed"
        assert result.errors[0].code == "service_not_allowed"
        assert http.captured == {}
    project.close()
