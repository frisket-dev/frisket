"""Ask accepts explicit project sources, never execution authority or paths."""

import pytest
from pydantic import ValidationError

from frisket.contracts.http.project_qa import AskScope, AskTurnRequest


def test_turn_snapshots_existing_source_scope_and_options():
    request = AskTurnRequest.model_validate(
        {
            "request_id": "first-question",
            "question": "What changed?",
            "scope": {
                "kind": "sources",
                "sources": [
                    {"kind": "rows", "sheet_id": 1, "row_ids": [2, 3]},
                    {"kind": "file", "sheet_id": 4, "row_id": 5, "column_id": 6},
                ],
            },
            "web": False,
            "suggest_actions": True,
        }
    )
    assert request.scope.kind == "sources"
    assert request.model is None
    assert request.web is False


@pytest.mark.parametrize(
    "extra", [{"confirmed": True}, {"execute": True}, {"project_id": "other"}]
)
def test_question_cannot_grant_write_authority_or_change_project(extra):
    with pytest.raises(ValidationError):
        AskTurnRequest.model_validate(
            {
                "request_id": "question",
                "question": "Read this",
                "scope": {"kind": "project"},
                **extra,
            }
        )


@pytest.mark.parametrize(
    "scope",
    [
        {"kind": "sources", "sources": []},
        {"kind": "project", "sources": [{"kind": "sheet", "sheet_id": 1}]},
        {"kind": "sources", "sources": [{"kind": "file", "path": "/secret"}]},
        {
            "kind": "sources",
            "sources": [{"kind": "rows", "sheet_id": 1, "row_ids": [1, 1]}],
        },
        {
            "kind": "sources",
            "sources": [
                {"kind": "rows", "sheet_id": 1, "row_ids": list(range(1, 602))},
                {"kind": "rows", "sheet_id": 2, "row_ids": list(range(1, 602))},
            ],
        },
    ],
)
def test_invalid_or_overlarge_source_scope_is_not_silently_broadened(scope):
    with pytest.raises(ValidationError):
        AskScope.model_validate(scope)
