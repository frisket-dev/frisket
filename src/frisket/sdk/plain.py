"""Wire a PLAIN action (receipt-only, no child sheet) onto the composable core.

Plain actions are the leanest lifecycle shape: an in-txn read/validate that produces a
receipt (and optionally a project write), with no target-sheet/duplicate concepts. The op
declares an optional best-effort pre-txn resolve plus an in-txn perform that does the
reads/validation, any project writes, the receipt insert (`insert_completed(commit=False)`)
and returns the ActionResult; this builder wires those hand-written callables onto an
`_ActionCoreSpec` (body_kind="plain") and runs the shared core. The core owns idempotency
replay (via the reused deterministic `result_from_existing_fn`, with optional staleness
validation) and the BEGIN IMMEDIATE/recheck/commit envelope; the op stays ignorant of the
receipt schema and the txn boundary. Mirrors
`build_deterministic_owned_write_child_sheet_run_fn` for the no-child-sheet case.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from frisket.contracts.action import ActionError, ActionResult


def build_plain_action_run_fn(
    *,
    kind: str,
    params_model: Any,
    perform_in_txn_fn: Callable[..., ActionResult],
    params_hash_fn: Callable[[Any], str],
    replay_validate_fn: Callable[[Any, Any], ActionError | None] | None = None,
    resolve_fn: Callable[[Any, Any], Any] | None = None,
    exception_error_fn: Callable[[Any], ActionError] | None = None,
    cleanup_resolved_fn: Callable[[Any], None] | None = None,
    post_commit_fn: Callable[[Any], None] | None = None,
    persist_failure: bool = False,
    resolve_needs_router: bool = False,
    resolve_needs_edition_context: bool = False,
) -> Callable[..., ActionResult]:
    """The plain action adapter.

    `perform_in_txn_fn(project, cur, action, params, *, project_id,
    action_id, receipt_id, params_hash, resolved) -> ActionResult` is REQUIRED: it runs
    inside the core's transaction, does the in-txn reads/validation + any project writes +
    the receipt insert, and returns the result (a non-completed result makes the core roll
    back, so no receipt is persisted). `resolve_fn(project, params) -> resolved |
    ActionError | None` is the optional best-effort pre-txn resolve. `replay_validate_fn`
    validates staleness of the completed receipt on idempotency replay (the outer replay);
    leave it unset for ops with no source-staleness to re-check. `exception_error_fn(action)
    -> ActionError` is the optional in-txn exception seam: when set, the core's shared in-txn
    helper builds the rollback error from it instead of the fixed project_write_failed; leave
    it unset for ops that surface the default project_write_failed. `cleanup_resolved_fn(resolved)
    -> None` is the optional cleanup-on-rollback seam: when the perform stages out-of-txn side
    effects (filesystem artifacts moved into place), the core calls it on every non-commit path
    (in-txn idempotency-race skip, exception/rollback, non-completed rollback) to undo exactly
    what the perform recorded on the (mutable) resolved value; leave it unset for ops with no
    out-of-txn side effects to undo. `post_commit_fn(resolved) -> None` is the optional
    post-commit success-finalize seam: the core calls it ONLY on the committed-success path,
    after the commit succeeds, to finalize a staged side effect that a committed receipt should
    keep (the local exports' .bak removal); leave it unset for ops with nothing to finalize.
    `persist_failure` (default False) keeps the deterministic plain semantics (commit only a
    `completed` result, roll back receiptless otherwise). Set True for background/scheduled ops
    (source.poll) that want a `failed` result to ALSO commit, persisting the failed receipt + a
    failure trace as a durable record; on that path the perform must write only the failure
    record, not partial project mutations."""

    def run_fn(
        project: Any,
        action: Any,
        params: Any,
        *,
        project_id: str,
        router: Any = None,
        edition_run_context: Mapping[str, Any] | None = None,
    ) -> ActionResult:
        from frisket.engine.executor.action_inventory import (
            ExecutorContext,
            ExecutorDeps,
            _ActionCoreSpec,
        )
        from frisket.engine.executor.action_lifecycle import (
            _child_sheet_deterministic_result_from_existing,
            _run_action_core_spec,
        )

        spec = _ActionCoreSpec(
            kind=kind,
            params_model=params_model,
            body_kind="plain",
            params_hash_fn=params_hash_fn,
            result_from_existing_fn=_child_sheet_deterministic_result_from_existing(
                replay_validate_fn=replay_validate_fn
            ),
            plain_resolve_fn=resolve_fn,
            plain_resolve_needs_router=resolve_needs_router,
            plain_resolve_needs_edition_context=resolve_needs_edition_context,
            plain_perform_in_txn_fn=perform_in_txn_fn,
            plain_exception_error_fn=exception_error_fn,
            plain_cleanup_resolved_fn=cleanup_resolved_fn,
            plain_post_commit_fn=post_commit_fn,
            plain_persist_failure=persist_failure,
        )
        ctx = ExecutorContext(
            project_id=project_id,
            deps=ExecutorDeps(router=router),
            edition_run_context=edition_run_context,
        )
        return _run_action_core_spec(project, action, params, spec=spec, ctx=ctx)

    return run_fn
