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
    FOLLOWTHEMONEY_MATCH_REVIEW_PLAN_SCHEMA_VERSION,
    FOLLOWTHEMONEY_MATCHING_SCHEMA_VERSION,
    map_rowset_to_entities,
    match_followthemoney_entities,
    plan_followthemoney_match_review_sheet,
)
from frisket.features.investigations.rowsets import resolve_investigative_rowset
from frisket.engine.store import Project


def _diagnostic_codes(result: dict[str, Any]) -> set[str]:
    return {diagnostic["code"] for diagnostic in result["diagnostics"]}


def _sheet_counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("sheets", "columns", "rows", "ops", "receipts")
    }


def test_ftm_entity_matching_returns_read_only_candidate_groups_with_refs(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "ftm-match.frisket", name="FtM matching")
    try:
        leads_sheet = project.add_sheet("Extracted people")
        lead_columns = {
            "name": project.add_column(leads_sheet, "name", "text"),
            "email": project.add_column(leads_sheet, "email", "text"),
            "phone": project.add_column(leads_sheet, "phone", "text"),
            "source_url": project.add_column(leads_sheet, "source_url", "link"),
        }
        lead_rows = project.add_rows(
            leads_sheet,
            [
                {
                    "name": "Jane Smith",
                    "email": "JANE@example.org",
                    "phone": "+1 (202) 555-0100",
                    "source_url": "https://example.test/profile/jane/",
                },
                {
                    "name": "Jane Smith",
                    "email": "",
                    "phone": "",
                    "source_url": "https://example.test/unrelated-jane",
                },
            ],
            lead_columns,
        )
        values, refs = project.get_values_with_refs(
            leads_sheet, lead_columns["email"], row_ids=[lead_rows[0]]
        )
        assert values[lead_rows[0]] == "JANE@example.org"
        artifact = record_source_artifact(
            project,
            stable_id="source_artifact:email-profile",
            artifact_kind="html",
            media_type="text/html",
            source_sheet_id=leads_sheet,
            source_row_id=lead_rows[0],
            source_column_id=lead_columns["source_url"],
            title="Profile",
        )
        span = record_source_span(
            project,
            stable_id="source_span:email-profile",
            artifact_id=artifact["id"],
            span_kind="text",
            quote="Jane@example.org",
        )
        evidence = record_evidence_link(
            project,
            stable_id="evidence_link:email-profile",
            subject_kind="cell_value",
            subject_ref=refs[lead_rows[0]],
            spans=[{"span_id": span["id"]}],
            sheet_id=leads_sheet,
            row_id=lead_rows[0],
            column_id=lead_columns["email"],
        )

        registry_sheet = project.add_sheet("Registry people")
        registry_columns = {
            "name": project.add_column(registry_sheet, "name", "text"),
            "email": project.add_column(registry_sheet, "email", "text"),
            "phone": project.add_column(registry_sheet, "phone", "text"),
            "source_url": project.add_column(registry_sheet, "source_url", "link"),
        }
        registry_rows = project.add_rows(
            registry_sheet,
            [
                {
                    "name": "Jane Smith",
                    "email": "jane@example.org",
                    "phone": "+12025550100",
                    "source_url": "https://example.test/profile/jane",
                }
            ],
            registry_columns,
        )

        companies_sheet = project.add_sheet("Companies")
        company_columns = {
            "name": project.add_column(companies_sheet, "name", "text"),
            "registration_number": project.add_column(
                companies_sheet, "registration_number", "text"
            ),
        }
        project.add_rows(
            companies_sheet,
            [
                {"name": "Acme LLC", "registration_number": "DE-123"},
                {"name": "ACME Limited", "registration_number": "DE-123"},
            ],
            company_columns,
        )

        people_a = map_rowset_to_entities(
            resolve_investigative_rowset(
                project,
                {"kind": "sheet", "sheet_id": leads_sheet},
                project_id="case-1",
            ),
            {
                "schema": "Person",
                "id_policy": {"kind": "row_ref"},
                "properties": {
                    "name": {"column": "name"},
                    "email": {"column": "email"},
                    "phone": {"column": "phone"},
                    "sourceUrl": {"column": "source_url"},
                },
            },
            project_id="case-1",
        )
        people_b = map_rowset_to_entities(
            resolve_investigative_rowset(
                project,
                {"kind": "sheet", "sheet_id": registry_sheet},
                project_id="case-1",
            ),
            {
                "schema": "Person",
                "id_policy": {"kind": "row_ref"},
                "properties": {
                    "name": {"column": "name"},
                    "email": {"column": "email"},
                    "phone": {"column": "phone"},
                    "sourceUrl": {"column": "source_url"},
                },
            },
            project_id="case-1",
        )
        companies = map_rowset_to_entities(
            resolve_investigative_rowset(
                project,
                {"kind": "sheet", "sheet_id": companies_sheet},
                project_id="case-1",
            ),
            {
                "schema": "Company",
                "id_policy": {"kind": "row_ref"},
                "properties": {
                    "name": {"column": "name"},
                    "registrationNumber": {"column": "registration_number"},
                },
            },
            project_id="case-1",
        )
        before_counts = _sheet_counts(project)

        result = match_followthemoney_entities([people_a, people_b, companies])
        after_counts = _sheet_counts(project)
    finally:
        project.close()

    assert before_counts == after_counts
    assert result["schema_version"] == FOLLOWTHEMONEY_MATCHING_SCHEMA_VERSION
    assert result["candidate_group_count"] == 2
    assert result["pair_count"] == 2
    assert "same_name_without_corroboration" in _diagnostic_codes(result)

    person_group = next(
        group for group in result["candidate_groups"] if group["schema"] == "Person"
    )
    assert person_group["score"] == 1.0
    assert {
        candidate["row_ref"]["row_id"] for candidate in person_group["candidates"]
    } == {
        lead_rows[0],
        registry_rows[0],
    }
    assert {reason["property"] for reason in person_group["reasons"]} >= {
        "email",
        "phone",
        "source_refs",
    }
    assert person_group["source_row_refs"] == [
        {"kind": "row", "sheet_id": leads_sheet, "row_id": lead_rows[0]},
        {"kind": "row", "sheet_id": registry_sheet, "row_id": registry_rows[0]},
    ]
    assert person_group["evidence_refs"][0]["stable_id"] == (
        "evidence_link:email-profile"
    )
    assert person_group["evidence_refs"][0]["id"] == evidence["id"]

    company_group = next(
        group for group in result["candidate_groups"] if group["schema"] == "Company"
    )
    assert {reason["property"] for reason in company_group["reasons"]} == {
        "registrationNumber"
    }


def test_ftm_match_review_materialization_is_an_ordinary_sheet_plan_only(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "ftm-review-plan.frisket", name="FtM review")
    try:
        sheet_id = project.add_sheet("Companies")
        columns = {
            "name": project.add_column(sheet_id, "name", "text"),
            "registration_number": project.add_column(
                sheet_id, "registration_number", "text"
            ),
        }
        project.add_rows(
            sheet_id,
            [
                {"name": "Acme LLC", "registration_number": "DE-123"},
                {"name": "ACME Limited", "registration_number": "DE-123"},
            ],
            columns,
        )
        mapping = map_rowset_to_entities(
            resolve_investigative_rowset(
                project, {"kind": "sheet", "sheet_id": sheet_id}
            ),
            {
                "schema": "Company",
                "id_policy": {"kind": "row_ref"},
                "properties": {
                    "name": {"column": "name"},
                    "registrationNumber": {"column": "registration_number"},
                },
            },
        )
        before_counts = _sheet_counts(project)
        matches = match_followthemoney_entities([mapping])
        group_id = matches["candidate_groups"][0]["group_id"]

        plan = plan_followthemoney_match_review_sheet(
            matches,
            sheet_name="Entity match review",
            decisions={group_id: "accepted", "missing": "rejected"},
        )
        after_counts = _sheet_counts(project)
    finally:
        project.close()

    assert before_counts == after_counts
    assert plan["schema_version"] == FOLLOWTHEMONEY_MATCH_REVIEW_PLAN_SCHEMA_VERSION
    assert plan["sheet_name"] == "Entity match review"
    assert plan["row_count"] == 1
    assert plan["operations"] == [
        {
            "kind": "create_ordinary_sheet",
            "sheet_name": "Entity match review",
            "columns": plan["columns"],
            "rows": plan["rows"],
        }
    ]
    assert [column["name"] for column in plan["columns"]] == [
        "match_group_id",
        "decision",
        "score",
        "schema",
        "candidate_count",
        "entity_ids",
        "source_row_refs_json",
        "evidence_refs_json",
        "source_refs_json",
        "reasons_json",
        "pair_scores_json",
        "review_notes",
    ]
    assert plan["rows"][0]["values"]["decision"] == "accepted"
    assert plan["rows"][0]["values"]["candidate_count"] == 2
    assert plan["rows"][0]["values"]["schema"] == "Company"
