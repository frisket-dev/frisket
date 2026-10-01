from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from frisket.actions.system import root_action_catalog_payload
from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.server.services.action_runs import ActionRunResponse
from frisket.server.services.project_qa_child_runs import (
    ChildRunDispatch,
    ChildRunOutcome,
)
from frisket.server.services.project_qa_execution import (
    ProjectQAExecutionRefused,
    ProjectQAExecutionService,
    ResearchActionSkipped,
)


def _quote(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "estimate": {
            "billed_cost": 17,
            "policy_id": "normal-policy",
            "promise_set_hash": f"promise:{action['idempotency_key']}",
            "requires_confirmation": True,
        }
    }


class _QAStore:
    def __init__(self, base: ProjectQAStore) -> None:
        self.base = base
        self.research_children: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base, name)

    def append_event(
        self, turn_id: str, *, kind: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if kind == "research_child":
            event = {"turn_id": turn_id, "kind": kind, "payload": dict(payload)}
            self.research_children.append(event)
            return event
        return self.base.append_event(turn_id, kind=kind, payload=payload)


class _BudgetPause(Exception):
    def __init__(self, approval: dict[str, Any]) -> None:
        self.approval = approval
        super().__init__(approval["kind"])


class _ResearchStore:
    def __init__(self, turn_id: str, *, write_mode: str) -> None:
        self.run = {
            "id": "research-1",
            "turn_id": turn_id,
            "write_mode": write_mode,
            "output_grants": [],
            "revision": 1,
        }
        self.admissions: list[dict[str, Any]] = []
        self.settlements: list[dict[str, Any]] = []
        self.block_next_admission = False

    def get(self, research_id: str) -> dict[str, Any]:
        assert research_id == self.run["id"]
        return dict(self.run)

    def admit_operation(self, research_id: str, **facts: Any) -> dict[str, Any]:
        assert research_id == self.run["id"]
        if self.block_next_admission:
            raise RuntimeError("budget admission blocked")
        record = {"research_id": research_id, **facts}
        if record not in self.admissions:
            self.admissions.append(record)
        return record

    def settle_operation(self, research_id: str, **facts: Any) -> dict[str, Any]:
        record = {"research_id": research_id, **facts}
        if record not in self.settlements:
            self.settlements.append(record)
        return record

    def update(
        self, research_id: str, *, expected_revision: int, **changes: Any
    ) -> dict[str, Any]:
        assert research_id == self.run["id"]
        assert expected_revision == self.run["revision"]
        self.run.update(changes)
        self.run["revision"] += 1
        return dict(self.run)


class _Session:
    id = "research-1"

    def __init__(
        self, store: _ResearchStore, decisions: list[str] | None = None
    ) -> None:
        self.store = store
        self.context: dict[str, Any] = {"messages": ["saved-message"]}
        self.decisions = list(decisions or [])
        self.authority_checks = 0
        self.pauses: list[dict[str, Any]] = []
        self.checkpoints: list[tuple[list[Any], dict[str, Any]]] = []

    async def check_authority(self) -> None:
        self.authority_checks += 1

    async def pause(self, approval: dict[str, Any]) -> str:
        self.pauses.append(dict(approval))
        return self.decisions.pop(0)

    async def admit(
        self,
        operation_id: str,
        identity: str,
        kind: str,
        estimate: int | None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.store.block_next_admission:
            self.store.block_next_admission = False
            raise _BudgetPause(
                {"kind": "budget", "operation_id": operation_id, "estimate": estimate}
            )
        return self.store.admit_operation(
            self.id,
            operation_id=operation_id,
            payload_identity=identity,
            operation_kind=kind,
            estimate_micros=estimate,
            metadata=metadata,
        )

    async def checkpoint(self, messages: list[Any], **changes: Any) -> dict[str, Any]:
        self.checkpoints.append((list(messages), dict(changes)))
        self.store.run.update(changes)
        return dict(self.store.run)


def _project_state(tmp_path, *, write_mode: str = "ask_overwrite"):
    project = Project.create(tmp_path / "project.frisket")
    sheet_id = project.add_sheet("People")
    project.add_column(sheet_id, "Name")
    base = ProjectQAStore(project)
    thread = base.create_thread(title="Research")
    turn = base.submit_turn(thread["id"], request_id="one", question="Research")
    store = _QAStore(base)
    research_store = _ResearchStore(str(turn["id"]), write_mode=write_mode)
    session = _Session(research_store)
    return project, sheet_id, turn, store, session


def _draft(sheet_id: int) -> dict[str, Any]:
    return {
        "action_id": "map.template",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"template": {"text": "Hello {{Name}}"}},
        "output_names": {},
    }


def _response(dispatch_id: str) -> ActionRunResponse:
    return ActionRunResponse(
        status_code=200,
        payload={
            "schema_version": "frisket.action_result.v1",
            "action": {"kind": "map.template", "action_id": "action-1"},
            "status": "queued",
            "project_id": "project-1",
            "run_id": 7,
            "job_id": 9,
            "op_ids": [31],
            "outputs": [],
            "receipt_id": None,
            "warnings": [],
            "errors": [],
            "idempotency_key": dispatch_id,
        },
    )


def _outcome(dispatch_id: str, record_dispatch) -> ChildRunOutcome:
    base_dispatch = ChildRunDispatch(
        project_id="project-1",
        action_kind="map.template",
        action_id="action-1",
        idempotency_key=None,
        job_id=9,
        run_id=7,
        receipt_id=None,
        op_ids=(31,),
    )
    record_dispatch(base_dispatch)
    dispatch = replace(base_dispatch, idempotency_key=dispatch_id)
    record_dispatch(dispatch)
    return ChildRunOutcome(
        dispatch=dispatch,
        status="completed",
        outputs=({"kind": "column", "ref": {"column_id": 4}},),
        errors=(),
        warnings=(),
        provider_cost_usd=0.25,
        billed_cost_micros=23,
        public_job_status={"status": "done", "receipt_id": "receipt-1"},
    )


@pytest.mark.asyncio
async def test_safe_additive_action_runs_once_and_checkpoints_receipt_grant(tmp_path):
    project, sheet_id, turn, store, session = _project_state(tmp_path)
    launched: list[dict[str, Any]] = []
    grant_reads: list[None] = []

    async def wait_child(**kwargs: Any) -> ChildRunOutcome:
        outcome = _outcome(
            kwargs["response"].payload["idempotency_key"], kwargs["record_dispatch"]
        )
        assert store.research_children
        return outcome

    service = ProjectQAExecutionService(
        project,
        "project-1",
        turn,
        store,
        session,
        catalog_payload_provider=root_action_catalog_payload,
        quote_provider=_quote,
        run_action=lambda _project_id, body: (
            launched.append(dict(body)) or _response(body["idempotency_key"])
        ),
        wait_child=wait_child,
        output_grants_provider=lambda: (
            grant_reads.append(None) or SimpleNamespace(grants=())
        ),
        output_grant_allows=lambda *_args, **_kwargs: False,
    )
    try:
        prepared = await service.prepare_action("Greet", _draft(sheet_id))
        event_ref = prepared["event_ref"]
        outcome = await service.execute_action(event_ref)

        assert outcome == {
            "status": "completed",
            "receipt_id": "receipt-1",
            "outputs": [{"kind": "column", "ref": {"column_id": 4}}],
            "errors": [],
            "warnings": [],
        }
        assert len(launched) == 1
        assert launched[0]["idempotency_key"] == event_ref["dispatch_id"]
        assert launched[0]["confirmation"] == (f"promise:{event_ref['dispatch_id']}")
        assert session.pauses == []
        assert grant_reads == [None, None]
        assert session.store.admissions[0]["operation_id"] == event_ref["dispatch_id"]
        assert session.store.settlements == [
            {
                "research_id": "research-1",
                "operation_id": event_ref["dispatch_id"],
                "payload_identity": session.store.admissions[0]["payload_identity"],
                "actual_micros": 23,
            }
        ]
        assert len(store.research_children) == 2
        assert store.research_children[0]["payload"]["job_id"] == 9
        assert (
            store.research_children[-1]["payload"]["dispatch_id"]
            == (event_ref["dispatch_id"])
        )
        assert session.store.run["output_grants"] == ["receipt-1"]
        usage = [
            event
            for event in store.events(str(turn["thread_id"]))["events"]
            if event["kind"] == "usage"
        ]
        assert usage[-1]["payload"] == {
            "operation": "action",
            "operation_id": event_ref["dispatch_id"],
            "cost": 0.25,
            "receipt_id": "receipt-1",
        }
    finally:
        project.close()


@pytest.mark.asyncio
async def test_action_approval_survives_budget_pause_without_budget_granting_it(
    tmp_path,
):
    project, sheet_id, turn, store, session = _project_state(
        tmp_path, write_mode="ask_each"
    )
    session.decisions[:] = ["approve", "continue"]
    session.store.block_next_admission = True
    launched: list[str] = []

    async def wait_child(**kwargs: Any) -> ChildRunOutcome:
        return _outcome(
            kwargs["response"].payload["idempotency_key"], kwargs["record_dispatch"]
        )

    service = ProjectQAExecutionService(
        project,
        "project-1",
        turn,
        store,
        session,
        catalog_payload_provider=root_action_catalog_payload,
        quote_provider=_quote,
        run_action=lambda _project_id, body: (
            launched.append(body["idempotency_key"])
            or _response(body["idempotency_key"])
        ),
        wait_child=wait_child,
    )
    try:
        prepared = await service.prepare_action("Greet", _draft(sheet_id))
        await service.execute_action(prepared["event_ref"])

        assert [pause["kind"] for pause in session.pauses] == ["action", "budget"]
        assert (
            session.pauses[0]["payload_identity"]
            == (session.store.admissions[0]["payload_identity"])
        )
        assert len(launched) == 1
        assert launched[0] == prepared["event_ref"]["dispatch_id"]
    finally:
        project.close()


@pytest.mark.asyncio
async def test_replay_keeps_one_budget_operation_and_one_usage_event(tmp_path):
    project, sheet_id, turn, store, session = _project_state(tmp_path)
    launched: list[str] = []

    async def wait_child(**kwargs: Any) -> ChildRunOutcome:
        return _outcome(
            kwargs["response"].payload["idempotency_key"], kwargs["record_dispatch"]
        )

    service = ProjectQAExecutionService(
        project,
        "project-1",
        turn,
        store,
        session,
        catalog_payload_provider=root_action_catalog_payload,
        quote_provider=_quote,
        run_action=lambda _project_id, body: (
            launched.append(body["idempotency_key"])
            or _response(body["idempotency_key"])
        ),
        wait_child=wait_child,
    )
    try:
        prepared = await service.prepare_action("Greet", _draft(sheet_id))
        await service.execute_action(prepared["event_ref"])
        await service.execute_action(prepared["event_ref"])

        assert launched == [
            prepared["event_ref"]["dispatch_id"],
            prepared["event_ref"]["dispatch_id"],
        ]
        assert len(session.store.admissions) == 1
        assert len(session.store.settlements) == 1
        usage = [
            event
            for event in store.events(str(turn["thread_id"]))["events"]
            if event["kind"] == "usage"
            and event["payload"].get("operation") == "action"
        ]
        assert len(usage) == 1
    finally:
        project.close()


@pytest.mark.asyncio
async def test_skip_does_not_admit_or_dispatch(tmp_path):
    project, sheet_id, turn, store, session = _project_state(
        tmp_path, write_mode="ask_each"
    )
    session.decisions[:] = ["skip"]
    service = ProjectQAExecutionService(
        project,
        "project-1",
        turn,
        store,
        session,
        catalog_payload_provider=root_action_catalog_payload,
        quote_provider=_quote,
        run_action=lambda *_args: pytest.fail("must not dispatch"),
        wait_child=lambda **_kwargs: pytest.fail("must not wait"),
    )
    try:
        prepared = await service.prepare_action("Greet", _draft(sheet_id))
        with pytest.raises(ResearchActionSkipped):
            await service.execute_action(prepared["event_ref"])
        assert session.store.admissions == []
    finally:
        project.close()


@pytest.mark.asyncio
async def test_external_action_needs_approval_even_in_full_access(tmp_path):
    project, sheet_id, turn, store, session = _project_state(
        tmp_path, write_mode="full_access"
    )
    session.decisions[:] = ["skip"]
    service = ProjectQAExecutionService(
        project,
        "project-1",
        turn,
        store,
        session,
        catalog_payload_provider=root_action_catalog_payload,
        quote_provider=_quote,
        run_action=lambda *_args: pytest.fail("must not dispatch"),
        wait_child=lambda **_kwargs: pytest.fail("must not wait"),
    )
    try:
        draft = {
            "action_id": "map.api_call",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {
                "request": {
                    "url": "https://example.test/{{Name}}",
                    "method": "GET",
                }
            },
            "output_names": {},
        }
        prepared = await service.prepare_action("Look up", draft)
        with pytest.raises(ResearchActionSkipped):
            await service.execute_action(prepared["event_ref"])
        assert session.pauses[0]["reason"] == "external_effect"
    finally:
        project.close()


@pytest.mark.asyncio
async def test_malformed_reference_and_forbidden_authority_are_refused(
    tmp_path,
):
    project, sheet_id, turn, store, session = _project_state(tmp_path)
    service = ProjectQAExecutionService(
        project,
        "project-1",
        turn,
        store,
        session,
        catalog_payload_provider=root_action_catalog_payload,
        quote_provider=_quote,
        run_action=lambda *_args: pytest.fail("must not dispatch"),
        wait_child=lambda **_kwargs: pytest.fail("must not wait"),
    )
    try:
        prepared = await service.prepare_action("Greet", _draft(sheet_id))
        wrong = {**prepared["event_ref"], "dispatch_id": "another-dispatch"}
        with pytest.raises(ValueError, match="does not match dispatch"):
            await service.execute_action(wrong)
        with pytest.raises(ValueError, match="malformed"):
            await service.execute_action({**prepared["event_ref"], "turn_id": "other"})

        with pytest.raises(ProjectQAExecutionRefused, match="unsafe"):
            service._authorize_for_policy(  # noqa: SLF001 - security boundary proof
                {
                    "payload_identity": "identity",
                    "request": {"action_id": "plugin.admin"},
                    "required_capabilities": ["unsafe:local_code"],
                    "catalog_effects": ["execute_trusted_local_python"],
                    "effects": {"replace_existing": False},
                    "quote": {"estimate": {"promise_set_hash": "promise"}},
                }
            )
    finally:
        project.close()
