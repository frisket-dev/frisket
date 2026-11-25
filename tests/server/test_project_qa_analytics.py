"""Project Ask scoped SQL analytics, composed flow, tracing, and failure attribution tests."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from helpers import make_client
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import StatusCode
from pydantic_ai import Agent
from pydantic_ai.models.instrumented import InstrumentationSettings
from pydantic_ai.models.test import TestModel

import frisket.querysets as querysets
from frisket.ai.llm import LLMError, ModelRouter
from frisket.ai.llm.types import LLMResponse
from frisket.contracts.http.project_qa import AskThreadCreate, AskTurnRequest
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
from frisket.server.services import project_qa as project_qa_service
from frisket.server.services import (
    project_qa_analytics,
    project_qa_query,
    project_qa_tracing,
)
from frisket.server.services.project_qa import ProjectQAService
from frisket.server.services.project_qa_query import (
    AnalyticsCancelled,
    AnalyticsRequest,
    NumericOverflowError,
    evaluate_analytics,
    evaluate_query,
)
from frisket.server.services.project_qa_runner import run_turn
from frisket.server.services.project_qa_tools import ProjectQATools
from frisket.server.services.project_qa_tracing import ProjectQATracing
from frisket.server.workspace import Workspace
from tests.execution_composition_helpers import RecordingPurchasePolicy

# --- analytics ---


def _seed(make_project):
    project = make_project("analytics")
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


def test_grouped_metrics_filter_scope_having_sort_and_percent(make_project):
    project, sheet, columns, rows = _seed(make_project)
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


def test_scalar_quality_is_reported_once_per_referenced_column(make_project):
    project, sheet, columns, _rows = _seed(make_project)
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
    assert result["row_count"] == 5
    assert result["denominators"] == {}
    assert "quality" not in result["groups"][0]

    percentage = evaluate_analytics(
        project,
        {
            "sheet_id": sheet,
            "filter": {"status": {"eq": "awarded"}},
            "metrics": [
                {
                    "id": "sum",
                    "kind": "sum",
                    "column_id": columns["amount"],
                    "percent_of_total": True,
                }
            ],
        },
        {"kind": "sheet", "sheet_id": sheet},
    )
    assert percentage["denominators"]["sum"] == {
        "value": 9,
        "reason": None,
        "mixed_sign": True,
    }
    assert percentage["groups"][0]["percentages"]["sum"] == 100.0
    project.close()


def test_having_is_numeric_and_denominator_precedes_group_filtering(make_project):
    project, sheet, columns, _rows = _seed(make_project)
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

    non_percentage = evaluate_analytics(
        project,
        {
            "sheet_id": sheet,
            "groups": [{"column_id": columns["supplier"]}],
            "metrics": [
                {"id": "rows", "kind": "count"},
                {"id": "total", "kind": "sum", "column_id": columns["amount"]},
            ],
            "having": [{"metric_id": "rows", "operator": "gt", "value": 2}],
            "sort": [{"kind": "metric", "metric_id": "total", "direction": "desc"}],
        },
        {"kind": "sheet", "sheet_id": sheet},
    )
    assert [group["group"][0]["value"] for group in non_percentage["groups"]] == ["A"]
    assert non_percentage["groups"][0]["metrics"] == {"rows": 3, "total": 4.0}
    assert non_percentage["groups"][0]["quality"][str(columns["amount"])] == {
        "column_id": columns["amount"],
        "present": 2,
        "missing": 1,
        "invalid": 0,
    }
    assert non_percentage["row_count"] == 6
    assert non_percentage["denominators"] == {}

    empty = evaluate_analytics(
        project,
        {
            "sheet_id": sheet,
            "filter": {"status": {"eq": "not-present"}},
            "groups": [{"column_id": columns["supplier"]}],
            "metrics": [{"id": "rows", "kind": "count"}],
        },
        {"kind": "sheet", "sheet_id": sheet},
    )
    assert empty["groups"] == []
    assert empty["row_count"] == 0
    assert empty["denominators"] == {}

    off_end = evaluate_analytics(
        project,
        {
            "sheet_id": sheet,
            "groups": [{"column_id": columns["supplier"]}],
            "metrics": [{"id": "rows", "kind": "count", "percent_of_total": True}],
            "offset": 99,
        },
        {"kind": "sheet", "sheet_id": sheet},
    )
    assert off_end["groups"] == []
    assert off_end["row_count"] == 6
    assert off_end["denominators"]["rows"]["value"] == 6
    project.close()


def test_metric_sort_counts_excluded_groups_in_the_page_query(make_project):
    project = make_project("sorted-null-groups")
    sheet = project.add_sheet("values")
    group = project.add_column(sheet, "group")
    amount = project.add_column(sheet, "amount", type="number")
    project.add_rows(
        sheet,
        [
            {"group": "A", "amount": 4},
            {"group": "A"},
            {"group": "B"},
            {"group": "C"},
            {"group": "C", "amount": None},
        ],
        {"group": group, "amount": amount},
    )

    def evaluate(offset: int) -> tuple[dict, int]:
        request = AnalyticsRequest.model_validate(
            {
                "sheet_id": sheet,
                "groups": [{"column_id": group}],
                "metrics": [
                    {
                        "id": "rows",
                        "kind": "count",
                        "percent_of_total": True,
                    },
                    {"id": "total", "kind": "sum", "column_id": amount},
                ],
                "having": [{"metric_id": "rows", "operator": "gt", "value": 1}],
                "sort": [{"kind": "metric", "metric_id": "total", "direction": "desc"}],
                "offset": offset,
            }
        )
        statements: list[str] = []
        with project.read_snapshot() as snapshot:
            snapshot.db.set_trace_callback(statements.append)
            try:
                result = project_qa_analytics._evaluate(
                    snapshot,
                    request,
                    {"kind": "sheet", "sheet_id": sheet},
                )
            finally:
                snapshot.db.set_trace_callback(None)
        analytics_statements = sum(
            statement.lstrip().startswith("WITH scoped AS") for statement in statements
        )
        return result, analytics_statements

    first_page, first_page_statements = evaluate(0)
    assert [item["group"][0]["value"] for item in first_page["groups"]] == ["A"]
    assert first_page["excluded_null_groups"] == 1
    assert first_page["row_count"] == 5
    assert first_page["denominators"]["rows"]["value"] == 5
    assert first_page_statements == 1

    off_end, off_end_statements = evaluate(99)
    assert off_end["groups"] == []
    assert off_end["excluded_null_groups"] == 1
    assert off_end["row_count"] == 5
    assert off_end["denominators"]["rows"]["value"] == 5
    assert off_end_statements == 2
    project.close()


def test_overflow_and_pre_cancel_fail_the_whole_analytics_request(make_project):
    project = make_project("overflow")
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


def test_grouped_float_overflow_hidden_by_having_and_in_denominator_fails(make_project):
    project = make_project("grouped-float-overflow")

    hidden_sheet = project.add_sheet("hidden group overflow")
    hidden_group = project.add_column(hidden_sheet, "group", type="category")
    hidden_amount = project.add_column(hidden_sheet, "amount", type="number")
    project.add_rows(
        hidden_sheet,
        [
            {"group": "overflow", "amount": 1e308},
            {"group": "overflow", "amount": 1e308},
            {"group": "kept", "amount": 1.0},
        ],
        {"group": hidden_group, "amount": hidden_amount},
    )
    hidden_request = AnalyticsRequest.model_validate(
        {
            "sheet_id": hidden_sheet,
            "groups": [{"column_id": hidden_group}],
            "metrics": [{"id": "sum", "kind": "sum", "column_id": hidden_amount}],
            "having": [{"metric_id": "sum", "operator": "lt", "value": 10}],
            "limit": 1,
        }
    )
    with pytest.raises(NumericOverflowError, match="numeric_overflow"):
        evaluate_analytics(
            project, hidden_request, {"kind": "sheet", "sheet_id": hidden_sheet}
        )

    denominator_sheet = project.add_sheet("denominator overflow")
    denominator_group = project.add_column(denominator_sheet, "group", type="category")
    denominator_amount = project.add_column(denominator_sheet, "amount", type="number")
    project.add_rows(
        denominator_sheet,
        [
            {"group": "first", "amount": 1e308},
            {"group": "second", "amount": 1e308},
        ],
        {"group": denominator_group, "amount": denominator_amount},
    )
    denominator_request = AnalyticsRequest.model_validate(
        {
            "sheet_id": denominator_sheet,
            "groups": [{"column_id": denominator_group}],
            "metrics": [
                {
                    "id": "sum",
                    "kind": "sum",
                    "column_id": denominator_amount,
                    "percent_of_total": True,
                }
            ],
        }
    )
    with pytest.raises(NumericOverflowError, match="numeric_overflow"):
        evaluate_analytics(
            project,
            denominator_request,
            {"kind": "sheet", "sheet_id": denominator_sheet},
        )
    project.close()


def test_date_bucket_and_missing_locator_are_backend_generated(make_project):
    project, sheet, columns, _rows = _seed(make_project)
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
    make_project, monkeypatch
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
    project = make_project("anchored")
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

    anchored = {"awarded": {"between": {"start": "2026-04-04", "end": "2026-04-10"}}}
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


def test_value_missing_and_invalid_locators_replay_through_ordinary_query(make_project):
    project, sheet, columns, _rows = _seed(make_project)
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


def test_group_locator_replays_non_ascii_value(make_project):
    project = make_project("unicode-group")
    sheet = project.add_sheet("contracts")
    supplier = project.add_column(sheet, "supplier")
    project.add_rows(
        sheet,
        [{"supplier": "Café Ltd"}, {"supplier": "Café Ltd"}],
        {"supplier": supplier},
    )

    result = evaluate_analytics(
        project,
        {
            "sheet_id": sheet,
            "groups": [{"column_id": supplier}],
            "metrics": [{"id": "rows", "kind": "count"}],
        },
        {"kind": "sheet", "sheet_id": sheet},
    )
    group = result["groups"][0]
    replay = evaluate_query(
        project,
        {
            "schema_version": "frisket.query.v1",
            "kind": "sheet.filter",
            "scope": {"kind": "sheet", "sheet_id": sheet},
            "filter": group["locator"]["filter"],
        },
        {"kind": "sheet", "sheet_id": sheet},
    )

    assert group["group"][0]["value"] == "Café Ltd"
    assert replay["total"] == 2
    project.close()


def test_numeric_groups_and_distinct_count_use_decoded_values(make_project):
    project = make_project("numeric-groups")
    sheet = project.add_sheet("values")
    amount = project.add_column(sheet, "amount", type="number")
    project.add_rows(
        sheet,
        [{"amount": 1}, {"amount": 1.0}],
        {"amount": amount},
    )

    result = evaluate_analytics(
        project,
        {
            "sheet_id": sheet,
            "groups": [{"column_id": amount}],
            "metrics": [
                {"id": "rows", "kind": "count"},
                {"id": "distinct", "kind": "distinct_count", "column_id": amount},
            ],
        },
        {"kind": "sheet", "sheet_id": sheet},
    )

    assert len(result["groups"]) == 1
    assert result["groups"][0]["row_count"] == 2
    assert result["groups"][0]["metrics"] == {"rows": 2, "distinct": 1}
    replay = evaluate_query(
        project,
        {
            "schema_version": "frisket.query.v1",
            "kind": "sheet.filter",
            "scope": {"kind": "sheet", "sheet_id": sheet},
            "filter": result["groups"][0]["locator"]["filter"],
        },
        {"kind": "sheet", "sheet_id": sheet},
    )
    assert replay["total"] == 2
    project.close()


def test_distinct_count_preserves_json_scalar_types(make_project):
    project = make_project("json-distinct")
    sheet = project.add_sheet("values")
    value = project.add_column(sheet, "value", type="json")
    project.add_rows(
        sheet,
        [{"value": item} for item in (True, 1, False, 0, "1", 1.0)],
        {"value": value},
    )

    result = evaluate_analytics(
        project,
        {
            "sheet_id": sheet,
            "metrics": [
                {"id": "distinct", "kind": "distinct_count", "column_id": value}
            ],
        },
        {"kind": "sheet", "sheet_id": sheet},
    )

    assert result["groups"][0]["metrics"]["distinct"] == 5
    project.close()


def test_count_by_decodes_values_and_keeps_invalid_distinct_from_missing(make_project):
    project = make_project("count-by")
    sheet = project.add_sheet("values")
    amount = project.add_column(sheet, "amount", type="number")
    project.add_rows(
        sheet,
        [{"amount": 2}, {}, {"amount": "not-a-number"}, {"amount": True}],
        {"amount": amount},
    )

    result = evaluate_query(
        project,
        {
            "schema_version": "frisket.query.v1",
            "kind": "sheet.filter",
            "scope": {"kind": "sheet", "sheet_id": sheet},
        },
        {"kind": "sheet", "sheet_id": sheet},
        count_by=amount,
    )

    assert result["count_by"]["values"] == [
        {"kind": "invalid", "value": None, "count": 2},
        {"kind": "valid", "value": 2, "count": 1},
        {"kind": "missing", "value": None, "count": 1},
    ]
    project.close()


def test_boolean_group_values_and_locators_remain_boolean(make_project):
    project = make_project("boolean-groups")
    sheet = project.add_sheet("values")
    flag = project.add_column(sheet, "flag", type="boolean")
    project.add_rows(
        sheet,
        [{"flag": True}, {"flag": True}, {"flag": False}],
        {"flag": flag},
    )

    result = evaluate_analytics(
        project,
        {
            "sheet_id": sheet,
            "groups": [{"column_id": flag}],
            "metrics": [{"id": "rows", "kind": "count"}],
        },
        {"kind": "sheet", "sheet_id": sheet},
    )
    groups = {group["group"][0]["value"]: group for group in result["groups"]}

    assert all(type(value) is bool for value in groups)
    assert groups[True]["row_count"] == 2
    assert groups[False]["row_count"] == 1
    for value, group in groups.items():
        replay = evaluate_query(
            project,
            {
                "schema_version": "frisket.query.v1",
                "kind": "sheet.filter",
                "scope": {"kind": "sheet", "sheet_id": sheet},
                "filter": group["locator"]["filter"],
            },
            {"kind": "sheet", "sheet_id": sheet},
        )
        assert replay["total"] == (2 if value else 1)
    project.close()


@pytest.mark.parametrize(("literal", "expected"), [("true", 2), ("false", 1)])
def test_boolean_filter_literals_used_by_ask_tool_guidance(
    make_project, literal, expected
):
    project = make_project("boolean-filters")
    sheet = project.add_sheet("agencies")
    public_agency = project.add_column(sheet, "Public agency", type="boolean")
    project.add_rows(
        sheet,
        [
            {"Public agency": True},
            {"Public agency": True},
            {"Public agency": False},
        ],
        {"Public agency": public_agency},
    )
    filter_ = {"Public agency": {"eq": literal}}
    query = {
        "schema_version": "frisket.query.v1",
        "kind": "sheet.filter",
        "scope": {"kind": "sheet", "sheet_id": sheet},
        "filter": filter_,
    }
    assert (
        evaluate_query(project, query, {"kind": "sheet", "sheet_id": sheet})["total"]
        == expected
    )
    request = AnalyticsRequest.model_validate(
        {
            "sheet_id": sheet,
            "filter": filter_,
            "metrics": [{"id": "rows", "kind": "count"}],
        }
    )
    assert (
        evaluate_analytics(project, request, {"kind": "sheet", "sheet_id": sheet})[
            "row_count"
        ]
        == expected
    )


def test_analytics_rejects_plugin_filter_and_non_numeric_sum(make_project):
    project, sheet, columns, _rows = _seed(make_project)
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


def test_analytics_file_scope_cannot_filter_or_metric_another_column(make_project):
    project, sheet, columns, rows = _seed(make_project)
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


def test_group_drilldown_retains_filter_on_same_date_column(make_project):
    project, sheet, columns, rows = _seed(make_project)
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


# --- composed ---


def test_ask_calculation_opens_exact_underlying_records(tmp_path, monkeypatch):
    class Provider:
        calls = 0

        async def complete(self, request, client):
            self.calls += 1
            observations = [
                json.loads(m["content"])
                for m in request.messages
                if m.get("role") == "tool"
            ]
            if self.calls == 1:
                name, args = (
                    "analytics",
                    {
                        "request": {
                            "sheet_id": 1,
                            "filter": {"status": {"eq": "awarded"}},
                            "groups": [{"column_id": 1}],
                            "metrics": [
                                {"id": "total", "kind": "sum", "column_id": 3},
                                {"id": "middle", "kind": "median", "column_id": 3},
                            ],
                            "sort": [
                                {
                                    "kind": "metric",
                                    "metric_id": "total",
                                    "direction": "desc",
                                }
                            ],
                        }
                    },
                )
            elif self.calls == 2:
                [group] = observations[-1]["groups"]
                assert group["metrics"] == {"total": 4, "middle": 2.0}
                assert group["row_count"] == 2
                name, args = "open_source", {"citation_id": group["citation_id"]}
            else:
                assert observations[-1]["row_ids"] == [1, 2]
                name = next(
                    t["name"]
                    for t in request.tools
                    if {"text", "citation_ids"}
                    <= set(t["parameters"].get("properties", {}))
                )
                args = {
                    "text": "Awarded contracts total 4; their median is 2.",
                    "citation_ids": [observations[-1]["citation_id"]],
                }
            return LLMResponse(
                content=None,
                data=None,
                tokens_in=10,
                tokens_out=5,
                cost=0.01,
                model=request.model,
                tool_calls=[{"name": name, "args": args, "id": f"call-{self.calls}"}],
            )

    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        keys={"anthropic": "test-key"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
    )
    provider = Provider()
    router._adapters["anthropic"] = provider
    monkeypatch.setattr(Workspace, "router_for", lambda self, project: router)
    with make_client(tmp_path) as client:
        pid = client.post("/api/projects", json={"name": "Contracts"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet = project.add_sheet("Contracts")
        columns = {
            name: project.add_column(sheet, name, type=kind)
            for name, kind in [
                ("supplier", "text"),
                ("status", "text"),
                ("amount", "number"),
            ]
        }
        project.add_rows(
            sheet,
            [
                {"supplier": "A", "status": "awarded", "amount": 1},
                {"supplier": "A", "status": "awarded", "amount": 3},
                {"supplier": "A", "status": "draft", "amount": 1000},
            ],
            columns,
        )
        path = f"/api/projects/{pid}/qa/threads"
        thread = client.post(path, json={"scope": {"kind": "project"}}).json()
        path += "/" + thread["id"]
        submitted = client.post(
            path + "/turns",
            json={
                "request_id": "one",
                "question": "Total and median of awarded contracts by supplier?",
                "scope": {"kind": "project"},
                "model": "anthropic/test",
            },
        )
        assert submitted.status_code == 200, submitted.text
        deadline = time.monotonic() + 5  # realtime: bound real background turn
        while True:
            page = client.get(path + "/events").json()
            if page["active_turn"] is None:
                break
            assert time.monotonic() < deadline, page  # realtime: detect stalled turn
            time.sleep(0.01)  # realtime: yield while real worker threads finish
        answer = next(e for e in page["events"] if e["kind"] == "answer")
        citation = client.get(
            path + "/citations/" + answer["payload"]["citation_ids"][0]
        ).json()
        assert citation["status"] == "current"
        target = citation["target"]
        assert target["total"] == 2
        grid = client.get(
            f"/api/projects/{pid}/sheets/{sheet}/data",
            params={
                "filter": json.dumps(target["filter"]),
                "limit": 1,
            },
        )
        assert grid.status_code == 200, grid.text
        assert grid.json()["total"] == 2
        assert provider.calls == 3
        assert len([e for e in page["events"] if e["kind"] == "usage"]) == 3
        assert (
            "Awarded contracts total 4"
            in client.get(path + "/report").json()["markdown"]
        )


# --- tracing ---


class _CaptureExporter(SpanExporter):
    def __init__(
        self, *, result: SpanExportResult = SpanExportResult.SUCCESS, **kwargs
    ):
        self.kwargs = kwargs
        self.result = result
        self.spans: list[ReadableSpan] = []
        self.shutdown_calls = 0

    def export(self, spans):
        self.spans.extend(spans)
        return self.result

    def shutdown(self):
        self.shutdown_calls += 1


def test_standard_otlp_environment_builds_private_http_provider(monkeypatch):
    exporters: list[_CaptureExporter] = []

    def exporter(**kwargs):
        created = _CaptureExporter(**kwargs)
        exporters.append(created)
        return created

    global_provider = trace.get_tracer_provider()
    monkeypatch.setattr(project_qa_tracing, "OTLPSpanExporter", exporter)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://api.example/otel/")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_HEADERS",
        "Authorization=Bearer%20not-a-real-key,x-bt-parent=project_name%3AReporting",
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("FRISKET_ASK_TRACE_CONTENT", "true")

    tracing = project_qa_tracing.build_project_qa_tracing()
    assert tracing is not None
    assert trace.get_tracer_provider() is global_provider
    assert tracing.instrumentation.include_content is True
    assert tracing.instrumentation.include_binary_content is False
    assert exporters[0].kwargs == {
        "endpoint": "https://api.example/otel/v1/traces",
        "headers": {
            "authorization": "Bearer not-a-real-key",
            "x-bt-parent": "project_name:Reporting",
        },
    }
    tracing.shutdown()
    assert exporters[0].shutdown_calls == 1


def test_trace_specific_otlp_values_take_precedence(monkeypatch):
    exporters: list[_CaptureExporter] = []

    def exporter(**kwargs):
        created = _CaptureExporter(**kwargs)
        exporters.append(created)
        return created

    monkeypatch.setattr(project_qa_tracing, "OTLPSpanExporter", exporter)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://generic.example/otel")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "https://traces.example/custom",
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "generic=value")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "traces=value")

    tracing = project_qa_tracing.build_project_qa_tracing()
    assert tracing is not None
    assert exporters[0].kwargs == {
        "endpoint": "https://traces.example/custom",
        "headers": {"traces": "value"},
    }
    tracing.shutdown()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OTEL_SDK_DISABLED", "true"),
        ("OTEL_TRACES_EXPORTER", "none"),
    ],
)
def test_standard_otlp_opt_outs_disable_ask_tracing(monkeypatch, name, value):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://api.example/otel")
    monkeypatch.setenv(name, value)
    assert project_qa_tracing.build_project_qa_tracing() is None


def test_unsupported_protocol_disables_only_tracing(monkeypatch, caplog):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://api.example/otel")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "grpc")
    assert project_qa_tracing.build_project_qa_tracing() is None
    assert "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL" in caplog.text
    assert "grpc" not in caplog.text


def test_malformed_headers_do_not_reach_errors_or_logs(monkeypatch, caplog):
    secret = "not-a-real-secret-value"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://api.example/otel")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", f"Authorization {secret}")
    assert project_qa_tracing.build_project_qa_tracing() is None
    assert "OTEL_EXPORTER_OTLP_HEADERS" in caplog.text
    assert secret not in caplog.text


def test_actual_http_export_honors_content_opt_in(monkeypatch):
    received: list[tuple[str, dict[str, str], bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received.append(
                (
                    self.path,
                    {name.lower(): value for name, value in self.headers.items()},
                    self.rfile.read(length),
                )
            )
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # realtime: receive actual OTLP HTTP requests; shutdown joins before assertions.
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        host, port = server.server_address
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://{host}:{port}")
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer%20local-test"
        )
        monkeypatch.setenv("FRISKET_ASK_TRACE_CONTENT", "true")

        tracing = project_qa_tracing.build_project_qa_tracing()
        assert tracing is not None
        agent = Agent(
            TestModel(custom_output_text="private-content-answer-sentinel"),
            name="project_ask",
        )
        agent.instrument = tracing.instrumentation
        agent.run_sync("private-content-question-sentinel")
        tracing.shutdown()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert received
    for path, headers, _ in received:
        assert path == "/v1/traces"
        assert headers["authorization"] == "Bearer local-test"
        assert headers["content-type"] == "application/x-protobuf"
    body = b"".join(body for _, _, body in received)
    assert b"private-content-question-sentinel" in body
    assert b"private-content-answer-sentinel" in body


def test_ask_only_metadata_spans_survive_export_failure_and_shutdown_once(
    tmp_path, monkeypatch
):
    async def scenario():
        exporter = _CaptureExporter(result=SpanExportResult.FAILURE)
        provider = TracerProvider(shutdown_on_exit=False)
        provider.add_span_processor(
            BatchSpanProcessor(exporter, schedule_delay_millis=1)
        )
        tracing = ProjectQATracing(
            instrumentation=InstrumentationSettings(
                tracer_provider=provider,
                include_content=False,
                include_binary_content=False,
            ),
            provider=provider,
        )
        monkeypatch.setattr(
            project_qa_service, "build_project_qa_tracing", lambda: tracing
        )
        global_provider = trace.get_tracer_provider()

        question = "private-question-sentinel"
        answer = "private-answer-sentinel"
        sheet_name = "private-sheet-sentinel"

        class Adapter:
            calls = 0

            async def complete(self, request, client):
                self.calls += 1
                if self.calls == 1:
                    name = "inspect_sheets"
                    args = {}
                    call_id = "inspect"
                else:
                    name = "final_result"
                    args = {"text": answer, "citation_ids": []}
                    call_id = "answer"
                return LLMResponse(
                    content=None,
                    data=None,
                    model=request.model,
                    tokens_in=2,
                    tokens_out=3,
                    cost=0.02,
                    tool_calls=[
                        {
                            "name": name,
                            "id": call_id,
                            "args": args,
                        }
                    ],
                )

        router = ModelRouter(
            model_call_policy=RecordingPurchasePolicy(),
            keys={"anthropic": "test"},
            key_sources={"anthropic": "platform_key"},
            cache=None,
            cache_mode="off",
            use_env_keys=False,
        )
        router._adapters["anthropic"] = Adapter()
        workspace = Workspace(tmp_path / "ws", router=router)
        pid = workspace.create("Ask")["id"]
        workspace.get(pid).add_sheet(sheet_name)
        service = ProjectQAService(workspace)
        thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        await service.submit(
            pid,
            thread["id"],
            AskTurnRequest(
                request_id="trace-once",
                question=question,
                scope={"kind": "project"},
                model="anthropic/test",
            ),
        )
        await asyncio.gather(*list(service._tasks.values()))
        detail = await service.detail(pid, thread["id"])
        assert detail["history"]["events"][-1]["payload"]["status"] == "completed"

        await Agent(TestModel(custom_output_text="untraced sibling")).run(
            "sibling-agent-sentinel"
        )
        await asyncio.gather(service.shutdown(), service.shutdown())

        assert trace.get_tracer_provider() is global_provider
        assert exporter.shutdown_calls == 1
        assert exporter.spans
        trace_text = repr(
            [
                (span.name, span.attributes, span.events, span.status.description)
                for span in exporter.spans
            ]
        )
        assert "project_ask" in trace_text
        assert "inspect_sheets" in trace_text
        assert question not in trace_text
        assert answer not in trace_text
        assert sheet_name not in trace_text
        assert "sibling-agent-sentinel" not in trace_text
        assert "untraced sibling" not in trace_text

    asyncio.run(scenario())


@pytest.mark.parametrize("include_content", [False, True], ids=["metadata", "content"])
def test_ask_tool_exception_details_follow_content_setting(
    tmp_path, monkeypatch, include_content
):
    async def scenario():
        exporters: list[_CaptureExporter] = []

        def exporter(**kwargs):
            created = _CaptureExporter(**kwargs)
            exporters.append(created)
            return created

        monkeypatch.setattr(project_qa_tracing, "OTLPSpanExporter", exporter)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://api.example/otel")
        monkeypatch.setenv(
            "FRISKET_ASK_TRACE_CONTENT", "true" if include_content else "false"
        )
        tracing = project_qa_tracing.build_project_qa_tracing()
        assert tracing is not None

        secret = "private-project-tool-exception-sentinel"

        class Adapter:
            async def complete(self, request, client):
                return LLMResponse(
                    content=None,
                    data=None,
                    model=request.model,
                    tokens_in=1,
                    tokens_out=1,
                    cost=0.0,
                    tool_calls=[
                        {
                            "name": "read_rows",
                            "args": {"sheet_id": 1},
                            "id": "read-1",
                        }
                    ],
                )

        router = ModelRouter(
            model_call_policy=RecordingPurchasePolicy(),
            keys={"anthropic": "test"},
            key_sources={"anthropic": "platform_key"},
            cache=None,
            cache_mode="off",
            use_env_keys=False,
        )
        router._adapters["anthropic"] = Adapter()
        workspace = Workspace(tmp_path / "ws", router=router)
        pid = workspace.create("Ask")["id"]
        project = workspace.get(pid)
        project.add_sheet("Evidence")
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Ask", scope={"kind": "project"})
        turn = store.submit_turn(
            thread["id"],
            request_id="tool-error",
            question="private question",
            scope={"kind": "project"},
            model="anthropic/test",
        )

        def broken_read(*args, **kwargs):
            raise RuntimeError(secret)

        monkeypatch.setattr(ProjectQATools, "read_rows", broken_read)
        try:
            with pytest.raises(RuntimeError, match=secret):
                await run_turn(
                    project,
                    router,
                    turn,
                    store,
                    instrumentation=tracing.instrumentation,
                )
        finally:
            tracing.shutdown()
            project.close()

        spans = exporters[0].spans
        exception_events = [
            event
            for span in spans
            for event in span.events
            if event.name == "exception"
        ]
        assert exception_events
        assert any(
            str((event.attributes or {}).get("exception.type", "")).endswith(
                "RuntimeError"
            )
            for event in exception_events
        )
        assert any(span.status.status_code is StatusCode.ERROR for span in spans)
        trace_text = repr(
            [
                (
                    span.name,
                    span.attributes,
                    [(event.name, event.attributes) for event in span.events],
                    span.status.description,
                )
                for span in spans
            ]
        )

        if include_content:
            assert any(
                secret in str((event.attributes or {}).get("exception.message", ""))
                for event in exception_events
            )
            assert any(
                secret in str((event.attributes or {}).get("exception.stacktrace", ""))
                for event in exception_events
            )
            assert any(secret in str(span.status.description) for span in spans)
        else:
            assert all(
                "exception.message" not in (event.attributes or {})
                and "exception.stacktrace" not in (event.attributes or {})
                for event in exception_events
            )
            assert all(span.status.description is None for span in spans)
            assert secret not in trace_text

    asyncio.run(scenario())


def test_provider_error_is_sanitized_before_metadata_only_export(tmp_path, monkeypatch):
    async def scenario():
        exporter = _CaptureExporter()
        provider = TracerProvider(shutdown_on_exit=False)
        provider.add_span_processor(
            BatchSpanProcessor(exporter, schedule_delay_millis=1)
        )
        tracing = ProjectQATracing(
            instrumentation=InstrumentationSettings(
                tracer_provider=provider,
                include_content=False,
                include_binary_content=False,
            ),
            provider=provider,
        )
        monkeypatch.setattr(
            project_qa_service, "build_project_qa_tracing", lambda: tracing
        )

        secret = "provider-secret-sentinel"

        class Adapter:
            async def complete(self, request, client):
                raise LLMError(f"api_key={secret}")

        router = ModelRouter(
            model_call_policy=RecordingPurchasePolicy(),
            keys={"anthropic": secret},
            key_sources={"anthropic": "platform_key"},
            cache=None,
            cache_mode="off",
            use_env_keys=False,
        )
        router._adapters["anthropic"] = Adapter()
        workspace = Workspace(tmp_path / "ws", router=router)
        pid = workspace.create("Ask")["id"]
        service = ProjectQAService(workspace)
        thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        await service.submit(
            pid,
            thread["id"],
            AskTurnRequest(
                request_id="trace-error",
                question="private-error-question",
                scope={"kind": "project"},
                model="anthropic/test",
            ),
        )
        await asyncio.gather(*list(service._tasks.values()))
        await service.shutdown()

        trace_text = repr(
            [
                (span.attributes, span.events, span.status.description)
                for span in exporter.spans
            ]
        )
        assert secret not in trace_text

    asyncio.run(scenario())


# --- failure attribution ---


@pytest.mark.parametrize(
    ("completed", "expected_tool"),
    [
        (["inspect_sheets"], "search_actions"),
        ([], None),
        (["inspect_sheets", "search_actions"], None),
    ],
)
def test_failure_attributes_only_one_unfinished_tool(
    tmp_path, completed, expected_tool
):
    async def scenario():
        workspace = Workspace(tmp_path / "ws")
        project_id = workspace.create("Tool diagnostics")["id"]

        async def runner(project, router, turn, store):
            for tool in ("inspect_sheets", "search_actions"):
                store.append_event(
                    turn["id"], kind="tool_started", payload={"tool": tool}
                )
            for tool in completed:
                store.append_event(
                    turn["id"], kind="tool_completed", payload={"tool": tool}
                )
            raise RuntimeError("Internal catalog failure")

        service = ProjectQAService(workspace, runner=runner)
        try:
            thread = await service.create(
                project_id, AskThreadCreate(scope={"kind": "project"})
            )
            await service.submit(
                project_id,
                thread["id"],
                AskTurnRequest(
                    request_id="parallel-tools",
                    question="Which action should I use?",
                    scope={"kind": "project"},
                    model="openai/test",
                ),
            )
            await asyncio.gather(*list(service._tasks.values()))
            detail = await service.detail(project_id, thread["id"])
            failure = detail["history"]["events"][-1]["payload"]
            assert failure["status"] == "failed"
            assert failure["diagnostic"]["code"] == "internal_error"
            assert failure["diagnostic"].get("tool") == expected_tool
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_internal_error_reporter_failure_does_not_prevent_terminal_turn(tmp_path):
    async def scenario():
        workspace = Workspace(tmp_path / "ws")
        project_id = workspace.create("Reporter failure")["id"]
        reports = []

        async def runner(project, router, turn, store):
            raise RuntimeError("sensitive prompt must not reach reporter")

        def report(*args):
            state = ProjectQAStore(workspace.get(project_id)).get_turn(args[2])[
                "status"
            ]
            reports.append((args, state))
            raise RuntimeError("reporter unavailable")

        service = ProjectQAService(
            workspace, runner=runner, internal_error_reporter=report
        )
        try:
            thread = await service.create(
                project_id, AskThreadCreate(scope={"kind": "project"})
            )
            turn = await service.submit(
                project_id,
                thread["id"],
                AskTurnRequest(
                    request_id="failed-reporter",
                    question="Question",
                    scope={"kind": "project"},
                    model="openai/test",
                ),
            )
            await asyncio.gather(*list(service._tasks.values()))
            detail = await service.detail(project_id, thread["id"])
            failure = detail["history"]["events"][-1]["payload"]
            assert failure["status"] == "failed"
            assert failure["diagnostic"]["code"] == "internal_error"
            assert reports == [
                ((project_id, thread["id"], turn["id"], "RuntimeError"), "failed")
            ]
        finally:
            await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("group_count", [0, 4])
def test_distinct_medians_at_supported_width_keep_missing_and_odd_counts(
    make_project, group_count
):
    project = make_project("wide-medians")
    sheet = project.add_sheet("figures")
    groups = {
        f"group{i}": project.add_column(sheet, f"group{i}") for i in range(group_count)
    }
    amounts = {
        f"value{i}": project.add_column(sheet, f"value{i}", type="number")
        for i in range(12)
    }
    group_values = dict.fromkeys(groups, "A")
    if groups:
        group_values["group0"] = None
    records = []
    for value in (1, 3):
        record = {**group_values, **{name: value + i for i, name in enumerate(amounts)}}
        record["value11"] = None
        if value == 3:
            record["value0"] = None
        records.append(record)
    project.add_rows(sheet, records, {**groups, **amounts})
    request = AnalyticsRequest.model_validate(
        {
            "sheet_id": sheet,
            "groups": [{"column_id": cid} for cid in groups.values()],
            "metrics": [
                {"id": name, "kind": "median", "column_id": cid}
                for name, cid in amounts.items()
            ],
        }
    )
    result = evaluate_analytics(project, request, {"kind": "sheet", "sheet_id": sheet})
    expected = {name: i + 2 for i, name in enumerate(amounts)}
    expected["value0"] = 1
    expected["value11"] = None
    assert result["row_count"] == 2
    assert len(result["groups"]) == 1
    assert result["groups"][0]["metrics"] == expected
