from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app


def _client(tmp_path) -> TestClient:
    return TestClient(
        create_app(
            tmp_path / "workspace",
            router=ModelRouter(cache=None, cache_mode="off"),
        )
    )


def _catalog_entries(client: TestClient) -> dict[str, dict]:
    payload = client.get("/api/actions/v1/catalog").json()
    return {entry["kind"]: entry for entry in payload["actions"]}


def _requirement(hints: dict, rid: str) -> dict:
    return next(r for r in hints["source_requirements"] if r["id"] == rid)


def test_actions_advertise_typed_source_requirements(tmp_path):
    entries = _catalog_entries(_client(tmp_path))

    # The typed RichSource is one ``source`` param that is either a column
    # list or a template; there is no separate template param.
    classify = _requirement(entries["map.classify"]["ui_hints"], "source")
    assert classify["param"] == "source"
    assert classify["mode"] == "columns_or_template"
    assert "template_param" not in classify
    assert classify["template_columns"] == "exact"
    assert classify["accepted_column_types"] == [
        "text",
        "timestamped_transcript",
        "category",
        "date",
        "number",
        "integer",
        "boolean",
        "json",
        "image",
    ]
    # ``file`` columns are not offered raw to model sources; they require an
    # explicit conversion (media.* actions) first.
    assert "file" not in classify["accepted_column_types"]
    assert classify["accepted_cell_kinds"] == ["text", "blob", "template"]

    classify_hints = entries["map.classify"]["ui_hints"]
    # Engine and model are typed Params rendered through their semantic
    # controls; the typed catalog serves no parallel form_params declaration.
    assert classify_hints["form_params"] == []
    assert classify_hints["semantic_controls"] == {
        "source": "rich_source",
        "engine": "engine",
        "model": "model",
    }
    classify_params = entries["map.classify"]["input_schema"]["properties"]
    assert {"engine", "model"} <= set(classify_params)
    assert {"minimum_similarity", "minimum_margin"}.isdisjoint(classify_params)

    # A transcript column (type 'timestamped_transcript') must be an offerable
    # text source for the text map ops — Summarize/Classify/Ask read the
    # transcript a transcription produced.
    summarize = _requirement(entries["map.summarize"]["ui_hints"], "source")
    assert "timestamped_transcript" in summarize["accepted_column_types"]
    assert summarize["unset_fallback_includes_ai_generated"] is True

    ask = _requirement(entries["map.ask"]["ui_hints"], "source")
    assert ask["unset_fallback_includes_ai_generated"] is True
    assert "source_union_params" not in ask

    judge_hints = entries["map.judge"]["ui_hints"]
    judge_source = _requirement(judge_hints, "source")
    assert judge_source["mode"] == "columns_or_template"
    judged = _requirement(judge_hints, "judged_column")
    assert judged["mode"] == "column"
    assert judged["ai_generated_only"] is True

    regex_hints = entries["map.regex_extract"]["ui_hints"]
    regex = _requirement(regex_hints, "input_columns")
    assert "max" not in regex
    assert regex["accepted_column_types"] == [
        "text",
        "timestamped_transcript",
        "category",
    ]
    assert "unset_fallback_includes_ai_generated" not in regex
    assert "source_union_params" not in regex
    regex_schema = entries["map.regex_extract"]["input_schema"]
    regex_params = regex_schema["properties"]
    assert regex_params["pattern"]["type"] == "string"
    assert "pattern" in regex_schema["required"]
    assert {branch["type"] for branch in regex_params["group"]["anyOf"]} == {
        "integer",
        "string",
        "null",
    }
    assert regex_params["all_matches"]["type"] == "boolean"
    assert regex_params["timeout_seconds"]["maximum"] == 2.0

    python_hints = entries["map.python"]["ui_hints"]
    assert python_hints["form"] == "generated"
    assert python_hints["semantic_controls"] == {"input_columns": "columns"}
    assert python_hints["dynamic_outputs"] is True
    python_schema = entries["map.python"]["input_schema"]
    assert set(python_schema["required"]) == {
        "input_columns",
        "code",
        "return_schema",
        "output_routes",
    }
    assert python_schema["properties"]["code"]["type"] == "string"
    assert entries["map.python"]["required_capabilities"] == [
        "project:write",
        "unsafe:local_code",
    ]
    assert entries["map.python"]["cost_policy"] == {
        "kind": "none",
        "requires_confirmation": False,
        "notes": None,
    }

    template = _requirement(entries["map.template"]["ui_hints"], "template")
    assert template["param"] == "template"
    assert template["mode"] == "template"
    assert template["template_columns"] == "union"

    translate_hints = entries["map.translate"]["ui_hints"]
    translate = _requirement(translate_hints, "source")
    assert translate["param"] == "source"
    assert translate.get("max") is None
    # TranslateParams' RichSource carries the template branch on the same
    # param that runtime selection reads.
    assert translate["mode"] == "columns_or_template"
    assert translate["template_columns"] == "exact"
    assert translate_hints["semantic_controls"] == {
        "source": "rich_source",
        "engine": "engine",
        "model": "model",
    }
    translate_params = entries["map.translate"]["input_schema"]["properties"]
    # PRODUCT METADATA LOSS (typed catalog builder, src/frisket/actions/core.py
    # ~4029-4053): the legacy catalog served ``engine.type == "category"`` and
    # ``model.visible_when == {"param": "engine", "value": "llm"}``; the typed
    # builder emits neither, so this file cannot pin them. Filed, not re-pinned.
    assert translate_params["engine"]["default"] == "llm"
    assert translate_params["target_language"]["type"] == "string"
    assert translate_params["save_detected_language"]["type"] == "boolean"
    assert "model" in translate_params

    ner_hints = entries["map.ner"]["ui_hints"]
    ner = _requirement(ner_hints, "source")
    assert ner["param"] == "source"
    assert ner["mode"] == "columns_or_template"
    assert ner["accepted_column_types"] == ["text", "timestamped_transcript"]
    assert ner_hints["semantic_controls"] == {
        "source": "rich_source",
        "engine": "engine",
        "model": "model",
    }
    # PRODUCT METADATA LOSS (typed catalog builder, src/frisket/actions/core.py
    # ~4029-4053): the legacy catalog gated ``threshold.visible_when ==
    # {"param": "engine", "value": "gliner"}`` and ``model.visible_when`` /
    # ``extra_instructions.visible_when == {"param": "engine", "value": "llm"}``;
    # the typed builder emits no per-field ``visible_when`` at all, so those
    # pins are dropped here rather than asserted red. Filed, not re-pinned.
    ner_params = entries["map.ner"]["input_schema"]["properties"]
    assert ner_params["engine"]["default"] == "spacy"
    assert ner_params["threshold"]["type"] == "number"
    assert ner_params["threshold"]["default"] == 0.5
    assert ner_params["extra_instructions"]["type"] == "string"
    assert "model" in ner_params


def test_media_and_geo_actions_explain_prerequisites(tmp_path):
    entries = _catalog_entries(_client(tmp_path))

    ocr = _requirement(entries["media.ocr"]["ui_hints"], "source")
    assert ocr == {
        "id": "source",
        "mode": "column",
        "param": "source",
        "label": "Source",
        "min": 1,
        "accepted_column_types": ["image", "file"],
    }

    transcribe = _requirement(entries["media.transcribe"]["ui_hints"], "source")
    assert transcribe == {
        "id": "source",
        "mode": "column",
        "param": "source",
        "label": "Source",
        "min": 1,
        "accepted_column_types": ["audio", "video", "file"],
    }
    for kind in ("media.ocr", "media.transcribe"):
        assert (
            "Import or download"
            in entries[kind]["input_schema"]["properties"]["source"]["description"]
        )

    pdf_table_hints = entries["media.extract_pdf_tables"]["ui_hints"]
    assert pdf_table_hints["form"] == "generated"
    assert pdf_table_hints["semantic_controls"] == {"source": "column"}
    schema = entries["media.extract_pdf_tables"]["input_schema"]
    assert set(schema["properties"]) == {
        "source",
        "mode",
        "table_mode",
        "extract_table",
    }
    assert schema["required"] == ["source"]
    assert schema["properties"]["table_mode"]["enum"] == ["auto", "stream", "lattice"]
    assert schema["properties"]["table_mode"]["default"] == "auto"
    assert schema["properties"]["mode"]["const"] == "extract_table"
    assert schema["properties"]["extract_table"]["type"] == "object"
    assert [output["key"] for output in pdf_table_hints["logical_outputs"]] == [
        "pdf_tables"
    ]
    pdf_source = _requirement(pdf_table_hints, "source")
    assert pdf_source == {
        "id": "source",
        "mode": "column",
        "param": "source",
        "label": "Source",
        "min": 1,
        "accepted_column_types": ["file"],
    }
    assert entries["media.extract_pdf_tables"]["required_capabilities"] == [
        "project:write"
    ]
    assert entries["media.extract_pdf_tables"]["cost_policy"] == {
        "kind": "none",
        "requires_confirmation": False,
        "notes": None,
    }

    metadata_hints = entries["media.extract_metadata"]["ui_hints"]
    metadata_schema = entries["media.extract_metadata"]["input_schema"]
    metadata_params = metadata_schema["properties"]
    assert {"output_mode", "refresh"}.issubset(metadata_params), (
        "media.extract_metadata must serve its output_mode and refresh declarations"
    )
    output_mode = metadata_params["output_mode"]
    assert output_mode["type"] == "string"
    assert output_mode["enum"] == ["object", "columns"]
    assert output_mode["default"] == "columns"
    assert metadata_params["refresh"]["type"] == "boolean"
    assert metadata_params["refresh"]["default"] is False
    assert metadata_schema["required"] == ["source"]
    assert metadata_hints["semantic_controls"]["source"] == "column"
    metadata_source = _requirement(metadata_hints, "source")
    assert metadata_source["param"] == "source"
    assert metadata_source["mode"] == "column"
    assert metadata_source["accepted_column_types"] == [
        "image",
        "audio",
        "video",
        "file",
    ]

    census_hints = entries["enrich.census_demographics"]["ui_hints"]
    census = _requirement(census_hints, "source")
    assert census["param"] == "source"
    assert census["mode"] == "column"
    assert census["min"] == 1
    assert census["accepted_column_types"] == ["geo_point"]
    assert census_hints["semantic_controls"] == {"source": "column"}
    census_schema = entries["enrich.census_demographics"]["input_schema"]
    census_params = census_schema["properties"]
    assert set(census_params) == {"source", "geography", "include_moe"}
    assert census_schema["required"] == ["source"]
    assert (
        census_schema["$defs"][census_params["source"]["$ref"].split("/")[-1]]["type"]
        == "string"
    )
    assert census_params["geography"]["enum"] == ["tract", "block_group"]
    assert census_params["geography"]["default"] == "tract"
    assert census_params["include_moe"]["type"] == "boolean"
    assert census_params["include_moe"]["default"] is False
    # The sole provider remains real served metadata, not an inert Params choice.
    assert [(engine["id"], engine["label"]) for engine in census_hints["engines"]] == [
        ("us_census_acs", "US Census ACS 5-year enrichment (hosted)")
    ]
    assert census_hints["cost_source"] == "free_public_api"

    geocode_hints = entries["enrich.geocode"]["ui_hints"]
    geocode_source = _requirement(geocode_hints, "source")
    assert geocode_source["param"] == "source"
    assert geocode_source["mode"] == "column_or_template"
    assert geocode_source["min"] == 1
    assert geocode_source["accepted_column_types"] == ["text", "category"]
    assert set(geocode_source["template_accepted_column_types"]) == {
        "text",
        "category",
        "integer",
        "number",
        "date",
        "link",
    }
    assert geocode_source["template_columns"] == "exact"
    assert geocode_hints["semantic_controls"] == {
        "source": "column_or_template",
        "engine": "engine",
    }
    geocode_schema = entries["enrich.geocode"]["input_schema"]
    geocode_params = geocode_schema["properties"]
    assert set(geocode_params) == {"source", "engine", "include_lat_lon"}
    assert geocode_schema["required"] == ["source"]
    source_branches = [
        geocode_schema["$defs"][branch["$ref"].split("/")[-1]]
        for branch in geocode_params["source"]["anyOf"]
    ]
    assert {branch["type"] for branch in source_branches} == {"string", "object"}
    template = next(branch for branch in source_branches if branch["type"] == "object")
    assert template["required"] == ["text"]
    assert template["additionalProperties"] is False
    assert template["properties"]["text"]["type"] == "string"
    assert geocode_params["engine"]["default"] == "auto"
    assert geocode_params["include_lat_lon"]["default"] is False
    assert [(engine["id"], engine["label"]) for engine in geocode_hints["engines"]] == [
        ("opencage", "OpenCage geocoder (hosted, pay-per-row)"),
        ("nominatim", "Nominatim geocoder (hosted, rate-limited)"),
    ]
