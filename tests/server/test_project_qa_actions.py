from __future__ import annotations

from dataclasses import dataclass

import pytest

from frisket.actions.system import root_action_catalog_payload
from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.server.services.project_qa_actions import (
    ProjectAskActionService,
    save_prepared_project_ask_action,
)


@dataclass(frozen=True)
class _OutputGrant:
    receipt_id: str
    sheet_id: int
    column_ids: frozenset[int] | None
    row_ids: frozenset[int] | None


def _allows_output_cell(
    grants: tuple[_OutputGrant, ...], *, sheet_id: int, row_id: int, column_id: int
) -> bool:
    return any(
        grant.sheet_id == sheet_id
        and (grant.row_ids is None or row_id in grant.row_ids)
        and (grant.column_ids is None or column_id in grant.column_ids)
        for grant in grants
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


def _quote(action: dict[str, object]) -> dict[str, object]:
    assert action["idempotency_key"].startswith("ask-action:")
    return {
        "estimate": {
            "billed_cost": 17,
            "policy_id": "normal-policy",
            "promise_set_hash": "confirmation-hash",
            "requires_confirmation": True,
        }
    }


def _approval(authority: dict[str, object]) -> dict[str, object]:
    return {
        "payload_identity": authority["payload_identity"],
        "promise_set_hash": authority["quote"]["estimate"]["promise_set_hash"],
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
            quote_provider=_quote,
        )
        assert saved.event["payload"]["proposal"] == saved.proposal
        assert saved.event["payload"]["prepared_action"]["payload_identity"] == (
            saved.payload_identity
        )
        stored = saved.event["payload"]["prepared_action"]
        assert stored["effects"] == {
            "kind": "typed_map_rows",
            "output_names": {"rendered": "rendered"},
            "output_target_preconditions": {"rendered": None},
            "replace_existing": False,
        }
        assert stored["quote"]["estimate"]["billed_cost"] == 17

        admitted: list[dict[str, object]] = []
        launched: list[tuple[str, dict[str, object]]] = []
        service = ProjectAskActionService(
            catalog_payload_provider=lambda: catalog,
            quote_provider=_quote,
            research_for_turn=lambda turn_id: {"id": "research-1", "turn_id": turn_id},
            authorize_dispatch=_approval,
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
                    "expected_output_columns": {"rendered": None},
                    "confirmation": "confirmation-hash",
                },
            )
        ]
        assert launched[0][1]["replace_existing"] is False
    finally:
        project.close()


def test_ask_action_revalidates_a_file_derived_column_receipt_at_launch(tmp_path):
    project = Project.create(tmp_path / "project.frisket")
    try:
        sheet_id = project.add_sheet("Files")
        file_column = project.add_column(sheet_id, "File")
        markdown_column = project.add_column(sheet_id, "Markdown")
        [row_id] = project.add_rows(
            sheet_id,
            [{"File": "report.pdf", "Markdown": "derived"}],
            {"File": file_column, "Markdown": markdown_column},
        )
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Ask")
        turn = store.submit_turn(
            thread["id"],
            request_id="derived-column",
            question="Use the converted document",
            scope={
                "kind": "sources",
                "sources": [
                    {
                        "kind": "file",
                        "sheet_id": sheet_id,
                        "row_id": row_id,
                        "column_id": file_column,
                    }
                ],
            },
        )
        grant = _OutputGrant(
            receipt_id="receipt-markdown",
            sheet_id=sheet_id,
            column_ids=frozenset({markdown_column}),
            row_ids=frozenset({row_id}),
        )
        saved = save_prepared_project_ask_action(
            project,
            turn,
            store,
            title="Summarize derived text",
            draft={
                "action_id": "map.template",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"template": {"text": "{{Markdown}}"}},
                "output_names": {},
            },
            catalog_payload=root_action_catalog_payload(),
            quote_provider=_quote,
            output_grants=(grant,),
            output_grant_allows=_allows_output_cell,
        )
        assert saved.event["payload"]["prepared_action"]["owned_output_inputs"] == [
            {
                "receipt_id": "receipt-markdown",
                "sheet_id": sheet_id,
                "row_id": row_id,
                "column_id": markdown_column,
            }
        ]

        current_grants: tuple[_OutputGrant, ...] = (grant,)
        admitted: list[dict[str, object]] = []
        service = ProjectAskActionService(
            catalog_payload_provider=root_action_catalog_payload,
            quote_provider=_quote,
            research_for_turn=lambda turn_id: {"id": "research-1", "turn_id": turn_id},
            authorize_dispatch=_approval,
            admit_operation=lambda research_id, **kwargs: admitted.append(
                {"research_id": research_id, **kwargs}
            ),
            run_action=lambda _project_id, _body: {"status": "queued"},
            output_grants_provider=lambda: current_grants,
            output_grant_allows=_allows_output_cell,
        )
        assert service.launch(
            project, store, saved.reference, project_id="project"
        ) == {"status": "queued"}
        assert len(admitted) == 1

        current_grants = ()
        with pytest.raises(ValueError, match="outside the Ask source scope"):
            service.launch(project, store, saved.reference, project_id="project")
        assert len(admitted) == 1
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
            quote_provider=_quote,
        )
        service = ProjectAskActionService(
            catalog_payload_provider=lambda: {"actions": []},
            quote_provider=_quote,
            research_for_turn=lambda turn_id: {"id": "research-1", "turn_id": turn_id},
            authorize_dispatch=lambda _authority: pytest.fail("must not authorize"),
            admit_operation=lambda *_args, **_kwargs: pytest.fail("must not admit"),
            run_action=lambda *_args, **_kwargs: pytest.fail("must not run"),
        )
        with pytest.raises(ValueError, match="not available"):
            service.launch(
                project,
                store,
                saved.reference,
                project_id="project",
            )
    finally:
        project.close()


def test_launch_uses_the_saved_normal_quote_not_a_caller_price(tmp_path):
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
            quote_provider=_quote,
        )
        admitted: list[dict[str, object]] = []
        service = ProjectAskActionService(
            catalog_payload_provider=root_action_catalog_payload,
            quote_provider=_quote,
            research_for_turn=lambda turn_id: {"id": "research-1", "turn_id": turn_id},
            authorize_dispatch=_approval,
            admit_operation=lambda research_id, **kwargs: admitted.append(
                {"research_id": research_id, **kwargs}
            ),
            run_action=lambda *_args: {"status": "queued"},
        )

        service.launch(
            project,
            store,
            saved.reference,
            project_id="project",
            confirmation_hash="confirmation-hash",
        )
        assert admitted[0]["estimate_micros"] == 17
        with pytest.raises(TypeError):
            service.launch(
                project,
                store,
                saved.reference,
                project_id="project",
                confirmation_hash="confirmation-hash",
                estimate_micros=0,
            )
    finally:
        project.close()


def test_launch_requires_its_research_turn_before_repreparing(tmp_path):
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
            quote_provider=_quote,
        )
        service = ProjectAskActionService(
            catalog_payload_provider=lambda: pytest.fail("must not read catalog"),
            quote_provider=lambda _action: pytest.fail("must not quote"),
            research_for_turn=lambda turn_id: {"id": "other", "turn_id": "other-turn"},
            authorize_dispatch=lambda _authority: pytest.fail("must not authorize"),
            admit_operation=lambda *_args, **_kwargs: pytest.fail("must not admit"),
            run_action=lambda *_args: pytest.fail("must not run"),
        )
        with pytest.raises(ValueError, match="research turn"):
            service.launch(project, store, saved.reference, project_id="project")
    finally:
        project.close()


def test_unquoted_proposals_remain_visible_but_cannot_launch(tmp_path):
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
        assert saved.event["payload"]["proposal"] == saved.proposal
        service = ProjectAskActionService(
            catalog_payload_provider=root_action_catalog_payload,
            quote_provider=_quote,
            research_for_turn=lambda turn_id: {"id": "research-1", "turn_id": turn_id},
            authorize_dispatch=lambda _authority: pytest.fail("must not authorize"),
            admit_operation=lambda *_args, **_kwargs: pytest.fail("must not admit"),
            run_action=lambda *_args: pytest.fail("must not run"),
        )
        with pytest.raises(ValueError, match="changed; reprepare"):
            service.launch(project, store, saved.reference, project_id="project")
    finally:
        project.close()


def test_current_exact_approval_can_dispatch_a_non_map_action(tmp_path):
    project = Project.create(tmp_path / "project.frisket")
    try:
        store, turn, sheet_id = _turn(project)
        saved = save_prepared_project_ask_action(
            project,
            turn,
            store,
            title="Match people",
            draft={
                "action_id": "join.semantic",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {
                    "source": "Name",
                    "target": {"sheet_id": sheet_id, "column": "Name"},
                },
                "output_names": {},
                "sheet_name": "Matches",
            },
            catalog_payload=root_action_catalog_payload(),
            quote_provider=_quote,
        )
        approved: list[dict[str, object]] = []
        service = ProjectAskActionService(
            catalog_payload_provider=root_action_catalog_payload,
            quote_provider=_quote,
            research_for_turn=lambda turn_id: {"id": "research-1", "turn_id": turn_id},
            authorize_dispatch=lambda authority: (
                approved.append(dict(authority)) or _approval(authority)
            ),
            admit_operation=lambda *_args, **_kwargs: None,
            run_action=lambda _project_id, body: {"action_id": body["action_id"]},
        )
        assert service.launch(
            project,
            store,
            saved.reference,
            project_id="project",
            confirmation_hash="confirmation-hash",
        ) == {"action_id": "join.semantic"}
        assert approved[0]["automatic_eligible"] is False
        assert (
            approved[0]["catalog_effects"]
            == saved.event["payload"]["prepared_action"]["catalog_effects"]
        )
    finally:
        project.close()
