from contextlib import closing

import pytest
from pydantic import BaseModel, Field, create_model

from frisket.actions.core import ActionCategory, RegisteredAction, action, map_rows
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import ActionParams, ColumnRef, Outcome, Row, RowResult
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.recordsets import is_feedable_named_result_ref
from frisket.engine.store import Project
from frisket.sdk.ops import transcribe_engines


class Params(ActionParams):
    source: ColumnRef[str]
    fail_sibling: bool = False
    prefix: str = ""


class Item(BaseModel):
    text: str


class Output(BaseModel):
    items: list[Item] = Field(
        json_schema_extra={
            "named_result": {"schema": "items", "may_feed": ["derive.table_from_list"]}
        }
    )
    sibling: Outcome[str]


def collect(params: Params, row: Row) -> RowResult[Output]:
    return RowResult(
        output=Output(
            items=[Item(text=params.prefix + params.source.read(row))],
            sibling=Outcome.failed("sibling_failed", "Sibling failed")
            if params.fail_sibling
            else Outcome.ok("ok"),
        )
    )


@pytest.fixture
def project(tmp_path, monkeypatch):
    registered = RegisteredAction(
        "custom.collect",
        action(
            name="collect",
            title="Collect",
            description="Collect rows.",
            category=ActionCategory.EXTRACT,
            run=map_rows(collect),
        ),
    )
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {
            **ACTION_REGISTRY._actions,
            registered.action_id: registered,
        },
    )
    with closing(Project.create(tmp_path / "named.frisket")) as project:
        yield project


def run(project, body):
    result = run_action_spec(project, body, project_id="named")
    if result.status == "needs_confirmation":
        result = run_action_spec(
            project,
            {
                **body,
                "confirmation": result.errors[0].details["promise_set_hash"],
            },
            project_id="named",
        )
    return result


def derive(project, named):
    ref = named.ref
    return run(
        project,
        {
            "action_id": "derive.table_from_list",
            "scope": {"kind": "project"},
            "sheet_name": "Expanded",
            "idempotency_key": "expand",
            "params": {
                "source": {
                    "kind": "named_result",
                    **{
                        key: ref[key]
                        for key in (
                            "sheet_id",
                            "column_id",
                            "run_id",
                            "route",
                            "schema",
                        )
                    },
                },
                "item_schema": ref["item_schema"],
                "columns": [{"name": "text", "path": "$.text", "type": "text"}],
            },
        },
    )


def assert_visible_pinned_output(project, result, name):
    named = next(output for output in result.outputs if output.kind == "named_result")
    column = next(
        output
        for output in result.outputs
        if output.kind == "column" and output.name == name
    )
    assert named.name == name
    assert named.ref["column_id"] == column.column_id
    assert not project.db.execute(
        "SELECT hidden FROM columns WHERE id=?", (column.column_id,)
    ).fetchone()[0]
    return named


@pytest.mark.parametrize("kind", ["custom.collect", "media.transcribe"])
def test_static_named_output_reuses_visible_column_and_feeds_pinned_derivation(
    project, monkeypatch, kind
):
    sheet = project.add_sheet("Source")
    asr = kind == "media.transcribe"
    source = project.add_column(sheet, "source", type="audio" if asr else "text")
    digest = (
        project.add_blob(b"audio", mime="audio/wav", filename="interview.wav")
        if asr
        else None
    )
    [row] = project.add_rows(
        sheet,
        [{"source": {"blob": digest, "mime": "audio/wav"} if asr else "Original"}],
        {"source": source},
    )

    spoken = ["Original"]

    async def transcribe(*args, **kwargs):
        return transcribe_engines.TranscriptionEngineResult(
            output={
                "text": spoken[0],
                "segments": [{"text": spoken[0], "start": 0, "end": 1}],
                "language": "en",
                "cost": 0,
            },
            model_calls=(),
        )

    monkeypatch.setattr(transcribe_engines, "run_transcription_engine", transcribe)
    result = run(
        project,
        {
            "action_id": kind,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "source"},
            "output_names": {"segments" if asr else "items": "kept"},
            "idempotency_key": "produce",
        },
    )
    assert result.status == "completed", result.errors
    named = assert_visible_pinned_output(project, result, "kept")
    assert named.ref["row_ids"] == [row]
    # Item schemas keep their definitions when detached from the parent array.
    assert "$defs" in named.ref["item_schema"]
    spoken[0] = "Changed"
    replaced = run(
        project,
        {
            "action_id": kind,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "source", **({} if asr else {"prefix": "Changed "})},
            "output_names": {"segments" if asr else "items": "kept"},
            "replace_existing": True,
            "idempotency_key": "replace",
        },
    )
    assert replaced.status == "completed", replaced.errors
    assert next(iter(project.get_values(sheet, named.ref["column_id"]).values()))[0][
        "text"
    ].startswith("Changed")
    derived = derive(project, named)
    assert derived.status == "completed", derived.errors
    child = next(
        output.sheet_id for output in derived.outputs if output.kind == "sheet"
    )
    text_column = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name='text'", (child,)
    ).fetchone()[0]
    assert list(project.get_values(child, text_column).values()) == ["Original"]


def test_successful_list_is_not_removed_by_failed_sibling(project):
    sheet = project.add_sheet("Source")
    source = project.add_column(sheet, "source", type="text")
    [row] = project.add_rows(sheet, [{"source": "Original"}], {"source": source})
    result = run(
        project,
        {
            "action_id": "custom.collect",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "source", "fail_sibling": True},
            "idempotency_key": "partial",
        },
    )
    named = assert_visible_pinned_output(project, result, "items")
    assert named.ref["row_ids"] == [row]


def test_declared_named_output_ref_cannot_claim_another_schema(project):
    from frisket.engine.executor.recordsets import feedable_named_result_ref

    field = ACTION_REGISTRY.get("custom.collect").definition.run.output_fields[0]
    ref = feedable_named_result_ref(
        source_action_kind="custom.collect",
        sheet_id=1,
        column_id=2,
        run_id=3,
        op_id=4,
        route="renamed",
        schema_name="items",
        schema_json=dict(field.schema),
        row_ids=[1],
        may_feed=["derive.table_from_list"],
        extra={"output_key": "items"},
    )
    kwargs = dict(
        receipt_action_kind="custom.collect",
        sheet_id=1,
        column_id=2,
        run_id=3,
        op_id=4,
        route="renamed",
        schema_name="items",
    )
    assert is_feedable_named_result_ref(ref, **kwargs)
    assert not is_feedable_named_result_ref({**ref, "output_key": "sibling"}, **kwargs)
    assert not is_feedable_named_result_ref(
        {**ref, "schema_json": {"type": "array", "items": {"type": "number"}}}, **kwargs
    )


@pytest.mark.parametrize(
    "annotation,metadata",
    [
        (str, {"schema": "items"}),
        (list[str], {"schema": ""}),
        (list[str], {"schema": "items", "unknown": True}),
    ],
)
def test_invalid_static_named_declarations_refuse_at_registration(annotation, metadata):
    from frisket.actions.core import _output_fields

    output = create_model(
        "InvalidOutput",
        items=(annotation, Field(json_schema_extra={"named_result": metadata})),
    )
    with pytest.raises((TypeError, ValueError)):
        _output_fields(output, allow_json=True)
