"""Public declaration contract for Solo tool-assisted Extract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def _params(**updates):
    params = {
        "source": ["company"],
        "model": "anthropic/claude-haiku-4-5",
        "instruction": "Resolve the company and assess its risk.",
        "fields": [
            {"name": "company_name", "type": "text"},
            {"name": "risk", "type": "score"},
        ],
        "mcp_server_ids": ["crm", "internal-taxonomy"],
    }
    params.update(updates)
    return params


def test_mcp_extract_uses_one_capability_and_typed_field_definitions() -> None:
    from frisket.actions.mcp_extract import MCP_EXTRACT, McpExtractParams
    from frisket.actions.mcp_types import McpExtractor

    assert MCP_EXTRACT.run.capabilities == (McpExtractor,)
    params = McpExtractParams.model_validate(_params())
    output_fields = MCP_EXTRACT.run.resolve_output_fields(params)
    assert [(field.key, field.column_type) for field in output_fields] == [
        ("company_name", "text"),
        ("risk", "integer"),
    ]
    assert output_fields[0].schema == {"type": "string"}
    assert output_fields[1].schema["type"] == "integer"
    assert (
        output_fields[1].schema["minimum"],
        output_fields[1].schema["maximum"],
    ) == (0, 10)

    # Tool context cannot honestly satisfy map.extract's source-span contract.
    schema = {"properties": {field.key: dict(field.schema) for field in output_fields}}
    assert schema["properties"]["company_name"] == {"type": "string"}
    assert "evidence" not in str(schema)


def test_action_selects_servers_not_a_saved_tool_inventory() -> None:
    from frisket.actions.mcp_extract import McpExtractParams

    model = McpExtractParams
    parsed = model.model_validate(_params())
    assert parsed.mcp_server_ids.root == ["crm", "internal-taxonomy"]

    schema = model.model_json_schema()
    properties = schema["properties"]
    assert "mcp_server_ids" in properties
    assert "tools" not in properties
    assert "tool_names" not in properties
    assert "grounding" not in properties
    assert "evidence_policy" not in properties

    with pytest.raises(ValidationError):
        model.model_validate(_params(mcp_server_ids=[]))
    with pytest.raises(ValidationError):
        model.model_validate(_params(mcp_server_ids=["crm", "crm"]))
    with pytest.raises(ValidationError):
        model.model_validate(_params(tools=["lookup_company"]))
