from __future__ import annotations

from pathlib import Path
from typing import Any

from frisket.ai.llm import ModelRouter
from typed_model_fixtures import model_request, model_plan
from frisket.ops.entities import (
    ENTITY_ITEM_SCHEMA,
    ENTITY_OUTPUT_ARRAY_SCHEMA,
    canonicalize_entities,
    canonicalize_entity_type,
)
from frisket.engine.store import Project
from frisket.engine.store.evidence import list_cell_evidence, resolve_evidence_viewer


PROJECT_ID = "project-ner-spans"


# ---------- canonicalize_entity_type / canonicalize_entities ----------


def test_canonicalize_entity_type_routes_known_aliases_through_alias_table():
    assert canonicalize_entity_type("person") == "person"
    assert canonicalize_entity_type("People") == "person"
    assert canonicalize_entity_type("place") == "location"
    assert canonicalize_entity_type("org") == "organization"
    # C4a regression guard: `gpe` is load-bearing (see ops/entities.py) so
    # spaCy's GPE tag must land on the same canonical type regardless of case.
    assert canonicalize_entity_type("GPE") == "location"
    assert canonicalize_entity_type("gpe") == "location"


def test_canonicalize_entity_type_lowercases_unknown_types_without_dropping():
    assert canonicalize_entity_type("DATE") == "date"
    assert canonicalize_entity_type("Work Of Art") == "work_of_art"


def test_canonicalize_entity_type_empty_falls_back_to_entity():
    assert canonicalize_entity_type("") == "entity"
    assert canonicalize_entity_type("   ") == "entity"


def test_canonicalize_entities_renames_label_to_type():
    out = canonicalize_entities(
        [
            {
                "text": "Ada Lovelace",
                "label": "person",
                "start": 0,
                "end": 12,
                "score": 0.9,
            }
        ]
    )
    # The STORED shape: `type` (never `label`) plus the server-derived
    # `fingerprint` -- pinned exactly, since it is the entity-column contract
    # the Mentions panel groups on.
    assert out == [
        {
            "text": "Ada Lovelace",
            "type": "person",
            "start": 0,
            "end": 12,
            "score": 0.9,
            "fingerprint": "ada lovelace",
        }
    ]


def test_canonicalize_entities_accepts_type_key_directly():
    out = canonicalize_entities(
        [{"text": "London", "type": "location", "start": 0, "end": 6, "score": None}]
    )
    assert out == [
        {
            "text": "London",
            "type": "location",
            "start": 0,
            "end": 6,
            "score": None,
            "fingerprint": "london",
        }
    ]


def test_canonicalize_entities_drops_malformed_entries():
    out = canonicalize_entities(
        [
            {"text": "Ada", "label": "person", "start": 0, "end": 3, "score": 0.9},
            {"label": "person"},  # missing text/start/end
            "not-a-dict",  # type: ignore[list-item]
            {"text": "Bad", "label": "person", "start": "0", "end": 3},  # start not int
        ]
    )
    assert out == [
        {
            "text": "Ada",
            "type": "person",
            "start": 0,
            "end": 3,
            "score": 0.9,
            "fingerprint": "ada",
        }
    ]


def test_canonicalize_entities_omits_fingerprint_for_numeric_temporal_types():
    """The stored `fingerprint` is present for MERGEABLE surfaces and absent
    for the numeric/temporal set (grouping "March 2022" with "2022 March"
    would be wrong) -- the panel groups those by exact text instead. Absence
    is part of the contract, not an oversight, so it is pinned here."""
    out = canonicalize_entities(
        [
            {"text": "March 2022", "label": "DATE", "start": 0, "end": 10},
            {"text": "$4.2 million", "label": "MONEY", "start": 11, "end": 23},
            {"text": "Acme Corp.", "label": "org", "start": 24, "end": 34},
        ]
    )
    assert out == [
        {"text": "March 2022", "type": "date", "start": 0, "end": 10, "score": None},
        {
            "text": "$4.2 million",
            "type": "money",
            "start": 11,
            "end": 23,
            "score": None,
        },
        {
            "text": "Acme Corp.",
            "type": "organization",
            "start": 24,
            "end": 34,
            "score": None,
            "fingerprint": "acme corp",
            # `org` -> `organization` is a real rename, so the raw engine
            # label is preserved (see the metadata test above).
            "metadata": {"raw_label": "org"},
        },
    ]


def test_canonicalize_entities_omits_fingerprint_for_punctuation_only_surface():
    """An empty/punctuation-only surface yields no key at all rather than an
    empty-string key -- otherwise every unparseable surface would collapse
    into one bogus group."""
    (out,) = canonicalize_entities(
        [{"text": "---", "label": "person", "start": 0, "end": 3}]
    )
    assert out == {"text": "---", "type": "person", "start": 0, "end": 3, "score": None}


def test_canonicalize_entities_empty_and_none():
    assert canonicalize_entities(None) == []
    assert canonicalize_entities([]) == []


def test_canonicalize_entities_keeps_raw_label_in_metadata_when_type_differs():
    """`place` -> `location` via the alias table in ops/entities.py is a real
    rename (not just a lowercase/whitespace normalization) -- the raw engine
    label is preserved in metadata so it is not silently lost."""
    out = canonicalize_entities(
        [{"text": "London", "label": "place", "start": 0, "end": 6, "score": 0.5}]
    )
    assert out[0]["type"] == "location"
    assert out[0]["metadata"] == {"raw_label": "place"}


def test_entity_output_array_schema_matches_item_schema():
    assert ENTITY_OUTPUT_ARRAY_SCHEMA == {"type": "array", "items": ENTITY_ITEM_SCHEMA}
    assert ENTITY_ITEM_SCHEMA["properties"]["type"] == {"type": "string"}
    assert "label" not in ENTITY_ITEM_SCHEMA["properties"]
    assert ENTITY_ITEM_SCHEMA["properties"]["score"] == {"type": ["number", "null"]}


# ---------- output_fields() schema uses the unified contract ----------


def test_ner_recipe_output_fields_schema_is_unified_contract():
    plan = model_plan(
        {"action_kind": "map.ner", "input_columns": ["body"], "labels": ["person"]}
    )
    assert plan.output_fields[0]["schema"]["type"] == "array"
    from frisket.actions.ner import NerOutput

    assert (
        "fingerprint"
        in NerOutput.model_json_schema()["$defs"]["EntityMention"]["properties"]
    )


# ---------- integration: map.ner becomes the 4th span-producer ----------


class _SidecarResponse:
    status_code = 200
    text = "ok"

    def __init__(self, entities: list[dict[str, Any]]) -> None:
        self.entities = entities

    def json(self) -> dict[str, Any]:
        return {"results": [self.entities]}


class _SidecarHttp:
    is_closed = False

    async def post(self, url: str, **kwargs: Any) -> _SidecarResponse:
        text = str((kwargs.get("json") or {}).get("texts", [""])[0])
        if "Ada" in text:
            return _SidecarResponse(
                [
                    {
                        "text": "Ada Lovelace",
                        "label": "person",
                        "start": 0,
                        "end": 12,
                        "score": 0.98,
                    },
                    {
                        # "Ada Lovelace wrote notes for Babbage." — Babbage is at
                        # 29:36. (Previously 27:34, which does not slice to
                        # "Babbage"; map.ner now validates text[start:end]==quote
                        # and would correctly drop a mismatched span.)
                        "text": "Babbage",
                        "label": "person",
                        "start": 29,
                        "end": 36,
                        "score": 0.9,
                    },
                ]
            )
        return _SidecarResponse([])


def _router() -> ModelRouter:
    router = ModelRouter(cache=None, cache_mode="off")
    router._client = _SidecarHttp()  # noqa: SLF001
    return router


def _seed_project(project_path: Path) -> tuple[int, int]:
    project = Project.create(project_path, name="NER Spans")
    try:
        sheet_id = project.add_sheet("Transcripts")
        body_col = project.add_column(sheet_id, "body", type="text")
        project.add_rows(
            sheet_id,
            [{"body": "Ada Lovelace wrote notes for Babbage."}],
            {"body": body_col},
        )
        return sheet_id, body_col
    finally:
        project.close()


def _map_ner_action(sheet_id: int, *, idempotency_key: str) -> dict[str, Any]:
    return model_request(
        {
            "action_kind": "map.ner",
            "sheet_id": sheet_id,
            "input_columns": ["body"],
            "labels": ["person"],
            "engine": "gliner",
            "idempotency_key": idempotency_key,
        }
    ).model_dump(mode="json", exclude_none=True)


def test_map_ner_writes_unified_type_key_and_evidence_spans(
    tmp_path: Path, monkeypatch
) -> None:
    from frisket.engine.executor import actions as executor_actions

    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")

    project_path = tmp_path / "ner-spans.frisket"
    sheet_id, body_col = _seed_project(project_path)

    project = Project(project_path)
    try:
        result = executor_actions.run_action_spec(
            project,
            _map_ner_action(sheet_id, idempotency_key="map_ner_spans@sha256:stable"),
            project_id=PROJECT_ID,
            router=_router(),
        )
        assert result.status == "completed", result.errors

        entities_col = next(
            c for c in project.columns(sheet_id) if c["name"] == "entities"
        )
        values = project.get_values(sheet_id, entities_col["id"])
        (row_id, entities) = next(iter(values.items()))
        # entity canonicalization: label -> type, canonicalized -- no "label" key survives.
        assert entities == [
            {
                "text": "Ada Lovelace",
                "type": "person",
                "start": 0,
                "end": 12,
                "score": 0.98,
                "fingerprint": "ada lovelace",
            },
            {
                "text": "Babbage",
                "type": "person",
                "start": 29,
                "end": 36,
                "score": 0.9,
                "fingerprint": "babbage",
            },
        ]

        # capability refinement: entities are now Source-QA retrievable -- an evidence link
        # exists on the entities cell with one span per entity.
        evidence = list_cell_evidence(
            project, sheet_id=sheet_id, row_id=row_id, column_id=entities_col["id"]
        )
        assert evidence["links"], (
            "map.ner must write an evidence link for its entities cell"
        )
        link = evidence["links"][0]
        assert link["span_count"] == 2

        viewer = resolve_evidence_viewer(project, link["id"])
        assert len(viewer["artifacts"]) == 1
        artifact = viewer["artifacts"][0]
        assert artifact["artifact_kind"] == "text"
        spans = artifact["spans"]
        assert len(spans) == 2
        quotes = {span["quote"] for span in spans}
        assert quotes == {"Ada Lovelace", "Babbage"}
        # every span shares the SAME text_layer_hash -- one shared text
        # surface (the row's assembled input text), consistent char offsets.
        hashes = {span["text_layer_hash"] for span in spans}
        assert len(hashes) == 1
        assert next(iter(hashes)).startswith("sha256:")
        for span in spans:
            assert span["selector"]["char_start"] is not None
            assert span["selector"]["char_end"] is not None
    finally:
        project.close()


def test_map_ner_writes_no_spans_when_no_entities_found(
    tmp_path: Path, monkeypatch
) -> None:
    """A row with zero entities gets no artifact/span/link -- span-writing is
    entity-driven, not a blanket per-row artifact regardless of content."""
    from frisket.engine.executor import actions as executor_actions

    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")

    project_path = tmp_path / "ner-spans-empty.frisket"
    project = Project.create(project_path, name="NER Spans Empty")
    try:
        sheet_id = project.add_sheet("Transcripts")
        body_col = project.add_column(sheet_id, "body", type="text")
        project.add_rows(
            sheet_id, [{"body": "Nothing notable happened."}], {"body": body_col}
        )
    finally:
        project.close()

    project = Project(project_path)
    try:
        result = executor_actions.run_action_spec(
            project,
            _map_ner_action(
                sheet_id, idempotency_key="map_ner_spans_empty@sha256:stable"
            ),
            project_id=PROJECT_ID,
            router=_router(),
        )
        assert result.status == "completed", result.errors
        entities_col = next(
            c for c in project.columns(sheet_id) if c["name"] == "entities"
        )
        (row_id,) = project.get_values(sheet_id, entities_col["id"]).keys()
        evidence = list_cell_evidence(
            project, sheet_id=sheet_id, row_id=row_id, column_id=entities_col["id"]
        )
        assert evidence["links"] == []
    finally:
        project.close()
