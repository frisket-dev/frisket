from __future__ import annotations

from typing import Any, get_args

import pytest
from pydantic import ValidationError

from frisket.authoring.copilot import WIRE_ACTION_FAMILIES
from frisket.contracts.http.copilot import (
    CopilotProposal,
    CopilotReply,
    CopilotRequest,
)

REPLY_SCHEMA_VERSION = "frisket.copilot_reply.v1"
FORBIDDEN_REPLY_FIELDS = {"recipe", "wrapper_kind", "cost"}


def _proposals() -> list[dict[str, Any]]:
    """Real canonical drafts, not placeholders inferred from JSON Schema."""
    return [
        {
            "kind": "resolve",
            "title": "Normalize company names",
            "spec": {
                "action_id": "resolve.substitute",
                "scope": {"kind": "sheet_rows", "sheet_id": 7},
                "params": {"source": "company", "mapping": {"ACME": "Acme"}},
                "output_names": {"cleaned": "company_clean"},
            },
        },
        {
            "kind": "media",
            "title": "Read asset metadata",
            "spec": {
                "action_id": "media.extract_metadata",
                "scope": {"kind": "sheet_rows", "sheet_id": 7},
                "params": {"source": "asset", "output_mode": "object"},
                "output_names": {"details": "asset_details"},
            },
        },
    ]


@pytest.mark.gap
def test_copilot_request_model_bounds_messages_roles_and_fails_closed() -> None:
    valid = CopilotRequest.model_validate(
        {"messages": [{"role": "user", "content": "Add a column"}]}
    )
    assert valid.model_dump()["messages"] == [
        {"role": "user", "content": "Add a column"}
    ]
    for invalid in (
        {"messages": []},
        {"messages": [{"role": "user", "content": "   "}]},
        {"messages": [{"role": "system", "content": "hi"}]},
        {"messages": [{"role": "user", "content": "hi", "extra": 1}]},
        {"messages": [{"role": "user", "content": "hi"}], "unexpected": True},
    ):
        with pytest.raises(ValidationError):
            CopilotRequest.model_validate(invalid)


@pytest.mark.gap
def test_copilot_reply_pins_schema_version_cost_and_forbids_alias_fields() -> None:
    fields = set(CopilotReply.model_fields)
    assert {
        "schema_version",
        "reply",
        "proposals",
        "needs_import",
        "cost_usd",
    } <= fields
    assert not (FORBIDDEN_REPLY_FIELDS & fields)
    base = {
        "schema_version": REPLY_SCHEMA_VERSION,
        "reply": "Import a file before requesting edits.",
        "proposals": [],
        "needs_import": True,
    }
    accepted = CopilotReply.model_validate(base)
    assert accepted.model_dump()["schema_version"] == REPLY_SCHEMA_VERSION
    for cost in (0.0, 1.25):
        CopilotReply.model_validate({**base, "cost_usd": cost})
    for invalid in (
        {"cost_usd": -0.01},
        {"schema_version": "frisket.copilot_reply.v2"},
        {"cost": 1.0},
        {"recipe": {}},
        {"wrapper_kind": "map"},
    ):
        with pytest.raises(ValidationError):
            CopilotReply.model_validate({**base, **invalid})


@pytest.mark.gap
def test_copilot_proposals_are_a_closed_family_union_agreeing_with_action_kind() -> (
    None
):
    variants = get_args(CopilotProposal.model_fields["root"].annotation)
    kinds = set()
    for variant in variants:
        kind_values = get_args(variant.model_fields["kind"].annotation)
        assert len(kind_values) == 1
        kinds.add(kind_values[0])
        assert {"kind", "spec"} <= variant.model_fields.keys()
        assert variant.model_config["extra"] == "forbid"
    assert len(kinds) == len(variants)
    assert kinds == {family for family, _ in WIRE_ACTION_FAMILIES}

    for proposal in _proposals():
        accepted = CopilotProposal.model_validate(proposal)
        assert accepted.model_dump(mode="json") == proposal
        assert proposal["spec"]["action_id"].split(".", 1)[0] == proposal["kind"]
        for wrong_kind in (kinds - {proposal["kind"]}) | {"unknown"}:
            with pytest.raises(ValidationError):
                CopilotProposal.model_validate({**proposal, "kind": wrong_kind})
        with pytest.raises(ValidationError):
            CopilotProposal.model_validate({**proposal, "unexpected": True})
        for invalid_spec in (
            {"action_id": "unknown.action"},
            {"action_kind": proposal["spec"]["action_id"]},
        ):
            with pytest.raises(ValidationError):
                CopilotProposal.model_validate(
                    {**proposal, "spec": {**proposal["spec"], **invalid_spec}}
                )


@pytest.mark.gap
def test_needs_import_true_forbids_proposals_at_pydantic_runtime() -> None:
    for proposal in _proposals():
        normal = {
            "schema_version": REPLY_SCHEMA_VERSION,
            "reply": "I can apply this proposal.",
            "proposals": [proposal],
            "needs_import": False,
        }
        # Prove the draft is valid before testing the reply-level invariant.
        assert CopilotReply.model_validate(normal).model_dump()["proposals"] == [
            proposal
        ]
        with pytest.raises(
            ValidationError, match="needs_import=True forbids proposals"
        ):
            CopilotReply.model_validate({**normal, "needs_import": True})
