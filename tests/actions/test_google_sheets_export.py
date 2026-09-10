from __future__ import annotations

import asyncio
from dataclasses import replace
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    google_sheets_export,
)
from frisket.actions.google_sheets_types import GoogleSheetsExportRequest
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionParams, ActionRequest
from frisket.engine.executor import ExecutorDeps, resolve_map_preview
from frisket.engine.executor.action_jobs import ActionJobEnvelope, run_action_run_job
from frisket.engine.executor.action_specs import execution_registry
from frisket.engine.executor.google_sheets_action import (
    prepare_google_sheets_action_job,
    run_google_sheets_action_job,
    run_typed_google_sheets_export,
)
from frisket.engine.store import Project
from frisket.engine.store.effect_checkpoints import EffectCheckpointStore
from frisket.engine.store.receipts import ReceiptStore


class RenamedParams(ActionParams):
    account: str
    table: int
    title: str


@pytest.fixture
def export_case(tmp_path, monkeypatch):
    project = Project.create(tmp_path / "export.frisket")
    sheet = project.add_sheet("Rows")
    col = project.add_column(sheet, "name", "text")
    project.add_rows(sheet, [{"name": "Ada"}], {"name": col})
    calls, prepared = [], []

    class Client:
        def export_tabs(self, **kwargs):
            calls.append(kwargs)
            return {"spreadsheet_id": "made", "updated_tabs": []}

    def prepare(params: RenamedParams) -> GoogleSheetsExportRequest:
        prepared.append(True)
        return GoogleSheetsExportRequest(
            connection_id=params.account + "-derived",
            source={"kind": "current_sheet", "sheet_id": params.table},
            destination={
                "kind": "google_sheets",
                "mode": "new_spreadsheet",
                "spreadsheet_title": params.title.upper(),
            },
        )

    registered = ActionRegistry(
        (
            ActionNamespace(
                "example",
                actions=(
                    action(
                        name="sheets",
                        title="Sheets",
                        description="Export derived intent",
                        category=ActionCategory.CONVERT,
                        run=google_sheets_export(prepare),
                    ),
                ),
            ),
        )
    ).get("example.sheets")
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        MappingProxyType(
            {
                **ACTION_REGISTRY._actions,
                registered.action_id: registered,
            }
        ),
    )
    execution_registry.cache_clear()
    request = ActionRequest(
        action_id=registered.action_id,
        scope={"kind": "project"},
        params={"account": "chosen", "table": sheet, "title": "Export"},
        idempotency_key="custom-export",
    )
    bound = BoundTypedActionRequest.bind(registered, request)
    deps = ExecutorDeps(
        google_sheets_client=Client(),
        connected_account_resolver=lambda provider, key: (
            {
                "id": key,
                "provider": provider,
                "external_subject": "owner",
                "refresh_token": "SECRET",
            }
            if key == "chosen-derived"
            else None
        ),
    )
    try:
        yield project, bound, deps, prepared, calls
    finally:
        project.close()
        execution_registry.cache_clear()


def _confirmed(project, bound, deps):
    challenge = prepare_google_sheets_action_job(project, "project", bound, deps=deps)
    assert challenge.status == "needs_confirmation"
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    return BoundTypedActionRequest.bind(
        bound.action,
        bound.request.model_copy(
            update={
                "confirmation": challenge.errors[0].details["promise_set_hash"],
            }
        ),
    )


def test_renamed_derived_params_are_admitted_once_and_pinned_for_worker(export_case):
    project, bound, deps, prepared, calls = export_case
    bound = _confirmed(project, bound, deps)
    prepared.clear()
    envelope = prepare_google_sheets_action_job(project, "project", bound, deps=deps)
    assert isinstance(envelope, ActionJobEnvelope)
    assert prepared == [True] and calls == []
    pinned = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
    assert pinned.inputs[-1].ref == envelope.resolved_snapshot
    result = run_action_run_job(
        project,
        {"action_job": envelope.to_json()},
        executor_lookup=lambda _kind: (
            lambda project_, envelope_: run_google_sheets_action_job(
                project_, envelope_, deps=deps
            )
        ),
    )
    assert result.status == "completed"
    assert prepared == [True]
    assert calls[0]["connection"]["id"] == "chosen-derived"
    assert calls[0]["destination"]["spreadsheet_title"] == "EXPORT"
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt.inputs[0].ref["source"]["sheet_id"] == bound.params.table
    assert receipt.provider_use[0]["connection_id"] == "chosen-derived"
    assert "SECRET" not in receipt.model_dump_json()
    replay = run_typed_google_sheets_export(
        project, "project", bound, deps=ExecutorDeps()
    )
    assert replay.receipt_id == result.receipt_id
    assert len(calls) == len(prepared) == 1


@pytest.mark.parametrize("tamper", ["intent", "confirmation", "request", "receipt"])
def test_worker_refuses_tampered_queue_authority_before_provider(export_case, tamper):
    project, bound, deps, prepared, calls = export_case
    bound = _confirmed(project, bound, deps)
    envelope = prepare_google_sheets_action_job(project, "project", bound, deps=deps)
    if tamper == "intent":
        snapshot = dict(envelope.resolved_snapshot)
        snapshot["intent"] = {**snapshot["intent"], "connection_id": "other"}
        envelope = replace(envelope, resolved_snapshot=snapshot)
    elif tamper == "confirmation":
        envelope = replace(
            envelope, action={**envelope.action, "confirmation": "wrong"}
        )
    elif tamper == "request":
        envelope = replace(
            envelope,
            action={
                **envelope.action,
                "params": {**envelope.action["params"], "title": "Other"},
            },
        )
    else:
        envelope = replace(envelope, receipt_id="unrelated")
    result = run_google_sheets_action_job(project, envelope, deps=deps)
    assert result.status == "failed"
    assert result.errors[0].code == (
        "irreversible_external_requires_confirmation"
        if tamper == "confirmation"
        else "invalid_action_request"
    )
    assert calls == []
    assert EffectCheckpointStore(project.db).list_all() == []


def test_preview_refuses_before_producer_or_provider(export_case):
    project, bound, deps, prepared, calls = export_case
    result = resolve_map_preview(
        project, bound.request.model_dump(mode="json"), deps=deps
    )
    assert result.code == "unsupported_action_kind"
    assert prepared == calls == []


def test_terminal_catalog_preserves_async_export_metadata(export_case):
    project, bound, deps, prepared, calls = export_case
    catalog = bound.action.catalog_entry()
    assert catalog["async_mode"] == "async"
    assert catalog["execution_mode"] == "whole_project"
    assert catalog["row_scope_policy"] == {"kind": "project", "selectors": []}
    assert set(catalog["required_capabilities"]) == {
        "project:read",
        "project:write",
        "external:google_sheets",
    }
    assert prepared == calls == []


@pytest.mark.parametrize("field", ["spreadsheet_id", "spreadsheet_title"])
def test_blank_destination_values_refuse(field):
    with pytest.raises(ValidationError):
        GoogleSheetsExportRequest(
            connection_id="account",
            source={"kind": "all_sheets"},
            destination={
                "kind": "google_sheets",
                "mode": "update_existing",
                "spreadsheet_id": "existing",
                field: "   ",
            },
        )


@pytest.mark.parametrize("failure", [ValueError, asyncio.CancelledError])
def test_producer_failure_cannot_leave_an_export_effect(
    export_case, monkeypatch, failure
):
    project, bound, deps, prepared, calls = export_case

    def broken(params: RenamedParams) -> GoogleSheetsExportRequest:
        raise failure("SECRET author detail")

    definition = replace(bound.action.definition, run=google_sheets_export(broken))
    registered = replace(bound.action, definition=definition)
    bound = BoundTypedActionRequest.bind(registered, bound.request)
    if failure is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            run_typed_google_sheets_export(project, "project", bound, deps=deps)
    else:
        result = run_typed_google_sheets_export(project, "project", bound, deps=deps)
        assert result.status == "failed"
        assert "SECRET" not in result.model_dump_json()
    assert calls == []
    assert not project.db.in_transaction
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_changed_actual_intent_requires_fresh_consent(export_case):
    project, bound, deps, prepared, calls = export_case
    bound = _confirmed(project, bound, deps)
    original = bound.action.definition.run.handler

    def changed(params: RenamedParams) -> GoogleSheetsExportRequest:
        intent = original(params)
        intent.destination.spreadsheet_title = "Changed by producer"
        return intent

    registered = replace(
        bound.action,
        definition=replace(bound.action.definition, run=google_sheets_export(changed)),
    )
    changed_bound = BoundTypedActionRequest.bind(registered, bound.request)
    result = prepare_google_sheets_action_job(
        project, "project", changed_bound, deps=deps
    )
    assert result.status == "needs_confirmation"
    assert result.errors[0].details["promise_set_hash"] != bound.request.confirmation
    assert calls == []
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "failure", [KeyboardInterrupt, SystemExit, asyncio.CancelledError]
)
def test_interrupted_intent_reservation_rolls_back_before_reraise(
    export_case, monkeypatch, failure
):
    project, bound, deps, prepared, calls = export_case
    bound = _confirmed(project, bound, deps)
    insert = ReceiptStore.insert_queued

    def interrupted(store, receipt, *, commit=True):
        insert(store, receipt, commit=commit)
        raise failure("interrupted after receipt write")

    monkeypatch.setattr(ReceiptStore, "insert_queued", interrupted)
    with pytest.raises(failure):
        prepare_google_sheets_action_job(project, "project", bound, deps=deps)
    assert not project.db.in_transaction
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    assert calls == []
    assert EffectCheckpointStore(project.db).list_all() == []
