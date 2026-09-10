import json
from datetime import UTC, date, datetime, timedelta

import pytest

from frisket.querysets import resolve_sheet_filter_rows


@pytest.fixture
def client(replay_client):
    return replay_client


def _date_sheet(client) -> tuple[str, int, dict[str, int], dict[str, int]]:
    pid = client.post("/api/projects", json={"name": "Date Range Filters"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("events")
    columns = {
        "title": project.add_column(sheet_id, "title"),
        "published": project.add_column(sheet_id, "published", type="date"),
        "status": project.add_column(sheet_id, "status"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "before-window",
                "published": "2025-12-31",
                "status": "open",
            },
            {
                "title": "window-start",
                "published": "2026-01-01",
                "status": "open",
            },
            {
                "title": "window-middle",
                "published": "2026-02-15",
                "status": "closed",
            },
            {
                "title": "window-end",
                "published": "2026-03-31",
                "status": "open",
            },
            {
                "title": "same-day-datetime",
                "published": "2026-03-31T12:00:00Z",
                "status": "open",
            },
            {
                "title": "utc-next-day",
                "published": "2026-03-31T23:30:00-02:00",
                "status": "open",
            },
            {
                "title": "after-window",
                "published": "2026-04-01",
                "status": "open",
            },
            {
                "title": "invalid-date",
                "published": "not-a-date",
                "status": "open",
            },
            {"title": "compact-date", "published": "20260331", "status": "open"},
            {"title": "week-date", "published": "2026-W14-2", "status": "open"},
            {"title": "julian-date", "published": "0", "status": "open"},
            {
                "title": "missing-date",
                "status": "open",
            },
            {
                "title": "empty-date",
                "published": "  ",
                "status": "open",
            },
        ],
        columns,
    )
    names = {
        "before-window": row_ids[0],
        "window-start": row_ids[1],
        "window-middle": row_ids[2],
        "window-end": row_ids[3],
        "same-day-datetime": row_ids[4],
        "utc-next-day": row_ids[5],
        "after-window": row_ids[6],
        "invalid-date": row_ids[7],
        "compact-date": row_ids[8],
        "week-date": row_ids[9],
        "julian-date": row_ids[10],
        "missing-date": row_ids[11],
        "empty-date": row_ids[12],
    }
    return pid, sheet_id, columns, names


def _titles(payload: dict) -> list[str]:
    title_column = next(c for c in payload["columns"] if c["name"] == "title")
    title_id = str(title_column["id"])
    return [row["cells"][title_id] for row in payload["rows"]]


def test_sheet_data_supports_date_range_filters_and_row_locate(client):
    pid, sheet_id, _columns, rows = _date_sheet(client)
    between_filter = {
        "published": {"between": {"start": "2026-01-01", "end": "2026-03-31"}}
    }

    between = client.get(
        f"/api/projects/{pid}/sheets/{sheet_id}/data",
        params={
            "filter": json.dumps(between_filter),
            "sort": json.dumps([{"column": "published", "dir": "asc"}]),
        },
    )
    assert between.status_code == 200, between.text
    body = between.json()
    assert body["total"] == 4
    assert _titles(body) == [
        "window-start",
        "window-middle",
        "window-end",
        "same-day-datetime",
    ]

    gte = client.get(
        f"/api/projects/{pid}/sheets/{sheet_id}/data",
        params={"filter": json.dumps({"published": {"gte": "2026-03-31"}})},
    )
    assert gte.status_code == 200, gte.text
    assert _titles(gte.json()) == [
        "window-end",
        "same-day-datetime",
        "utc-next-day",
        "after-window",
    ]

    lte_date_only = client.get(
        f"/api/projects/{pid}/sheets/{sheet_id}/data",
        params={"filter": json.dumps({"published": {"lte": "2026-03-31"}})},
    )
    assert lte_date_only.status_code == 200, lte_date_only.text
    assert _titles(lte_date_only.json()) == [
        "before-window",
        "window-start",
        "window-middle",
        "window-end",
        "same-day-datetime",
    ]

    lte_datetime = client.get(
        f"/api/projects/{pid}/sheets/{sheet_id}/data",
        params={"filter": json.dumps({"published": {"lte": "2026-03-31T12:00:00Z"}})},
    )
    assert lte_datetime.status_code == 200, lte_datetime.text
    assert _titles(lte_datetime.json()) == [
        "before-window",
        "window-start",
        "window-middle",
        "window-end",
        "same-day-datetime",
    ]

    utc_normalized_bounds = client.get(
        f"/api/projects/{pid}/sheets/{sheet_id}/data",
        params={
            "filter": json.dumps(
                {
                    "published": {
                        "between": {
                            "start": "2026-04-01T00:30:00+02:00",
                            "end": "2026-03-31T23:30:00Z",
                        }
                    }
                }
            )
        },
    )
    assert utc_normalized_bounds.status_code == 200, utc_normalized_bounds.text
    assert _titles(utc_normalized_bounds.json()) == [
        "window-end",
        "same-day-datetime",
    ]

    located = client.get(
        f"/api/projects/{pid}/sheets/{sheet_id}/rows/{rows['window-middle']}/locate",
        params={
            "filter": json.dumps(between_filter),
            "sort": json.dumps([{"column": "published", "dir": "asc"}]),
            "page_size": 2,
        },
    )
    assert located.status_code == 200, located.text
    assert located.json()["found"] is True
    assert located.json()["index"] == 1
    assert located.json()["page_offset"] == 0

    outside = client.get(
        f"/api/projects/{pid}/sheets/{sheet_id}/rows/{rows['after-window']}/locate",
        params={"filter": json.dumps(between_filter), "page_size": 2},
    )
    assert outside.status_code == 200, outside.text
    assert outside.json()["found"] is False


def test_date_filters_are_date_aware_and_bad_values_are_inspectable(client):
    pid, sheet_id, _columns, _rows = _date_sheet(client)

    cases = [
        ({"published": {"eq": "2026-03-31"}}, ["window-end", "same-day-datetime"]),
        (
            {"published": {"date_year": "2026"}},
            [
                "window-start",
                "window-middle",
                "window-end",
                "same-day-datetime",
                "utc-next-day",
                "after-window",
            ],
        ),
        (
            {"published": {"date_month": "3"}},
            ["window-end", "same-day-datetime"],
        ),
        (
            {"published": {"date_weekday": "2"}},
            ["window-end", "same-day-datetime"],
        ),
        (
            {"published": {"eq": "2026-04-01"}},
            ["utc-next-day", "after-window"],
        ),
        (
            {"published": {"date_invalid": "true"}},
            ["invalid-date", "compact-date", "week-date", "julian-date"],
        ),
    ]
    for spec, expected in cases:
        response = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/data",
            params={"filter": json.dumps(spec)},
        )
        assert response.status_code == 200, response.text
        assert _titles(response.json()) == expected


def test_relative_date_filters_move_with_today(client):
    pid = client.post("/api/projects", json={"name": "Relative Dates"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("events")
    columns = {
        "title": project.add_column(sheet_id, "title"),
        "published": project.add_column(sheet_id, "published", type="date"),
    }
    today = datetime.now(UTC).date()
    project.add_rows(
        sheet_id,
        [
            {"title": "eight-days-ago", "published": str(today - timedelta(days=8))},
            {"title": "six-days-ago", "published": str(today - timedelta(days=6))},
            {"title": "today", "published": str(today)},
            {"title": "future-this-year", "published": str(date(today.year, 12, 31))},
        ],
        columns,
    )

    cases = [
        (
            {"published": {"date_relative": {"amount": 7, "unit": "days"}}},
            ["six-days-ago", "today"],
        ),
        (
            {"published": {"date_relative": {"amount": 1, "unit": "weeks"}}},
            ["six-days-ago", "today"],
        ),
        (
            {"published": {"date_relative": {"amount": 1, "unit": "months"}}},
            ["eight-days-ago", "six-days-ago", "today"],
        ),
        (
            {"published": {"date_this_year": "true"}},
            ["eight-days-ago", "six-days-ago", "today", "future-this-year"],
        ),
        (
            {"published": {"date_ytd": "true"}},
            ["eight-days-ago", "six-days-ago", "today"],
        ),
    ]
    for spec, expected in cases:
        response = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/data",
            params={"filter": json.dumps(spec)},
        )
        assert response.status_code == 200, response.text
        assert _titles(response.json()) == expected


@pytest.mark.parametrize(
    ("reference_date", "values", "expected"),
    [
        (
            date(2024, 3, 31),
            ["2024-02-28", "2024-02-29", "2024-03-31", "2024-04-01"],
            ["2024-02-29", "2024-03-31"],
        ),
        (
            date(2023, 3, 31),
            ["2023-02-27", "2023-02-28", "2023-03-31", "2023-04-01"],
            ["2023-02-28", "2023-03-31"],
        ),
    ],
)
def test_relative_months_clamp_to_target_month_end(
    client, reference_date, values, expected
):
    pid = client.post("/api/projects", json={"name": "Month Boundary"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("events")
    columns = {
        "title": project.add_column(sheet_id, "title"),
        "published": project.add_column(sheet_id, "published", type="date"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [{"title": value, "published": value} for value in values],
        columns,
    )
    resolved = resolve_sheet_filter_rows(
        project,
        sheet_id,
        filter_=json.dumps(
            {"published": {"date_relative": {"amount": 1, "unit": "months"}}}
        ),
        reference_date=reference_date,
    )
    titles_by_row = dict(zip(row_ids, values, strict=True))
    assert [titles_by_row[row_id] for row_id in resolved.row_ids] == expected


def test_date_range_filters_validate_bounds_and_column_type(client):
    pid, sheet_id, _columns, _rows = _date_sheet(client)

    cases = [
        {"published": {"gte": "June 1, 2026"}},
        {"published": {"lte": ""}},
        {"published": {"between": {"start": "2026-03-31", "end": "2026-01-01"}}},
        {"published": {"between": {"start": "2026-01-01"}}},
        {
            "published": {
                "between": {
                    "start": "2026-03-31T23:30:00-02:00",
                    "end": "2026-04-01T00:30:00+02:00",
                }
            }
        },
        {"published": {"gte": "2026-01-01", "lte": "2026-03-31"}},
        {"status": {"gte": "2026-01-01"}},
        {"published": {"eq": "June 1, 2026"}},
        {"published": {"date_relative": {"amount": 0, "unit": "days"}}},
        {"published": {"date_relative": {"amount": 7, "unit": "fortnights"}}},
        {"published": {"date_relative": {"amount": 7}}},
        {"published": {"date_year": 0}},
        {"published": {"date_year": 2026.5}},
        {"published": {"date_month": 13}},
        {"published": {"date_weekday": 7}},
        {"published": {"date_ytd": "yes"}},
        {"status": {"date_invalid": "true"}},
        {"published": {"contains": "2026"}},
    ]
    for spec in cases:
        response = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/data",
            params={"filter": json.dumps(spec)},
        )
        assert response.status_code == 400, spec


def test_saved_views_preserve_date_range_filter_json(client):
    pid, sheet_id, _columns, _rows = _date_sheet(client)
    date_filter = {
        "published": {"between": {"start": "2026-01-01", "end": "2026-03-31"}}
    }
    response = client.post(
        f"/api/projects/{pid}/views",
        json={
            "name": "Q1 dates",
            "sheet_id": sheet_id,
            "filter": date_filter,
            "sort": [{"column": "published", "dir": "asc"}],
        },
    )
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["spec"]["filter"] == date_filter

    listed = client.get(
        f"/api/projects/{pid}/views", params={"sheet_id": sheet_id}
    ).json()
    saved = next(item for item in listed if item["id"] == view["id"])
    assert saved["spec"]["filter"] == date_filter
