"""One saved/draft sheet selection binding shared by server read surfaces."""

from contextlib import contextmanager
import json


@contextmanager
def open_sheet_selection(project, sheet_id, selection, view_id, *, legacy_scope=()):
    from frisket.features.views.evaluate import open_selection
    from frisket.contracts.view import ViewSelection
    from frisket.features.views.specs import stored_selection, SelectionUnavailable
    from frisket.retrieval.query import RetrievalQueryError
    from frisket.ai.embeddings.similarity import SimilarityError

    try:
        if selection is not None and view_id is not None:
            raise ValueError("Use selection or view_id, not both")
        if any(value is not None for value in legacy_scope):
            raise ValueError(
                "Selection contains its own filters, order and parent scope"
            )
        if view_id is not None:
            view = project.get_view(view_id)
            if view is None:
                raise ValueError("View is unavailable")
            definition = stored_selection(json.loads(view["spec"]))
        else:
            definition = (
                json.loads(selection) if isinstance(selection, str) else selection
            )
        validated = ViewSelection.model_validate(definition)
        if validated.scope.kind != "sheet" or validated.scope.sheet_id != sheet_id:
            raise ValueError("Selection must target this sheet")
        with open_selection(project, definition) as selected:
            yield selected
    except (
        ValueError,
        SelectionUnavailable,
        RetrievalQueryError,
        SimilarityError,
    ) as exc:
        raise ValueError(str(exc)) from exc
