from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.contracts.http.document_priority import DocumentPriorityResponse
from frisket.engine.jobs.queue import PROJECT_RUN_KIND, SqliteJobQueue
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.server.route_errors import RouteError, register_route_error_handler
from frisket.server.routes.document_priority import register_document_priority_route
from frisket.server.services.document_priority import DocumentPriorityService
from frisket.server.services.selector_choices_capabilities import SelectorCapabilities


class _PromotingQueue:
    def __init__(self, backing: SqliteJobQueue) -> None:
        self.engine = backing.engine
        self.calls: list[tuple[int, str, int | None, int]] = []

    def promote_project_run(
        self,
        job_id: int,
        *,
        project_id: str,
        storage_org_id: int | None,
        preferred_row_id: int,
    ) -> bool:
        self.calls.append((job_id, project_id, storage_org_id, preferred_row_id))
        return True


def test_service_promotes_only_the_marked_run_that_admits_the_exact_target(
    tmp_path,
) -> None:
    project_id = "project-a"
    project = Project.create(tmp_path / "project.frisket")
    backing = SqliteJobQueue(tmp_path / "queue.db")
    queue = _PromotingQueue(backing)
    try:
        sheet_id = project.add_sheet("PDFs")
        column_id = project.add_column(sheet_id, "PDF", type="image")
        row_ids = project.add_rows(sheet_id, [{}, {}], {})
        op_id = project.append_op("media.ocr", {})
        run_id = RunResultStore(project).start_run(
            op_id,
            sheet_id,
            "media.ocr",
            row_ids=row_ids,
        )
        payload: dict[str, Any] = {
            "project_id": project_id,
            "run_id": run_id,
            "action_kind": "media.ocr",
            "spec": {"sheet_id": sheet_id},
            "v1_input_column_ids": {"media": column_id},
            "background_priority": "pdf_import_ocr",
        }
        correct_job = backing.enqueue(PROJECT_RUN_KIND, payload)
        # A newer OCR run without the server-owned import marker must not be
        # promoted merely because its public target ids happen to match.
        backing.enqueue(
            PROJECT_RUN_KIND,
            {k: v for k, v in payload.items() if k != "background_priority"},
        )
        workspace = SimpleNamespace(
            queue=queue,
            queue_storage_org_id=None,
            get=lambda requested: (
                project
                if requested == project_id
                else (_ for _ in ()).throw(KeyError(requested))
            ),
        )
        service = DocumentPriorityService(workspace)

        promoted = service.promote(
            project_id,
            sheet_id=sheet_id,
            row_id=row_ids[1],
            column_id=column_id,
            may_run_actions=True,
        )
        assert promoted.promoted
        assert promoted.run_id == run_id
        assert queue.calls == [(correct_job, project_id, None, row_ids[1])]

        missing = service.promote(
            project_id,
            sheet_id=sheet_id,
            row_id=row_ids[1],
            column_id=column_id + 1,
            may_run_actions=True,
        )
        assert not missing.promoted
        assert missing.run_id is None
        assert len(queue.calls) == 1

        try:
            service.promote(
                project_id,
                sheet_id=sheet_id,
                row_id=row_ids[0],
                column_id=column_id,
                may_run_actions=False,
            )
        except RouteError as exc:
            assert exc.status_code == 403
        else:  # pragma: no cover - assertion spelling preserves error detail
            raise AssertionError("missing run permission must fail closed")
    finally:
        project.close()
        backing.close()


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
            return DocumentPriorityResponse(promoted=True, run_id=17)

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
        "run_id": 17,
    }
    assert calls == [("project-a", 7, 11, 13, True)]
