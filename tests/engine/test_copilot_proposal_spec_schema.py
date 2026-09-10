"""Copilot proposal-schema contract: a proposal may never carry an empty
``spec: {}`` that server validation silently drops.

Root cause: ``copilot_chat``'s structured-output schema declared the
proposal spec as a bare ``{"type": "object"}`` with NO properties. Native
structured output (gemini's responseSchema; strict modes generally) emits
ONLY declared properties — an empty-properties object schema GUARANTEES an
empty spec. The old hardcoded anthropic path happened to tolerate free-form
objects, so the schema itself must enumerate the proposal fields.

The contract is:
- The model-facing copilot schema stays flat and declares REAL properties: at least the
  routing essentials (``action_kind`` as an enum of the wire-carryable
  map/derive/reduce kinds, ``sheet_id``) plus the fields the proposal
  families actually use
  (``input_columns``, ``output_fields`` with typed items, ``prompt``,
  ``model``, ``group_by``, ``child_sheet``, ``item_field``, ``output_sheet``).
- ``action_kind`` selects a generated per-ActionDefinition required-field
  branch, so map/reduce kinds require their ``sheet_id`` while derive kinds
  require their own source identity instead of an impossible map field.
- The producer validates those flat fields at their canonical registry owner,
  then creates a
  keyless ``{action_id, scope, params, output_names}`` ActionRequest draft.
  Neither layer accepts a mixed flat+nested shape or execution authorization.
- Silent drops become observable: dropping a proposal logs a warning naming
  the reason (frisket.copilot logger).
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from frisket.actions.core import CreateSheet
from frisket.actions.registry import ACTION_REGISTRY, COPILOT_ACTION_IDS
from frisket.actions.types import discover_references
from frisket.contracts.http.copilot import (
    CopilotRegisteredActionDraft,
    CopilotReply,
)
from frisket.authoring import copilot


_SAVED_AUTHORIZATION_FIELDS = {
    "confirmed",
    "consented_promise_set_hash",
}


def _spec_schema() -> dict:
    schema = copilot.copilot_reply_schema()
    return schema["properties"]["proposals"]["items"]["properties"]["spec"]


def test_spec_schema_declares_the_fields_proposals_need() -> None:
    spec = _spec_schema()
    properties = spec.get("properties", {})

    canonical_fields = {
        name
        for kind in copilot.WIRE_ACTION_KINDS
        for name in (
            copilot._ACTION_CATALOG_BY_KIND[kind].input_schema.get("properties") or {}
        )
        if name not in _SAVED_AUTHORIZATION_FIELDS
    }
    assert set(properties) == {
        "action_kind",
        "sheet_id",
        "output_names",
        "sheet_name",
        *canonical_fields,
    }

    assert spec.get("required") == ["action_kind"]
    variants = spec.get("anyOf", [])
    assert len(variants) == len(copilot.WIRE_ACTION_KINDS)
    required_by_kind = {
        variant["properties"]["action_kind"]["enum"][0]: set(variant["required"])
        for variant in variants
    }
    assert "sheet_id" in required_by_kind["map.summarize"]
    assert "sheet_id" in required_by_kind["map.ner"]
    assert "sheet_id" in required_by_kind["map.template"]
    assert "sheet_id" in required_by_kind["map.clean_dates"]
    assert all("output_names" not in required for required in required_by_kind.values())
    assert {"source", "sheet_name"} <= required_by_kind["derive.table_from_list"]
    assert {"sheet_id", "output_names", "target_sheet_name"}.isdisjoint(
        required_by_kind["derive.table_from_list"]
    )
    assert "sheet_id" not in required_by_kind["derive.collection_expand"]
    assert {
        "source_sheet_id",
        "source_column_id",
        "source_row_id",
        "sheet_name",
    } <= required_by_kind["derive.collection_expand"]

    # action_kind is a closed enum of the wire-carryable map/derive/reduce
    # kinds only (copilot.WIRE_ACTION_KINDS) -- narrower than the full V1
    # taxonomy, since untouched enrich/media/cluster actions remain excluded.
    assert set(properties["action_kind"].get("enum", [])) == copilot.WIRE_ACTION_KINDS

    # Nested schemas are inlined for providers that cannot resolve local
    # ActionDefinition references from this standalone response schema.
    assert "$ref" not in repr(properties)
    field_variants = properties["fields"].get("anyOf", [properties["fields"]])
    assert field_variants
    for variant in field_variants:
        items = variant["items"]
        assert {"name", "type"} <= set(items.get("properties", {}))


def test_live_model_schema_is_flat_and_does_not_ask_the_model_for_wire_metadata() -> (
    None
):
    """The LLM authors canonical fields, never a saved envelope or version."""

    properties = _spec_schema().get("properties", {})
    assert "params" not in properties
    assert "authoring_contract_version" not in properties
    assert "idempotency_key" not in properties
    assert "confirmation" not in properties


def _registered_output_names(kind: str, params: dict) -> dict[str, str]:
    terminal = ACTION_REGISTRY.get(kind).definition.run
    typed_params = terminal.params_model.model_validate(params)
    return {
        field.key: field.key
        for field in terminal.resolve_output_fields(typed_params) or ()
        if not field.hidden
    }


def _flat_target(kind: str, sheet_id: int) -> dict:
    if kind in copilot._REGISTERED_ROW_CREATE_SHEET_KINDS:
        return {"sheet_id": sheet_id, "sheet_name": "Expanded"}
    if kind in COPILOT_ACTION_IDS and isinstance(
        ACTION_REGISTRY.get(kind).definition.run, CreateSheet
    ):
        return {"sheet_name": "Expanded"}
    if kind in COPILOT_ACTION_IDS:
        return {"sheet_id": sheet_id}
    return {}


def test_every_copilot_kind_survives_the_live_proposal_path(tmp_path: Path) -> None:
    """The producer, not the browser, owns proposal-shape canonicalization.

    This is generated from the live Copilot kind set and ActionDefinition
    examples. The live model input is FLAT (``action_kind`` plus canonical
    fields); only the producer creates the nested saved envelope. It
    deliberately has no per-kind translator/exemption table; the remaining
    definition without examples uses explicit schema-valid test params.
    """
    assert COPILOT_ACTION_IDS <= copilot.WIRE_ACTION_KINDS
    cases: list[tuple[str, dict[str, object]]] = []
    for kind in sorted(copilot.WIRE_ACTION_KINDS):
        entry = copilot._ACTION_CATALOG_BY_KIND[kind]
        examples = entry.examples
        assert examples, f"{kind} needs a typed catalog example"
        authored_params = dict(examples[0]["params"])
        params = {
            name: value
            for name, value in authored_params.items()
            if name not in _SAVED_AUTHORIZATION_FIELDS
        }
        output_names = _registered_output_names(kind, params)
        flat_model_spec = {
            "action_kind": kind,
            **_flat_target(kind, 1),
            **params,
            **({"output_names": output_names} if output_names is not None else {}),
        }
        assert "params" not in flat_model_spec
        assert set(flat_model_spec) <= set(_spec_schema()["properties"]), kind
        Draft202012Validator(_spec_schema()).validate(flat_model_spec)
        cases.append((kind, params))

    from frisket.engine.store import Project

    project = Project.create(tmp_path / "p.frisket", name="copilot-live-path")
    try:
        for kind, original_params in cases:
            # Catalog examples may reuse a name for incompatible source types.
            # Give each admitted action its own sheet instead of letting the
            # last example turn another action's text source into audio.
            sheet_id = project.add_sheet(kind)
            typed_references = discover_references(
                ACTION_REGISTRY.get(kind).definition.run.params_model.model_validate(
                    original_params
                )
            )
            typed_column_types = {
                ref.column: ref.accepted_column_types[0]
                for ref in typed_references
                if ref.accepted_column_types
            }
            referenced_names = (
                {"source", "media_source"}
                | {ref.column for ref in typed_references}
                | {
                    value
                    for name, raw in original_params.items()
                    if name == "group_by"
                    or name.endswith("_column")
                    or name.endswith("_columns")
                    for value in (raw if isinstance(raw, list) else [raw])
                    if isinstance(value, str) and value
                }
            )
            column_ids = [
                project.add_column(
                    sheet_id,
                    name,
                    typed_column_types.get(
                        name,
                        "image"
                        if name == "media_source"
                        else "json"
                        if name == "json_source"
                        else "text",
                    ),
                    ai_generated=name == "answer",
                )
                for name in sorted(referenced_names)
            ]
            column_id = column_ids[0]

            def bind_project_ids(value, name: str = ""):
                if name == "sheet_id" or name.endswith("_sheet_id"):
                    return sheet_id
                if name == "column_id" or name.endswith("_column_id"):
                    return column_id
                if isinstance(value, dict):
                    return {
                        key: bind_project_ids(item, key) for key, item in value.items()
                    }
                if isinstance(value, list):
                    return [bind_project_ids(item) for item in value]
                return value

            params = bind_project_ids(deepcopy(original_params))
            validated_params = ACTION_REGISTRY.get(
                kind
            ).definition.run.params_model.model_validate_json(
                json.dumps(params), strict=True
            )
            output_names = _registered_output_names(kind, params)
            flat_model_spec = {
                "action_kind": kind,
                **_flat_target(kind, sheet_id),
                **params,
                **({"output_names": output_names} if output_names is not None else {}),
            }
            raw = {
                "reply": "I drafted an action.",
                "needs_import": False,
                "proposals": [
                    {
                        "kind": kind.split(".", 1)[0],
                        "title": kind,
                        "spec": flat_model_spec,
                    }
                ],
            }
            proposals = copilot.validate_proposals(project, raw)
            assert len(proposals) == 1, kind
            (proposal,) = proposals

            assert {
                ref.column for ref in discover_references(validated_params)
            } <= referenced_names
            expected_spec = {
                "action_id": kind,
                **(
                    {"scope": {"kind": "project"}, "sheet_name": "Expanded"}
                    if isinstance(ACTION_REGISTRY.get(kind).definition.run, CreateSheet)
                    else {"scope": {"kind": "sheet_rows", "sheet_id": sheet_id}}
                ),
                "params": {
                    name: value for name, value in params.items() if value is not None
                },
                "output_names": output_names,
            }
            if kind in copilot._REGISTERED_ROW_CREATE_SHEET_KINDS:
                expected_spec["sheet_name"] = "Expanded"
            assert proposal["spec"] == expected_spec, kind
    finally:
        project.close()


@pytest.mark.parametrize(
    ("action_id", "params", "output_names"),
    [
        (
            "map.clean_dates",
            {"source": "published", "format": None},
            {"cleaned": "published_iso"},
        ),
        (
            "map.template",
            {"template": {"text": "Hello {{name}}"}},
            {"rendered": "greeting"},
        ),
        (
            "map.api_call",
            {
                "request": {
                    "method": "POST",
                    "url": "https://api.example.com/people/{{name}}",
                    "headers": [["Authorization", "Bearer {{secret.API_TOKEN}}"]],
                    "body_mode": "json",
                    "body": '{"published": "{{published}}"}',
                }
            },
            {"api_result": "API response"},
        ),
        (
            "web.capture_screenshot",
            {
                "source": "published",
                "full_page": False,
                "viewport_width": 1440,
                "viewport_height": 900,
                "max_bytes": 7_500_000,
                "timeout_ms": 45_500,
            },
            {"screenshot": "Page image"},
        ),
    ],
)
def test_registered_proposals_save_keyless_action_request_drafts(
    tmp_path: Path,
    action_id: str,
    params: dict[str, object],
    output_names: dict[str, str],
) -> None:
    from frisket.engine.store import Project

    project = Project.create(tmp_path / f"{action_id}.frisket", name=action_id)
    try:
        sheet_id = project.add_sheet("source")
        for column in ("published", "name"):
            project.add_column(sheet_id, column, "text")
        raw = {
            "proposals": [
                {
                    "kind": action_id.split(".", 1)[0],
                    "title": action_id,
                    "spec": {
                        "action_kind": action_id,
                        "sheet_id": sheet_id,
                        **params,
                        "output_names": output_names,
                    },
                }
            ]
        }

        (proposal,) = copilot.validate_proposals(project, raw)

        assert proposal["spec"] == {
            "action_id": action_id,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {
                name: value for name, value in params.items() if value is not None
            },
            "output_names": output_names,
        }
        assert "idempotency_key" not in proposal["spec"]
        assert "confirmation" not in proposal["spec"]
        CopilotReply.model_validate(
            {
                "reply": "Drafted.",
                "needs_import": False,
                "proposals": [proposal],
            }
        )
    finally:
        project.close()


@pytest.mark.parametrize("output_names", [None, {"name": "Person"}])
def test_copilot_list_table_draft_runs_through_public_registered_dispatch(
    tmp_path, output_names
):
    from frisket.engine.executor import run_action_spec
    from frisket.engine.store import Project

    project = Project.create(tmp_path / "list-table.frisket")
    try:
        sheet_id = project.add_sheet("Source")
        column_id = project.add_column(sheet_id, "people", "json")
        project.add_rows(
            sheet_id,
            [{"people": [{"name": "Ada"}, {"name": "Grace"}]}],
            {"people": column_id},
        )
        spec = {
            "action_kind": "derive.table_from_list",
            "sheet_name": "People",
            "source": {
                "kind": "column",
                "sheet_id": sheet_id,
                "column_id": column_id,
                "include_columns": ["name"],
            },
            **({"output_names": output_names} if output_names is not None else {}),
        }
        Draft202012Validator(_spec_schema()).validate(spec)
        [proposal] = copilot.validate_proposals(
            project,
            {"proposals": [{"kind": "derive", "title": "People", "spec": spec}]},
        )
        wire = CopilotReply.model_validate(
            {"reply": "Drafted.", "needs_import": False, "proposals": [proposal]}
        ).model_dump(mode="json")
        draft = wire["proposals"][0]["spec"]
        assert draft == {
            "action_id": "derive.table_from_list",
            "scope": {"kind": "project"},
            "sheet_name": "People",
            "params": {"source": spec["source"]},
            "output_names": output_names or {},
        }
        Draft202012Validator(CopilotRegisteredActionDraft.model_json_schema()).validate(
            draft
        )
        result = run_action_spec(
            project,
            {**draft, "idempotency_key": "copilot-people"},
            project_id="p",
        )
        assert result.status == "completed", result.errors
        child = next(output for output in result.outputs if output.kind == "sheet")
        columns = project.columns(child.sheet_id)
        assert [column["name"] for column in columns] == [
            "Person" if output_names else "name"
        ]
        assert list(project.get_values(child.sheet_id, columns[0]["id"]).values()) == [
            "Ada",
            "Grace",
        ]
    finally:
        project.close()


def test_copilot_named_list_draft_preserves_schema_projection_and_target():
    params = {
        "source": {
            "kind": "named_result",
            "sheet_id": 1,
            "column_id": 2,
            "run_id": 3,
            "route": "entities",
            "schema": "entities.v1",
        },
        "item_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        "columns": [{"name": "name", "path": "$.name", "type": "text"}],
    }
    spec = {
        "action_kind": "derive.table_from_list",
        "sheet_name": "People",
        "output_names": {"name": "Person"},
        **params,
    }
    Draft202012Validator(_spec_schema()).validate(spec)
    coerced = copilot._coerce_proposal("derive", spec)
    assert coerced is not None
    draft, references = coerced
    assert draft["params"] == params
    assert draft["sheet_name"] == "People"
    assert draft["output_names"] == {"name": "Person"}
    assert references == ()


@pytest.mark.parametrize("missing", ["sheet", "column"])
def test_copilot_project_draft_rejects_unknown_nested_source(tmp_path, missing):
    from frisket.engine.store import Project

    project = Project.create(tmp_path / "missing-list-source.frisket")
    try:
        sheet_id = project.add_sheet("Source")
        column_id = project.add_column(sheet_id, "people", "json")
        assert (
            copilot.validate_proposals(
                project,
                {
                    "proposals": [
                        {
                            "kind": "derive",
                            "title": "People",
                            "spec": {
                                "action_kind": "derive.table_from_list",
                                "sheet_name": "People",
                                "source": {
                                    "kind": "column",
                                    "sheet_id": sheet_id + 1
                                    if missing == "sheet"
                                    else sheet_id,
                                    "column_id": column_id + 1
                                    if missing == "column"
                                    else column_id,
                                },
                            },
                        }
                    ]
                },
            )
            == []
        )
    finally:
        project.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": {"kind": "sheet_rows", "sheet_id": 1}},
        {"sheet_name": None},
        {"sheet_name": ""},
        {"action_id": "import.rows"},
    ],
)
def test_registered_table_wire_refuses_wrong_target_and_unadmitted_import(changes):
    draft = {
        "action_id": "derive.table_from_list",
        "scope": {"kind": "project"},
        "sheet_name": "People",
        "params": {"source": {"kind": "column", "sheet_id": 1, "column_id": 2}},
        **changes,
    }
    with pytest.raises(ValidationError):
        CopilotRegisteredActionDraft.model_validate(draft)
    assert not Draft202012Validator(
        CopilotRegisteredActionDraft.model_json_schema()
    ).is_valid(draft)


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": {"kind": "project"}},
        {"sheet_name": "Unexpected"},
        {"output_names": []},
    ],
)
def test_registered_row_wire_keeps_scope_and_output_requirements(changes):
    draft = {
        "action_id": "map.template",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "params": {"template": {"text": "{{name}}"}},
        "output_names": {"rendered": "Greeting"},
        **changes,
    }
    with pytest.raises(ValidationError):
        CopilotRegisteredActionDraft.model_validate(draft)
    assert not Draft202012Validator(
        CopilotRegisteredActionDraft.model_json_schema()
    ).is_valid(draft)


def test_registered_derive_wire_rejects_wrong_proposal_family():
    draft = {
        "action_id": "derive.table_from_list",
        "scope": {"kind": "project"},
        "sheet_name": "People",
        "params": {"source": {"kind": "column", "sheet_id": 1, "column_id": 2}},
    }
    with pytest.raises(ValidationError):
        CopilotReply.model_validate(
            {
                "reply": "Drafted.",
                "needs_import": False,
                "proposals": [{"kind": "map", "title": "People", "spec": draft}],
            }
        )


@pytest.mark.parametrize(
    ("action_id", "params", "output_names"),
    [
        ("map.clean_dates", {"source": "missing"}, {"cleaned": "cleaned"}),
        (
            "map.template",
            {"template": {"text": "{{missing}}"}},
            {"rendered": "rendered"},
        ),
    ],
)
def test_registered_proposals_reject_unknown_semantic_sources(
    tmp_path: Path,
    action_id: str,
    params: dict[str, object],
    output_names: dict[str, str],
) -> None:
    from frisket.engine.store import Project

    project = Project.create(tmp_path / f"missing-{action_id}.frisket", name=action_id)
    try:
        sheet_id = project.add_sheet("source")
        project.add_column(sheet_id, "known", "text")

        assert (
            copilot.validate_proposals(
                project,
                {
                    "proposals": [
                        {
                            "kind": "map",
                            "title": action_id,
                            "spec": {
                                "action_kind": action_id,
                                "sheet_id": sheet_id,
                                **params,
                                "output_names": output_names,
                            },
                        }
                    ]
                },
            )
            == []
        )
    finally:
        project.close()


def test_registered_proposals_reject_incompatible_semantic_source_types(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from frisket.engine.store import Project

    project = Project.create(tmp_path / "wrong-type.frisket", name="wrong-type")
    try:
        sheet_id = project.add_sheet("source")
        project.add_column(sheet_id, "payload", "integer")

        assert (
            copilot.validate_proposals(
                project,
                {
                    "proposals": [
                        {
                            "kind": "map",
                            "title": "Extract text",
                            "spec": {
                                "action_kind": "map.regex_extract",
                                "sheet_id": sheet_id,
                                "input_columns": ["payload"],
                                "pattern": r"\w+",
                                "output_names": {"extracted": "extracted"},
                            },
                        }
                    ]
                },
            )
            == []
        )
        dropped = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "copilot_proposal_dropped"
        ]
        assert len(dropped) == 1
        assert getattr(dropped[0], "reason", None) == "incompatible_input_columns"
        assert getattr(dropped[0], "columns", None) == [
            {
                "name": "payload",
                "actual_type": "integer",
                "accepted_column_types": [
                    "text",
                    "timestamped_transcript",
                    "category",
                ],
            }
        ]
    finally:
        project.close()


def test_registered_proposal_output_names_are_exact_and_execution_fields_refused() -> (
    None
):
    base = {
        "action_kind": "map.template",
        "sheet_id": 1,
        "template": {"text": "{{name}}"},
    }

    default_names = copilot._coerce_proposal("map", base)
    assert default_names is not None
    assert default_names[0]["output_names"] == {}
    assert (
        copilot._coerce_proposal("map", {**base, "output_names": {"unknown": "value"}})
        is None
    )
    assert (
        copilot._coerce_proposal(
            "map",
            {
                **base,
                "output_names": {"rendered": "value"},
                "idempotency_key": "model-authored-key",
            },
        )
        is None
    )

    saved_with_key = {
        "reply": "Drafted.",
        "needs_import": False,
        "proposals": [
            {
                "kind": "map",
                "title": "Template",
                "spec": {
                    "action_id": "map.template",
                    "scope": {"kind": "sheet_rows", "sheet_id": 1},
                    "params": {"template": {"text": "{{name}}"}},
                    "output_names": {"rendered": "value"},
                    "idempotency_key": "saved-key",
                },
            }
        ],
    }
    with pytest.raises(ValidationError):
        CopilotReply.model_validate(saved_with_key)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("params", {}),
        ("authoring_contract_version", 1),
        ("idempotency_key", "model-authored-key"),
        ("confirmation", None),
        ("confirmed", False),
        ("consented_promise_set_hash", "model-authored-consent"),
    ],
)
def test_native_fill_pruning_preserves_forbidden_fields_for_rejection(
    field: str, value: object
) -> None:
    spec = {
        "action_kind": "map.template",
        "sheet_id": 1,
        "template": {"text": "{{name}}"},
        "output_names": {"rendered": "value"},
        field: value,
    }

    pruned = copilot._prune_native_fill(spec)

    assert field in pruned
    assert copilot._coerce_proposal("map", pruned) is None


def test_copilot_producer_rejects_mixed_flat_and_nested_params() -> None:
    flat = {
        "action_kind": "map.summarize",
        "sheet_id": 1,
        "source": ["body"],
        "model": "anthropic/claude-haiku-4-5",
        "instruction": "Summarize the body.",
        "output_names": {"summary": "summary"},
    }

    assert (
        copilot._coerce_proposal(
            "map",
            {
                **flat,
                "params": {
                    name: value for name, value in flat.items() if name != "action_kind"
                },
            },
        )
        is None
    )


def test_copilot_producer_rejects_recipe_key_as_an_action_kind() -> None:
    assert (
        copilot._coerce_proposal("map", {"recipe": "classify", "sheet_id": 1}) is None
    )


def test_copilot_producer_rejects_action_kind_shorthand() -> None:
    assert copilot._coerce_proposal("map", {"action_kind": "classify"}) is None


def test_validate_proposals_preserves_schema_owned_false_zero_and_template(
    tmp_path,
) -> None:
    """Union-schema filler pruning must never erase typed authored values."""

    from frisket.engine.store import Project

    project = Project.create(tmp_path / "p.frisket", name="contacts")
    try:
        sheet_id = project.add_sheet("contacts")
        project.add_column(sheet_id, "email", "text")
        raw = {
            "reply": "I drafted a clean-column action.",
            "needs_import": False,
            "proposals": [
                {
                    "kind": "map",
                    "title": "Clean email",
                    "spec": {
                        "action_kind": "map.clean_column",
                        "sheet_id": sheet_id,
                        "source": "email",
                        "lowercase_emails": False,
                        "output_names": {"cleaned": "cleaned"},
                    },
                },
                {
                    "kind": "map",
                    "title": "Find named entities",
                    "spec": {
                        "action_kind": "map.ner",
                        "sheet_id": sheet_id,
                        "source": ["email"],
                        "labels": ["person"],
                        "threshold": 0.0,
                        "output_names": {"entities": "entities"},
                    },
                },
                {
                    "kind": "map",
                    "title": "Classify templated email",
                    "spec": {
                        "action_kind": "map.classify",
                        "sheet_id": sheet_id,
                        "source": {"text": "{{email}}"},
                        "engine": "llm",
                        "model": "anthropic/claude-haiku-4-5",
                        "fields": [
                            {
                                "name": "kind",
                                "type": "category",
                                "labels": ["work", "personal"],
                            }
                        ],
                        "output_names": {"kind": "kind"},
                    },
                },
            ],
        }

        clean, ner, classify = copilot.validate_proposals(project, raw)
        assert clean["spec"]["params"]["lowercase_emails"] is False
        assert ner["spec"]["params"]["threshold"] == 0.0
        assert "sheet_id" not in ner["spec"]["params"]
        assert ner["spec"]["scope"] == {
            "kind": "sheet_rows",
            "sheet_id": sheet_id,
        }
        assert classify["spec"]["params"]["source"] == {"text": "{{email}}"}
    finally:
        project.close()


def test_public_copilot_wire_accepts_only_the_exact_typed_envelope() -> None:
    params = {
        "source": ["body"],
        "model": "anthropic/claude-haiku-4-5",
        "instruction": "Summarize the body.",
    }
    envelope = {
        "action_id": "map.summarize",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "params": params,
        "output_names": {"summary": "summary"},
    }
    reply = {
        "schema_version": "frisket.copilot_reply.v1",
        "reply": "I drafted a summarize action.",
        "needs_import": False,
        "proposals": [{"kind": "map", "title": "Summarize", "spec": envelope}],
        "cost_usd": None,
    }

    assert (
        CopilotReply.model_validate(reply).model_dump(mode="json")["proposals"][0][
            "spec"
        ]
        == envelope
    )
    with pytest.raises(ValidationError):
        CopilotReply.model_validate(
            {
                **reply,
                "proposals": [
                    {
                        "kind": "map",
                        "title": "Summarize",
                        "spec": {
                            "action_kind": "map.summarize",
                            "sheet_id": 1,
                            **params,
                            "output_names": {"summary": "summary"},
                        },
                    }
                ],
            }
        )
    with pytest.raises(ValidationError):
        CopilotReply.model_validate(
            {
                **reply,
                "proposals": [
                    {
                        "kind": "map",
                        "title": "Summarize",
                        "spec": {**envelope, "action_kind": "map.summarize"},
                    }
                ],
            }
        )


def test_public_copilot_wire_pins_ner_scope_outside_params() -> None:
    spec = {
        "action_id": "map.ner",
        "params": {
            "source": ["body"],
            "labels": ["person"],
            "threshold": 0.0,
        },
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": 1,
        },
        "output_names": {"entities": "entities"},
    }
    reply = {
        "schema_version": "frisket.copilot_reply.v1",
        "reply": "I drafted a named-entity action.",
        "needs_import": False,
        "proposals": [{"kind": "map", "title": "Find names", "spec": spec}],
        "cost_usd": None,
    }

    dumped = CopilotReply.model_validate(reply).model_dump(mode="json")
    assert dumped["proposals"][0]["spec"] == spec

    duplicate_params = deepcopy(reply)
    duplicate_params["proposals"][0]["spec"]["sheet_id"] = 1
    with pytest.raises(ValidationError):
        CopilotReply.model_validate(duplicate_params)

    missing_scope = deepcopy(reply)
    del missing_scope["proposals"][0]["spec"]["scope"]
    with pytest.raises(ValidationError):
        CopilotReply.model_validate(missing_scope)


def test_native_fill_noise_is_pruned_before_canonical_validation(tmp_path) -> None:
    """Native structured output fills EVERY declared property (live gemini
    evidence: prompt "", match_threshold 0.0, model "gemini-2.5-flash" with
    no provider prefix — unroutable). The wire spec keeps real intent only."""
    from frisket.engine.store import Project

    project = Project.create(tmp_path / "p.frisket", name="articles")
    sheet_id = project.add_sheet("articles")
    project.add_column(sheet_id, "body", "text")

    raw = {
        "reply": "…",
        "needs_import": False,
        "proposals": [
            {
                "kind": "map",
                "title": "Summarize article",
                "spec": {
                    "action_kind": "map.summarize",
                    "sheet_id": sheet_id,
                    "source": ["body"],
                    "instruction": "Summarize the article.",
                    "output_names": {"summary": "summary"},
                    # native-mode fill noise:
                    "prompt": "",
                    "pattern": "",
                    "carry_columns": [],
                    "match_threshold": 0.0,
                    "frame_count": 0,
                    "include_justification": False,
                    "model": "gemini/gemini-2.5-flash",
                },
            }
        ],
    }
    (proposal,) = copilot.validate_proposals(project, raw)
    spec = proposal["spec"]

    assert spec == {
        "action_id": "map.summarize",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["body"],
            "instruction": "Summarize the article.",
            "model": "gemini/gemini-2.5-flash",
        },
        "output_names": {"summary": "summary"},
    }


def test_registered_model_proposal_rejects_incompatible_template_source(
    tmp_path: Path,
) -> None:
    from frisket.engine.store import Project

    project = Project.create(tmp_path / "p.frisket", name="places")
    try:
        sheet_id = project.add_sheet("places")
        project.add_column(sheet_id, "point", "geo_point")
        raw = {
            "proposals": [
                {
                    "kind": "map",
                    "title": "Ask about the point",
                    "spec": {
                        "action_kind": "map.ask",
                        "sheet_id": sheet_id,
                        "source": {"text": "{{point}}"},
                        "model": "anthropic/claude-haiku-4-5",
                        "question": "Where is this?",
                        "output_names": {"answer": "answer"},
                    },
                }
            ]
        }

        assert copilot.validate_proposals(project, raw) == []
    finally:
        project.close()


def test_copilot_producer_rejects_bare_model_ids(tmp_path) -> None:
    from frisket.engine.store import Project

    project = Project.create(tmp_path / "p.frisket", name="articles")
    sheet_id = project.add_sheet("articles")
    project.add_column(sheet_id, "body", "text")

    raw = {
        "reply": "…",
        "needs_import": False,
        "proposals": [
            {
                "kind": "map",
                "title": "Summarize article",
                "spec": {
                    "action_kind": "map.summarize",
                    "sheet_id": sheet_id,
                    "source": ["body"],
                    "instruction": "Summarize the article.",
                    "output_name": "summary",
                    "model": "gemini-2.5-flash",
                },
            }
        ],
    }

    assert copilot.validate_proposals(project, raw) == []


def test_copilot_drops_column_transform_that_fails_live_type_preflight(
    tmp_path,
) -> None:
    from frisket.engine.store import Project

    project = Project.create(tmp_path / "p.frisket", name="articles")
    sheet_id = project.add_sheet("articles")
    project.add_column(sheet_id, "body", "text")
    raw = {
        "reply": "…",
        "needs_import": False,
        "proposals": [
            {
                "kind": "resolve",
                "title": "Fill with the mean",
                "spec": {
                    "action_kind": "resolve.fill_missing",
                    "sheet_id": sheet_id,
                    "source": "body",
                    "method": "mean",
                    "output_names": {"cleaned": "body_filled"},
                },
            }
        ],
    }

    assert copilot.validate_proposals(project, raw) == []


def test_dropped_proposals_log_a_warning(tmp_path, caplog) -> None:
    """The live-repro failure mode — a proposal arriving without sheet_id —
    must at least be observable in the server log, never a silent vanish."""
    from frisket.engine.store import Project

    project = Project.create(tmp_path / "p.frisket", name="places")
    sheet_id = project.add_sheet("places")
    project.add_column(sheet_id, "location", "text")

    raw = {
        "reply": "I can create an action…",
        "needs_import": False,
        "proposals": [
            {"kind": "map", "title": "Extract state from location", "spec": {}}
        ],
    }
    with caplog.at_level(logging.WARNING, logger="frisket.copilot"):
        result = copilot.validate_proposals(project, raw)

    assert result == []
    assert any("dropped" in record.message.lower() for record in caplog.records)
