from __future__ import annotations


import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.contracts.http.document_priority import DocumentPriorityResponse
from frisket.server.route_errors import RouteError, register_route_error_handler
from frisket.server.routes.document_priority import register_document_priority_route
from frisket.server.services.document_priority import DocumentPriorityService
from frisket.server.services.selector_choices_capabilities import SelectorCapabilities


def test_route_forwards_only_its_scoped_target_and_run_capability() -> None:
    calls: list[tuple[str, int, int, int, bool]] = []

    class Service:
        def promote(
            self,
            project_id: str,
            *,
            sheet_id: int,
            row_id: int,
            column_id: int,
            may_run_actions: bool,
        ) -> DocumentPriorityResponse:
            calls.append((project_id, sheet_id, row_id, column_id, may_run_actions))
            return DocumentPriorityResponse(promoted=True, import_ref="import-ref")

    app = FastAPI()
    register_route_error_handler(app)
    register_document_priority_route(
        app,
        service=Service(),  # type: ignore[arg-type]
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            may_run_actions=True
        ),
    )

    response = TestClient(app).post(
        "/api/projects/project-a/search-preparation/document-priority",
        json={"sheet_id": 7, "row_id": 11, "column_id": 13},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "schemaVersion": "frisket.document_priority.v1",
        "promoted": True,
        "import_ref": "import-ref",
    }
    assert calls == [("project-a", 7, 11, 13, True)]


def test_service_refuses_without_run_capability_before_reading_the_project() -> None:
    opened: list[str] = []

    class Workspace:
        def get(self, project_id: str):
            opened.append(project_id)
            raise AssertionError("the project must not be opened")

    with pytest.raises(RouteError) as refused:
        DocumentPriorityService(Workspace()).promote(
            "project-a", sheet_id=1, row_id=2, column_id=3, may_run_actions=False
        )
    assert refused.value.status_code == 403
    assert opened == []
