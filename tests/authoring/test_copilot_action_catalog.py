"""Drift guard for the generated action catalog (copilot-action-catalog-v1,
first pass): the model spec's ``action_kind`` enum, the registered draft's
``action_id`` Literal values, and the generated one-liner catalog inlined in the
system prompt must all name exactly the same wire-carryable
map/resolve/derive/reduce/media/enrich/web/research kinds.
A new proposal-capable kind that drifts out of the wire union, or a
schema/catalog that stops tracking it, fails here before it fails a user.

Scope note: this only proves the wire-carryable kinds stay in sync.
Actions listed in
``COPILOT_UNSUPPORTED_ACTION_KINDS`` are not in the wire union and are out of
scope for this pass.
"""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from frisket.authoring import copilot
from frisket.actions.registry import COPILOT_ACTION_IDS, NEW_ACTION_IDS
from frisket.contracts.http.copilot import (
    COPILOT_UNSUPPORTED_ACTION_KINDS,
    CopilotDeriveProposal,
    CopilotMapProposal,
    CopilotRegisteredActionDraft,
    CopilotReduceProposal,
    CopilotReply,
    CopilotWebProposal,
)


def _wire_union_kinds() -> set[str]:
    """The action_id Literal values actually accepted by the closed wire
    union — computed independently of copilot.WIRE_ACTION_KINDS by
    introspecting the pydantic models themselves."""
    kinds = set(
        get_args(CopilotRegisteredActionDraft.model_fields["action_id"].annotation)
    )
    return kinds


def test_schema_enum_matches_wire_union_matches_catalog() -> None:
    schema_kinds = set(
        copilot._proposal_spec_schema()["properties"]["action_kind"]["enum"]
    )
    wire_kinds = _wire_union_kinds()

    assert schema_kinds == wire_kinds
    assert wire_kinds == copilot.WIRE_ACTION_KINDS
    assert len(wire_kinds) == len(copilot.WIRE_ACTION_KINDS)

    registered_kinds = set(
        get_args(CopilotRegisteredActionDraft.model_fields["action_id"].annotation)
    )
    assert registered_kinds == COPILOT_ACTION_IDS
    assert COPILOT_ACTION_IDS <= NEW_ACTION_IDS
    for model in (CopilotMapProposal, CopilotReduceProposal):
        assert model.model_fields["spec"].annotation is CopilotRegisteredActionDraft
    assert (
        CopilotDeriveProposal.model_fields["spec"].annotation
        is CopilotRegisteredActionDraft
    )
    assert "derive.link_table" in registered_kinds
    assert "map.api_call" in registered_kinds
    assert "web.capture_screenshot" in registered_kinds
    assert "research.answer" in registered_kinds
    assert (
        CopilotWebProposal.model_fields["spec"].annotation
        is CopilotRegisteredActionDraft
    )
    assert "derive.table_from_list" in registered_kinds
    assert "import.rows" not in registered_kinds

    for kind in wire_kinds:
        assert kind in copilot.ACTION_CATALOG, f"catalog is missing {kind}"


@pytest.mark.parametrize(
    "action_id", ["map.extract", "reduce.group_summary", "research.answer"]
)
def test_proposals_reject_legacy_envelopes_and_mismatched_families(
    action_id: str,
) -> None:
    family = action_id.split(".", 1)[0]
    spec = {
        "action_id": action_id,
        "scope": {"kind": "sheet_rows", "sheet_id": 7},
        "params": {},
        "output_names": {"result": "Result"},
    }
    if action_id in copilot._REGISTERED_CREATE_SHEET_KINDS:
        spec["sheet_name"] = "Results"
    reply = {
        "reply": "Drafted.",
        "needs_import": False,
        "proposals": [{"kind": family, "title": "Draft", "spec": spec}],
    }
    assert (
        CopilotReply.model_validate(reply).proposals[0].root.spec.action_id == action_id
    )
    reply["proposals"][0]["kind"] = "derive"
    with pytest.raises(ValidationError):
        CopilotReply.model_validate(reply)
    reply["proposals"][0] = {
        "kind": family,
        "title": "Old draft",
        "spec": {
            "action_kind": action_id,
            "authoring_contract_version": 1,
            "params": {},
        },
    }
    with pytest.raises(ValidationError):
        CopilotReply.model_validate(reply)


def test_registered_resolve_proposal_reaches_the_typed_draft_path() -> None:
    coerced = copilot._coerce_proposal(
        "resolve",
        {
            "action_kind": "resolve.substitute",
            "sheet_id": 7,
            "source": "company",
            "mapping": {"ACME": "Acme"},
            "output_names": {"cleaned": "company_clean"},
        },
    )

    assert coerced is not None
    draft, references = coerced
    assert draft["action_id"] == "resolve.substitute"
    assert draft["scope"] == {"kind": "sheet_rows", "sheet_id": 7}
    assert references is not None and references[0].column == "company"


@pytest.mark.parametrize("kind", ["derive.join", "join.semantic"])
def test_join_actions_remain_runnable_without_advertising_unsupported_proposals(kind):
    from frisket.actions.registry import ACTION_REGISTRY

    assert ACTION_REGISTRY.get(kind).catalog_entry()["kind"] == kind
    assert kind in NEW_ACTION_IDS
    assert kind in COPILOT_UNSUPPORTED_ACTION_KINDS
    assert kind not in COPILOT_ACTION_IDS
    assert kind not in _wire_union_kinds()
    assert kind not in copilot.WIRE_ACTION_KINDS
    assert copilot._coerce_proposal(kind.split(".")[0], {"action_kind": kind}) is None


@pytest.mark.parametrize("family", ["media", "map"])
def test_registered_screenshot_rejects_the_wrong_proposal_family(family: str) -> None:
    draft = {
        "action_id": "web.capture_screenshot",
        "scope": {"kind": "sheet_rows", "sheet_id": 7},
        "params": {"source": "URL", "full_page": False},
        "output_names": {"screenshot": "Page image"},
    }
    reply = {
        "reply": "Capture this page.",
        "needs_import": False,
        "proposals": [{"kind": "web", "title": "Capture", "spec": draft}],
    }
    assert (
        CopilotReply.model_validate(reply).model_dump(mode="json")["proposals"][0][
            "spec"
        ]
        == draft
    )
    reply["proposals"][0]["kind"] = family
    with pytest.raises(ValidationError):
        CopilotReply.model_validate(reply)


@pytest.mark.parametrize(
    ("kind", "params", "columns", "output_count"),
    [
        (
            "enrich.geocode",
            {"source": "address", "engine": "nominatim", "include_lat_lon": False},
            ["address"],
            2,
        ),
        (
            "enrich.geocode",
            {
                "source": {"text": "{{street}}, {{city}}"},
                "engine": "auto",
                "include_lat_lon": True,
            },
            ["street", "city"],
            4,
        ),
        (
            "enrich.census_demographics",
            {"source": "point", "geography": "tract", "include_moe": False},
            ["point"],
            19,
        ),
        (
            "enrich.census_demographics",
            {"source": "point", "geography": "block_group", "include_moe": True},
            ["point"],
            22,
        ),
    ],
)
def test_registered_enrichment_proposal_preserves_source_options_and_active_names(
    kind: str, params: dict, columns: list[str], output_count: int
) -> None:
    from jsonschema import Draft202012Validator

    from frisket.actions.registry import ACTION_REGISTRY

    registered = ACTION_REGISTRY.get(kind)
    terminal = registered.definition.run
    fields = terminal.resolve_output_fields(
        terminal.params_model.model_validate(params)
    )
    output_names = {field.key: f"saved_{field.key}" for field in fields}
    spec = {"action_kind": kind, "sheet_id": 7, **params, "output_names": output_names}
    Draft202012Validator(copilot._proposal_spec_schema()).validate(spec)
    coerced = copilot._coerce_proposal("enrich", spec)
    assert coerced is not None
    draft, references = coerced
    assert draft == {
        "action_id": kind,
        "scope": {"kind": "sheet_rows", "sheet_id": 7},
        "params": params,
        "output_names": output_names,
    }
    assert len(output_names) == output_count
    assert references is not None
    assert [reference.column for reference in references] == columns
    assert {"confirmation", "capabilities", "idempotency_key"}.isdisjoint(draft)
    proposal = {"kind": "enrich", "title": "Enrich places", "spec": draft}
    reply = {"reply": "Drafted.", "needs_import": False, "proposals": [proposal]}
    CopilotReply.model_validate(reply)
    for wrong_family in ("map", "media", "derive", "resolve"):
        assert copilot._coerce_proposal(wrong_family, spec) is None
        with pytest.raises(ValidationError):
            CopilotReply.model_validate(
                {**reply, "proposals": [{**proposal, "kind": wrong_family}]}
            )
    default_names = copilot._coerce_proposal("enrich", {**spec, "output_names": {}})
    assert default_names is not None
    assert default_names[0]["output_names"] == {}


@pytest.mark.parametrize("mode", ["object", "columns"])
def test_registered_metadata_proposal_preserves_typed_intent_and_active_names(
    mode: str,
) -> None:
    from jsonschema import Draft202012Validator

    from frisket.actions.registry import ACTION_REGISTRY

    registered = ACTION_REGISTRY.get("media.extract_metadata")
    terminal = registered.definition.run
    params = {"source": "asset", "output_mode": mode, "refresh": True}
    fields = terminal.resolve_output_fields(
        terminal.params_model.model_validate(params)
    )
    output_names = {field.key: f"asset_{field.key}" for field in fields}
    spec = {
        "action_kind": registered.action_id,
        "sheet_id": 7,
        **params,
        "output_names": output_names,
    }
    Draft202012Validator(copilot._proposal_spec_schema()).validate(spec)
    coerced = copilot._coerce_proposal("media", spec)
    assert coerced is not None
    draft, references = coerced
    assert draft == {
        "action_id": "media.extract_metadata",
        "scope": {"kind": "sheet_rows", "sheet_id": 7},
        "params": params,
        "output_names": output_names,
    }
    assert len(output_names) == (1 if mode == "object" else 24)
    assert references is not None and len(references) == 1
    assert references[0].column == "asset"
    assert references[0].accepted_column_types == ("image", "audio", "video", "file")
    assert set(registered.catalog_entry()["required_capabilities"]) == {
        "project:write",
        "project:read",
    }
    assert {"confirmation", "idempotency_key", "capabilities"}.isdisjoint(draft)
    proposal = {"kind": "media", "title": "Read metadata", "spec": draft}
    reply = {"reply": "Drafted.", "needs_import": False, "proposals": [proposal]}
    CopilotReply.model_validate(reply)
    with pytest.raises(ValidationError):
        CopilotReply.model_validate(
            {**reply, "proposals": [{**proposal, "kind": "map"}]}
        )
    assert copilot._coerce_proposal("map", spec) is None
    if mode == "columns":
        partial = copilot._coerce_proposal(
            "media", {**spec, "output_names": {"details": "asset_details"}}
        )
        assert partial is not None
        assert partial[0]["output_names"] == {"details": "asset_details"}
    else:
        assert (
            copilot._coerce_proposal(
                "media",
                {
                    **spec,
                    "output_names": {**output_names, "filename": "asset_filename"},
                },
            )
            is None
        )
    assert {"media.ocr", "media.transcribe"} <= copilot.WIRE_ACTION_KINDS


def test_registered_list_table_proposal_reaches_project_draft_without_inference() -> (
    None
):
    coerced = copilot._coerce_proposal(
        "derive",
        {
            "action_kind": "derive.table_from_list",
            "sheet_name": "People",
            "source": {"kind": "column", "sheet_id": 7, "column_id": 9},
        },
    )

    assert coerced is not None
    draft, references = coerced
    assert draft == {
        "action_id": "derive.table_from_list",
        "scope": {"kind": "project"},
        "sheet_name": "People",
        "params": {"source": {"kind": "column", "sheet_id": 7, "column_id": 9}},
        "output_names": {},
    }
    assert references == ()
    with pytest.raises(ValidationError):
        CopilotRegisteredActionDraft.model_validate(
            {
                "action_kind": "derive.table_from_list",
                "authoring_contract_version": 1,
                "params": {
                    "source": {"kind": "column", "sheet_id": 7, "column_id": 9},
                    "target_sheet_name": "People",
                },
            }
        )


def test_actions_with_unrepresentable_nested_params_are_not_proposed() -> None:
    wire_kinds = _wire_union_kinds()

    assert COPILOT_UNSUPPORTED_ACTION_KINDS == {
        "derive.join",
        "join.semantic",
        "derive.temporal_segments",
        "derive.transcript_segments",
    }
    assert wire_kinds.isdisjoint(COPILOT_UNSUPPORTED_ACTION_KINDS)


def test_link_table_proposal_uses_typed_params_and_receipt_wide_default():
    spec = {
        "action_kind": "derive.link_table",
        "sheet_name": "Reviewed links",
        "source": {"kind": "semantic_join", "receipt_id": "semantic-receipt"},
        "include_unmatched": True,
        "output_names": {"target_value": "Company"},
    }
    coerced = copilot._coerce_proposal("derive", spec)
    assert coerced is not None
    draft, references = coerced
    assert draft["scope"] == {"kind": "project"}
    assert draft["params"] == {"source": spec["source"], "include_unmatched": True}
    assert draft["sheet_name"] == "Reviewed links"
    assert draft["output_names"] == {"target_value": "Company"}
    assert references == ()
    assert (
        CopilotDeriveProposal(kind="derive", title="Links", spec=draft).spec.action_id
        == "derive.link_table"
    )


@pytest.mark.parametrize("selection", [[3, 1], [], None])
def test_link_table_selected_draft_schema_and_python_agree(selection):
    from jsonschema import Draft202012Validator

    draft = {
        "action_id": "derive.link_table",
        "scope": {"kind": "sheet_rows", "sheet_id": 7, "row_ids": selection},
        "params": {
            "source": {"kind": "semantic_join", "receipt_id": "semantic-receipt"}
        },
        "sheet_name": "Links",
        "output_names": {},
    }
    schema_errors = list(
        Draft202012Validator(
            CopilotRegisteredActionDraft.model_json_schema()
        ).iter_errors(draft)
    )
    if selection:
        assert not schema_errors
        assert (
            CopilotRegisteredActionDraft.model_validate(draft).scope.row_ids
            == selection
        )
    else:
        assert schema_errors
        with pytest.raises(ValidationError):
            CopilotRegisteredActionDraft.model_validate(draft)


def test_derive_join_is_absent_from_generated_copilot_contracts() -> None:
    """The action is migrated and runnable, but its typed draft is not
    representable by the current proposal object. Pin both server-generated
    action-kind projections so regeneration cannot advertise a lossy wire."""

    prompt_schema_kinds = set(
        copilot._proposal_spec_schema()["properties"]["action_kind"]["enum"]
    )
    wire_schema_kinds = _wire_union_kinds()

    assert "derive.join" in COPILOT_UNSUPPORTED_ACTION_KINDS
    assert "derive.join" not in prompt_schema_kinds
    assert "derive.join" not in wire_schema_kinds
    with pytest.raises(ValidationError):
        CopilotRegisteredActionDraft.model_validate(
            {
                "action_id": "derive.join",
                "scope": {"kind": "project"},
                "params": {},
                "sheet_name": "Joined",
                "output_names": {},
            }
        )


@pytest.mark.parametrize(
    "kind",
    ["derive.temporal_segments", "derive.transcript_segments"],
)
def test_temporal_child_sheet_actions_are_absent_from_generated_copilot_contracts(
    kind: str,
) -> None:
    """Native proposals cannot carry selection/row/target child-sheet state."""

    prompt_schema_kinds = set(
        copilot._proposal_spec_schema()["properties"]["action_kind"]["enum"]
    )
    wire_schema_kinds = _wire_union_kinds()

    assert kind in COPILOT_UNSUPPORTED_ACTION_KINDS
    assert kind not in prompt_schema_kinds
    assert kind not in wire_schema_kinds
    with pytest.raises(ValidationError):
        CopilotRegisteredActionDraft.model_validate(
            {
                "action_id": kind,
                "scope": {"kind": "project"},
                "params": {},
                "sheet_name": "Segments",
                "output_names": {},
            }
        )
