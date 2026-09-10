"""Host-owned execution for typed source metadata actions."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Mapping

from frisket.actions.core import _ProjectAction
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    CheckedSource,
    DeletedSource,
    SourceChecker,
    SourceCreator,
    SourceDeleter,
    SourcePatch,
    SourceRecord,
    SourceUpdater,
    UpdatedSource,
)
from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionOutput,
    ActionResult,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.engine.executor.action_inventory import (
    ExecutorContext,
    ExecutorDeps,
    _ActionCoreSpec,
    _TypedProjectEnvelope,
)
from frisket.engine.executor.action_lifecycle import (
    _child_sheet_deterministic_result_from_existing,
    _run_action_core_spec,
)
from frisket.engine.executor.action_support import _failed_result
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.sources import SourceStore


def _text_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _config(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _record(row: Mapping[str, Any]) -> SourceRecord:
    return SourceRecord(
        source_id=int(row["id"]),
        name=str(row["name"]),
        kind=str(row["kind"]),
        url=row["url"],
        config=_config(row["config"]),
        sheet_id=row["sheet_id"],
        schedule=row["schedule"],
        enabled=bool(row["enabled"]),
        last_checked_at=row["last_checked_at"],
        last_status=row["last_status"],
        new_rows_total=int(row["new_rows_total"] or 0),
    )


class _CallOnce:
    def __init__(self, project: Any):
        self._project = project
        self._called = False
        self.result: (
            SourceRecord | UpdatedSource | DeletedSource | CheckedSource | None
        ) = None
        self.before: dict[str, Any] | None = None
        self.after: dict[str, Any] | None = None

    def _begin(self) -> SourceStore:
        if self._called:
            raise RuntimeError("source capability may be called only once")
        self._called = True
        return SourceStore(self._project)


class _Creator(_CallOnce):
    def create(self, **values: Any) -> SourceRecord:
        store = self._begin()
        source_id = store.add_source(commit=False, **values)
        row = store.get_source(source_id)
        if row is None:
            raise RuntimeError("created source is unavailable")
        self.after = dict(row)
        result = _record(row)
        self.result = result
        return result


class _Updater(_CallOnce):
    def update(self, source_id: int, *, patch: SourcePatch) -> UpdatedSource:
        store = self._begin()
        before = store.get_source(source_id)
        if before is None:
            raise LookupError(source_id)
        self.before = dict(before)
        values = patch.values()
        if values.get("config", object()) is None:
            values["config"] = {}
        store.update_source(source_id, commit=False, **values)
        row = store.get_source(source_id)
        if row is None:
            raise LookupError(source_id)
        self.after = dict(row)
        result = UpdatedSource(_record(row), tuple(sorted(values)))
        self.result = result
        return result


class _Deleter(_CallOnce):
    def delete(self, source_id: int) -> DeletedSource:
        store = self._begin()
        row = store.get_source(source_id)
        if row is None:
            raise LookupError(source_id)
        self.before = dict(row)
        result = DeletedSource(_record(row), store.source_runs_total(source_id))
        store.delete_source(source_id, commit=False)
        self.result = result
        return result


class _UnsupportedSourceKind(Exception):
    pass


class _Checker(_CallOnce):
    cursor: str | None = None

    def check(
        self,
        source_id: int,
        *,
        new_rows: int,
        status: Literal["ok", "error"],
        error: str | None,
        cursor: str | None,
    ) -> CheckedSource:
        store = self._begin()
        before = store.get_source(source_id)
        if before is None:
            raise LookupError(source_id)
        if before["kind"] == "rss":
            raise _UnsupportedSourceKind("rss")
        self.before = dict(before)
        self.cursor = cursor
        source_run_id = store.record_source_run(
            source_id,
            new_rows=new_rows,
            status=status,
            error=error,
            cursor=cursor,
            commit=False,
        )
        after = store.get_source(source_id)
        if after is None:
            raise LookupError(source_id)
        self.after = dict(after)
        result = CheckedSource(
            source=_record(after),
            source_run_id=source_run_id,
            status=status,
            new_rows=new_rows,
            error=error,
        )
        self.result = result
        return result


_CAPABILITY_IMPL = {
    SourceCreator: _Creator,
    SourceUpdater: _Updater,
    SourceDeleter: _Deleter,
    SourceChecker: _Checker,
}
_CAPABILITY_OPERATION = {
    SourceCreator: "create",
    SourceUpdater: "update",
    SourceDeleter: "delete",
    SourceChecker: "check",
}


def supports_typed_source_action(terminal: object) -> bool:
    return isinstance(terminal, _ProjectAction) and any(
        terminal.capabilities == (cap,) for cap in _CAPABILITY_IMPL
    )


def _source_ref(
    result: SourceRecord,
    *,
    action: str,
    params_hash: str,
    raw: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    raw_config = (raw or {}).get("config") or "{}"
    config_text = raw_config if isinstance(raw_config, str) else json.dumps(raw_config)
    ref = {
        "kind": f"source_{action}_source",
        "source_id": result.source_id,
        "name": result.name,
        "source_kind": result.kind,
        "sheet_id": result.sheet_id,
        "schedule": result.schedule,
        "enabled": result.enabled,
        "last_checked_at": result.last_checked_at,
        "last_status": result.last_status or ("never" if action == "create" else None),
        "new_rows_total": result.new_rows_total,
        "params_hash": params_hash,
    }
    if action != "delete":
        ref.update(
            url=result.url,
            url_hash=_text_hash(result.url or ""),
            config=dict(result.config),
            config_hash=_text_hash(config_text),
        )
        if action == "create":
            ref.update(
                cursor_hash=_text_hash(str((raw or {}).get("cursor") or "")),
                created_at=(raw or {}).get("created_at"),
            )
    else:
        ref.update(
            url_hash=_text_hash(result.url or ""),
            config_hash=_text_hash(config_text),
            deleted=True,
        )
    return ref


def _public_output(ref: dict[str, Any]) -> ActionOutput:
    return ActionOutput(
        kind="source",
        name=str(ref.get("name") or "source"),
        sheet_id=ref.get("sheet_id"),
        ref=ref,
    )


def _source_check_refs(
    result: CheckedSource,
    *,
    cursor: str | None,
    params_hash: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    source = result.source
    config_raw = after.get("config") or before.get("config") or "{}"
    config_text = (
        config_raw if isinstance(config_raw, str) else json.dumps(dict(config_raw))
    )
    source_kind = source.kind or str(before.get("kind") or "")
    url = source.url or str(before.get("url") or "")
    return {
        "source": {
            "kind": "source_check_source",
            "source_id": source.source_id,
            "name": source.name or before.get("name"),
            "source_kind": source_kind,
            "url_hash": _text_hash(url),
            "sheet_id": source.sheet_id,
            "config_hash": _text_hash(config_text),
            "params_hash": params_hash,
        },
        "request": {
            "kind": "source_check_request",
            "source_id": source.source_id,
            "reported_status": result.status,
            "reported_new_rows": result.new_rows,
            "reported_error": result.error,
            "cursor_provided": cursor is not None,
            "params_hash": params_hash,
        },
        "source_run": {
            "kind": "source_check_run",
            "source_id": source.source_id,
            "source_run_id": result.source_run_id,
            "status": result.status,
            "new_rows": result.new_rows,
            "error": result.error,
            "sheet_id": source.sheet_id,
            "op_id": None,
        },
        "cursor": {
            "kind": "source_check_cursor",
            "source_id": source.source_id,
            "source_run_id": result.source_run_id,
            "cursor_hash": _text_hash(cursor or ""),
            "cursor_provided": cursor is not None,
        },
    }


def _source_check_outputs(refs: Mapping[str, dict[str, Any]]) -> list[ActionOutput]:
    return [
        _public_output(refs["source"]),
        ActionOutput(
            kind="source_run",
            name="source_run",
            sheet_id=refs["source_run"].get("sheet_id"),
            ref=refs["source_run"],
        ),
    ]


def _perform(
    project: Any,
    cur: Any,
    action: _TypedProjectEnvelope,
    params: Any,
    *,
    terminal: _ProjectAction[Any, Any],
    project_id: str,
    action_id: str,
    receipt_id: str,
    params_hash: str,
    resolved: Any,
) -> ActionResult:
    del cur, resolved
    capability = _CAPABILITY_IMPL[terminal.single_capability()](project)
    try:
        returned = terminal.handler(params, capability)
    except LookupError:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="invalid_input_ref",
                message=f"{action.kind} source_id does not exist",
                action_kind=action.kind,
                field="params",
            ),
        )
    except _UnsupportedSourceKind as exc:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="unsupported_source_kind",
                message="RSS sources must be polled with source.poll",
                action_kind=action.kind,
                field="params",
                details={"source_kind": str(exc)},
            ),
        )
    if capability.result is None or returned != capability.result:
        raise TypeError("source handler must return its capability result")

    operation = _CAPABILITY_OPERATION[terminal.single_capability()]
    semantic = (
        returned.source
        if isinstance(returned, (UpdatedSource, DeletedSource, CheckedSource))
        else returned
    )
    if isinstance(returned, CheckedSource):
        refs = _source_check_refs(
            returned,
            cursor=capability.cursor,
            params_hash=params_hash,
            before=capability.before or {},
            after=capability.after or {},
        )
        receipt = Receipt(
            receipt_id=receipt_id,
            project_id=project_id,
            action_id=action_id,
            action_kind=action.kind,
            idempotency_key=action.idempotency_key,
            params_hash=params_hash,
            status="completed",
            inputs=[
                ReceiptIO(name="source", ref=refs["source"]),
                ReceiptIO(name="request", ref=refs["request"]),
            ],
            outputs=[
                ReceiptIO(name="source", ref=refs["source"]),
                ReceiptIO(name="source_run", ref=refs["source_run"]),
            ],
            evidence=[ReceiptEvidence(ref=refs["cursor"])],
        )
        ReceiptStore(project).insert_completed(receipt, commit=False)
        return ActionResult(
            action=ActionIdentity(kind=action.kind, action_id=action_id),
            status="completed",
            project_id=project_id,
            outputs=_source_check_outputs(refs),
            receipt_id=receipt_id,
        )
    ref = _source_ref(
        semantic,
        action=operation,
        params_hash=params_hash,
        raw=capability.after or capability.before,
    )
    request_ref: dict[str, Any] = {
        "kind": f"source_{operation}_request",
        "params_hash": params_hash,
    }
    if operation != "create":
        request_ref["source_id"] = semantic.source_id
    if isinstance(returned, UpdatedSource):
        ref["updated_fields"] = list(returned.updated_fields)
        request_ref["updated_fields"] = list(returned.updated_fields)
    if isinstance(returned, DeletedSource):
        ref["source_run_count"] = returned.source_run_count
    inputs = [ReceiptIO(name="request", ref=request_ref)]
    evidence_ref = request_ref
    if isinstance(returned, (UpdatedSource, DeletedSource)):
        if isinstance(returned, UpdatedSource):
            raw_before = capability.before or {}
            before_config = raw_before.get("config") or "{}"
            before_config_text = (
                before_config
                if isinstance(before_config, str)
                else json.dumps(before_config)
            )
            before = {
                "kind": "source_update_before",
                "source_id": semantic.source_id,
                "name": raw_before.get("name"),
                "source_kind": raw_before.get("kind"),
                "url_hash": _text_hash(str(raw_before.get("url") or "")),
                "config_hash": _text_hash(before_config_text),
                "sheet_id": raw_before.get("sheet_id"),
                "schedule": raw_before.get("schedule"),
                "enabled": bool(raw_before.get("enabled")),
                "last_checked_at": raw_before.get("last_checked_at"),
                "last_status": raw_before.get("last_status"),
                "new_rows_total": int(raw_before.get("new_rows_total") or 0),
                "params_hash": params_hash,
            }
            inputs.insert(0, ReceiptIO(name="source_before", ref=before))
            evidence_ref = before
        else:
            inputs.insert(0, ReceiptIO(name="source_before", ref=ref))
            evidence_ref = ref
    elif operation == "create":
        request_ref.update(
            source_kind=semantic.kind,
            has_url=semantic.url is not None,
            schedule=semantic.schedule,
            enabled=semantic.enabled,
            config_hash=ref["config_hash"],
        )
    receipt = Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status="completed",
        inputs=inputs,
        outputs=[ReceiptIO(name="source", ref=ref)],
        evidence=[ReceiptEvidence(ref=evidence_ref)],
    )
    ReceiptStore(project).insert_completed(receipt, commit=False)
    return ActionResult(
        action=ActionIdentity(kind=action.kind, action_id=action_id),
        status="completed",
        project_id=project_id,
        outputs=[_public_output(ref)],
        receipt_id=receipt_id,
    )


def run_typed_source_action(
    project: Any, project_id: str, bound: BoundTypedActionRequest
) -> ActionResult:
    terminal = bound.action.definition.run
    if not supports_typed_source_action(terminal):
        raise TypeError("typed source executor requires a source action")
    envelope = _TypedProjectEnvelope(
        kind=bound.action.action_id,
        idempotency_key=bound.request.idempotency_key,
        params=bound.params.model_dump(mode="json"),
    )
    params_hash = typed_request_hash(bound)

    spec = _ActionCoreSpec(
        kind=envelope.kind,
        params_model=terminal.params_model,
        body_kind="plain",
        params_hash_fn=lambda _action: params_hash,
        result_from_existing_fn=_child_sheet_deterministic_result_from_existing(),
        plain_perform_in_txn_fn=lambda project_, cur, action, params, **kwargs: (
            _perform(project_, cur, action, params, terminal=terminal, **kwargs)
        ),
        plain_exception_error_fn=lambda action: ActionError(
            code="project_write_failed",
            message=(
                "source.check could not write source_run and receipt"
                if terminal.capabilities == (SourceChecker,)
                else f"{action.kind} could not mutate source and receipt"
            ),
            action_kind=action.kind,
        ),
    )
    return _run_action_core_spec(
        project,
        envelope,
        bound.params,
        spec=spec,
        ctx=ExecutorContext(project_id=project_id, deps=ExecutorDeps()),
    )
