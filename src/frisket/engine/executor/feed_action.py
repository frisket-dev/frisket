"""Host-owned execution for typed feed metadata actions."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Mapping, TypeGuard

from frisket.engine.executor.action_inventory import _typed_params_envelope
from frisket.actions.core import _ProjectAction
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    CheckedFeed,
    DeletedFeed,
    FeedChecker,
    FeedCreator,
    FeedDeleter,
    FeedPatch,
    FeedRecord,
    FeedUpdater,
    UpdatedFeed,
)
from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionOutput,
    ActionResult,
    Receipt,
    ReceiptRecord,
    ReceiptIO,
)
from frisket.engine.executor.action_inventory import (
    ExecutorDeps,
    ExecutorContext,
    _TypedProjectEnvelope,
)
from frisket.engine.executor.action_lifecycle import (
    _run_typed_plain_action,
)
from frisket.engine.executor.action_support import ActionCtx
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.feeds import FeedStore


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


def _record(row: Mapping[str, Any]) -> FeedRecord:
    return FeedRecord(
        feed_id=int(row["id"]),
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
        self.result: FeedRecord | UpdatedFeed | DeletedFeed | CheckedFeed | None = None
        self.before: dict[str, Any] | None = None
        self.after: dict[str, Any] | None = None

    def _begin(self) -> FeedStore:
        if self._called:
            raise RuntimeError("feed capability may be called only once")
        self._called = True
        return FeedStore(self._project)


class _Creator(_CallOnce):
    def create(self, **values: Any) -> FeedRecord:
        store = self._begin()
        feed_id = store.add_feed(**values)
        row = store.get_feed(feed_id)
        if row is None:
            raise RuntimeError("created feed is unavailable")
        self.after = dict(row)
        result = _record(self.after)
        self.result = result
        return result


class _Updater(_CallOnce):
    def update(self, feed_id: int, *, patch: FeedPatch) -> UpdatedFeed:
        store = self._begin()
        before = store.get_feed(feed_id)
        if before is None:
            raise LookupError(feed_id)
        self.before = dict(before)
        values = patch.values()
        if values.get("config", object()) is None:
            values["config"] = {}
        store.update_feed(feed_id, **values)
        row = store.get_feed(feed_id)
        if row is None:
            raise LookupError(feed_id)
        self.after = dict(row)
        result = UpdatedFeed(_record(self.after), tuple(sorted(values)))
        self.result = result
        return result


class _Deleter(_CallOnce):
    def delete(self, feed_id: int) -> DeletedFeed:
        store = self._begin()
        row = store.get_feed(feed_id)
        if row is None:
            raise LookupError(feed_id)
        self.before = dict(row)
        result = DeletedFeed(_record(self.before), store.feed_checks_total(feed_id))
        store.delete_feed(feed_id)
        self.result = result
        return result


class _UnsupportedFeedKind(Exception):
    pass


class _Checker(_CallOnce):
    cursor: str | None = None

    def check(
        self,
        feed_id: int,
        *,
        new_rows: int,
        status: Literal["ok", "error"],
        error: str | None,
        cursor: str | None,
    ) -> CheckedFeed:
        store = self._begin()
        before = store.get_feed(feed_id)
        if before is None:
            raise LookupError(feed_id)
        if before["kind"] == "rss":
            raise _UnsupportedFeedKind("rss")
        self.before = dict(before)
        self.cursor = cursor
        feed_check_id = store.record_feed_check(
            feed_id,
            new_rows=new_rows,
            status=status,
            error=error,
            cursor=cursor,
        )
        after = store.get_feed(feed_id)
        if after is None:
            raise LookupError(feed_id)
        self.after = dict(after)
        result = CheckedFeed(
            feed=_record(self.after),
            feed_check_id=feed_check_id,
            status=status,
            new_rows=new_rows,
            error=error,
        )
        self.result = result
        return result


_CAPABILITY_IMPL = {
    FeedCreator: _Creator,
    FeedUpdater: _Updater,
    FeedDeleter: _Deleter,
    FeedChecker: _Checker,
}
_CAPABILITY_OPERATION = {
    FeedCreator: "create",
    FeedUpdater: "update",
    FeedDeleter: "delete",
    FeedChecker: "check",
}


def supports_typed_feed_action(terminal: object) -> TypeGuard[_ProjectAction]:
    return isinstance(terminal, _ProjectAction) and any(
        terminal.capabilities == (cap,) for cap in _CAPABILITY_IMPL
    )


def _feed_ref(
    result: FeedRecord,
    *,
    action: str,
    params_hash: str,
    raw: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    raw_config = (raw or {}).get("config") or "{}"
    config_text = raw_config if isinstance(raw_config, str) else json.dumps(raw_config)
    ref = {
        "kind": f"feed_{action}_feed",
        "feed_id": result.feed_id,
        "name": result.name,
        "feed_kind": result.kind,
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
        kind="feed",
        name=str(ref.get("name") or "feed"),
        sheet_id=ref.get("sheet_id"),
        ref=ref,
    )


def _feed_check_refs(
    result: CheckedFeed,
    *,
    cursor: str | None,
    params_hash: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    feed = result.feed
    config_raw = after.get("config") or before.get("config") or "{}"
    config_text = (
        config_raw if isinstance(config_raw, str) else json.dumps(dict(config_raw))
    )
    feed_kind = feed.kind or str(before.get("kind") or "")
    url = feed.url or str(before.get("url") or "")
    return {
        "feed": {
            "kind": "feed_check_feed",
            "feed_id": feed.feed_id,
            "name": feed.name or before.get("name"),
            "feed_kind": feed_kind,
            "url_hash": _text_hash(url),
            "sheet_id": feed.sheet_id,
            "config_hash": _text_hash(config_text),
            "params_hash": params_hash,
        },
        "request": {
            "kind": "feed_check_request",
            "feed_id": feed.feed_id,
            "reported_status": result.status,
            "reported_new_rows": result.new_rows,
            "reported_error": result.error,
            "cursor_provided": cursor is not None,
            "params_hash": params_hash,
        },
        "feed_check": {
            "kind": "feed_check_record",
            "feed_id": feed.feed_id,
            "feed_check_id": result.feed_check_id,
            "status": result.status,
            "new_rows": result.new_rows,
            "error": result.error,
            "sheet_id": feed.sheet_id,
            "change_id": None,
        },
        "cursor": {
            "kind": "feed_check_cursor",
            "feed_id": feed.feed_id,
            "feed_check_id": result.feed_check_id,
            "cursor_hash": _text_hash(cursor or ""),
            "cursor_provided": cursor is not None,
        },
    }


def _feed_check_outputs(refs: Mapping[str, dict[str, Any]]) -> list[ActionOutput]:
    return [
        _public_output(refs["feed"]),
        ActionOutput(
            kind="feed_check",
            name="feed_check",
            sheet_id=refs["feed_check"].get("sheet_id"),
            ref=refs["feed_check"],
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
    ctx = ActionCtx(project_id, action.kind)
    del cur, resolved
    capability = _CAPABILITY_IMPL[terminal.single_capability()](project)
    try:
        returned = terminal.handler(params, capability)
    except LookupError:
        return ctx.fail(
            "invalid_input_ref",
            f"{action.kind} feed_id does not exist",
            field="params",
        )
    except _UnsupportedFeedKind as exc:
        return ctx.fail(
            "unsupported_feed_kind",
            "RSS feeds must be polled with feed.poll",
            field="params",
            details={"feed_kind": str(exc)},
        )
    if capability.result is None or returned != capability.result:
        raise TypeError("feed handler must return its capability result")

    operation = _CAPABILITY_OPERATION[terminal.single_capability()]
    semantic = (
        returned.feed
        if isinstance(returned, (UpdatedFeed, DeletedFeed, CheckedFeed))
        else returned
    )
    if isinstance(returned, CheckedFeed):
        refs = _feed_check_refs(
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
                ReceiptIO(name="feed", ref=refs["feed"]),
                ReceiptIO(name="request", ref=refs["request"]),
            ],
            outputs=[
                ReceiptIO(name="feed", ref=refs["feed"]),
                ReceiptIO(name="feed_check", ref=refs["feed_check"]),
            ],
            records=[ReceiptRecord(ref=refs["cursor"])],
        )
        ReceiptStore(project).insert_completed(receipt)
        return ActionResult(
            action=ActionIdentity(kind=action.kind, action_id=action_id),
            status="completed",
            project_id=project_id,
            outputs=_feed_check_outputs(refs),
            receipt_id=receipt_id,
        )
    ref = _feed_ref(
        semantic,
        action=operation,
        params_hash=params_hash,
        raw=capability.after or capability.before,
    )
    request_ref: dict[str, Any] = {
        "kind": f"feed_{operation}_request",
        "params_hash": params_hash,
    }
    if operation != "create":
        request_ref["feed_id"] = semantic.feed_id
    if isinstance(returned, UpdatedFeed):
        ref["updated_fields"] = list(returned.updated_fields)
        request_ref["updated_fields"] = list(returned.updated_fields)
    if isinstance(returned, DeletedFeed):
        ref["feed_check_count"] = returned.feed_check_count
    inputs = [ReceiptIO(name="request", ref=request_ref)]
    evidence_ref = request_ref
    if isinstance(returned, (UpdatedFeed, DeletedFeed)):
        if isinstance(returned, UpdatedFeed):
            raw_before = capability.before or {}
            before_config = raw_before.get("config") or "{}"
            before_config_text = (
                before_config
                if isinstance(before_config, str)
                else json.dumps(before_config)
            )
            before = {
                "kind": "feed_update_before",
                "feed_id": semantic.feed_id,
                "name": raw_before.get("name"),
                "feed_kind": raw_before.get("kind"),
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
            inputs.insert(0, ReceiptIO(name="feed_before", ref=before))
            evidence_ref = before
        else:
            inputs.insert(0, ReceiptIO(name="feed_before", ref=ref))
            evidence_ref = ref
    elif operation == "create":
        request_ref.update(
            feed_kind=semantic.kind,
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
        outputs=[ReceiptIO(name="feed", ref=ref)],
        records=[ReceiptRecord(ref=evidence_ref)],
    )
    ReceiptStore(project).insert_completed(receipt)
    return ActionResult(
        action=ActionIdentity(kind=action.kind, action_id=action_id),
        status="completed",
        project_id=project_id,
        outputs=[_public_output(ref)],
        receipt_id=receipt_id,
    )


def run_typed_feed_action(
    project: Any, project_id: str, bound: BoundTypedActionRequest
) -> ActionResult:
    terminal = bound.action.definition.run
    if not supports_typed_feed_action(terminal):
        raise TypeError("typed feed executor requires a feed action")
    envelope = _typed_params_envelope(bound)
    params_hash = typed_request_hash(bound)

    return _run_typed_plain_action(
        project,
        bound,
        envelope,
        ctx=ExecutorContext(project_id=project_id, deps=ExecutorDeps()),
        params_hash=params_hash,
        plain_perform_in_txn_fn=lambda project_, cur, action, params, **kwargs: (
            _perform(project_, cur, action, params, terminal=terminal, **kwargs)
        ),
        plain_exception_error_fn=lambda action: ActionError(
            code="project_write_failed",
            message=(
                "feed.check could not write feed_check and receipt"
                if terminal.capabilities == (FeedChecker,)
                else f"{action.kind} could not mutate feed and receipt"
            ),
            action_kind=action.kind,
        ),
    )
