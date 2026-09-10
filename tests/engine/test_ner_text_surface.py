"""map.ner writes a coordinate surface for entity spans over one exact
raw-string cell. Template/join text keeps
quote evidence but claims no renderable coordinate surface.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

import frisket.ops.ner_evidence as ner_module
from frisket.ops.ner_local import LocalNerExtractor
from typed_model_fixtures import model_request, render_ner
from frisket.ai.llm import ModelRouter
from frisket.engine.store import Project
from frisket.engine.store.evidence import _text_hash, list_cell_evidence
from frisket.ops.ner_evidence import (
    capture_result_evidence,
    _NER_CAPTURE_PREFIX,
    _NER_EVIDENCE_KEY,
)

PROJECT_ID = "proj-ner-surface"


class _Resp:
    status_code = 200
    text = "ok"

    def __init__(self, entities: list[dict[str, Any]]) -> None:
        self._entities = entities

    def json(self) -> dict[str, Any]:
        return {"results": [self._entities]}


class _FixedSidecar:
    is_closed = False

    def __init__(self, entities: list[dict[str, Any]]) -> None:
        self._entities = entities

    async def post(self, url: str, **kwargs: Any) -> _Resp:
        return _Resp(self._entities)


def _router_with(entities: list[dict[str, Any]]) -> ModelRouter:
    router = ModelRouter(cache=None, cache_mode="off")
    router._client = _FixedSidecar(entities)  # noqa: SLF001
    return router


def _action(
    sheet_id: int, input_columns: list[str], template: str | None = None
) -> dict:
    return model_request(
        {
            "action_kind": "map.ner",
            "sheet_id": sheet_id,
            "input_columns": input_columns,
            "input_template": template,
            "labels": ["person"],
            "engine": "gliner",
            "idempotency_key": "ner_surface@sha256:stable",
        }
    ).model_dump(mode="json", exclude_none=True)


def _run(
    tmp_path, monkeypatch, *, columns, row, entities, input_columns, template=None
):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")
    from frisket.engine.executor import actions as executor_actions

    path = tmp_path / "ner-surface.frisket"
    project = Project.create(path, name="s")
    sheet_id = project.add_sheet("data")
    col_ids = {
        name: project.add_column(sheet_id, name, type="text") for name in columns
    }
    project.add_rows(sheet_id, [row], col_ids)
    project.close()

    project = Project(path)
    result = executor_actions.run_action_spec(
        project,
        _action(sheet_id, input_columns, template),
        project_id=PROJECT_ID,
        router=_router_with(entities),
    )
    assert result.status == "completed", result.errors
    return project, sheet_id, col_ids


def _surfaces(project) -> list[dict]:
    return [dict(r) for r in project.db.execute("SELECT * FROM text_surfaces")]


def _spans(project) -> list[dict]:
    return [dict(r) for r in project.db.execute("SELECT * FROM source_spans")]


def test_single_column_writes_cell_surface(tmp_path, monkeypatch):
    project, sheet_id, col_ids = _run(
        tmp_path,
        monkeypatch,
        columns=["body"],
        row={"body": "Ada Lovelace"},
        input_columns=["body"],
        entities=[{"text": "Ada Lovelace", "label": "person", "start": 0, "end": 12}],
    )
    try:
        surfaces = _surfaces(project)
        assert len(surfaces) == 1
        s = surfaces[0]
        assert s["surface_kind"] == "cell"
        assert s["text_sheet_id"] == sheet_id
        assert s["text_column_id"] == col_ids["body"]
        assert s["content_hash"] == _text_hash("Ada Lovelace")
        assert s["offset_unit"] == "unicode_codepoint"
        # the span references the surface
        spans = _spans(project)
        assert spans and all(sp["text_surface_id"] == s["id"] for sp in spans)
        # the link is an annotation layer
        link = project.db.execute(
            "SELECT layer_family FROM evidence_links WHERE layer_family IS NOT NULL"
        ).fetchone()
        assert link["layer_family"] == "entities"
        roles = {
            r["span_role"]
            for r in project.db.execute("SELECT span_role FROM evidence_link_spans")
        }
        assert roles == {"annotation"}
        assert (
            project.db.execute("SELECT justification FROM results").fetchone()[
                "justification"
            ]
            is None
        )
    finally:
        project.close()


def test_capture_sidecar_never_lingers_in_justification(tmp_path, monkeypatch):
    """Private evidence transport never enters the result justification."""
    project, _, _ = _run(
        tmp_path,
        monkeypatch,
        columns=["body"],
        row={"body": "Ada Lovelace"},
        input_columns=["body"],
        entities=[{"text": "Ada Lovelace", "label": "person", "start": 0, "end": 12}],
    )
    try:
        lingering = project.db.execute(
            "SELECT COUNT(*) AS c FROM results WHERE substr(justification, 1, ?) = ?",
            (len(_NER_CAPTURE_PREFIX), _NER_CAPTURE_PREFIX),
        ).fetchone()["c"]
        assert lingering == 0
    finally:
        project.close()


def test_generation_publication_keeps_ner_cells_and_evidence_immutable(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")
    from frisket.engine.executor import actions as executor_actions

    project = Project.create(tmp_path / "ner-generations.frisket")
    sheet_id = project.add_sheet("data")
    body_column_id = project.add_column(sheet_id, "body", type="text")
    row_ids = project.add_rows(
        sheet_id,
        [{"body": "Ada"}, {"body": "null"}, {"body": "error"}],
        {"body": body_column_id},
    )

    async def execute(self, text, *, labels, threshold):
        if text == "null":
            return []
        if text == "error":
            raise RuntimeError("intentional NER failure")
        return [{"text": "Ada", "type": "person", "start": 0, "end": 3, "score": 0.9}]

    monkeypatch.setattr(LocalNerExtractor, "extract", execute)
    try:
        result = executor_actions.run_action_spec(
            project,
            _action(sheet_id, ["body"]),
            project_id=PROJECT_ID,
            router=ModelRouter(cache=None, cache_mode="off"),
        )
        assert result.status == "partial", result.errors
        run_id = int(result.run_id)
        run = project.db.execute(
            "SELECT op_id FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        op_id = int(run["op_id"])
        output = next(c for c in project.columns(sheet_id) if c["name"] == "entities")
        output_column_id = int(output["id"])
        assert int(output["current_run_id"]) == run_id
        generation = project.db.execute(
            "SELECT state,terminal_disposition FROM run_output_generations "
            "WHERE run_id=? AND column_id=?",
            (run_id, output_column_id),
        ).fetchone()
        assert tuple(generation) == ("sealed", "completed")

        heads = project.db.execute(
            "SELECT head.row_id,result.publication_effect "
            "FROM cell_result_heads head JOIN results result "
            "ON result.run_id=head.run_id AND result.row_id=head.row_id "
            "AND result.column_id=head.column_id WHERE head.column_id=? "
            "ORDER BY head.row_id",
            (output_column_id,),
        ).fetchall()
        assert [(int(row[0]), row[1]) for row in heads] == [
            (row_ids[0], "publish_value"),
            (row_ids[1], "publish_value"),
            (row_ids[2], "publish_error"),
        ]
        result_rows = project.db.execute(
            "SELECT row_id,justification FROM results WHERE run_id=? "
            "AND column_id=? ORDER BY row_id",
            (run_id, output_column_id),
        ).fetchall()
        assert [(int(row[0]), row[1]) for row in result_rows] == [
            (row_ids[0], None),
            (row_ids[1], None),
            (row_ids[2], None),
        ]
        link = project.db.execute(
            "SELECT subject_ref_json FROM evidence_links WHERE row_id=? "
            "AND column_id=? AND status='active'",
            (row_ids[0], output_column_id),
        ).fetchone()
        assert json.loads(link["subject_ref_json"]) == {
            "kind": "run_result",
            "op_id": op_id,
            "row_id": row_ids[0],
            "column_id": output_column_id,
            "run_id": run_id,
        }

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            project.db.execute(
                "UPDATE results SET justification='changed' WHERE run_id=? "
                "AND row_id=? AND column_id=?",
                (run_id, row_ids[0], output_column_id),
            )
        project.db.rollback()

        assert project.undo() == op_id
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM cell_result_heads WHERE column_id=?",
                (output_column_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            list_cell_evidence(
                project,
                sheet_id=sheet_id,
                row_id=row_ids[0],
                column_id=output_column_id,
            )["links"]
            == []
        )

        assert project.redo() == op_id
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM cell_result_heads WHERE column_id=?",
                (output_column_id,),
            ).fetchone()[0]
            == 3
        )
        assert list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_ids[0],
            column_id=output_column_id,
        )["links"]
    finally:
        project.close()


def test_evidence_failure_rolls_back_result_head_and_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")
    from frisket.engine.executor import actions as executor_actions

    project = Project.create(tmp_path / "ner-evidence-rollback.frisket")
    sheet_id = project.add_sheet("data")
    body_column_id = project.add_column(sheet_id, "body", type="text")
    project.add_rows(sheet_id, [{"body": "Ada"}], {"body": body_column_id})

    calls = []

    async def execute(self, text, *, labels, threshold):
        calls.append(text)
        return [{"text": "Ada", "type": "person", "start": 0, "end": 3, "score": 0.9}]

    original = ner_module._write_ner_result_evidence

    def fail_after_evidence(*args, **kwargs):  # noqa: ANN002, ANN003
        original(*args, **kwargs)
        raise RuntimeError("evidence write failed")

    monkeypatch.setattr(LocalNerExtractor, "extract", execute)
    monkeypatch.setattr(ner_module, "_write_ner_result_evidence", fail_after_evidence)
    try:
        result = executor_actions.run_action_spec(
            project,
            _action(sheet_id, ["body"]),
            project_id=PROJECT_ID,
            router=ModelRouter(cache=None, cache_mode="off"),
        )
        assert result.status == "failed"
        assert result.run_id is not None and result.receipt_id is not None
        assert [error.code for error in result.errors] == ["project_write_failed"]
        assert result.errors[0].message == "project write failed"
        assert len(calls) == 1
        assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
        assert (
            project.db.execute("SELECT COUNT(*) FROM cell_result_heads").fetchone()[0]
            == 0
        )
        assert (
            project.db.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0] == 0
        )
        assert (
            project.db.execute("SELECT COUNT(*) FROM source_spans").fetchone()[0] == 0
        )
        monkeypatch.setattr(ner_module, "_write_ner_result_evidence", original)
        before = tuple(project.db.iterdump())
        replay = executor_actions.run_action_spec(
            project,
            _action(sheet_id, ["body"]),
            project_id=PROJECT_ID,
            router=ModelRouter(cache=None, cache_mode="off"),
        )
        assert replay.model_dump() == result.model_dump()
        assert len(calls) == 1
        assert tuple(project.db.iterdump()) == before
    finally:
        project.close()


def test_multi_column_degrades_to_quote_only(tmp_path, monkeypatch):
    # _text joins the two columns with "\n": "Ada Lovelace\nBabbage".
    project, _, _ = _run(
        tmp_path,
        monkeypatch,
        columns=["first", "second"],
        row={"first": "Ada Lovelace", "second": "Babbage"},
        input_columns=["first", "second"],
        entities=[{"text": "Ada Lovelace", "label": "person", "start": 0, "end": 12}],
    )
    try:
        assert _surfaces(project) == []
        spans = _spans(project)
        assert len(spans) == 1
        assert spans[0]["quote"] == "Ada Lovelace"
        assert spans[0]["char_start"] is None
        assert spans[0]["text_surface_id"] is None
    finally:
        project.close()


def test_template_input_is_rendered_once_and_degrades_to_quote_only(
    tmp_path, monkeypatch
):
    project, _, _ = _run(
        tmp_path,
        monkeypatch,
        columns=["body"],
        row={"body": "Ada Lovelace"},
        input_columns=["body"],
        template="Name: {{body}}",
        entities=[{"text": "Ada Lovelace", "label": "person", "start": 6, "end": 18}],
    )
    try:
        assert _surfaces(project) == []
        span = _spans(project)[0]
        assert span["quote"] == "Ada Lovelace"
        assert span["char_start"] is None
    finally:
        project.close()


def test_ner_template_is_not_rendered_twice():
    from frisket.ops.ner_text import ner_text

    assert ner_text({"input": "Name: Ada Lovelace"}) == "Name: Ada Lovelace"


def test_llm_prompt_user_content_is_exact_source_text():
    rendered = render_ner(
        {"body": "Ada wrote notes."},
        {"engine": "llm", "labels": ["person"], "output_name": "entities"},
    )
    assert rendered.messages[1] == {
        "role": "user",
        "content": [{"type": "text", "text": "Ada wrote notes."}],
    }
    assert "Extract" in rendered.messages[0]["content"]


@pytest.mark.parametrize(
    ("source", "model_start", "model_end", "expected"),
    [
        ("Lead: Ada", 0, 3, [[[6, 9]]]),
        ("Ada   Ada", 6, 9, [[[0, 3]], [[6, 9]]]),
    ],
)
def test_llm_capture_uses_all_exact_quotes_not_model_offsets(
    source, model_start, model_end, expected
):
    entities = [
        {
            "text": "Ada",
            "type": "person",
            "start": model_start,
            "end": model_end,
            "score": None,
        }
    ]
    results = {"entities": {"value": entities}}
    capture_result_evidence(
        {"body": source},
        {
            "body": {
                "column_id": 7,
                "value": source,
                "value_ref": {
                    "kind": "source_cell",
                    "row_id": 2,
                    "column_id": 7,
                    "run_id": None,
                    "op_id": None,
                },
            }
        },
        results,
        {"engine": "llm", "output_name": "entities"},
        run_id=7,
        op_id=8,
        row_id=9,
        output_columns={"entities": 10},
    )
    payload = results["entities"][_NER_EVIDENCE_KEY]
    assert payload["positions"] == expected


def test_offset_mismatch_entity_is_not_positioned_but_kept(tmp_path, monkeypatch):
    # Two entities; the second's offsets (0:7) slice "Ada Lov", not "Babbage". Both
    # keep a span (capability refinement evidence), but only the matching one gets a surface + the
    # annotation role.
    project, _, _ = _run(
        tmp_path,
        monkeypatch,
        columns=["body"],
        row={"body": "Ada Lovelace"},
        input_columns=["body"],
        entities=[
            {"text": "Ada Lovelace", "label": "person", "start": 0, "end": 12},
            {"text": "Babbage", "label": "person", "start": 0, "end": 7},
        ],
    )
    try:
        spans = _spans(project)
        assert (
            len(spans) == 2
        )  # capability refinement preserved: both entities keep a span
        by_quote = {sp["quote"]: sp for sp in spans}
        assert by_quote["Ada Lovelace"]["text_surface_id"] is not None
        assert by_quote["Babbage"]["text_surface_id"] is None  # not positionable
        roles = {
            r["quote"]: r["span_role"]
            for r in project.db.execute(
                "SELECT sp.quote, els.span_role FROM source_spans sp "
                "JOIN evidence_link_spans els ON els.span_id = sp.id"
            )
        }
        assert roles == {"Ada Lovelace": "annotation", "Babbage": "support"}
    finally:
        project.close()


def test_duplicate_text_entities_each_keep_a_span_when_unpositioned(
    tmp_path, monkeypatch
):
    # Templated input degrades to quote-only (unpositioned): every entity's
    # position is None. Three distinct "Ada" mentions must each keep their own
    # span -- the stored entity list and evidence spans agree.
    project, _, _ = _run(
        tmp_path,
        monkeypatch,
        columns=["body"],
        row={"body": "Ada and Ada and Ada"},
        input_columns=["body"],
        template="Name: {{body}}",
        entities=[
            {"text": "Ada", "label": "person", "start": 0, "end": 3},
            {"text": "Ada", "label": "person", "start": 8, "end": 11},
            {"text": "Ada", "label": "person", "start": 16, "end": 19},
        ],
    )
    try:
        stored = json.loads(
            project.db.execute("SELECT value FROM results").fetchone()["value"]
        )
        assert len(stored) == 3
        spans = _spans(project)
        assert len(spans) == len(stored)
        assert all(sp["quote"] == "Ada" for sp in spans)
        assert all(sp["char_start"] is None for sp in spans)
    finally:
        project.close()


def test_no_positionable_entities_writes_spans_without_a_layer(tmp_path, monkeypatch):
    """Invalid deterministic geometry remains quote evidence, never a layer."""
    project, _, _ = _run(
        tmp_path,
        monkeypatch,
        columns=["body"],
        row={"body": "Ada Lovelace"},
        input_columns=["body"],
        entities=[{"text": "Ada Lovelace", "label": "person", "start": 5, "end": 40}],
    )
    try:
        assert len(_spans(project)) == 1
        assert _surfaces(project) == []
        link = project.db.execute("SELECT layer_family FROM evidence_links").fetchone()
        assert link["layer_family"] is None
    finally:
        project.close()
