"""Decision extraction keeps existing field outputs and classifier probabilities."""

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from frisket.actions.classify import ClassifyParams
from frisket.actions.extract.llm import ExtractParams, extract_outputs, extract_row
from frisket.actions.types import Outcome, Row, RowError
from frisket.contracts.classification import classification_options
from frisket.contracts.clef import (
    CLEF_FLASH_MAX_DESCRIPTION_CHARS,
    CLEF_FLASH_MAX_SCHEMA_CHARS,
    CLEF_FLASH_MAX_TEXT_CHARS,
    classification_context,
    clef_questions,
    validate_clef_request,
)


FIELDS = [
    {"name": "Relevant", "type": "boolean", "required": True},
    {"name": "Priority", "type": "score", "description": "Reporting priority"},
    {"name": "Document kind", "type": "category", "labels": ["Email", "Filing"]},
]


def params(engine="clef", **overrides):
    return ExtractParams.model_validate(
        {
            "source": ["body"],
            "engine": engine,
            "grounding": {"enabled": False},
            "fields": FIELDS,
            **overrides,
        }
    )


def test_legacy_extract_defaults_to_llm_and_keeps_citations():
    value = ExtractParams.model_validate(
        {
            "source": ["body"],
            "model": "anthropic/test",
            "fields": [{"name": "answer", "type": "text"}],
        }
    )
    assert value.engine.root == "llm"
    assert value.model.root == "anthropic/test"
    assert value.grounding.enabled
    with pytest.raises(ValidationError, match="requires a model"):
        params("llm")


@pytest.mark.parametrize("engine", ["clef", "clef-flash"])
def test_direct_model_and_source_contracts(engine):
    assert params(engine).model is None
    assert params(engine).fields[0].required
    with pytest.raises(ValidationError, match="does not use a model"):
        params(engine, model="anthropic/test")
    with pytest.raises(ValidationError):
        params(engine, source=["body", "body"])


@pytest.mark.parametrize("engine", ["gliclass", "jeff", "local_semantic", "unknown"])
def test_extract_only_exposes_the_two_decision_engines(engine):
    with pytest.raises(ValidationError, match="engine must be one of"):
        params(engine)


@pytest.mark.parametrize("engine", ["clef", "clef-flash"])
@pytest.mark.parametrize("kind", ["text", "integer", "number", "date", "list", "json"])
def test_incompatible_fields_refuse(engine, kind):
    with pytest.raises(ValidationError, match="category, boolean, and score"):
        params(engine, fields=[{"name": "x", "type": kind}])


@pytest.mark.parametrize("engine", ["clef", "clef-flash"])
@pytest.mark.parametrize(
    "overrides",
    [
        {"grounding": {"enabled": True}},
        {"grounding": {"enabled": False, "citation_required": True}},
        {"evidence_policy": {"citation_required": True}},
        {"grounding": None, "evidence_policy": {"citation_required": True}},
    ],
)
def test_citations_and_required_evidence_refuse_even_when_grounding_off(
    engine, overrides
):
    with pytest.raises(ValidationError, match="Turn Citations Off"):
        params(engine, **overrides)


def test_implicit_default_citations_refuse_direct_engine():
    with pytest.raises(ValidationError, match="Turn Citations Off"):
        ExtractParams.model_validate(
            {"source": ["body"], "engine": "clef", "fields": FIELDS}
        )


def test_blank_category_labels_refuse_before_classification():
    with pytest.raises(ValidationError, match="non-empty labels"):
        params(fields=[{"name": "kind", "type": "category", "labels": ["Email", " "]}])


@pytest.mark.parametrize("engine", ["clef", "clef-flash"])
def test_shared_limits_match_classify_and_catalog(engine):
    options = classification_options(engine)
    assert options["field_types"] == ["category", "boolean", "score"]
    assert options["include_confidence"] and not options["include_justification"]
    assert "inconclusive" in options["behavior_note"]
    for count in (options["min_labels"] - 1, options["max_labels"] + 1):
        fields = [
            {"name": "kind", "type": "category", "labels": list(map(str, range(count)))}
        ]
        with pytest.raises(ValidationError, match="2 to 254 labels"):
            params(engine, fields=fields)
        with pytest.raises(ValidationError, match="2 to 254 labels"):
            ClassifyParams(source=["body"], engine=engine, fields=fields)
    with pytest.raises(ValidationError):
        params(
            engine,
            fields=[
                {"name": f"f{i}", "type": "boolean"}
                for i in range(options["max_fields"] + 1)
            ],
        )


def test_effective_context_carries_dataset_and_instruction_in_every_question():
    value = params(
        context="Local election archive", instruction="  Identify misconduct.  "
    )
    context = classification_context(value.model_dump())
    questions = clef_questions([field.model_dump() for field in value.fields], context)
    for question in questions.values():
        assert "Dataset context: Local election archive" in question["instructions"]
        assert (
            "Extraction instructions: Identify misconduct." in question["instructions"]
        )
    assert (
        classification_context({"context": "  existing context  "})
        == "  existing context  "
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["clef", "clef-flash"])
@pytest.mark.parametrize("include_confidence", [False, True])
async def test_direct_outcomes_preserve_false_zero_and_distinct_probabilities(
    engine, include_confidence
):
    value = params(engine, include_confidence=include_confidence)
    outcomes = {
        "Relevant": Outcome.ok(False, confidence=0.7),
        "Priority": Outcome.ok(0, confidence=0.8),
        "Document kind": Outcome.ok("Filing", confidence=0.9),
    }
    classifier = AsyncMock()
    classifier.classify.return_value = outcomes
    row = Row(values={"body": "Election filing", "extra": "Notes"})
    result = (await extract_row(value, row, classifier)).output.root
    classifier.classify.assert_awaited_once()
    args = classifier.classify.call_args.args
    assert args[0] is row
    assert args[1] == "Election filing\nNotes"
    assert [field.name for field in args[2]] == list(outcomes)
    assert args[2][1].description == "Reporting priority"
    assert args[2][2].labels == ["Email", "Filing"]
    for name, outcome in outcomes.items():
        assert result[name] is outcome
    assert set(result) == set(extract_outputs(value))
    if include_confidence:
        assert result["Relevant_confidence"].value == 0.7


@pytest.mark.asyncio
async def test_direct_outcomes_preserve_failure_and_reject_missing_fields():
    value = params(fields=[FIELDS[0]], include_confidence=True)
    failure = Outcome.failed("empty_input", "Input is empty")
    classifier = AsyncMock()
    classifier.classify.return_value = {"Relevant": failure}
    result = (
        await extract_row(value, Row(values={"body": ""}), classifier)
    ).output.root
    assert result["Relevant"] is failure
    assert result["Relevant_confidence"].value is None
    classifier.classify.return_value = {}
    with pytest.raises(RowError, match="no outcome for Relevant"):
        await extract_row(value, Row(values={"body": ""}), classifier)


@pytest.mark.parametrize("param_type", [ClassifyParams, ExtractParams])
def test_flash_total_options_bound_is_checked_before_execution(param_type):
    fields = [
        {"name": f"kind{i}", "type": "category", "labels": list(map(str, range(254)))}
        for i in range(2)
    ]
    fields += [{"name": f"flag{i}", "type": "boolean"} for i in range(2)]
    request = {"source": ["body"], "engine": "clef-flash", "fields": fields}
    if param_type is ExtractParams:
        request["grounding"] = {"enabled": False}
    # 508 choice options plus two boolean pairs exactly fills the Flash budget.
    assert param_type.model_validate(request)
    fields.append({"name": "extra", "type": "boolean"})
    with pytest.raises(ValidationError, match="at most 512 total options"):
        param_type.model_validate(request)
    # These are worker-specific limits, not hosted Clef limits.
    assert param_type.model_validate({**request, "engine": "clef"})


@pytest.mark.parametrize("param_type", [ClassifyParams, ExtractParams])
def test_flash_description_bounds_include_rendered_context_and_labels(param_type):
    request = {
        "source": ["body"],
        "engine": "clef-flash",
        "fields": [{"name": "flag", "type": "boolean", "description": "x" * 4096}],
    }
    if param_type is ExtractParams:
        request["grounding"] = {"enabled": False}
    assert param_type.model_validate(request)
    with pytest.raises(ValidationError, match="each fit 4096 characters"):
        param_type.model_validate({**request, "context": "Dataset context"})
    assert param_type.model_validate(
        {**request, "engine": "clef", "context": "Context"}
    )
    request["fields"] = [
        {"name": "kind", "type": "category", "labels": ["x" * 4097, "y"]}
    ]
    with pytest.raises(ValidationError, match="label descriptions"):
        param_type.model_validate(request)


def test_flash_instruction_and_score_suffix_are_counted_after_rendering():
    with pytest.raises(ValidationError, match="each fit 4096 characters"):
        params("clef-flash", instruction="x" * 4096)
    with pytest.raises(ValidationError, match="each fit 4096 characters"):
        params(
            "clef-flash",
            fields=[{"name": "score", "type": "score", "description": "x" * 4096}],
        )


@pytest.mark.parametrize("param_type", [ClassifyParams, ExtractParams])
def test_flash_schema_character_bound_counts_question_identifiers(param_type):
    fields = [
        {
            "name": f"f{i}",
            "type": "boolean",
            "description": "x" * CLEF_FLASH_MAX_DESCRIPTION_CHARS,
        }
        for i in range(16)
    ]
    identifier_chars = sum(len(f"q{i}") for i in range(len(fields)))
    fields[-1]["description"] = fields[-1]["description"][:-identifier_chars]
    assert (
        sum(len(field["description"]) for field in fields) + identifier_chars
        == CLEF_FLASH_MAX_SCHEMA_CHARS
    )
    request = {"source": ["body"], "engine": "clef-flash", "fields": fields}
    if param_type is ExtractParams:
        request["grounding"] = {"enabled": False}
    assert param_type.model_validate(request)
    fields[-1]["description"] += "x"
    with pytest.raises(ValidationError, match="schema exceeds 65536 characters"):
        param_type.model_validate(request)
    assert param_type.model_validate({**request, "engine": "clef"})


def test_flash_schema_character_bound_counts_option_ids_and_descriptions():
    questions = {
        f"q{i}": {
            "type": "choice",
            "instructions": "",
            "criteria": {"o0": "x" * 4096, "o1": "y" * 4096},
        }
        for i in range(8)
    }
    overhead = sum(len(name) + 4 for name in questions)
    questions["q7"]["criteria"]["o1"] = "y" * (4096 - overhead)
    validate_clef_request("clef-flash", questions)
    questions["q7"]["criteria"]["o1"] += "y"
    with pytest.raises(ValueError, match="schema exceeds 65536 characters"):
        validate_clef_request("clef-flash", questions)


def test_flash_text_boundary_counts_characters_without_restricting_hosted_clef():
    questions = clef_questions([{"name": "flag", "type": "boolean"}])
    text = "é" * CLEF_FLASH_MAX_TEXT_CHARS
    validate_clef_request("clef-flash", questions, text)
    with pytest.raises(ValueError, match="input exceeds 65536 characters"):
        validate_clef_request("clef-flash", questions, text + "é")
    validate_clef_request("clef", questions, text + "é")
