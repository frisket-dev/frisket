"""Resolve authored View selection once; workers consume its frozen row scope."""

from dataclasses import replace
from typing import Any, Iterable

from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, SheetRows


def authored_request(bound: BoundTypedActionRequest) -> ActionRequest:
    return (
        bound.request
        if bound.authored_scope is None
        else bound.request.model_copy(update={"scope": bound.authored_scope})
    )


def freeze_action_selection(
    bound: BoundTypedActionRequest, row_ids: Iterable[int]
) -> BoundTypedActionRequest:
    scope = bound.request.scope
    if not isinstance(scope, SheetRows) or scope.selection is None:
        if bound.authored_scope is not None:
            return bound
        raise ValueError("Frozen selection requires an authored View selection")
    rows = tuple(row_ids)
    if not rows:
        raise ValueError("Selection contains no rows")
    resolved = SheetRows(sheet_id=scope.sheet_id, row_ids=rows)
    request = bound.request.model_copy(update={"scope": resolved})
    # Existing action restrictions (all rows, single-row, bounded selection)
    # apply to the resolved cohort too; no authoring path silently widens it.
    bound.action.bind_request(request)
    return replace(
        bound,
        request=request,
        authored_scope=scope,
    )


def resolve_action_selection(
    project: Any,
    bound: BoundTypedActionRequest,
    *,
    replay_before_resolution: bool = False,
) -> BoundTypedActionRequest:
    scope = bound.request.scope
    if (
        not isinstance(scope, SheetRows)
        or scope.selection is None
        or bound.authored_scope is not None
    ):
        return bound
    from frisket.engine.store.receipts import ReceiptStore

    existing = ReceiptStore(project).find_by_idempotency_key(
        bound.request.idempotency_key
    )
    # Each action's existing replay handler remains authoritative (including
    # output existence checks). Do not consult a mutable selection first.
    if existing is not None and replay_before_resolution:
        return bound
    from frisket.engine.executor.map_rows_action import typed_request_hash

    if (
        existing is not None
        and existing.parsed().run_id is not None
        and existing.parsed().params_hash == typed_request_hash(bound)
    ):
        from frisket.engine.store.runs import RunResultStore

        rows = RunResultStore(project).run_row_scope_if_present(
            existing.parsed().run_id
        )
        if rows is not None:
            return freeze_action_selection(bound, rows)
    from frisket.features.views.evaluate import open_selection
    from frisket.retrieval.query import RetrievalQueryError
    from frisket.ai.embeddings.similarity import SimilarityError

    try:
        with open_selection(
            project, scope.selection.model_dump(mode="json", exclude_none=True)
        ) as selected:
            return freeze_action_selection(
                bound, (int(hit["row_id"]) for hit in selected.hits())
            )
    except (RetrievalQueryError, SimilarityError) as exc:
        raise ValueError(f"Selection unavailable: {exc}") from exc
