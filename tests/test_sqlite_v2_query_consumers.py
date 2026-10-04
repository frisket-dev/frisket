"""Public query behavior over the native-value SQLite relation."""

from __future__ import annotations

import json

from frisket.engine.store import Project
from frisket.querysets import resolve_sheet_filter_rows
from frisket.server.services.document_browse import document_browse
from frisket.server.services.project_qa_query import evaluate_analytics


def test_list_filter_requires_a_top_level_json_array(tmp_path):
    project = Project.create(tmp_path / "list-shapes.frisket")
    try:
        sheet = project.add_sheet("values")
        tags = project.add_column(sheet, "tags", type="json")
        object_row, array_row = project.add_rows(
            sheet,
            [{"tags": {"0": "NYPD"}}, {"tags": ["NYPD"]}],
            {"tags": tags},
        )

        result = resolve_sheet_filter_rows(
            project,
            sheet,
            filter_=json.dumps(
                {"tags": {"list_contains_any": [{"kind": "scalar", "value": "NYPD"}]}}
            ),
        )

        assert result.row_ids == [array_row]
        assert object_row not in result.row_ids
    finally:
        project.close()


def test_json_backed_equality_filters_preserve_compact_comparison(tmp_path):
    project = Project.create(tmp_path / "geo-equality.frisket")
    try:
        sheet = project.add_sheet("places")
        location = project.add_column(sheet, "location", type="geo_point")
        payload = project.add_column(sheet, "payload", type="json")
        first = {"lat": 40.7128, "lon": -74.006}
        second = {"lat": 51.5074, "lon": -0.1278}
        matching, other = project.add_rows(
            sheet,
            [
                {"location": first, "payload": first},
                {"location": second, "payload": second},
            ],
            {"location": location, "payload": payload},
        )
        # The pre-typed query path compared json_extract(value, '$'), whose
        # object rendering is compact even when the stored JSON contains spaces.
        expected = json.dumps(first, separators=(",", ":"))

        for column_name in ("location", "payload"):
            equal = resolve_sheet_filter_rows(
                project,
                sheet,
                filter_=json.dumps({column_name: {"eq": expected}}),
            )
            unequal = resolve_sheet_filter_rows(
                project,
                sheet,
                filter_=json.dumps({column_name: {"neq": expected}}),
            )

            assert equal.row_ids == [matching]
            assert unequal.row_ids == [other]
    finally:
        project.close()


def test_bigint_sort_keeps_historical_query_boundary_order(tmp_path):
    project = Project.create(tmp_path / "bigint-sort.frisket")
    try:
        sheet = project.add_sheet("values")
        amount = project.add_column(sheet, "amount", type="number")
        negative, zero, positive, large_real = project.add_rows(
            sheet,
            [
                {"amount": -(2**63) - 1},
                {"amount": 0},
                {"amount": 2**63},
                {"amount": 1e30},
            ],
            {"amount": amount},
        )

        result = resolve_sheet_filter_rows(
            project,
            sheet,
            sort=json.dumps([{"column": "amount", "dir": "asc"}]),
        )

        assert result.row_ids == [negative, zero, positive, large_real]
        assert project.get_values(sheet, amount)[positive] == 2**63
    finally:
        project.close()


def test_bigint_payload_stays_exact_with_historical_numeric_evaluation(tmp_path):
    project = Project.create(tmp_path / "bigint-analytics.frisket")
    try:
        sheet = project.add_sheet("values")
        amount = project.add_column(sheet, "amount", type="number")
        first, second = project.add_rows(
            sheet,
            [{"amount": 1}, {"amount": 2**63}],
            {"amount": amount},
        )

        assert project.get_values(sheet, amount) == {first: 1, second: 2**63}
        result = evaluate_analytics(
            project,
            {
                "sheet_id": sheet,
                "metrics": [
                    {"id": "sum", "kind": "sum", "column_id": amount},
                    {"id": "min", "kind": "min", "column_id": amount},
                    {"id": "max", "kind": "max", "column_id": amount},
                ],
            },
            {"kind": "sheet", "sheet_id": sheet},
        )

        assert result["groups"][0]["metrics"] == {
            "sum": float(2**63),
            "min": 1,
            "max": float(2**63),
        }
    finally:
        project.close()


def test_document_media_accepts_a_json_encoded_object_string(tmp_path):
    project = Project.create(tmp_path / "encoded-media.frisket")
    try:
        sheet = project.add_sheet("documents")
        source = project.add_column(sheet, "file", type="file")
        title = project.add_column(sheet, "title")
        row = project.add_rows(
            sheet,
            [
                {
                    "file": json.dumps(
                        {
                            "blob": "abc",
                            "filename": "encoded.pdf",
                            "mime": "application/pdf",
                        }
                    ),
                    "title": None,
                }
            ],
            {"file": source, "title": title},
        )[0]

        result = document_browse(
            project,
            sheet,
            source_column_id=source,
            title_column_id=title,
        )

        assert result["items"] == [
            {
                **result["items"][0],
                "row_id": row,
                "title": "encoded.pdf",
                "source_label": "encoded.pdf",
                "source_kind": "pdf",
            }
        ]
    finally:
        project.close()


def test_document_title_hides_malformed_legacy_json(tmp_path):
    project = Project.create(tmp_path / "legacy-title.frisket")
    try:
        sheet = project.add_sheet("documents")
        source = project.add_column(sheet, "file", type="file")
        title = project.add_column(sheet, "title")
        row = project.add_rows(
            sheet,
            [{"file": "https://example.test/fallback.pdf", "title": "visible"}],
            {"file": source, "title": title},
        )[0]
        project.db.execute(
            "UPDATE cells SET value_kind='legacy_invalid',value=? "
            "WHERE row_id=? AND column_id=?",
            ("malformed legacy bytes", row, title),
        )
        project.db.commit()

        result = document_browse(
            project,
            sheet,
            source_column_id=source,
            title_column_id=title,
        )

        assert result["items"][0]["title"] == "fallback.pdf"
    finally:
        project.close()
