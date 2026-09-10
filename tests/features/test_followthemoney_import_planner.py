from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip(
    "followthemoney",
    reason="requires the entities extra: pip install 'frisket-data[entities]'",
)

import frisket.features.followthemoney.import_planner as import_planner
from frisket.features.followthemoney.import_planner import (
    FOLLOWTHEMONEY_IMPORT_PLAN_SCHEMA_VERSION,
    TECHNICAL_IMPORT_COLUMNS,
    plan_followthemoney_import,
)


def _jsonl(*entities: dict[str, Any] | str) -> str:
    lines: list[str] = []
    for entity in entities:
        if isinstance(entity, str):
            lines.append(entity)
        else:
            lines.append(json.dumps(entity, sort_keys=True))
    return "\n".join(lines) + "\n"


def _diagnostic_codes(plan: dict[str, Any]) -> list[str]:
    return [diagnostic["code"] for diagnostic in plan["diagnostics"]]


def test_import_planner_groups_supported_entities_and_relationships() -> None:
    payload = _jsonl(
        {
            "id": "person-1",
            "schema": "Person",
            "datasets": ["case-alpha"],
            "source_refs": [
                {"kind": "url", "url": "https://example.test/profile/jane"}
            ],
            "properties": {
                "name": ["Jane Smith"],
                "alias": ["J. Smith", "Jane S."],
                "birthDate": ["1980-01-02"],
                "sourceUrl": ["https://example.test/profile/jane"],
            },
        },
        {
            "id": "company-1",
            "schema": "Company",
            "properties": {
                "name": ["Acme LLC"],
                "jurisdiction": ["us"],
                "registrationNumber": ["DE-123"],
            },
        },
        {
            "id": "membership-1",
            "schema": "Membership",
            "properties": {
                "member": ["person-1"],
                "organization": ["company-1"],
                "role": ["Director"],
                "startDate": ["2020-03-04"],
            },
        },
    )

    plan = plan_followthemoney_import(payload, dataset_name="Imported case")

    assert plan["schema_version"] == FOLLOWTHEMONEY_IMPORT_PLAN_SCHEMA_VERSION
    assert plan["dataset"] == "Imported case"
    assert plan["entity_count"] == 3
    assert plan["planned_row_count"] == 3
    assert plan["unsupported_count"] == 0
    assert [sheet["schema"] for sheet in plan["sheets"]] == ["Company", "Person"]
    assert [sheet["schema"] for sheet in plan["relationship_sheets"]] == ["Membership"]

    person_sheet = next(
        sheet for sheet in plan["sheets"] if sheet["schema"] == "Person"
    )
    assert [column["name"] for column in person_sheet["columns"][-7:]] == list(
        TECHNICAL_IMPORT_COLUMNS
    )
    assert all(column["hidden"] for column in person_sheet["columns"][-7:])
    assert {column["name"] for column in person_sheet["columns"][:-7]} == {
        "name",
        "alias",
        "birthDate",
        "sourceUrl",
    }

    person_row = person_sheet["rows"][0]
    assert person_row["entity_id"] == "person-1"
    assert person_row["values"]["name"] == "Jane Smith"
    assert person_row["values"]["alias"] == "J. Smith"
    assert person_row["values"]["_ftm_id"] == "person-1"
    assert person_row["values"]["_ftm_schema"] == "Person"
    assert person_row["values"]["_ftm_caption"] == "Jane Smith"
    assert person_row["values"]["_ftm_dataset"] == ["case-alpha"]
    assert person_row["values"]["_ftm_properties_json"] == {
        "alias": ["J. Smith", "Jane S."],
        "birthDate": ["1980-01-02"],
        "name": ["Jane Smith"],
        "sourceUrl": ["https://example.test/profile/jane"],
    }
    assert person_row["values"]["_ftm_source_refs_json"] == [
        {"kind": "url", "url": "https://example.test/profile/jane"}
    ]
    assert person_row["values"]["_ftm_raw_json"]["datasets"] == ["case-alpha"]

    membership_sheet = plan["relationship_sheets"][0]
    assert membership_sheet["kind"] == "relationship"
    assert membership_sheet["source_property"] == "member"
    assert membership_sheet["target_property"] == "organization"
    membership_row = membership_sheet["rows"][0]
    assert membership_row["source_ftm_id"] == "person-1"
    assert membership_row["target_ftm_id"] == "company-1"
    assert membership_row["values"]["member"] == "person-1"
    assert membership_row["values"]["organization"] == "company-1"
    json.dumps(plan)


def test_import_planner_records_invalid_and_unsupported_diagnostics() -> None:
    payload = _jsonl(
        {
            "id": "person:1",
            "schema": "Person",
            "properties": {"name": ["Jane Smith"]},
        },
        "{not-json",
        {
            "id": "person:missing-name",
            "schema": "Person",
            "properties": {"birthDate": ["1980-01-02"]},
        },
        {
            "id": "address:1",
            "schema": "Address",
            "properties": {"full": ["1 Main Street"]},
        },
        {
            "id": "missing-schema:1",
            "properties": {"name": ["No schema"]},
        },
    )

    plan = plan_followthemoney_import(payload)

    assert [sheet["schema"] for sheet in plan["sheets"]] == ["Person"]
    assert plan["sheets"][0]["row_count"] == 1
    assert [sheet["schema"] for sheet in plan["unsupported_sheets"]] == ["Address"]
    assert plan["unsupported_sheets"][0]["row_count"] == 1
    assert plan["unsupported_sheets"][0]["rows"][0]["values"]["_ftm_raw_json"][
        "properties"
    ] == {"full": ["1 Main Street"]}

    assert plan["entity_count"] == 2
    assert plan["invalid_count"] == 3
    assert plan["unsupported_count"] == 1
    assert _diagnostic_codes(plan) == [
        "invalid_json",
        "missing_required_property",
        "unsupported_schema",
        "missing_schema",
    ]
    assert plan["diagnostics"][0]["line_number"] == 2
    assert plan["diagnostics"][2]["schema"] == "Address"


def test_import_planner_accepts_iterable_entity_streams() -> None:
    plan = plan_followthemoney_import(
        [
            {
                "id": "document:1",
                "schema": "Document",
                "dataset": {"name": "leak archive"},
                "properties": {
                    "fileName": ["memo.pdf"],
                    "mimeType": ["application/pdf"],
                    "sourceUrl": ["https://example.test/memo.pdf"],
                },
            }
        ],
        dataset_name="Document import",
    )

    assert [sheet["schema"] for sheet in plan["sheets"]] == ["Document"]
    row = plan["sheets"][0]["rows"][0]
    assert row["values"]["fileName"] == "memo.pdf"
    assert row["values"]["_ftm_dataset"] == {"name": "leak archive"}
    assert row["values"]["_ftm_raw_json"]["dataset"] == {"name": "leak archive"}
    assert plan["diagnostics"] == []


def test_import_planner_reports_stream_read_failures_without_losing_rows() -> None:
    def stream() -> Any:
        yield {
            "id": "person:stream",
            "schema": "Person",
            "properties": {"name": ["Stream Person"]},
        }
        raise RuntimeError("fixture stream broke")

    plan = plan_followthemoney_import(stream())

    assert plan["entity_count"] == 1
    assert plan["planned_row_count"] == 1
    assert plan["invalid_count"] == 1
    assert _diagnostic_codes(plan) == ["stream_read_failed"]
    assert plan["diagnostics"][0]["entity_index"] == 2


def test_import_planner_reports_invalid_bytes_and_non_json_values() -> None:
    invalid_bytes = plan_followthemoney_import(b"\xff")

    assert invalid_bytes["entity_count"] == 0
    assert invalid_bytes["invalid_count"] == 1
    assert _diagnostic_codes(invalid_bytes) == ["invalid_encoding"]

    non_json = plan_followthemoney_import(
        [
            {
                "id": "person:object",
                "schema": "Person",
                "properties": {"name": [object()]},
            }
        ]
    )

    assert non_json["entity_count"] == 0
    assert non_json["invalid_count"] == 1
    assert _diagnostic_codes(non_json) == ["non_json_entity"]


def test_import_planner_is_defensive_about_schema_metadata(monkeypatch: Any) -> None:
    original_get_schema_metadata = import_planner.get_schema_metadata

    def malformed_metadata(schema_name: str) -> dict[str, Any] | None:
        if schema_name == "Person":
            return {
                "plural": "People",
                "properties": [{"label": "missing name"}, "bad metadata item"],
            }
        return original_get_schema_metadata(schema_name)

    monkeypatch.setattr(import_planner, "get_schema_metadata", malformed_metadata)

    plan = plan_followthemoney_import(
        {
            "id": "person:metadata",
            "schema": "Person",
            "properties": {"name": ["Metadata Person"]},
        }
    )

    assert plan["diagnostics"] == []
    assert plan["sheets"][0]["rows"][0]["values"]["name"] == "Metadata Person"


def test_import_planner_keeps_edge_schema_relationship_when_props_missing(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        import_planner, "_relationship_properties", lambda *_args: (None, None)
    )

    plan = plan_followthemoney_import(
        {
            "id": "membership:missing-props",
            "schema": "Membership",
            "properties": {
                "member": ["person-1"],
                "organization": ["company-1"],
            },
        }
    )

    assert plan["sheets"] == []
    assert [sheet["schema"] for sheet in plan["relationship_sheets"]] == ["Membership"]
    assert _diagnostic_codes(plan) == ["relationship_props_missing"]
