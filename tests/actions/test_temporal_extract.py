import pytest

from frisket.actions.core import RegisteredAction, _ProjectAction
from frisket.actions.temporal_extract import EXTRACT_RANGE
from frisket.actions.temporal_extract_types import TemporalExtractor
from frisket.actions.types import ActionRequest


def request(**changes):
    return ActionRequest.model_validate(
        {
            "action_id": "temporal.extract_range",
            "scope": {"kind": "sheet_rows", "sheet_id": 1, "row_ids": [1]},
            "params": {
                "source": "media",
                "selection": {"kind": "draft_range", "start_ms": 0, "end_ms": 1000},
            },
            "output_names": {"clip": "excerpt"},
            "idempotency_key": "extract-contract",
            **changes,
        }
    )


def test_extract_declares_prepared_callable_and_runtime_output_schema():
    registered = RegisteredAction("temporal.extract_range", EXTRACT_RANGE)
    params, outputs = registered.bind_request(request())
    assert isinstance(EXTRACT_RANGE.run, _ProjectAction)
    assert EXTRACT_RANGE.run.capabilities == (TemporalExtractor,)
    assert params.source.root == "media"
    assert outputs is None


def test_literal_batch_requires_explicit_repeat_acknowledgment():
    registered = RegisteredAction("temporal.extract_range", EXTRACT_RANGE)
    body = request(scope={"kind": "sheet_rows", "sheet_id": 1, "row_ids": [1, 2]})
    with pytest.raises(ValueError, match="literal_selection_requires_confirmation"):
        registered.bind_request(body)
    body = body.model_copy(
        update={
            "params": {
                **body.params,
                "selection": {**body.params["selection"], "repeat_for_rows": True},
            }
        }
    )
    registered.bind_request(body)
    with pytest.raises(ValueError, match="explicit rows"):
        registered.bind_request(request(scope={"kind": "sheet_rows", "sheet_id": 1}))


def test_extract_rejects_split_shapes_and_project_scope():
    registered = RegisteredAction("temporal.extract_range", EXTRACT_RANGE)
    with pytest.raises(ValueError):
        registered.bind_request(
            request(
                params={
                    "source": "media",
                    "selection": {"kind": "draft_points", "items": [{"at_ms": 50}]},
                }
            )
        )
    with pytest.raises(ValueError, match="sheet_rows"):
        registered.bind_request(request(scope={"kind": "project"}))
