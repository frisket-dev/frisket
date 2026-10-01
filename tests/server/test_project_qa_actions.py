from __future__ import annotations

import pytest

from frisket.actions.system import root_action_catalog_payload
from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.server.services.project_qa_actions import (
    ProjectAskActionService,
    save_prepared_project_ask_action,
)


def _turn(project: Project) -> tuple[ProjectQAStore, dict[str, object], int]:
    sheet_id = project.add_sheet("People")
    project.add_column(sheet_id, "Name")
    store = ProjectQAStore(project)
    thread = store.create_thread(title="Ask")
    return (
        store,
        store.submit_turn(thread["id"], request_id="one", question="Prepare"),
        sheet_id,
    )


def _draft(sheet_id: int) -> dict[str, object]:
    return {
        "action_id": "map.template",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"template": {"text": "Hello {{Name}}"}},
        "output_names": {},
    }


def test_prepared_ask_event_revalidates_then_uses_budget_and_action_services(tmp_path):
    project = Project.create(tmp_path / "project.frisket")
    try:
        store, turn, sheet_id = _turn(project)
        catalog = root_action_catalog_payload()
        saved = save_prepared_project_ask_action(
            project,
            turn,
            store,
            title="Greet people",
            draft=_draft(sheet_id),
            catalog_payload=catalog,
        )
        assert saved.event["payload"]["proposal"] == saved.proposal
        assert saved.event["payload"]["prepared_action"]["payload_identity"] == (
            saved.payload_identity
        )

        admitted: list[dict[str, object]] = []
        launched: list[tuple[str, dict[str, object]]] = []
        service = ProjectAskActionService(
            catalog_payload_provider=lambda: catalog,
            admit_operation=lambda research_id, **kwargs: admitted.append(
                {"research_id": research_id, **kwargs}
            ),
            run_action=lambda project_id, body: (
                launched.append((project_id, body)) or {"status": "queued"}
            ),
        )
        assert service.launch(
            project,
            store,
            saved.reference,
            project_id="project",
            research_id="research-1",
            estimate_micros=17,
            confirmation_hash="confirmation-hash",
        ) == {"status": "queued"}
        assert admitted == [
            {
                "research_id": "research-1",
                "operation_id": saved.reference.dispatch_id,
                "payload_identity": saved.payload_identity,
                "operation_kind": "action",
                "estimate_micros": 17,
                "metadata": {
                    "prepared_event_seq": saved.reference.event_seq,
                    "prepared_turn_id": saved.reference.turn_id,
                    "action_id": "map.template",
                },
            }
        ]
        assert launched == [
            (
                "project",
                {
                    **saved.prepared.request.model_dump(mode="json"),
                    "idempotency_key": saved.reference.dispatch_id,
                    "confirmation": "confirmation-hash",
                },
            )
        ]
        assert launched[0][1]["replace_existing"] is False
    finally:
        project.close()


def test_prepared_ask_action_refuses_catalog_drift_before_budget_admission(tmp_path):
    project = Project.create(tmp_path / "project.frisket")
    try:
        store, turn, sheet_id = _turn(project)
        saved = save_prepared_project_ask_action(
            project,
            turn,
            store,
            title="Greet people",
            draft=_draft(sheet_id),
            catalog_payload=root_action_catalog_payload(),
        )
        service = ProjectAskActionService(
            catalog_payload_provider=lambda: {"actions": []},
            admit_operation=lambda *_args, **_kwargs: pytest.fail("must not admit"),
            run_action=lambda *_args, **_kwargs: pytest.fail("must not run"),
        )
        with pytest.raises(ValueError, match="not available"):
            service.launch(
                project,
                store,
                saved.reference,
                project_id="project",
                research_id="research-1",
                estimate_micros=None,
            )
    finally:
        project.close()
