from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "followthemoney",
    reason="requires the entities extra: pip install 'frisket-data[entities]'",
)

from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.features.followthemoney import (
    FOLLOWTHEMONEY_MAPPING_SCHEMA_VERSION,
    FollowTheMoneyAdapterError,
    deterministic_row_ref_id,
    get_schema_metadata,
    map_rowset_to_entities,
    parse_entities_jsonl,
    serialize_entities_jsonl,
    supported_schema_presets,
    validate_entity,
)
from frisket.features.investigations.rowsets import resolve_investigative_rowset
from frisket.engine.store import Project


def _diagnostic_codes(result: dict[str, Any]) -> set[str]:
    return {diagnostic["code"] for diagnostic in result["diagnostics"]}


def test_schema_catalog_uses_sdk_presets_and_relationship_metadata() -> None:
    presets = supported_schema_presets()
    names = {preset["name"] for preset in presets}

    assert {
        "Person",
        "Company",
        "Organization",
        "Asset",
        "Payment",
        "Membership",
        "CourtCase",
        "Document",
    }.issubset(names)

    person = get_schema_metadata("Person")
    assert person is not None
    assert person["required"] == ["name"]
    assert person["properties_by_name"]["birthDate"]["type"] == "date"
    assert person["properties_by_name"]["sourceUrl"]["type"] == "url"
    assert person["properties_by_name"]["name"]["required"] is True

    membership = get_schema_metadata("Membership")
    assert membership is not None
    assert membership["edge"] is True
    assert membership["source_property"] == "member"
    assert membership["target_property"] == "organization"
    assert membership["properties_by_name"]["member"]["type"] == "entity"
    assert membership["properties_by_name"]["organization"]["range"] == "Organization"


def test_row_ref_mapping_normalizes_typed_values_and_preserves_refs(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "ftm.frisket", name="FtM")
    try:
        sheet_id = project.add_sheet("People")
        columns = {
            "name": project.add_column(sheet_id, "name", "text"),
            "aliases": project.add_column(sheet_id, "aliases", "json"),
            "birth_date": project.add_column(sheet_id, "birth_date", "date"),
            "source_url": project.add_column(sheet_id, "source_url", "link"),
        }
        row_id = project.add_rows(
            sheet_id,
            [
                {
                    "name": "Jane Smith",
                    "aliases": ["J. Smith", "Jane S."],
                    "birth_date": "1980-01-02",
                    "source_url": "https://example.org/profile/jane",
                }
            ],
            columns,
        )[0]

        _values, refs = project.get_values_with_refs(
            sheet_id, columns["name"], row_ids=[row_id]
        )
        artifact = record_source_artifact(
            project,
            stable_id="source_artifact:profile",
            artifact_kind="html",
            media_type="text/html",
            source_sheet_id=sheet_id,
            source_row_id=row_id,
            source_column_id=columns["source_url"],
            title="Profile",
        )
        span = record_source_span(
            project,
            stable_id="source_span:profile-name",
            artifact_id=artifact["id"],
            span_kind="text",
            quote="Jane Smith",
        )
        evidence = record_evidence_link(
            project,
            stable_id="evidence_link:profile-name",
            subject_kind="cell_value",
            subject_ref=refs[row_id],
            spans=[{"span_id": span["id"]}],
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=columns["name"],
        )

        rowset = resolve_investigative_rowset(
            project, {"kind": "sheet", "sheet_id": sheet_id}, project_id="case-1"
        )
    finally:
        project.close()

    result = map_rowset_to_entities(
        rowset,
        {
            "schema": "Person",
            "id_policy": {"kind": "row_ref"},
            "properties": {
                "name": {"column": "name"},
                "alias": {"column": "aliases"},
                "birthDate": {"column": "birth_date"},
                "sourceUrl": {"column": "source_url"},
            },
        },
        project_id="case-1",
    )

    assert result["schema_version"] == FOLLOWTHEMONEY_MAPPING_SCHEMA_VERSION
    assert result["entity_count"] == 1
    assert result["skipped_count"] == 0
    expected_id = deterministic_row_ref_id(
        {"kind": "row", "sheet_id": sheet_id, "row_id": row_id},
        project_id="case-1",
    )
    assert result["entities"] == [
        {
            "id": expected_id,
            "schema": "Person",
            "properties": {
                "name": ["Jane Smith"],
                "alias": ["J. Smith", "Jane S."],
                "birthDate": ["1980-01-02"],
                "sourceUrl": ["https://example.org/profile/jane"],
            },
        }
    ]
    assert result["rows"][0]["source_refs"][0]["property"] == "name"
    assert result["rows"][0]["source_refs"][0]["value_ref"] == refs[row_id]
    assert result["rows"][0]["evidence_refs"][0]["stable_id"] == (
        "evidence_link:profile-name"
    )
    assert result["rows"][0]["evidence_refs"][0]["id"] == evidence["id"]
    assert result["coverage"]["rows_with_source_refs"] == 1
    assert result["coverage"]["rows_with_evidence_refs"] == 1
    assert {"source_ref_coverage", "evidence_ref_coverage"}.issubset(
        _diagnostic_codes(result)
    )


def test_id_column_relationship_mapping_reports_diagnostics_and_skips_rows(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "membership.frisket", name="Membership")
    try:
        sheet_id = project.add_sheet("Memberships")
        columns = {
            "ftm_id": project.add_column(sheet_id, "ftm_id", "text"),
            "person_ftm_id": project.add_column(sheet_id, "person_ftm_id", "text"),
            "org_ftm_id": project.add_column(sheet_id, "org_ftm_id", "text"),
            "role": project.add_column(sheet_id, "role", "text"),
            "start_date": project.add_column(sheet_id, "start_date", "text"),
            "unused": project.add_column(sheet_id, "unused", "text"),
        }
        project.add_rows(
            sheet_id,
            [
                {
                    "ftm_id": "membership-1",
                    "person_ftm_id": "person-1",
                    "org_ftm_id": "org-1",
                    "role": "Director",
                    "start_date": "2020-01-01",
                    "unused": "keep",
                },
                {
                    "ftm_id": "membership-2",
                    "person_ftm_id": "",
                    "org_ftm_id": "org-2",
                    "role": "Advisor",
                    "start_date": "2021-03-04",
                    "unused": "keep",
                },
                {
                    "ftm_id": "",
                    "person_ftm_id": "person-3",
                    "org_ftm_id": "org-3",
                    "role": "Treasurer",
                    "start_date": "2022-05-06",
                    "unused": "keep",
                },
                {
                    "ftm_id": "membership-4",
                    "person_ftm_id": "person-4",
                    "org_ftm_id": "org-4",
                    "role": "Board",
                    "start_date": "not a date",
                    "unused": "keep",
                },
                {
                    "ftm_id": "membership-1",
                    "person_ftm_id": "person-5",
                    "org_ftm_id": "org-5",
                    "role": "Duplicate",
                    "start_date": "2023-01-01",
                    "unused": "keep",
                },
            ],
            columns,
        )
        rowset = resolve_investigative_rowset(
            project, {"kind": "sheet", "sheet_id": sheet_id}
        )
    finally:
        project.close()

    result = map_rowset_to_entities(
        rowset,
        {
            "schema": "Membership",
            "id_policy": {"kind": "column", "column": "ftm_id"},
            "source_entity": {"column": "person_ftm_id"},
            "target_entity": {"column": "org_ftm_id"},
            "properties": {
                "role": {"column": "role"},
                "startDate": {"column": "start_date"},
                "unknownProperty": {"column": "unused"},
            },
        },
    )

    assert [entity["id"] for entity in result["entities"]] == [
        "membership-1",
        "membership-4",
    ]
    assert result["entities"][0]["properties"] == {
        "member": ["person-1"],
        "organization": ["org-1"],
        "role": ["Director"],
        "startDate": ["2020-01-01"],
    }
    assert result["entities"][1]["properties"] == {
        "member": ["person-4"],
        "organization": ["org-4"],
        "role": ["Board"],
    }
    assert result["skipped_count"] == 3
    assert {
        "unsupported_property",
        "duplicate_id",
        "missing_source_ref",
        "missing_id",
        "invalid_value",
        "skipped_row",
        "source_ref_coverage",
        "evidence_ref_coverage",
    }.issubset(_diagnostic_codes(result))


def test_jsonl_roundtrip_uses_sdk_validation() -> None:
    entity = {
        "id": "person-1",
        "schema": "Person",
        "properties": {"name": ["Jane Smith"], "birthDate": ["1980-01-02"]},
    }
    jsonl = serialize_entities_jsonl([entity])
    assert jsonl == (
        '{"id":"person-1","properties":{"birthDate":["1980-01-02"],'
        '"name":["Jane Smith"]},"schema":"Person"}\n'
    )
    parsed = parse_entities_jsonl(
        jsonl
        + '{"id":"person-2","schema":"Person",'
        + '"properties":{"birthDate":["not a date"]}}\n'
        + "not-json\n"
    )

    assert parsed["entities"] == [entity]
    assert parsed["entity_count"] == 1
    assert [diagnostic["code"] for diagnostic in parsed["diagnostics"]] == [
        "invalid_entity",
        "invalid_json",
    ]
    assert validate_entity(entity)["valid"] is True
    assert (
        validate_entity({"schema": "Person", "properties": {"name": ["No ID"]}})[
            "diagnostics"
        ][0]["code"]
        == "missing_id"
    )
    assert (
        validate_entity({"id": "person-3", "schema": "Bogus", "properties": {}})[
            "diagnostics"
        ][0]["code"]
        == "unsupported_schema"
    )


def test_jsonl_serializer_rejects_invalid_entities() -> None:
    with pytest.raises(FollowTheMoneyAdapterError) as excinfo:
        serialize_entities_jsonl(
            [
                {
                    "id": "person-1",
                    "schema": "Person",
                    "properties": {"birthDate": ["not a date"]},
                }
            ]
        )

    assert excinfo.value.code == "invalid_entity"
