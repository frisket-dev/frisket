"""Scoped SQL analytics for Project Ask."""

from __future__ import annotations

from datetime import UTC, datetime
import threading

import pytest

import frisket.querysets as querysets
from frisket.engine.store import Project
from frisket.server.services import project_qa_analytics, project_qa_query
from frisket.server.services.project_qa_query import (
    AnalyticsRequest,
    AnalyticsCancelled,
    NumericOverflowError,
    evaluate_analytics,
    evaluate_query,
)


def _seed(tmp_path):
    project = Project.create(tmp_path / "analytics.frisket", name="analytics")
    sheet = project.add_sheet("contracts")
    columns = {
        "supplier": project.add_column(sheet, "supplier"),
        "status": project.add_column(sheet, "status"),
        "amount": project.add_column(sheet, "amount", type="number"),
        "awarded": project.add_column(sheet, "awarded", type="date"),
    }
    rows = project.add_rows(
        sheet,
        [
            {
                "supplier": "A",
                "status": "awarded",
                "amount": 1,
                "awarded": "2026-01-01",
            },
            {
                "supplier": "A",
                "status": "awarded",
                "amount": 3,
                "awarded": "2026-01-02",
            },
            {"supplier": "A", "status": "awarded", "awarded": "2026-01-02"},
            {
                "supplier": "B",
                "status": "awarded",
                "amount": 10,
                "awarded": "2026-02-01",
            },
            {
                "supplier": "B",
                "status": "draft",
                "amount": 1000,
                "awarded": "2026-02-01",
            },
            {"supplier": "C", "status": "awarded", "amount": -5},
        ],
        columns,
    )
    return project, sheet, columns, rows


def test_grouped_metrics_filter_scope_having_sort_and_percent(tmp_path):
    project, sheet, columns, rows = _seed(tmp_path)
    request = AnalyticsRequest.model_validate(
        {
            "sheet_id": sheet,
            "filter": {"status": {"eq": "awarded"}},
            "groups": [{"column_id": columns["supplier"]}],
            "metrics": [
                {"id": "rows", "kind": "count", "percent_of_total": True},
                {
                    "id": "present",
                    "kind": "value_count",
                    "column_id": columns["amount"],
                },
                {
                    "id": "missing",
                    "kind": "missing_count",
                    "column_id": columns["amount"],
                },
                {
                    "id": "distinct",
                    "kind": "distinct_count",
                    "column_id": columns["amount"],
                },
                {
                    "id": "sum",
                    "kind": "sum",
                    "column_id": columns["amount"],
                    "percent_of_total": True,
                },
                {"id": "mean", "kind": "mean", "column_id": columns["amount"]},
                {"id": "median", "kind": "median", "column_id": columns["amount"]},
            ],
            "having": [{"metric_id": "rows", "operator": "gte", "value": 1}],
            "sort": [{"kind": "metric", "metric_id": "sum", "direction": "desc"}],
            "limit": 10,
        }
    )
    result = evaluate_analytics(
        project,
        request,
        {"kind": "rows", "sheet_id": sheet, "row_ids": rows[:4]},
    )

    assert result["row_count"] == 4
    assert result["has_more"] is False
    assert [group["group"][0]["value"] for group in result["groups"]] == ["B", "A"]
    a = result["groups"][1]
    assert a["metrics"] == {
        "rows": 3,
        "present": 2,
        "missing": 1,
        "distinct": 2,
        "sum": 4.0,
        "mean": 2.0,
        "median": 2.0,
    }
    assert a["percentages"]["rows"] == 75.0
    assert a["quality"][str(columns["amount"])] == {
        "column_id": columns["amount"],
        "present": 2,
        "missing": 1,
        "invalid": 0,
    }
    assert result["denominators"]["sum"]["value"] == 14.0
    assert result["denominators"]["sum"]["mixed_sign"] is False
    project.close()


def test_scalar_quality_is_reported_once_per_referenced_column(tmp_path):
    project, sheet, columns, _rows = _seed(tmp_path)
    result = evaluate_analytics(
        project,
        {
            "sheet_id": sheet,
            "filter": {"status": {"eq": "awarded"}},
            "metrics": [
                {"id": "sum", "kind": "sum", "column_id": columns["amount"]},
                {
                    "id": "median",
                    "kind": "median",
                    "column_id": columns["amount"],
                },
            ],
        },
        {"kind": "sheet", "sheet_id": sheet},
    )

    assert result["quality"] == {
        str(columns["amount"]): {
            "column_id": columns["amount"],
            "present": 4,
            "missing": 1,
            "invalid": 0,
        }
    }
    assert "quality" not in result["groups"][0]
    project.close()


def test_having_is_numeric_and_denominator_precedes_group_filtering(tmp_path):
    project, sheet, columns, _rows = _seed(tmp_path)
    with pytest.raises(ValueError, match="numeric"):
        AnalyticsRequest.model_validate(
            {
                "sheet_id": sheet,
                "metrics": [
                    {"id": "minimum", "kind": "min", "column_id": columns["supplier"]}
                ],
                "having": [{"metric_id": "minimum", "operator": "eq", "value": 1}],
            }
        )

    result = evaluate_analytics(
        project,
        {
            "sheet_id": sheet,
            "groups": [{"column_id": columns["supplier"]}],
            "metrics": [{"id": "rows", "kind": "count", "percent_of_total": True}],
            "having": [{"metric_id": "rows", "operator": "gt", "value": 99}],
        },
        {"kind": "sheet", "sheet_id": sheet},
    )

    assert result["groups"] == []
    assert result["row_count"] == 6
    assert result["denominators"]["rows"] == {
        "value": 6,
        "reason": None,
        "mixed_sign": False,
    }
    project.close()


def test_overflow_and_pre_cancel_fail_the_whole_analytics_request(tmp_path):
    project = Project.create(tmp_path / "overflow.frisket", name="overflow")
    sheet = project.add_sheet("values")
    amount = project.add_column(sheet, "amount", type="integer")
    project.add_rows(
        sheet,
        [{"amount": 2**63 - 1}, {"amount": 2**63 - 1}],
        {"amount": amount},
    )
    request = AnalyticsRequest.model_validate(
        {
            "sheet_id": sheet,
            "metrics": [{"id": "sum", "kind": "sum", "column_id": amount}],
        }
    )
    with pytest.raises(NumericOverflowError, match="numeric_overflow"):
        evaluate_analytics(project, request, {"kind": "sheet", "sheet_id": sheet})
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(AnalyticsCancelled, match="stopped"):
        evaluate_analytics(
            project,
            request,
            {"kind": "sheet", "sheet_id": sheet},
            cancel_event=stopped,
        )
    project.close()


def test_date_bucket_and_missing_locator_are_backend_generated(tmp_path):
    project, sheet, columns, _rows = _seed(tmp_path)
    request = AnalyticsRequest.model_validate(
        {
            "sheet_id": sheet,
            "groups": [{"column_id": columns["awarded"], "bucket": "month"}],
            "metrics": [{"id": "rows", "kind": "count"}],
            "sort": [{"kind": "group", "group_index": 0, "direction": "asc"}],
        }
    )
    result = evaluate_analytics(project, request, {"kind": "sheet", "sheet_id": sheet})

    assert [group["group"][0]["kind"] for group in result["groups"]] == [
        "valid_date_bucket",
        "valid_date_bucket",
        "missing",
    ]
    assert result["groups"][0]["locator"] == {
        "version": "frisket.ask.group.v1",
        "sheet_id": sheet,
        "filter": {
            "awarded": {
                "group_eq": {
                    "kind": "date_bucket",
                    "bucket": "month",
                    "value": "2026-01",
                }
            }
        },
    }
    locator_query = {
        "schema_version": "frisket.query.v1",
        "kind": "sheet.filter",
        "scope": {"kind": "sheet", "sheet_id": sheet},
        "filter": result["groups"][0]["locator"]["filter"],
    }
    replay = evaluate_query(
        project, locator_query, {"kind": "sheet", "sheet_id": sheet}
    )
    assert replay["total"] == result["groups"][0]["metrics"]["rows"]
    project.close()


def test_relative_date_filter_is_anchored_for_results_and_group_replay(
    tmp_path, monkeypatch
):
    class AprilTenth(datetime):
        @classmethod
        def now(cls, tz=None):
            current = cls(2026, 4, 10, tzinfo=UTC)
            return current if tz is not None else current.replace(tzinfo=None)

    class AprilEleventh(datetime):
        @classmethod
        def now(cls, tz=None):
            current = cls(2026, 4, 11, tzinfo=UTC)
            return current if tz is not None else current.replace(tzinfo=None)

    monkeypatch.setattr(querysets, "datetime", AprilTenth)
    monkeypatch.setattr(project_qa_analytics, "datetime", AprilTenth, raising=False)
    project = Project.create(tmp_path / "anchored-dates.frisket", name="anchored")
    sheet = project.add_sheet("awards")
    columns = {
        "supplier": project.add_column(sheet, "supplier"),
        "awarded": project.add_column(sheet, "awarded", type="date"),
    }
    rows = project.add_rows(
        sheet,
        [
            {"supplier": "A", "awarded": "2026-04-03"},
            {"supplier": "A", "awarded": "2026-04-04"},
            {"supplier": "A", "awarded": "2026-04-10"},
        ],
        columns,
    )
    scope = {"kind": "sheet", "sheet_id": sheet}

    try:
        result = evaluate_analytics(
            project,
            {
                "sheet_id": sheet,
                "filter": {"awarded": {"date_relative": {"amount": 7, "unit": "days"}}},
                "groups": [{"column_id": columns["supplier"]}],
                "metrics": [{"id": "rows", "kind": "count"}],
            },
            scope,
        )

        anchored = {
            "awarded": {"between": {"start": "2026-04-04", "end": "2026-04-10"}}
        }
        assert result["filter"] == anchored
        assert result["row_count"] == 2
        assert result["groups"][0]["metrics"]["rows"] == 2
        locator_filter = result["groups"][0]["locator"]["filter"]
        assert locator_filter["awarded"] == anchored["awarded"]

        monkeypatch.setattr(project_qa_query, "datetime", AprilEleventh)
        replayed = evaluate_query(
            project,
            {
                "schema_version": "frisket.query.v1",
                "kind": "sheet.filter",
                "scope": scope,
                "filter": locator_filter,
            },
            scope,
        )
        assert replayed["row_ids"] == rows[1:]
        assert replayed["total"] == result["groups"][0]["metrics"]["rows"] == 2
    finally:
        project.close()


def test_value_missing_and_invalid_locators_replay_through_ordinary_query(tmp_path):
    project, sheet, columns, _rows = _seed(tmp_path)
    project.add_rows(sheet, [{"amount": "not-a-number"}], {"amount": columns["amount"]})
    result = evaluate_analytics(
        project,
        {
            "sheet_id": sheet,
            "groups": [{"column_id": columns["amount"]}],
            "metrics": [{"id": "rows", "kind": "count"}],
        },
        {"kind": "sheet", "sheet_id": sheet},
    )

    groups = {
        group["group"][0]["kind"]: group
        for group in result["groups"]
        if group["group"][0]["kind"] in {"valid", "missing", "invalid"}
    }
    for group in groups.values():
        replay = evaluate_query(
            project,
            {
                "schema_version": "frisket.query.v1",
                "kind": "sheet.filter",
                "scope": {"kind": "sheet", "sheet_id": sheet},
                "filter": group["locator"]["filter"],
            },
            {"kind": "sheet", "sheet_id": sheet},
            count_by=columns["supplier"],
        )
        assert replay["total"] == group["metrics"]["rows"]
        assert (
            sum(item["count"] for item in replay["count_by"]["values"])
            == replay["total"]
        )
    assert set(groups) == {"valid", "missing", "invalid"}
    project.close()


def test_analytics_rejects_plugin_filter_and_non_numeric_sum(tmp_path):
    project, sheet, columns, _rows = _seed(tmp_path)
    bad_filter = AnalyticsRequest.model_validate(
        {
            "sheet_id": sheet,
            "filter": {"status": {"custom_plugin_operator": "x"}},
            "metrics": [{"id": "rows", "kind": "count"}],
        }
    )
    try:
        evaluate_analytics(project, bad_filter, {"kind": "sheet", "sheet_id": sheet})
    except ValueError as exc:
        assert "unsupported filter" in str(exc)
    else:
        raise AssertionError("plugin filters must not reach Ask analytics")

    backend_only_filter = AnalyticsRequest.model_validate(
        {
            "sheet_id": sheet,
            "filter": {"status": {"group_eq": {"kind": "missing"}}},
            "metrics": [{"id": "rows", "kind": "count"}],
        }
    )
    with pytest.raises(ValueError, match="unsupported filter"):
        evaluate_analytics(
            project,
            backend_only_filter,
            {"kind": "sheet", "sheet_id": sheet},
        )

    bad_metric = AnalyticsRequest.model_validate(
        {
            "sheet_id": sheet,
            "metrics": [{"id": "sum", "kind": "sum", "column_id": columns["supplier"]}],
        }
    )
    try:
        evaluate_analytics(project, bad_metric, {"kind": "sheet", "sheet_id": sheet})
    except ValueError as exc:
        assert "numeric column" in str(exc)
    else:
        raise AssertionError("sum must reject text columns")
    project.close()


def test_analytics_file_scope_cannot_filter_or_metric_another_column(tmp_path):
    project, sheet, columns, rows = _seed(tmp_path)
    request = AnalyticsRequest.model_validate(
        {
            "sheet_id": sheet,
            "filter": {"status": {"eq": "awarded"}},
            "metrics": [{"id": "rows", "kind": "count"}],
        }
    )
    with pytest.raises(ValueError, match="file scope"):
        evaluate_analytics(
            project,
            request,
            {
                "kind": "rows",
                "sheet_id": sheet,
                "row_ids": rows[:1],
                "column_ids": [columns["amount"]],
            },
        )
    project.close()


def test_group_drilldown_retains_filter_on_same_date_column(tmp_path):
    project, sheet, columns, rows = _seed(tmp_path)
    try:
        scope = {"kind": "sheet", "sheet_id": sheet}
        result = evaluate_analytics(
            project,
            {
                "sheet_id": sheet,
                "filter": {"awarded": {"gte": "2026-01-02"}},
                "groups": [{"column_id": columns["awarded"], "bucket": "month"}],
                "metrics": [{"id": "rows", "kind": "count"}],
            },
            scope,
        )
        january = next(
            g for g in result["groups"] if g["group"][0].get("value") == "2026-01"
        )
        replayed = evaluate_query(
            project,
            {
                "kind": "sheet.filter",
                "scope": scope,
                "filter": january["locator"]["filter"],
            },
            scope,
        )
        assert replayed["total"] == january["metrics"]["rows"] == 2
        assert set(replayed["row_ids"]) == set(rows[1:3])
    finally:
        project.close()
