from __future__ import annotations

import pytest

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project


@pytest.fixture
def ner_project(tmp_path, monkeypatch):
    from frisket.ops import spacy_ner

    monkeypatch.setattr(spacy_ner, "spacy_available", lambda: (True, None))
    monkeypatch.setattr(
        spacy_ner,
        "run_spacy_ner_default",
        lambda *args, **kwargs: [
            [{"text": "Alice", "type": "person", "start": 0, "end": 5, "score": None}]
        ],
    )
    project = Project.create(tmp_path / "typed-output-compatibility.frisket")
    sheet = project.add_sheet("People")
    source = project.add_column(sheet, "text", type="text")
    rows = project.add_rows(sheet, [{"text": "Alice"}], {"text": source})
    yield project, sheet, rows
    project.close()


def _request(
    sheet: int,
    *,
    key: str,
    replace: bool = False,
    row_ids: list[int] | None = None,
) -> dict:
    request = {
        "action_id": "map.ner",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"source": ["text"], "labels": ["person"], "engine": "spacy"},
        "output_names": {"entities": "mentions"},
        "replace_existing": replace,
        "idempotency_key": key,
    }
    if row_ids is not None:
        request["scope"]["row_ids"] = row_ids
    return request


def test_replacement_accepts_the_declared_semantic_type(ner_project):
    project, sheet, _ = ner_project
    first = run_action_spec(project, _request(sheet, key="ner-first"), project_id="p")
    assert first.status == "completed", first.errors
    column = next(
        column for column in project.columns(sheet) if column["name"] == "mentions"
    )

    replacement = run_action_spec(
        project,
        _request(sheet, key="ner-replacement", replace=True),
        project_id="p",
    )

    assert replacement.status == "completed", replacement.errors
    assert (
        next(
            column for column in project.columns(sheet) if column["name"] == "mentions"
        )["id"]
        == column["id"]
    )


def test_whole_column_replacement_repairs_an_incompatible_semantic_type(ner_project):
    project, sheet, _ = ner_project
    first = run_action_spec(project, _request(sheet, key="ner-first"), project_id="p")
    assert first.status == "completed", first.errors
    column = next(
        column for column in project.columns(sheet) if column["name"] == "mentions"
    )
    project.set_column_semantic_type(int(column["id"]), "incompatible")

    replacement = run_action_spec(
        project,
        _request(sheet, key="ner-incompatible", replace=True),
        project_id="p",
    )

    assert replacement.status == "completed", replacement.errors
    repaired = next(
        column for column in project.columns(sheet) if column["name"] == "mentions"
    )
    assert repaired["semantic_type"] == "entity_mentions"


def test_selected_row_replacement_refuses_an_incompatible_semantic_type(ner_project):
    project, sheet, rows = ner_project
    first = run_action_spec(project, _request(sheet, key="ner-first"), project_id="p")
    assert first.status == "completed", first.errors
    column = next(
        column for column in project.columns(sheet) if column["name"] == "mentions"
    )
    project.set_column_semantic_type(int(column["id"]), "incompatible")

    replacement = run_action_spec(
        project,
        _request(
            sheet,
            key="ner-incompatible-selected",
            replace=True,
            row_ids=rows,
        ),
        project_id="p",
    )

    assert replacement.status == "failed"
    assert replacement.errors[0].code == "output_column_exists"
