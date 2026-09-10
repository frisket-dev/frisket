from __future__ import annotations

import json
import traceback
from typing import Any

import httpx
import pytest

from frisket.ops.integrations.google_sheets import (
    GoogleSheetsClient,
    GoogleSheetsClientError,
)


def test_google_sheets_client_refreshes_and_writes_new_spreadsheet() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8")
        requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "path": request.url.path,
                "query": request.url.query.decode("utf-8"),
                "auth": request.headers.get("authorization"),
                "body": body,
            }
        )
        if request.url.host == "oauth2.googleapis.com":
            assert "grant_type=refresh_token" in body
            assert "refresh_token=refresh-123" in body
            return httpx.Response(200, json={"access_token": "access-abc"})
        if request.method == "POST" and request.url.path == "/v4/spreadsheets":
            payload = json_body(request)
            assert payload["properties"]["title"] == "Frisket export"
            assert payload["sheets"][0]["properties"]["title"] == "Stories"
            return httpx.Response(
                200,
                json={
                    "spreadsheetId": "created-1",
                    "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/created-1",
                },
            )
        if request.method == "PUT" and "/values/" in request.url.path:
            assert request.headers["authorization"] == "Bearer access-abc"
            assert "/values/%27Stories%27%21A1" in str(request.url)
            assert request.url.query.decode("utf-8") == "valueInputOption=RAW"
            assert json_body(request)["values"] == [
                ["name", "status"],
                ["Ada", "ready"],
            ]
            return httpx.Response(200, json={"updatedRows": 2})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = GoogleSheetsClient(
        client_id="cid",
        client_secret="secret",
        http=http,
    )

    result = client.export_tabs(
        connection={"refresh_token": "refresh-123"},
        destination={
            "kind": "google_sheets",
            "mode": "new_spreadsheet",
            "spreadsheet_title": "Frisket export",
        },
        tabs=[
            {
                "sheet_id": 1,
                "title": "Stories",
                "columns": ["name", "status"],
                "rows": [["Ada", "ready"]],
            }
        ],
        write_policy="replace_managed_tabs",
    )

    assert result["spreadsheet_id"] == "created-1"
    assert result["spreadsheet_url"].endswith("/created-1")
    assert result["updated_tabs"] == [
        {"title": "Stories", "sheet_id": 1, "row_count": 1, "column_count": 2}
    ]
    assert [item["method"] for item in requests] == ["POST", "POST", "PUT"]


def test_google_sheets_client_writes_native_typed_scalars() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access-abc"})
        if request.method == "POST" and request.url.path == "/v4/spreadsheets":
            return httpx.Response(
                200,
                json={
                    "spreadsheetId": "created-typed",
                    "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/created-typed",
                },
            )
        if request.method == "PUT" and "/values/" in request.url.path:
            assert request.url.query.decode("utf-8") == "valueInputOption=RAW"
            captured["values"] = json_body(request)["values"]
            return httpx.Response(200, json={"updatedRows": 2})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = GoogleSheetsClient(client_id="cid", client_secret="secret", http=http)

    client.export_tabs(
        connection={"refresh_token": "refresh-123"},
        destination={
            "kind": "google_sheets",
            "mode": "new_spreadsheet",
            "spreadsheet_title": "Typed",
        },
        tabs=[
            {
                "sheet_id": 1,
                "title": "Numbers",
                "columns": ["name", "count", "ok", "ratio"],
                "rows": [["Acme", 42, True, 3.5], ["Beta", None, False, 0]],
            }
        ],
        write_policy="replace_managed_tabs",
    )

    # RAW write keeps native JSON types: numbers stay numbers, bools stay bools,
    # strings stay strings, None becomes an empty cell.
    assert captured["values"] == [
        ["name", "count", "ok", "ratio"],
        ["Acme", 42, True, 3.5],
        ["Beta", "", False, 0],
    ]
    types = [type(value) for value in captured["values"][1]]
    assert types == [str, int, bool, float]


def test_google_sheets_client_updates_existing_spreadsheet_without_deleting_unowned_tabs() -> (
    None
):
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "path": request.url.path,
                "body": request.read().decode("utf-8"),
            }
        )
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access-abc"})
        if (
            request.method == "GET"
            and request.url.path == "/v4/spreadsheets/existing-1"
        ):
            return httpx.Response(
                200,
                json={
                    "spreadsheetId": "existing-1",
                    "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/existing-1",
                    "sheets": [
                        {
                            "properties": {
                                "sheetId": 10,
                                "title": "Stories",
                                "gridProperties": {"rowCount": 1000, "columnCount": 26},
                            }
                        },
                        {
                            "properties": {
                                "sheetId": 99,
                                "title": "Manual notes",
                                "gridProperties": {"rowCount": 1000, "columnCount": 26},
                            }
                        },
                    ],
                },
            )
        if (
            request.method == "POST"
            and request.url.path == "/v4/spreadsheets/existing-1:batchUpdate"
        ):
            assert json_body(request) == {
                "requests": [{"addSheet": {"properties": {"title": "Other"}}}]
            }
            return httpx.Response(200, json={"replies": [{"addSheet": {}}]})
        if request.method == "POST" and request.url.path.endswith(":clear"):
            assert "/values/%27Manual%20notes%27" not in request.url.path
            assert any(
                expected in request.url.path
                for expected in ("'Stories'!A1:Z1000", "'Other'!A1:A2")
            )
            return httpx.Response(200, json={})
        if request.method == "PUT" and "/values/" in request.url.path:
            return httpx.Response(200, json={"updatedRows": 2})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = GoogleSheetsClient(
        client_id="cid",
        client_secret="secret",
        http=http,
    )

    result = client.export_tabs(
        connection={"refresh_token": "refresh-123"},
        destination={
            "kind": "google_sheets",
            "mode": "update_existing",
            "spreadsheet_id": "existing-1",
        },
        tabs=[
            {"sheet_id": 1, "title": "Stories", "columns": ["name"], "rows": [["Ada"]]},
            {"sheet_id": 2, "title": "Other", "columns": ["name"], "rows": [["Lin"]]},
        ],
        write_policy="replace_managed_tabs",
    )

    assert result["spreadsheet_id"] == "existing-1"
    assert result["updated_tabs"] == [
        {"title": "Stories", "sheet_id": 1, "row_count": 1, "column_count": 1},
        {"title": "Other", "sheet_id": 2, "row_count": 1, "column_count": 1},
    ]
    paths = [item["path"] for item in requests]
    assert "/v4/spreadsheets/existing-1" in paths
    assert "/v4/spreadsheets/existing-1:batchUpdate" in paths
    assert not any("Manual%20notes" in path for path in paths)


def json_body(request: httpx.Request) -> dict[str, Any]:
    content = request.content.decode("utf-8")
    return json.loads(content)


def test_google_sheets_client_bounds_and_redacts_provider_error_body() -> None:
    refresh_secret = "refresh-provider-secret"
    client_secret = "client-provider-secret"
    access_secret = "access-provider-secret"
    oversized = "z" * 10_000

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": access_secret})
        return httpx.Response(
            500,
            json={
                "error": {
                    "message": (
                        f"credentials {access_secret} / {refresh_secret} / "
                        f"{client_secret} {oversized}"
                    )
                }
            },
        )

    client = GoogleSheetsClient(
        client_id="cid",
        client_secret=client_secret,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GoogleSheetsClientError) as caught:
        client.export_tabs(
            connection={"refresh_token": refresh_secret},
            destination={
                "kind": "google_sheets",
                "mode": "new_spreadsheet",
                "spreadsheet_title": "Secrets",
            },
            tabs=[
                {
                    "sheet_id": 1,
                    "title": "Stories",
                    "columns": ["name"],
                    "rows": [["Ada"]],
                }
            ],
            write_policy="replace_managed_tabs",
        )

    message = str(caught.value)
    assert len(message) <= 1_000
    for secret in (refresh_secret, client_secret, access_secret):
        assert secret not in message
    assert oversized not in message
    assert "[REDACTED]" in message


@pytest.mark.parametrize("failure_phase", ["refresh", "sheets"])
def test_google_sheets_client_redacts_transport_exceptions(
    failure_phase: str,
) -> None:
    refresh_secret = "refresh-transport-secret"
    client_secret = "client-transport-secret"
    access_secret = "access-transport-secret"
    oversized = "t" * 10_000

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            if failure_phase == "refresh":
                raise RuntimeError(
                    f"transport {client_secret} / {refresh_secret} {oversized}"
                )
            return httpx.Response(200, json={"access_token": access_secret})
        raise RuntimeError(
            f"transport {client_secret} / {refresh_secret} / "
            f"{access_secret} {oversized}"
        )

    client = GoogleSheetsClient(
        client_id="cid",
        client_secret=client_secret,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GoogleSheetsClientError) as caught:
        client.export_tabs(
            connection={"refresh_token": refresh_secret},
            destination={
                "kind": "google_sheets",
                "mode": "new_spreadsheet",
                "spreadsheet_title": "Transport secrets",
            },
            tabs=[
                {
                    "sheet_id": 1,
                    "title": "Stories",
                    "columns": ["name"],
                    "rows": [["Ada"]],
                }
            ],
            write_policy="replace_managed_tabs",
        )

    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert len(str(caught.value)) <= 1_000
    for secret in (refresh_secret, client_secret, access_secret):
        assert secret not in rendered
    assert oversized not in rendered
    assert "[REDACTED]" in rendered
