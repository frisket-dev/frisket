from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import ActionRequest
from frisket.authoring import copilot
from frisket.contracts.http.copilot import CopilotRegisteredActionDraft


@pytest.mark.parametrize(
    "kind", ["map.ner", "map.classify", "reduce.group_summary", "media.ytdlp_download"]
)
@pytest.mark.parametrize("renames", ["omitted", "empty", "partial"])
def test_copilot_rename_defaults_match_host_admission(kind, renames):
    registered = ACTION_REGISTRY.get(kind)
    request = dict(registered.catalog_entry()["examples"][0])
    request.pop("output_names", None)
    _, fields = registered.bind_request(ActionRequest.model_validate(request))
    assert fields
    output_names = {fields[0].key: "Renamed output"} if renames == "partial" else {}
    if renames != "omitted":
        request["output_names"] = output_names
    registered.bind_request(ActionRequest.model_validate(request))
    draft = {
        key: value
        for key, value in request.items()
        if key not in {"idempotency_key", "replace_existing"}
    }
    CopilotRegisteredActionDraft.model_validate(draft)
    Draft202012Validator(CopilotRegisteredActionDraft.model_json_schema()).validate(
        draft
    )

    spec = {
        "action_kind": kind,
        **request["params"],
        "sheet_id": request["scope"]["sheet_id"],
        **({"sheet_name": request["sheet_name"]} if "sheet_name" in request else {}),
        **({"output_names": output_names} if renames != "omitted" else {}),
    }
    Draft202012Validator(copilot._proposal_spec_schema()).validate(spec)
    result = copilot._coerce_proposal(kind.split(".")[0], spec)
    assert result is not None
    authored, _ = result
    assert authored == {**draft, "output_names": output_names}


@pytest.mark.parametrize(
    "output_names",
    [
        {"unknown": "Result"},
        {"summary": ""},
        {"summary": " "},
        {"summary": " Result"},
        {" summary": "Result"},
        {"summary": "Same", "group": "Same"},
        {"summary": "group"},
    ],
)
def test_copilot_still_refuses_invalid_explicit_renames(output_names):
    request = ACTION_REGISTRY.get("reduce.group_summary").catalog_entry()["examples"][0]
    spec = {
        "action_kind": request["action_id"],
        **request["params"],
        "sheet_id": request["scope"]["sheet_id"],
        "sheet_name": request["sheet_name"],
        "output_names": output_names,
    }
    assert copilot._coerce_proposal("reduce", spec) is None


@pytest.mark.parametrize("kind", ["reduce.group_summary", "map.find"])
def test_unconditional_table_catalog_matches_request_destination(kind):
    registered = ACTION_REGISTRY.get(kind)
    catalog = registered.catalog_entry()
    request = ActionRequest.model_validate(catalog["examples"][0])
    assert request.sheet_name
    registered.bind_request(request)
    assert catalog["ui_hints"]["typed_action"]["creates_sheet"] is True
