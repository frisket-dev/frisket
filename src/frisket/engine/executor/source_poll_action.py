"""Host-owned polling, source publication and durable failure receipts."""

from __future__ import annotations

from frisket.engine.store.transactions import inserted_rowid
import copy
import hashlib
import json
import logging
import re
import time
from typing import Any

from pydantic import TypeAdapter

from frisket.engine.executor.action_inventory import _typed_params_envelope
from frisket.actions.core import _ProjectAction
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import PolledSource, SourcePoller, SourcePollSelector
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
    ExecutorDeps,
    ExecutorContext,
    _TypedProjectEnvelope,
)
from frisket.engine.executor.action_lifecycle import (
    _run_typed_plain_action,
)
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.store import Project
from frisket.engine.store.cell_writes import (
    create_base_cell_producer,
)
from frisket.engine.store.keyed_updates import ValidatedKeyedInput, typed_key
from frisket.engine.store.keyed_publication import (
    index_keyed_targets,
    publish_keyed_records,
)
from frisket.engine.store.op_log import append_op, set_undo_info
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.sources import SourceStore
from frisket.server.sources.rss import (
    ensure_rss_feed_checker,
    rss_feed_checker_for_fetch_override,
)
from frisket.server.sources.runtime import (
    SourcePollContext,
    SourcePollItem,
    SourcePollResult,
    encode_cursor,
    get_feed_checker,
)

logger = logging.getLogger("frisket.executor")


def supports_typed_source_poll_action(terminal: object) -> bool:
    return isinstance(terminal, _ProjectAction) and terminal.capabilities == (
        SourcePoller,
    )


class _PollRefused(Exception):
    def __init__(self, error: ActionError):
        self.error = error


class _FeedChecker:
    def __init__(
        self,
        project: Project,
        action: _TypedProjectEnvelope,
        fetcher: Any,
        on_progress=None,
        should_cancel=None,
    ):
        self._project = project
        self._action = action
        self._fetcher = fetcher
        self._on_progress = on_progress
        self._should_cancel = should_cancel
        self._called = False
        self.resolved: dict[str, Any] | None = None

    def poll(self, source: SourcePollSelector) -> PolledSource:
        if self._called:
            raise RuntimeError("source polling capability may be called only once")
        self._called = True
        source = TypeAdapter(SourcePollSelector).validate_python(source)
        ensure_rss_feed_checker()
        resolved = _resolve_and_poll_source(
            self._project,
            self._action,
            source,
            rss_fetcher=self._fetcher,
            on_progress=self._on_progress,
            should_cancel=self._should_cancel,
        )
        if isinstance(resolved, ActionError):
            raise _PollRefused(resolved)
        self.resolved = resolved
        result = resolved["poll_result"]
        return PolledSource(
            source_id=resolved["source_id"],
            status="error" if resolved["poll_error"] is not None else "ok",
            item_count=len(result.items) if result is not None else 0,
            cost_micro=_source_poll_cost_micro(result.cost)
            if result is not None
            else 0,
            error=resolved["poll_error"],
        )


def run_typed_source_poll_action(
    project: Project,
    project_id: str,
    bound: BoundTypedActionRequest,
    *,
    rss_fetcher: Any = None,
    on_progress=None,
    should_cancel=None,
) -> ActionResult:
    terminal = bound.action.definition.run
    if not supports_typed_source_poll_action(terminal):
        raise TypeError("typed source poll executor requires a polling capability")
    envelope = _typed_params_envelope(bound)

    def resolve(project_: Project, params: Any) -> Any:
        capability = _FeedChecker(
            project_, envelope, rss_fetcher, on_progress, should_cancel
        )
        try:
            returned = terminal.handler(params, capability)
            if capability.resolved is None or not isinstance(returned, PolledSource):
                raise TypeError(
                    "source poll handler must call its capability and return PolledSource"
                )
        except Exception as error:
            if capability.resolved is not None:
                # Polling already happened. Preserve its observed usage and settle a
                # failed receipt, withholding rows rather than permitting a refetch.
                # Author exception text may contain secrets; never persist it.
                return {
                    **capability.resolved,
                    "poll_error": (
                        "Source poll handler failed after polling; no rows were published."
                    ),
                }
            if isinstance(error, _PollRefused):
                return error.error
            if isinstance(error, (ValueError, TypeError)):
                return ActionError(
                    code="invalid_params",
                    message=str(error),
                    action_kind=envelope.kind,
                    field="params",
                )
            raise
        # Custom return values are descriptive, never authoritative effect facts.
        return capability.resolved

    params_hash = typed_request_hash(bound)
    return _run_typed_plain_action(
        project,
        bound,
        envelope,
        ctx=ExecutorContext(project_id=project_id, deps=ExecutorDeps()),
        params_hash=params_hash,
        plain_resolve_fn=resolve,
        plain_perform_in_txn_fn=_perform_source_poll_in_txn,
        plain_persist_failure=True,
        plain_exception_error_fn=lambda action: ActionError(
            code="project_write_failed",
            message="Source poll publication and receipt could not be written.",
            action_kind=action.kind,
        ),
    )


def _resolve_and_poll_source(
    project: Project,
    action: _TypedProjectEnvelope,
    source: SourcePollSelector,
    *,
    rss_fetcher: Any | None,
    on_progress=None,
    should_cancel=None,
) -> dict[str, Any] | ActionError:
    """The source.poll pre-txn resolve (`plain_resolve_fn`): resolve/create the source,
    select the feed checker, validate config, then run the whole-source `feed_checker.poll(...)` network
    fetch. Pre-poll validation failures (missing source, unsupported kind/config) return an
    ActionError so the core fails receiptless; a poll exception is CAPTURED into the resolved
    payload (poll_error) so the in-txn perform writes the persisted failed receipt + error
    source_run. A created source is rolled back on a config error (the only pre-poll error
    reachable after creation)."""
    started = time.perf_counter()
    resolved = _resolve_source_poll_source(project, action, source)
    if isinstance(resolved, ActionError):
        return resolved
    source_before = dict(resolved["source"])
    source_id = int(source_before["id"])
    source_kind = str(source_before["kind"])
    try:
        _require_source_poll_destination(project, source_before)
    except ValueError as exc:
        if resolved["created"]:
            SourceStore(project).delete_source(source_id)
        return ActionError(
            code="invalid_input_ref",
            message=str(exc),
            action_kind=action.kind,
            field="params",
            details={"source_id": source_id},
        )
    feed_checker = rss_feed_checker_for_fetch_override(
        source_kind,
        rss_fetcher,
    ) or get_feed_checker(source_kind)
    if feed_checker is None:
        # The create path pre-checks the feed checker in _resolve_source_poll_source, so only an
        # existing source_id with an unsupported kind reaches here (nothing to roll back).
        return ActionError(
            code="unsupported_source_kind",
            message="Frisket cannot poll this feed type",
            action_kind=action.kind,
            field="params",
            details={"source_kind": source_kind},
        )
    source_for_feed_checker = _source_poll_source_dict(source_before)
    config_error = feed_checker.validate_config(source_for_feed_checker)
    if config_error:
        if resolved.get("created"):
            SourceStore(project).delete_source(source_id)
        return ActionError(
            code="unsupported_source_config",
            message=str(config_error),
            action_kind=action.kind,
            field="params",
        )

    cursor_before = source_before.get("cursor")
    cursor_before_text = str(cursor_before) if cursor_before is not None else None
    poll_result: SourcePollResult | None = None
    poll_error: str | None = None
    try:
        candidate = feed_checker.poll(
            SourcePollContext(
                source=source_for_feed_checker,
                cursor_before=cursor_before_text,
                on_progress=on_progress,
                should_cancel=should_cancel,
            )
        )
        if should_cancel and should_cancel():
            raise RuntimeError("Feed import cancelled")
        if not isinstance(candidate, SourcePollResult):
            raise TypeError("poller returned a non-SourcePollResult value")
        poll_result = copy.deepcopy(candidate)
    except Exception as exc:
        poll_error = str(exc)
    return {
        "source_before": source_before,
        "source_id": source_id,
        "source_kind": source_kind,
        "cursor_before_text": cursor_before_text,
        "started": started,
        "poll_result": poll_result,
        "poll_error": poll_error,
    }


def _perform_source_poll_in_txn(
    project: Project,
    cur: Any,
    action: _TypedProjectEnvelope,
    params: Any,
    *,
    project_id: str,
    action_id: str,
    receipt_id: str,
    params_hash: str,
    resolved: Any,
) -> ActionResult:
    """The source.poll in-txn perform (`plain_perform_in_txn_fn`): open a source_run, then
    EITHER materialize the polled rows (success) OR write only the failure record (a failed
    receipt + an error source_run) when the pre-txn poll raised. All project writes
    (sheet/columns/rows/op) and source-store writes run inline on the core's open cursor with
    `commit=False`; the core owns the BEGIN IMMEDIATE/recheck/commit envelope and supplies the
    ids. A `failed` result is committed by the body's persist_failure policy, preserving the
    failed-receipt + error source_run trace."""
    source_before = dict(resolved["source_before"])
    source_id = int(resolved["source_id"])
    source_kind = str(resolved["source_kind"])
    cursor_before_text = resolved["cursor_before_text"]
    started = resolved["started"]
    source_store = SourceStore(project)
    source_run_id = source_store.start_source_run(
        source_id,
        cursor_before=cursor_before_text,
        commit=False,
    )

    poll_result = resolved["poll_result"]
    poll_error = resolved["poll_error"]
    if poll_error is not None:
        warnings = list(poll_result.warnings) if poll_result is not None else []
        provider_use = list(poll_result.provider_use) if poll_result is not None else []
        cost_micro = (
            _source_poll_cost_micro(poll_result.cost) if poll_result is not None else 0
        )
        artifacts = list(poll_result.artifacts) if poll_result is not None else []
        error = ActionError(
            code="source_poll_failed",
            message=poll_error,
            action_kind=action.kind,
            field="params",
        )
        refs = _source_poll_refs(
            source_before=source_before,
            source_after=source_before,
            summary={
                "source_id": source_id,
                "source_kind": source_kind,
                "source_run_id": source_run_id,
                "status": "error",
                "new_rows": 0,
                "skipped_rows": 0,
                "changed_rows": 0,
                "revisions": 0,
                "materialized_rows": 0,
                "error": poll_error,
                "sheet_id": source_before.get("sheet_id"),
                "op_id": None,
                "row_ids": [],
                "cursor_before": cursor_before_text,
                "cursor_after": cursor_before_text,
                "warning_count": len(warnings),
                "duration_ms": _duration_ms(started),
                "cost_micro": cost_micro,
                "artifacts": artifacts,
            },
            params_hash=params_hash,
        )
        receipt = _source_poll_receipt(
            action=action,
            action_id=action_id,
            project_id=project_id,
            receipt_id=receipt_id,
            params_hash=params_hash,
            op_ids=[],
            refs=refs,
            status="failed",
            warnings=warnings,
            provider_use=provider_use,
            errors=[error],
        )
        # Insert the failed receipt before linking the error source_run to it
        # (source_runs.receipt_id carries a FK to receipts.id).
        ReceiptStore(project).insert_finished(receipt, commit=False)
        source_store.finish_source_run(
            source_run_id,
            status="error",
            receipt_id=receipt_id,
            error=poll_error,
            cursor_after=cursor_before_text,
            duration_ms=_duration_ms(started),
            warning_count=len(warnings),
            cost_micro=cost_micro,
            summary={
                "error": poll_error,
                **(
                    {"artifacts": artifacts, "summary": dict(poll_result.summary)}
                    if poll_result is not None
                    else {}
                ),
            },
            commit=False,
        )
        return ActionResult(
            action=ActionIdentity(kind=action.kind, action_id=action_id),
            status="failed",
            project_id=project_id,
            op_ids=[],
            outputs=_source_poll_outputs(refs),
            receipt_id=receipt_id,
            errors=[error],
            warnings=warnings,
        )

    summary = _materialize_registered_source_poll_in_txn(
        project,
        cur,
        source_before=source_before,
        source_run_id=source_run_id,
        poll_result=poll_result,
    )
    source_after = dict(source_store.get_source(source_id) or source_before)
    summary.update(
        {
            "source_id": source_id,
            "source_kind": source_kind,
            "source_run_id": source_run_id,
            "status": "ok",
            "cursor_before": cursor_before_text,
            "cursor_after": encode_cursor(poll_result.cursor_after),
            "warning_count": len(poll_result.warnings),
            "duration_ms": _duration_ms(started),
            "cost_micro": _source_poll_cost_micro(poll_result.cost),
            "artifacts": list(poll_result.artifacts),
            "summary": dict(poll_result.summary),
        }
    )
    refs = _source_poll_refs(
        source_before=source_before,
        source_after=source_after,
        summary=summary,
        params_hash=params_hash,
    )
    receipt = _source_poll_receipt(
        action=action,
        action_id=action_id,
        project_id=project_id,
        receipt_id=receipt_id,
        params_hash=params_hash,
        op_ids=[int(summary["op_id"])] if summary.get("op_id") is not None else [],
        refs=refs,
        status="completed",
        warnings=list(poll_result.warnings),
        provider_use=list(poll_result.provider_use),
        errors=[],
    )
    # Insert the receipt before linking the source_run to it (source_runs.receipt_id carries
    # a FK to receipts.id).
    from frisket.engine.store.changes import ChangeStore

    for op_id in receipt.op_ids:
        ChangeStore(project).close(op_id)
    ReceiptStore(project).insert_finished(receipt, commit=False)
    source_store.finish_source_run(
        source_run_id,
        status="ok",
        receipt_id=receipt_id,
        op_id=summary.get("op_id"),
        new_rows=int(summary["new_rows"]),
        skipped_rows=int(summary["skipped_rows"]),
        changed_rows=int(summary["changed_rows"]),
        revisions=int(summary["revisions"]),
        cursor_after=summary.get("cursor_after"),
        duration_ms=int(summary["duration_ms"]),
        warning_count=int(summary["warning_count"]),
        cost_micro=int(summary["cost_micro"]),
        summary=summary,
        commit=False,
    )
    return ActionResult(
        action=ActionIdentity(kind=action.kind, action_id=action_id),
        status="completed",
        project_id=project_id,
        op_ids=receipt.op_ids,
        outputs=_source_poll_outputs(refs),
        receipt_id=receipt_id,
        warnings=list(poll_result.warnings),
    )


def _resolve_source_poll_source(
    project: Project,
    action: _TypedProjectEnvelope,
    selector: SourcePollSelector,
) -> dict[str, Any] | ActionError:
    if isinstance(selector, int):
        source = SourceStore(project).get_source(selector)
        if source is None:
            return ActionError(
                code="invalid_input_ref",
                message="source.poll source_id does not exist",
                action_kind=action.kind,
                field="params",
            )
        return {"source": dict(source), "created": False}
    if get_feed_checker(selector.kind) is None:
        return ActionError(
            code="unsupported_source_kind",
            message="Frisket cannot poll this feed type",
            action_kind=action.kind,
            field="params",
            details={"source_kind": selector.kind},
        )
    source_store = SourceStore(project)
    source_id = source_store.add_source(
        name=selector.name,
        kind=selector.kind,
        url=selector.url,
        config=selector.config,
        sheet_id=selector.sheet_id,
        schedule=selector.schedule,
        enabled=selector.enabled,
    )
    source = source_store.get_source(source_id)
    if source is None:
        return ActionError(
            code="project_write_failed",
            message="source.poll could not create source",
            action_kind=action.kind,
        )
    return {"source": dict(source), "created": True}


def _source_poll_source_dict(source: dict[str, Any]) -> dict[str, Any]:
    out = dict(source)
    out["enabled"] = bool(out.get("enabled"))
    out["config"] = _source_config_dict(out.get("config"))
    return out


def _materialize_registered_source_poll_in_txn(
    project: Project,
    cur: Any,
    *,
    source_before: dict[str, Any],
    source_run_id: int,
    poll_result: SourcePollResult,
) -> dict[str, Any]:
    """Publish source rows and observations in the receipt owner's transaction."""
    source_id = int(source_before["id"])
    source_store = SourceStore(project)
    sheet_id = _source_poll_sheet_id_in_txn(project, cur, source_before)
    _rename_default_media_source_in_txn(
        cur,
        source_store=source_store,
        source_before=source_before,
        sheet_id=sheet_id,
        poll_summary=poll_result.summary,
    )
    # Finish key validation before creating or updating any row. A later
    # conflicting duplicate must not invalidate an earlier published value.
    records = [
        _source_poll_row(source_id, source_run_id, item) for item in poll_result.items
    ]
    column_ids = _ensure_source_poll_columns_in_txn(
        cur,
        sheet_id,
        records,
        source_kind=str(source_before["kind"]),
        bound_columns=_source_poll_key_columns(project, source_id, sheet_id),
    )
    if not records:
        return {
            "sheet_id": sheet_id,
            "op_id": None,
            "row_ids": [],
            "enclosure_row_ids": [],
            "new_rows": 0,
            "skipped_rows": 0,
            "changed_rows": 0,
            "revisions": 0,
            "materialized_rows": 0,
            "duplicates_collapsed": 0,
        }
    relation = f"source:{source_id}"
    key_definition = [
        (column_ids["source_id"], "integer"),
        (column_ids["source_item_id"], "text"),
    ]
    policy = _source_config_dict(source_before.get("config")).get(
        "update_policy", "keep_changes"
    )
    if policy not in ("keep_changes", "incoming_wins"):
        raise ValueError("unknown source update policy")
    op_id = None
    producer_id = None

    def writer():
        nonlocal op_id, producer_id
        if op_id is None:
            op_id = append_op(
                project,
                "source.poll",
                {"source_id": source_id, "source_run_id": source_run_id},
                label=f"source poll {source_before['name']}",
                commit=False,
            )
            producer_id = create_base_cell_producer(
                project.db, stage_id=f"op:{op_id}", op_id=op_id
            )
        return op_id, producer_id

    def create_rows(ordinals, producer):
        return project.add_rows(
            sheet_id,
            [records[ordinal - 1] for ordinal in ordinals],
            column_ids,
            producer_id=producer,
            commit=False,
        )

    with ValidatedKeyedInput() as staged:
        staged.stage(
            (
                typed_key([("integer", source_id), ("text", item.stable_item_id())]),
                {key: value for key, value in record.items() if key != "source_run_id"},
            )
            for item, record in zip(poll_result.items, records, strict=True)
        )
        staged.seal()
        index_keyed_targets(project, sheet_id, key_definition, staged)
        publication = publish_keyed_records(
            project,
            sheet_id=sheet_id,
            relationship_id=relation,
            key_definition=key_definition,
            columns=column_ids,
            writable_columns=set(column_ids.values()),
            staged=staged,
            records=((item.ordinal, item.record) for item in staged.records()),
            policy=policy,
            writer=writer,
            create_rows=create_rows,
            preserve_invalid=True,
            prefer_observed_targets=True,
        )
    row_ids = list(publication.published.values())
    enclosure_row_ids = [
        row
        for ordinal, row in publication.published.items()
        if poll_result.items[ordinal - 1].row.get("enclosure_url")
        or poll_result.items[ordinal - 1].row.get("enclosures")
    ]
    if op_id is not None:
        set_undo_info(
            project,
            op_id,
            {
                "created_rows": publication.created,
                "restored_rows": publication.restored,
            },
            commit=False,
        )
    return {
        "sheet_id": sheet_id,
        "op_id": op_id,
        "row_ids": row_ids,
        "enclosure_row_ids": enclosure_row_ids,
        "new_rows": len(publication.created),
        "skipped_rows": publication.skipped,
        "changed_rows": len(row_ids) - len(publication.created),
        "revisions": 0,
        "materialized_rows": len(row_ids),
        "duplicates_collapsed": publication.duplicates_collapsed,
    }


def _rename_default_media_source_in_txn(
    cur: Any,
    *,
    source_store: SourceStore,
    source_before: dict[str, Any],
    sheet_id: int,
    poll_summary: dict[str, Any],
) -> None:
    """Replace only the generated media-feed placeholder with provider identity.

    The source and its bound sheet share the resolved name. An explicit user
    name is never touched, including on later polls after an automatic rename.
    """
    kind = str(source_before.get("kind") or "")
    if kind not in {"youtube_playlist", "youtube_channel", "tiktok_profile"}:
        return
    current_name = str(source_before.get("name") or "")
    if current_name != _default_source_name(kind, source_before.get("url")):
        return

    channel_title = _nonempty_text(poll_summary.get("channel_title"))
    if kind in {"youtube_channel", "tiktok_profile"}:
        resolved_name = channel_title
    else:
        playlist_title = _nonempty_text(poll_summary.get("playlist_title"))
        resolved_name = (
            f"{channel_title}: {playlist_title}"
            if channel_title and playlist_title
            else None
        )
    if not resolved_name:
        return
    resolved_name = resolved_name[:160]
    source_store.update_source(
        int(source_before["id"]),
        name=resolved_name,
        commit=False,
    )
    cur.execute("UPDATE sheets SET name=? WHERE id=?", (resolved_name[:80], sheet_id))


def _default_source_name(kind: str, url: Any) -> str:
    label = {
        "youtube_playlist": "YouTube playlist",
        "youtube_channel": "YouTube channel",
        "tiktok_profile": "TikTok profile",
    }[kind]
    compact = str(url or "").strip()
    compact = re.sub(r"^https?://", "", compact, flags=re.IGNORECASE)
    compact = re.sub(r"^www\.", "", compact, flags=re.IGNORECASE)[:72]
    return f"{label}: {compact}" if compact else label


def _nonempty_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _source_poll_key_columns(
    project: Project, source_id: int, sheet_id: int
) -> dict[str, int]:
    """Source keys retain physical identity when the user renames their columns."""
    relationship = project.db.execute(
        "SELECT sheet_id,key_definition FROM keyed_relationships WHERE id=?",
        (f"source:{source_id}",),
    ).fetchone()
    if relationship is None:
        return {}
    if relationship["sheet_id"] != sheet_id:
        raise ValueError(
            "Source destination no longer matches its retained relationship"
        )
    definition = json.loads(relationship["key_definition"])
    if len(definition) != 2 or [part[1] for part in definition] != ["integer", "text"]:
        raise ValueError("Source key definition is unavailable")
    columns = {}
    for name, (column_id, _kind) in zip(
        ("source_id", "source_item_id"), definition, strict=True
    ):
        if (
            project.db.execute(
                "SELECT 1 FROM columns WHERE id=? AND sheet_id=? AND active=1 AND hidden=0",
                (column_id, sheet_id),
            ).fetchone()
            is None
        ):
            raise ValueError(
                "Source key column is unavailable; restore it before polling"
            )
        columns[name] = int(column_id)
    return columns


def _require_source_poll_destination(project: Project, source: dict[str, Any]) -> None:
    sheet_id = source.get("sheet_id")
    if sheet_id is not None:
        if (
            project.db.execute(
                "SELECT 1 FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
            ).fetchone()
            is None
        ):
            raise ValueError(
                "Source destination is unavailable; restore it before polling"
            )
        _source_poll_key_columns(project, int(source["id"]), int(sheet_id))


def _source_poll_sheet_id_in_txn(
    project: Project, cur: Any, source: dict[str, Any]
) -> int:
    """Resolve (or create-and-bind) the target sheet on the open cursor. Inlines
    `project.add_sheet` + the source's sheet_id update so they join the core's txn."""
    sheet_id = source.get("sheet_id")
    _require_source_poll_destination(project, source)
    if sheet_id is not None:
        return int(sheet_id)
    name = str(source.get("name") or "source")[:80] or "source"
    cur.execute(
        "INSERT INTO sheets (name, position, parent_sheet_id, parent_op_id) "
        "VALUES (?, (SELECT COALESCE(MAX(position),0)+1 FROM sheets), NULL, NULL)",
        (name,),
    )
    created = inserted_rowid(cur)
    # Raw SQL against `sources` stays in the store layer; commit=False so this
    # joins the core's open transaction on the same connection (cur =
    # project.db.cursor(), SourceStore.db = project.db) — the core commits once
    # at the end.
    SourceStore(project).update_source(
        int(source["id"]), sheet_id=created, commit=False
    )
    return created


def _ensure_source_poll_columns_in_txn(
    cur: Any,
    sheet_id: int,
    records: list[dict[str, Any]],
    *,
    source_kind: str,
    bound_columns: dict[str, int] | None = None,
) -> dict[str, int]:
    """Create-or-reuse the source-poll columns on the open cursor (inlines
    `project.add_column`, including its hidden-column revive)."""
    col_ids = {
        str(row["name"]): int(row["id"])
        for row in cur.execute(
            "SELECT name, id FROM columns WHERE sheet_id=? AND hidden=0 ORDER BY position",
            (sheet_id,),
        ).fetchall()
    }
    col_ids.update(bound_columns or {})
    for name in _source_poll_column_order(records):
        default_hidden = _source_poll_column_default_hidden(source_kind, name)
        if name not in col_ids:
            col_ids[name] = _insert_source_poll_column_in_txn(
                cur,
                sheet_id,
                name,
                _source_poll_column_type(name, records),
                default_hidden=default_hidden,
            )
        elif default_hidden:
            cur.execute(
                "UPDATE columns SET default_hidden=1 WHERE id=?", (col_ids[name],)
            )
    return col_ids


def _insert_source_poll_column_in_txn(
    cur: Any, sheet_id: int, name: str, type: str, *, default_hidden: bool
) -> int:
    existing_hidden = cur.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name=? AND active=1 AND hidden=1",
        (sheet_id, name),
    ).fetchone()
    if existing_hidden:
        cur.execute(
            "UPDATE columns SET hidden=0, type=?, ai_generated=0, default_hidden=?, "
            "current_run_id=NULL WHERE id=?",
            (type, int(default_hidden), existing_hidden["id"]),
        )
        return int(existing_hidden["id"])
    cur.execute(
        "INSERT INTO columns (sheet_id, name, type, ai_generated, format, hidden, "
        "default_hidden, position) VALUES (?, ?, ?, 0, NULL, 0, ?, "
        "(SELECT COALESCE(MAX(position),0)+1 FROM columns WHERE sheet_id=?))",
        (sheet_id, name, type, int(default_hidden), sheet_id),
    )
    return inserted_rowid(cur)


_MEDIA_VISIBLE_SOURCE_COLUMNS = frozenset(
    {"title", "source_url", "published_at", "channel_title", "description"}
)


def _source_poll_column_default_hidden(source_kind: str, name: str) -> bool:
    return source_kind in {
        "youtube_channel",
        "youtube_playlist",
        "tiktok_profile",
    } and name not in (_MEDIA_VISIBLE_SOURCE_COLUMNS)


def _source_poll_row(
    source_id: int,
    source_run_id: int,
    item: SourcePollItem,
) -> dict[str, Any]:
    row = dict(item.row)
    row["source_id"] = source_id
    row["source_run_id"] = source_run_id
    row["source_item_id"] = item.stable_item_id()
    row.setdefault("source_url", item.url)
    row.setdefault("source_raw", item.raw)
    if item.title is not None:
        row.setdefault("title", item.title)
    if item.published_at is not None:
        row.setdefault("published_at", item.published_at)
    if item.updated_at is not None:
        row.setdefault("updated_at", item.updated_at)
    return row


def _source_poll_column_order(records: list[dict[str, Any]]) -> list[str]:
    preferred = [
        "source_id",
        "source_run_id",
        "source_item_id",
        "title",
        "source_url",
        "published_at",
        "updated_at",
        "source_raw",
        "_revision",
        "_revises",
    ]
    names: list[str] = []
    for name in preferred:
        if any(name in record for record in records):
            names.append(name)
    for record in records:
        for name in record:
            if name not in names:
                names.append(name)
    return names


def _source_poll_column_type(name: str, records: list[dict[str, Any]]) -> str:
    if name.endswith("_url") or name in {"url", "source_url", "link"}:
        return "link"
    if name in {"source_id", "source_run_id", "_revision"}:
        return "integer"
    for record in records:
        value = record.get(name)
        if isinstance(value, (dict, list)):
            return "json"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int) and not isinstance(value, bool):
            return "integer"
    return "text"


def _source_poll_cost_micro(cost: dict[str, Any] | None) -> int:
    if not cost:
        return 0
    if isinstance(cost.get("cost_micro"), int):
        return int(cost["cost_micro"])
    if isinstance(cost.get("cost_usd"), (int, float)):
        return int(float(cost["cost_usd"]) * 1_000_000)
    return 0


def _duration_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _source_config_dict(config: Any) -> dict[str, Any]:
    if isinstance(config, dict):
        return dict(config)
    if isinstance(config, str):
        try:
            loaded = json.loads(config or "{}")
        except (TypeError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _latest_source_run_after(project: Project, source_id: int, run_id: int):
    return SourceStore(project).latest_source_run_after(source_id, run_id)


def _json_object_len(raw: str) -> int:
    if not raw:
        return 0
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return 0
    return len(data) if isinstance(data, dict) else 0


def _source_poll_refs(
    *,
    source_before: dict[str, Any],
    source_after: dict[str, Any],
    summary: dict[str, Any],
    params_hash: str,
) -> dict[str, dict[str, Any]]:
    source_id = int(summary["source_id"])
    source_kind = str(summary.get("source_kind") or source_after.get("kind") or "")
    sheet_id = summary.get("sheet_id")
    row_ids = list(summary.get("row_ids") or [])
    enclosure_row_ids = list(summary.get("enclosure_row_ids") or [])
    cursor_before = str(summary.get("cursor_before") or "")
    cursor_after = str(summary.get("cursor_after") or "")
    source_ref = {
        "kind": "source_poll_source",
        "source_id": source_id,
        "name": source_after.get("name") or source_before.get("name"),
        "source_kind": source_kind,
        "url_hash": _text_hash(str(source_after.get("url") or "")),
        "sheet_id_before": source_before.get("sheet_id"),
        "sheet_id": source_after.get("sheet_id"),
        "cursor_before_hash": _text_hash(cursor_before),
        "cursor_after_hash": _text_hash(cursor_after),
        "params_hash": params_hash,
    }
    source_run_ref = {
        "kind": "source_poll_run",
        "source_id": source_id,
        "source_kind": source_kind,
        "source_run_id": summary.get("source_run_id"),
        "status": summary.get("status"),
        "new_rows": int(summary.get("new_rows") or 0),
        "skipped_rows": int(summary.get("skipped_rows") or 0),
        "changed_rows": int(summary.get("changed_rows") or 0),
        "revisions": int(summary.get("revisions") or 0),
        "materialized_rows": int(summary.get("materialized_rows") or 0),
        "warning_count": int(summary.get("warning_count") or 0),
        "duration_ms": summary.get("duration_ms"),
        "cost_micro": int(summary.get("cost_micro") or 0),
        "error": summary.get("error"),
        "sheet_id": sheet_id,
        "op_id": summary.get("op_id"),
    }
    return {
        "source": source_ref,
        "request": {
            "kind": "source_poll_request",
            "source_id": source_id,
            "source_kind": source_kind,
            "external_api": True,
            "params_hash": params_hash,
        },
        "source_run": source_run_ref,
        "sheet": {
            "kind": "source_sheet",
            "source_id": source_id,
            "sheet_id": sheet_id,
            "source_run_id": summary.get("source_run_id"),
        },
        "rows": {
            "kind": "source_poll_rows",
            "source_id": source_id,
            "source_kind": source_kind,
            "source_run_id": summary.get("source_run_id"),
            "sheet_id": sheet_id,
            "row_ids": row_ids,
            "op_id": summary.get("op_id"),
        },
        "cursor": {
            "kind": "source_poll_cursor",
            "source_id": source_id,
            "source_run_id": summary.get("source_run_id"),
            "before_hash": _text_hash(cursor_before),
            "after_hash": _text_hash(cursor_after),
        },
        "artifacts": {
            "kind": "source_poll_artifacts",
            "source_id": source_id,
            "source_run_id": summary.get("source_run_id"),
            "artifacts": list(summary.get("artifacts") or []),
        },
        "enclosures": {
            "kind": "source_poll_enclosure_pointers",
            "source_id": source_id,
            "source_kind": source_kind,
            "source_run_id": summary.get("source_run_id"),
            "sheet_id": sheet_id,
            "row_ids": enclosure_row_ids,
            "downloaded": False,
            "may_feed": ["media.enclosure_materialize"],
        },
    }


def _source_poll_outputs(refs: dict[str, dict[str, Any]]) -> list[ActionOutput]:
    return [
        ActionOutput(
            kind="source",
            name=str(refs["source"].get("name") or "source"),
            sheet_id=refs["source"].get("sheet_id"),
            ref=refs["source"],
        ),
        ActionOutput(
            kind="source_run",
            name="source_run",
            sheet_id=refs["source_run"].get("sheet_id"),
            ref=refs["source_run"],
        ),
        ActionOutput(
            kind="sheet",
            name="source_sheet",
            sheet_id=refs["sheet"].get("sheet_id"),
            ref=refs["sheet"],
        ),
        ActionOutput(
            kind="rows",
            name="rows",
            sheet_id=refs["rows"].get("sheet_id"),
            row_ids=list(refs["rows"].get("row_ids") or []),
            ref=refs["rows"],
        ),
    ]


def _source_poll_receipt(
    *,
    action: _TypedProjectEnvelope,
    action_id: str,
    project_id: str,
    receipt_id: str,
    params_hash: str,
    op_ids: list[int],
    refs: dict[str, dict[str, Any]],
    status: str,
    warnings: list[str],
    provider_use: list[dict[str, Any]],
    errors: list[ActionError],
) -> Receipt:
    return Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        op_ids=op_ids,
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status=status,
        inputs=[
            ReceiptIO(name="source", ref=refs["source"]),
            ReceiptIO(name="request", ref=refs["request"]),
        ],
        outputs=[
            ReceiptIO(name="source", ref=refs["source"]),
            ReceiptIO(name="source_run", ref=refs["source_run"]),
            ReceiptIO(name="sheet", ref=refs["sheet"]),
            ReceiptIO(name="rows", ref=refs["rows"]),
        ],
        provider_use=provider_use,
        evidence=[
            ReceiptEvidence(ref=refs["cursor"]),
            ReceiptEvidence(ref=refs["artifacts"]),
            ReceiptEvidence(ref=refs["enclosures"]),
        ],
        warnings=warnings,
        errors=errors,
    )


def _text_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
