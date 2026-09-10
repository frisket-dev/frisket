"""Server-truthful param preflight (live-param-validation lane).

Pins the host-owned validator registry and the validate-params service:
valid/invalid patterns, the error position, that the Python-dialect named-group
syntax ``(?P<name>...)`` (which JS RegExp would spell ``(?<name>...)``) is
accepted by the legacy seam. Typed actions instead report their Pydantic Params
verdict through the same response envelope.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from frisket.actions.system import root_action_catalog
from frisket.server.param_validation import (
    ParamDiagnostic,
    ParamValidationContext,
    validate_param,
    validator_keys,
)
from frisket.server.services.action_param_validation import (
    ACTION_PARAM_VALIDATION_RESULT_SCHEMA_VERSION,
    _declared_validators,
)


def test_python_regex_accepts_a_valid_pattern():
    diag = validate_param("python_regex", r"\d{3}-\d{3}-\d{4}")
    assert diag == ParamDiagnostic(ok=True)
    assert diag.to_payload() == {"ok": True}


def test_python_regex_rejects_an_uncompilable_pattern_with_position():
    # A representative click-through case: `aaaa.*))))` — unbalanced parens.
    diag = validate_param("python_regex", "aaaa.*))))")
    assert diag.ok is False
    assert diag.message == "unbalanced parenthesis"
    assert diag.position == 6
    assert diag.to_payload() == {
        "ok": False,
        "message": "unbalanced parenthesis",
        "position": 6,
    }


def test_python_regex_rejects_empty_pattern():
    # Empty never runs (MapRegexExtractParams.pattern is min_length=1), so
    # preflight refuses it too.
    diag = validate_param("python_regex", "   ")
    assert diag.ok is False
    assert diag.message == "The regex pattern is empty."
    assert diag.position is None


def test_python_regex_accepts_python_named_group_dialect():
    # The browser's JS RegExp dialect must never judge a Python pattern:
    # `(?P<name>...)` is Python's named group (JS spells it `(?<name>...)`),
    # and the executor (regex.compile) accepts it, so the validator must too.
    assert validate_param("python_regex", r"(?P<area>\d{3})-(?P<line>\d{4})").ok is True


def test_unknown_validator_key_raises():
    with pytest.raises(KeyError):
        validate_param("javascript_regexp", "x")


def test_template_accepts_placeholders_that_name_real_columns():
    ctx = ParamValidationContext(column_names=frozenset({"country", "product"}))
    diag = validate_param("template", "US tariffs on {{country}} {{product}}", ctx)
    assert diag == ParamDiagnostic(ok=True)


def test_template_rejects_an_unknown_placeholder_with_position():
    ctx = ParamValidationContext(column_names=frozenset({"country"}))
    diag = validate_param("template", "Impact on {{regoin}} economy", ctx)
    assert diag.ok is False
    assert diag.message == 'No column named "regoin" on this sheet.'
    # 0-based offset of the offending "{{" so the field can point at it.
    assert diag.position == 10


def test_template_without_placeholders_is_valid_literal_text():
    ctx = ParamValidationContext(column_names=frozenset())
    assert validate_param("template", "just a plain question", ctx).ok is True


def test_template_passes_when_no_sheet_context_resolved():
    # The service could not resolve a sheet (empty column set): the preflight
    # must not false-alarm on placeholders it cannot judge.
    assert validate_param("template", "Ask about {{anything}}").ok is True


def test_typed_research_action_declares_no_parallel_validator_authority():
    # ``ResearchParams.question`` is a typed Template; the catalog serves it as
    # a template control and publishes no second ``param_validators`` seam.
    entry = next(
        action.model_dump(mode="json")
        for action in root_action_catalog().actions
        if action.kind == "research.answer"
    )
    assert _declared_validators(entry) == {}
    question = next(
        requirement
        for requirement in entry["ui_hints"]["source_requirements"]
        if requirement["param"] == "question"
    )
    assert question["mode"] == "template"


def test_typed_web_search_preflight_uses_its_params_model():
    from frisket.server.services.action_param_validation import (
        ActionParamValidationService,
    )

    entry = next(
        action.model_dump(mode="json")
        for action in root_action_catalog().actions
        if action.kind == "research.web_search"
    )
    assert _declared_validators(entry) == {}

    service = ActionParamValidationService(SimpleNamespace(edition="solo"))
    invalid = service.validate_params(
        "pid-ignored",
        {"action_id": "research.web_search", "params": {"query": {"text": "   "}}},
    )
    assert invalid["diagnostics"] == {
        "query": {"ok": False, "message": ".text: template must be non-empty"}
    }

    valid = service.validate_params(
        "pid-ignored",
        {
            "action_id": "research.web_search",
            "params": {"query": {"text": "tariffs {{country}}"}},
        },
    )
    assert valid["diagnostics"] == {}
    assert valid["logical_outputs"] == [
        {"key": "search_results", "column_type": "json"}
    ]


def test_registry_ships_the_declared_validator_vocabulary():
    # The registry is the validation seam, and its vocabulary
    # is exactly {python_regex, template}. A third validator is one
    # entry here plus one ui_hints.param_validators declaration.
    assert validator_keys() == frozenset({"python_regex", "template"})


def test_list_table_schema_validation_needs_no_destination_and_writes_nothing(tmp_path):
    from contextlib import closing

    from frisket.engine.store import Project
    from frisket.server.services.action_param_validation import (
        ActionParamValidationService,
    )

    with closing(Project.create(tmp_path / "lists.frisket")) as project:
        sheet = project.add_sheet("Lists")
        column = project.add_column(sheet, "items", "json")
        project.add_rows(
            sheet, [{"items": [{"vendor": "Acme", "amount": 1200}]}], {"items": column}
        )

        def counts():
            return tuple(
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("sheets", "columns", "rows", "ops", "receipts")
            )

        before = counts()
        service = ActionParamValidationService(
            SimpleNamespace(edition="solo", get=lambda _id: project)
        )
        result = service.validate_params(
            "project",
            {
                "action_id": "derive.table_from_list",
                "scope": {"kind": "project"},
                "params": {
                    "source": {"kind": "column", "sheet_id": sheet, "column_id": column}
                },
            },
        )
        assert result["diagnostics"] == {}
        assert result["creates_sheet"] is True
        assert result["logical_outputs"] == [
            {"key": "vendor", "column_type": "text"},
            {"key": "amount", "column_type": "integer"},
        ]
        assert counts() == before


def test_typed_regex_catalog_does_not_publish_a_second_validator_authority():
    entry = next(
        action.model_dump(mode="json")
        for action in root_action_catalog().actions
        if action.kind == "map.regex_extract"
    )
    assert _declared_validators(entry) == {}


def test_validate_params_service_returns_per_param_diagnostics():
    # The service is exercised without a project for the first-party regex kind
    # (its declaration comes directly from the typed registry), so we call
    # the pure declaration+dispatch path a stubbed workspace would reach.
    from frisket.server.services.action_param_validation import (
        ActionParamValidationService,
    )

    service = ActionParamValidationService(SimpleNamespace(edition="solo"))
    # The typed regex kind resolves without a catalog build or workspace hit.
    result = service.validate_params(
        "pid-ignored",
        {"kind": "map.regex_extract", "params": {"pattern": "aaaa.*))))"}},
    )
    assert result["schema_version"] == ACTION_PARAM_VALIDATION_RESULT_SCHEMA_VERSION
    assert result["action"] == {"kind": "map.regex_extract"}
    assert result["diagnostics"] == {
        "input_columns": {"ok": False, "message": "Field required"},
        "pattern": {"ok": False, "message": "invalid regex pattern"},
    }


def test_validate_params_service_ok_when_pattern_valid_and_no_declared_params_absent():
    from frisket.server.services.action_param_validation import (
        ActionParamValidationService,
    )

    service = ActionParamValidationService(SimpleNamespace(edition="solo"))
    result = service.validate_params(
        "pid-ignored",
        {"kind": "map.regex_extract", "params": {"pattern": r"\d+"}},
    )
    assert result["diagnostics"] == {
        "input_columns": {"ok": False, "message": "Field required"}
    }
    # A complete typed Params model produces no errors without needing a
    # parallel `param_validators` declaration.
    empty = service.validate_params(
        "pid-ignored",
        {"kind": "map.template", "params": {"template": {"text": "{{x}}"}}},
    )
    assert empty["diagnostics"] == {}


def test_validate_params_service_projects_the_typed_research_template_verdict():
    from frisket.server.services.action_param_validation import (
        ActionParamValidationService,
    )

    service = ActionParamValidationService(SimpleNamespace(edition="solo"))
    params = {"source": ["story"], "model": "anthropic/claude-haiku-4-5"}
    literal = service.validate_params(
        "pid-ignored",
        {
            "action_id": "research.answer",
            "params": {**params, "question": {"text": "A literal question"}},
        },
    )
    assert literal["action"] == {"kind": "research.answer"}
    assert literal["diagnostics"] == {}
    assert [output["key"] for output in literal["logical_outputs"]] == [
        "answer",
        "sources",
    ]

    placeholders = service.validate_params(
        "pid-ignored",
        {
            "action_id": "research.answer",
            "params": {**params, "question": {"text": "Ask about {{anything}}"}},
        },
    )
    assert placeholders["diagnostics"] == {}

    blank = service.validate_params(
        "pid-ignored",
        {
            "action_id": "research.answer",
            "params": {**params, "question": {"text": "   "}},
        },
    )
    assert blank["diagnostics"] == {
        "question": {"ok": False, "message": ".text: template must be non-empty"}
    }


def test_typed_preflight_projects_nested_and_model_pydantic_errors():
    from frisket.server.services.action_param_validation import (
        ActionParamValidationService,
    )

    service = ActionParamValidationService(SimpleNamespace(edition="solo"))
    nested = service.validate_params(
        "pid-ignored",
        {
            "action_id": "map.columns_from_json",
            "params": {
                "source_column": "payload",
                "routes": [{"name": "", "path": ""}],
            },
        },
    )
    assert nested["action"] == {"kind": "map.columns_from_json"}
    assert nested["diagnostics"] == {
        "routes": {
            "ok": False,
            "message": "[0].name: String should have at least 1 character; "
            "[0].path: String should have at least 1 character",
        }
    }

    model = service.validate_params(
        "pid-ignored",
        {
            "action_id": "map.to_geo_point",
            "params": {
                "latitude_column": "coordinate",
                "longitude_column": "coordinate",
            },
        },
    )
    assert model["diagnostics"] == {
        "__all__": {
            "ok": False,
            "message": "latitude and longitude must come from different columns",
        }
    }
