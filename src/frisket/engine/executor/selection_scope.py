"""Resolve authored View selection once; workers consume its frozen row scope."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, SheetRows
from frisket.contracts.view import ViewSelection
from frisket.engine.views.specs import SelectionUnavailable, stored_selection


def stored_view_id_from_runner_spec(runner_spec: Mapping[str, Any]) -> int | None:
    """Return the persisted View id on a runner spec, if view-scoped."""
    row_scope = runner_spec.get("row_scope")
    if not isinstance(row_scope, Mapping) or row_scope.get("kind") != "view":
        return None
    view_id = row_scope.get("view_id")
    if isinstance(view_id, bool) or not isinstance(view_id, int) or view_id <= 0:
        raise ValueError("stored row_scope view_id must be a positive integer")
    return view_id


def evaluate_sheet_selection_row_ids(
    project: Any,
    *,
    sheet_id: int,
    selection: dict[str, Any],
) -> list[int]:
    """Evaluate a validated sheet-scoped ViewSelection to exact sorted row IDs."""
    validated = ViewSelection.model_validate(selection)
    if validated.scope.kind != "sheet" or validated.scope.sheet_id != sheet_id:
        raise SelectionUnavailable(
            "View must target the action's sheet.",
            code="view_sheet_mismatch",
        )
    from frisket.ai.embeddings.similarity import SimilarityError
    from frisket.engine.views.evaluate import open_selection
    from frisket.retrieval.query import RetrievalQueryError

    try:
        with open_selection(
            project,
            selection,
            embedding_encoder=getattr(project, "_frisket_embedding_runtime", None),
        ) as selected:
            return sorted({int(hit["row_id"]) for hit in selected.hits()})
    except SelectionUnavailable:
        raise
    except (RetrievalQueryError, SimilarityError) as exc:
        raise SelectionUnavailable(
            f"Selection unavailable: {exc}",
            code=getattr(exc, "code", "selection_unavailable"),
        ) from exc


def evaluate_sheet_view_row_ids(
    project: Any,
    *,
    sheet_id: int,
    view_id: int,
) -> list[int]:
    """Load, validate sheet scope, and evaluate a View to exact sorted row IDs."""
    view = project.get_view(view_id)
    if view is None:
        raise SelectionUnavailable(
            f"View {view_id} was not found.",
            code="view_not_found",
        )
    if view["sheet_id"] is not None and int(view["sheet_id"]) != sheet_id:
        raise SelectionUnavailable(
            "View must target the action's sheet.",
            code="view_sheet_mismatch",
        )
    try:
        raw_spec = json.loads(view["spec"])
    except (TypeError, ValueError) as exc:
        raise SelectionUnavailable(
            "This view has an invalid selection definition.",
            code="selection_unavailable",
        ) from exc
    if not isinstance(raw_spec, dict):
        raise SelectionUnavailable(
            "This view has an invalid selection definition.",
            code="selection_unavailable",
        )
    definition = stored_selection(raw_spec)
    return evaluate_sheet_selection_row_ids(
        project,
        sheet_id=sheet_id,
        selection=definition,
    )


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
    if not isinstance(scope, SheetRows) or (
        scope.selection is None and scope.view_id is None
    ):
        if bound.authored_scope is not None:
            return bound
        raise ValueError("Frozen selection requires an authored View selection")
    rows = tuple(sorted({int(row_id) for row_id in row_ids}))
    if rows:
        resolved = SheetRows(sheet_id=scope.sheet_id, row_ids=rows)
    else:
        if scope.view_id is None:
            raise ValueError("Selection contains no rows")
        # A saved View may legitimately match zero rows at estimate or run time.
        # Keep the empty cohort frozen without permitting empty row_ids in the
        # public request schema.
        resolved = SheetRows(sheet_id=scope.sheet_id).model_copy(update={"row_ids": ()})
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
        or (scope.selection is None and scope.view_id is None)
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

    prior = existing.parsed() if existing is not None else None
    if (
        prior is not None
        and prior.run_id is not None
        and prior.params_hash == typed_request_hash(bound)
    ):
        from frisket.engine.store.runs import RunResultStore

        rows = RunResultStore(project).run_row_scope_if_present(prior.run_id)
        if rows is not None:
            return freeze_action_selection(bound, rows)
    if scope.view_id is not None:
        return freeze_action_selection(
            bound,
            evaluate_sheet_view_row_ids(
                project,
                sheet_id=scope.sheet_id,
                view_id=scope.view_id,
            ),
        )
    assert scope.selection is not None
    return freeze_action_selection(
        bound,
        evaluate_sheet_selection_row_ids(
            project,
            sheet_id=scope.sheet_id,
            selection=scope.selection.model_dump(mode="json", exclude_none=True),
        ),
    )
