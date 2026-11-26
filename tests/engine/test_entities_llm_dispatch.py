from __future__ import annotations

import json
from functools import partial
from typing import Any

import pytest
from executor_harness import run_action_with_confirmation
from jsonschema import Draft202012Validator
from model_doubles import ScriptedAdapter, canned_reply_router
from typed_model_fixtures import model_plan, render_ner

import frisket.ops.ner_citations as ner_module
from frisket.actions import ner
from frisket.actions.ner import normalize_ner_output
from frisket.actions.system import validate_root_action as validate_action_spec
from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from frisket.engine.store.citations import list_cell_citations, resolve_citation_viewer
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
from frisket.ops.ner_citations import _NER_CITATION_CAPTURE_KEY

PROJECT_ID = "project-ner-llm-dispatch"


# ---------- unit: is_llm/render/postprocess_value ----------


@pytest.mark.parametrize("engine", ["spacy", "gliner", "llm"])
def test_model_dispatch_is_selected_from_typed_engine(engine):
    spec = {
        "action_kind": "map.ner",
        "input_columns": ["body"],
        "labels": ["person"],
        "engine": engine,
    }
    if engine == "llm":
        spec["model"] = "anthropic/claude-haiku-4-5"
    plan = model_plan(spec)
    assert plan.program.is_llm(plan.spec_dict()) is (engine == "llm")


SPEECH = (
    "To a place that's Precincts out of four fifty two. We're at 174,000 "
    "votes to ten thousand. Mm-hmm. Uh Basically Joker."
)
# The exact structured answer a provider returned for SPEECH under the
# all-optional entity schema: a garbled type and no offsets.
GARBLED_ENTITY = {"text": "Joker", "type": "person','label':null,"}
PERSON_AND_ORGANIZATION = {"labels": ["person", "organization"]}


def _entity_item_schema(labels: list[str]) -> dict[str, Any]:
    schema = render_ner({"body": SPEECH}, {"labels": labels}).schema
    return schema["properties"]["entities"]["items"]


def test_model_schema_requires_located_entities_of_the_requested_labels():
    item = _entity_item_schema(["Person", "people", "organisation"])
    assert set(item["required"]) == {"text", "type", "start", "end"}
    assert item["properties"]["type"] == {
        "type": "string",
        "enum": ["person", "organization"],
    }
    assert {"fingerprint", "metadata", "label"}.isdisjoint(item["properties"])
    validator = Draft202012Validator(_entity_item_schema(["person", "organization"]))
    assert not validator.is_valid(GARBLED_ENTITY)
    assert not validator.is_valid({**GARBLED_ENTITY, "start": None, "end": None})
    assert validator.is_valid(
        {"text": "Joker", "type": "person", "start": None, "end": None}
    )


def _complete(
    entities: list[dict[str, Any]],
    values: dict[str, str] | None = None,
) -> dict[str, Any]:
    values = values if values is not None else {"body": SPEECH}
    plan = model_plan(
        {
            "action_kind": "map.ner",
            "input_columns": list(values),
            "engine": "llm",
            "model": "anthropic/claude-sonnet-5-5",
            **PERSON_AND_ORGANIZATION,
        }
    )
    publication = plan.program.normalize_model_output(
        {"entities": entities}, plan.spec_dict(), row_values=values
    )
    return publication.cells["entities"]


def _spans(cell: dict[str, Any]) -> list[tuple[str, str, int, int]]:
    return [
        (entity["text"], entity["type"], entity["start"], entity["end"])
        for entity in cell["value"]
    ]


def test_completion_locates_an_entity_returned_without_offsets():
    cell = _complete([{"text": "Joker", "type": "person", "start": None, "end": None}])
    assert _spans(cell) == [("Joker", "person", 112, 117)]
    assert "warnings" not in cell


def test_completion_relocates_wrong_offsets_and_keeps_correct_ones():
    cell = _complete(
        [
            {"text": "Joker", "type": "person", "start": 0, "end": 5},
            {"text": "Precincts", "type": "organization", "start": 18, "end": 27},
        ]
    )
    assert _spans(cell) == [
        ("Joker", "person", 112, 117),
        ("Precincts", "organization", 18, 27),
    ]


def test_repeated_mentions_take_distinct_positions_nearest_the_stated_offset():
    text = {"body": "Ada met Ada and Ada."}
    assert _spans(
        _complete(
            [
                {"text": "Ada", "type": "person", "start": 9, "end": 12},
                {"text": "Ada", "type": "person", "start": 9, "end": 12},
            ],
            text,
        )
    ) == [("Ada", "person", 8, 11), ("Ada", "person", 16, 19)]
    assert _spans(
        _complete(
            [
                {"text": "Ada", "type": "person", "start": None, "end": None},
                {"text": "Ada", "type": "person", "start": None, "end": None},
            ],
            text,
        )
    ) == [("Ada", "person", 0, 3), ("Ada", "person", 8, 11)]


def test_a_correct_position_is_kept_when_an_earlier_mention_has_none():
    cell = _complete(
        [
            {"text": "Jordan", "type": "location", "start": None, "end": None},
            {"text": "Jordan", "type": "person", "start": 0, "end": 6},
        ],
        values={"body": "Jordan scored. Jordan signed the treaty."},
    )
    assert _spans(cell) == [
        ("Jordan", "location", 15, 21),
        ("Jordan", "person", 0, 6),
    ]


def test_a_repeated_name_is_searched_for_once(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    find = ner.text_occurrences

    def counting(source: str, passage: str) -> list[tuple[int, int]]:
        calls.append(passage)
        return find(source, passage)

    monkeypatch.setattr(ner, "text_occurrences", counting)
    body = " ".join(["Ada"] * 40)
    stated = [
        {"text": "Ada", "type": "person", "start": 4 * i, "end": 4 * i + 3}
        for i in range(39)
    ]
    cell = _complete(
        [*stated, {"text": "Ada", "type": "person", "start": None, "end": None}],
        values={"body": body},
    )
    assert [(e["start"], e["end"]) for e in cell["value"]] == [
        (4 * i, 4 * i + 3) for i in range(40)
    ]
    assert calls == ["Ada"]


def test_a_stated_end_past_the_text_is_corrected():
    cell = _complete(
        [{"text": "Bob", "type": "person", "start": 10, "end": 1000}],
        values={"body": "Alice met Bob"},
    )
    assert _spans(cell) == [("Bob", "person", 10, 13)]


def test_prompt_names_the_same_types_the_schema_allows():
    call = render_ner({"body": "text"}, {"labels": ["ORG", "people"]})
    system = call.messages[0]["content"]
    assert "types: organization, person." in system
    assert "ORG" not in system


def test_discarded_entities_are_reported_on_the_row():
    cell = _complete(
        [
            {"text": "Joker", "type": "person", "start": 112, "end": 117},
            {"text": "Batman", "type": "person", "start": 0, "end": 6},
            {"text": "Riddler", "type": "person", "start": None, "end": None},
            {"text": "Joker", "type": "person", "start": 112, "end": 117},
        ]
    )
    assert _spans(cell) == [("Joker", "person", 112, 117)]
    assert cell["warnings"] == [
        "2 entities the model returned were discarded: text not found in the source.",
        "1 entity the model returned was discarded: it repeats a mention already "
        "returned.",
    ]


def test_multi_column_offsets_index_the_joined_source_text():
    values = {"speaker": "Ada", "body": "wrote notes"}
    cell = _complete(
        [
            {"text": "Ada", "type": "person", "start": 9, "end": 12},
            {"text": "speaker", "type": "person", "start": 0, "end": 7},
        ],
        values,
    )
    assert _spans(cell) == [("Ada", "person", 0, 3)]
    assert cell["warnings"] == [
        "1 entity the model returned was discarded: text not found in the source."
    ]


def test_stored_entity_schema_keeps_derived_fingerprint():
    plan = model_plan(
        {"action_kind": "map.ner", "input_columns": ["body"], "labels": ["person"]}
    )
    assert "fingerprint" in str(plan.output_fields[0]["schema"])


def test_render_instruction_includes_labels_and_extra_instructions():
    call = render_ner(
        {"body": "text"},
        {"labels": ["person"], "extra_instructions": "Only named people."},
    )
    assert "Only named people." in json.dumps(call.messages)
    assert "person" in json.dumps(call.messages)


def test_render_keeps_labels_for_multi_column_inputs():
    call = render_ner({"speaker": "Ada", "body": "wrote notes"}, {"labels": ["person"]})
    assert call.messages[1]["content"] == [
        {"type": "text", "text": "speaker: Ada\nbody: wrote notes"}
    ]


def test_render_refuses_empty_labels():
    with pytest.raises(ValueError):
        render_ner({"body": "text"}, {"labels": []})


def test_completion_derives_fingerprint_from_entity_values():
    assert normalize_ner_output(
        [{"text": "Ada", "label": "person", "start": 0, "end": 3, "score": 0.9}]
    ).entities == [
        {
            "text": "Ada",
            "type": "person",
            "start": 0,
            "end": 3,
            "score": 0.9,
            "fingerprint": "ada",
        }
    ]


def _seed_project(
    project: Project,
    *,
    body: str = "Ada Lovelace wrote notes for Charles Babbage.",
) -> int:
    sheet_id = project.add_sheet("Docs")
    columns = {"body": project.add_column(sheet_id, "body", type="text")}
    project.add_rows(
        sheet_id,
        [{"body": body}],
        columns,
    )
    return sheet_id


def test_estimate_prices_llm_engine_as_a_real_model_call(project: Project) -> None:
    sheet_id = _seed_project(project)
    runner = MapRunner(
        project,
        ModelRouter(
            model_call_policy=LOCAL_PURCHASE_POLICY, cache=None, cache_mode="off"
        ),
        authority=UnroutedOnlyAuthority(project),
    )
    llm_spec = {
        "action_kind": "map.ner",
        "sheet_id": sheet_id,
        "input_columns": ["body"],
        "labels": ["person"],
        "engine": "llm",
        "model": "anthropic/claude-haiku-4-5",
        "output_name": "entities",
    }
    gliner_spec = {**llm_spec, "engine": "gliner", "model": None}
    llm = model_plan(llm_spec, project)
    gliner = model_plan(gliner_spec, project)
    runner.allow_action_lifecycle_only_programs = True
    llm_est = runner.estimate(llm.spec_dict(), program=llm.program)
    gliner_est = runner.estimate(gliner.spec_dict(), program=gliner.program)
    assert "avg_input_tokens" in llm_est
    assert gliner_est["cost"] == 0.0


def test_llm_engine_declares_model_cost_while_local_engine_is_free():
    llm = model_plan(
        {
            "action_kind": "map.ner",
            "input_columns": ["body"],
            "labels": ["person"],
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
        }
    )
    local = model_plan(
        {
            "action_kind": "map.ner",
            "input_columns": ["body"],
            "labels": ["person"],
            "engine": "spacy",
        }
    )
    assert llm.program.cost_class == "metered"
    assert local.program.external_cost_is_known_zero is True


# ---------- end-to-end: kind="map.ner", engine="llm", through run_action_spec ----------


_stub_router = partial(canned_reply_router, tokens_in=42, tokens_out=17, cost=0.004)


def _map_ner_llm_action(
    sheet_id: int,
    *,
    idempotency_key: str,
    capabilities: list[str] | None = None,
    labels: list[str] | None = None,
    model: str | None = "anthropic/claude-haiku-4-5",
) -> dict[str, Any]:
    return {
        "action_id": "map.ner",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["body"],
            "labels": labels if labels is not None else ["person"],
            "engine": "llm",
            "model": model,
            "extra_instructions": "Only named people.",
        },
        "output_names": {"entities": "entities"},
        "idempotency_key": idempotency_key,
        **({"capabilities": capabilities} if capabilities is not None else {}),
    }


def test_map_ner_llm_engine_validates_through_the_contract_layer():
    action = _map_ner_llm_action(1, idempotency_key="map_ner_llm_dispatch@sha256:a")
    result = validate_action_spec(action)
    assert result.ok is True, result.error


def test_map_ner_llm_engine_requires_model_complete_capability():
    action = _map_ner_llm_action(
        1,
        idempotency_key="map_ner_llm_dispatch@sha256:b",
        capabilities=["project:write"],
    )
    result = validate_action_spec(action)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_action_request"


def test_map_ner_llm_engine_requires_a_model():
    action = _map_ner_llm_action(
        1, idempotency_key="map_ner_llm_dispatch@sha256:c", model=None
    )
    result = validate_action_spec(action)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_action_request"


def test_map_ner_llm_engine_runs_and_writes_unified_entities(
    project: Project,
) -> None:
    """The core dispatch proof: kind="map.ner", engine="llm" through the
    REAL executor (`run_action_spec`, no manual re-packaging into a
    map.extract action) completes and writes the SAME unified
    {text,type,start,end,score} shape spacy/gliner write."""
    sheet_id = _seed_project(project)
    action = _map_ner_llm_action(
        sheet_id, idempotency_key="map_ner_llm_dispatch@sha256:run"
    )
    router, adapter = _stub_router(
        {
            "entities": [
                {
                    "text": "Ada Lovelace",
                    "type": "person",
                    "start": 0,
                    "end": 12,
                    "score": 0.92,
                }
            ]
        }
    )

    result = run_action_with_confirmation(
        project,
        action,
        project_id=PROJECT_ID,
        router=router,
    )
    assert result.status == "completed", result.errors
    # stays a genuine map.ner run, not a re-labeled map.extract one.
    assert result.action.kind == "map.ner"
    assert len(adapter.requests) == 1
    assert "Only named people." in json.dumps([m for m in adapter.requests[0].messages])

    column = next(c for c in project.columns(sheet_id) if c["name"] == "entities")
    assert column["type"] == "json"
    values = project.get_values(sheet_id, column["id"])
    (row_id, entities) = next(iter(values.items()))
    # unified shape, canonicalized -- same contract
    # spacy/gliner write (tests/test_entities_output_spans.py).
    assert entities == [
        {
            "text": "Ada Lovelace",
            "type": "person",
            "start": 0,
            "end": 12,
            "score": 0.92,
            "fingerprint": "ada lovelace",
        }
    ]

    # output refs: column + named_result, matching the gliner/spacy shape
    # (tests/test_map_ner_executor.py) -- llm dispatch does not change
    # the receipt's public contract.
    assert [(output.kind, output.name) for output in result.outputs] == [
        ("column", "entities"),
        ("named_result", "entities"),
    ]

    # grounding/evidence per the NER precedent: map.ner's own span writer
    # ran (entities-unified-output-and-spans-v1), not map.extract's
    # generic grounding wrapper.
    evidence = list_cell_citations(
        project, sheet_id=sheet_id, row_id=row_id, column_id=column["id"]
    )
    assert evidence["links"], "map.ner(engine=llm) must write an evidence link"
    link = evidence["links"][0]
    assert link["span_count"] == 1
    viewer = resolve_citation_viewer(project, link["id"])
    artifact = viewer["artifacts"][0]
    assert artifact["artifact_kind"] == "text"
    span = artifact["spans"][0]
    assert span["quote"] == "Ada Lovelace"
    assert span["selector"]["char_start"] == 0
    assert span["selector"]["char_end"] == 12


def test_returned_ner_checkpoint_keeps_private_evidence_when_publish_rolls_back(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet_id = _seed_project(project)
    router, adapter = _stub_router(
        {
            "entities": [
                {
                    "text": "Ada Lovelace",
                    "type": "person",
                    "start": 0,
                    "end": 12,
                    "score": 0.92,
                }
            ]
        }
    )
    original = ner_module._write_ner_result_citations

    def fail_after_evidence(*args, **kwargs):  # noqa: ANN002, ANN003
        original(*args, **kwargs)
        raise RuntimeError("evidence write failed")

    monkeypatch.setattr(ner_module, "_write_ner_result_citations", fail_after_evidence)
    action = _map_ner_llm_action(
        sheet_id, idempotency_key="map_ner_llm_evidence_rollback@sha256:stable"
    )
    result = run_action_with_confirmation(
        project, action, project_id=PROJECT_ID, router=router
    )
    assert result.status == "failed"
    assert result.run_id is not None and result.receipt_id is not None
    assert [error.code for error in result.errors] == ["project_write_failed"]
    assert result.errors[0].message == "project write failed"
    assert len(adapter.requests) == 1
    run = project.db.execute("SELECT * FROM runs").fetchone()
    assert run["status"] == "failed"
    assert run["current_attempt_id"] is None
    assert run["cost_actual"] == pytest.approx(0.004)
    checkpoint = project.db.execute(
        "SELECT state,payload FROM effect_checkpoints WHERE family='row_effect'"
    ).fetchone()
    assert checkpoint["state"] == "returned"
    payload = json.loads(checkpoint["payload"])["entities"]
    assert payload[_NER_CITATION_CAPTURE_KEY]["schema_version"].endswith(
        "ner_input_capture.v1"
    )
    assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    assert (
        project.db.execute("SELECT COUNT(*) FROM cell_result_heads").fetchone()[0] == 0
    )
    assert project.db.execute("SELECT COUNT(*) FROM citations").fetchone()[0] == 0
    monkeypatch.setattr(ner_module, "_write_ner_result_citations", original)
    before = tuple(project.db.iterdump())
    replay = run_action_with_confirmation(
        project, action, project_id=PROJECT_ID, router=router
    )
    assert replay.model_dump() == result.model_dump()
    assert len(adapter.requests) == 1
    assert tuple(project.db.iterdump()) == before


def test_map_ner_llm_engine_reports_entities_whose_text_is_not_in_the_source(
    project: Project,
) -> None:
    sheet_id = _seed_project(project)
    action = _map_ner_llm_action(
        sheet_id, idempotency_key="map_ner_llm_dispatch@sha256:malformed"
    )
    router, _adapter = _stub_router(
        {
            "entities": [
                {"text": "Ada Lovelace", "type": "person", "start": 0, "end": 12},
                {"text": "Grace Hopper", "type": "person", "start": 0, "end": 12},
            ]
        }
    )

    result = run_action_with_confirmation(
        project, action, project_id=PROJECT_ID, router=router
    )
    assert result.status == "completed", result.errors
    column = next(c for c in project.columns(sheet_id) if c["name"] == "entities")
    (entities,) = project.get_values(sheet_id, column["id"]).values()
    assert entities == [
        {
            "text": "Ada Lovelace",
            "type": "person",
            "start": 0,
            "end": 12,
            "score": None,
            "fingerprint": "ada lovelace",
        }
    ]
    (warnings,) = project.db.execute(
        "SELECT warnings FROM results WHERE run_id=? AND column_id=?",
        (result.run_id, column["id"]),
    ).fetchone()
    assert json.loads(warnings) == [
        "1 entity the model returned was discarded: text not found in the source."
    ]


def _reply(data: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(data),
        data=data,
        tokens_in=42,
        tokens_out=17,
        cost=0.004,
        model="anthropic/claude-sonnet-5-5",
    )


def test_map_ner_llm_engine_corrects_a_garbled_type_and_locates_the_entity(
    project: Project,
) -> None:
    sheet_id = _seed_project(project, body=SPEECH)
    action = _map_ner_llm_action(
        sheet_id,
        idempotency_key="map_ner_llm_dispatch@sha256:garbled",
        labels=["person", "organization"],
        model="anthropic/claude-sonnet-5-5",
    )
    adapter = ScriptedAdapter(
        [
            _reply({"entities": [GARBLED_ENTITY]}),
            _reply(
                {
                    "entities": [
                        {"text": "Joker", "type": "person", "start": 0, "end": 5}
                    ]
                }
            ),
        ]
    )
    router, _canned = _stub_router({})
    router._adapters["anthropic"] = adapter

    result = run_action_with_confirmation(
        project, action, project_id=PROJECT_ID, router=router
    )
    assert result.status == "completed", result.errors
    assert len(adapter.seen) == 2
    column = next(c for c in project.columns(sheet_id) if c["name"] == "entities")
    (entities,) = project.get_values(sheet_id, column["id"]).values()
    assert [(e["text"], e["type"], e["start"], e["end"]) for e in entities] == [
        ("Joker", "person", 112, 117)
    ]


def test_map_ner_llm_highlights_every_exact_repeated_mention(
    project: Project,
) -> None:
    sheet_id = _seed_project(project, body="Ada met Ada.")
    action = _map_ner_llm_action(
        sheet_id, idempotency_key="map_ner_llm_dispatch@sha256:repeated"
    )
    # The model reports one mention and an unhelpful offset. Alignment is owned
    # by the server, which expands the exact quote to both occurrences.
    router, _adapter = _stub_router(
        {
            "entities": [
                {
                    "text": "Ada",
                    "type": "person",
                    "start": 8,
                    "end": 11,
                }
            ]
        }
    )

    result = run_action_with_confirmation(
        project, action, project_id=PROJECT_ID, router=router
    )
    assert result.status == "completed", result.errors
    column = next(c for c in project.columns(sheet_id) if c["name"] == "entities")
    values = project.get_values(sheet_id, column["id"])
    row_id, entities = next(iter(values.items()))
    assert [(entity["start"], entity["end"]) for entity in entities] == [
        (0, 3),
        (8, 11),
    ]
    link = list_cell_citations(
        project, sheet_id=sheet_id, row_id=row_id, column_id=column["id"]
    )["links"][0]
    viewer = resolve_citation_viewer(project, link["id"])
    spans = viewer["artifacts"][0]["spans"]
    assert [
        (span["selector"]["char_start"], span["selector"]["char_end"]) for span in spans
    ] == [(0, 3), (8, 11)]


def test_map_ner_llm_keeps_located_mentions_of_a_name_with_two_types(
    project: Project,
) -> None:
    sheet_id = _seed_project(project, body="Jordan scored. Jordan signed the treaty.")
    action = _map_ner_llm_action(
        sheet_id,
        idempotency_key="map_ner_llm_dispatch@sha256:two-types",
        labels=["person", "location"],
    )
    router, _adapter = _stub_router(
        {
            "entities": [
                {"text": "Jordan", "type": "person", "start": 0, "end": 6},
                {"text": "Jordan", "type": "location", "start": 15, "end": 21},
            ]
        }
    )

    result = run_action_with_confirmation(
        project, action, project_id=PROJECT_ID, router=router
    )
    assert result.status == "completed", result.errors
    column = next(c for c in project.columns(sheet_id) if c["name"] == "entities")
    row_id, entities = next(iter(project.get_values(sheet_id, column["id"]).items()))
    assert [(e["type"], e["start"], e["end"]) for e in entities] == [
        ("person", 0, 6),
        ("location", 15, 21),
    ]
    link = list_cell_citations(
        project, sheet_id=sheet_id, row_id=row_id, column_id=column["id"]
    )["links"][0]
    spans = resolve_citation_viewer(project, link["id"])["artifacts"][0]["spans"]
    assert [
        (span["selector"]["char_start"], span["selector"]["char_end"]) for span in spans
    ] == [(0, 6), (15, 21)]
