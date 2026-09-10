from __future__ import annotations

import json
import re
from typing import Any, Mapping
from urllib.parse import quote

import httpx

from frisket.redaction import safe_error


GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SHEETS_API_BASE = "https://sheets.googleapis.com"
GOOGLE_SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/{spreadsheet_id}"

_INVALID_SHEET_TITLE_CHARS = re.compile(r"[\[\]\*:/\\?]")
_MAX_SHEET_TITLE_LEN = 100


class GoogleSheetsClientError(RuntimeError):
    """Provider failure surfaced to the ActionSpec receipt layer."""


class GoogleSheetsClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        http: httpx.Client | None = None,
        token_url: str = GOOGLE_TOKEN_URL,
        sheets_api_base: str = GOOGLE_SHEETS_API_BASE,
        timeout: float = 15.0,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http or httpx.Client(timeout=timeout)
        self._token_url = token_url
        self._sheets_api_base = sheets_api_base.rstrip("/")

    def close(self) -> None:
        self._http.close()

    def export_tabs(
        self,
        *,
        connection: Mapping[str, Any],
        destination: Mapping[str, Any],
        tabs: list[dict[str, Any]],
        write_policy: str,
    ) -> dict[str, Any]:
        if write_policy != "replace_managed_tabs":
            raise GoogleSheetsClientError(f"unsupported write policy: {write_policy}")
        normalized_tabs = normalize_google_sheets_tabs(tabs)
        if not normalized_tabs:
            raise GoogleSheetsClientError("at least one tab is required")
        normalized_destination = normalize_google_sheets_destination(destination)
        refresh_token = str(connection.get("refresh_token") or "").strip()
        access_token = self._refresh_access_token(connection)
        response_secret_values = tuple(
            value
            for value in (self._client_secret, refresh_token, access_token)
            if value
        )
        mode = normalized_destination["mode"]
        if mode == "new_spreadsheet":
            spreadsheet_id, spreadsheet_url = self._create_spreadsheet(
                access_token,
                title=normalized_destination["spreadsheet_title"],
                sheet_titles=[tab["title"] for tab in normalized_tabs],
                secret_values=response_secret_values,
            )
        elif mode == "update_existing":
            spreadsheet_id = normalized_destination["spreadsheet_id"]
            if not spreadsheet_id:
                raise GoogleSheetsClientError("spreadsheet_id is required")
            metadata = self._spreadsheet_metadata(
                access_token,
                spreadsheet_id,
                secret_values=response_secret_values,
            )
            spreadsheet_url = str(
                metadata.get("spreadsheetUrl")
                or GOOGLE_SPREADSHEET_URL.format(spreadsheet_id=spreadsheet_id)
            )
            existing_clear_cells = _existing_tab_clear_cells(metadata)
            self._ensure_existing_tabs(
                access_token,
                spreadsheet_id,
                metadata=metadata,
                sheet_titles=[tab["title"] for tab in normalized_tabs],
                secret_values=response_secret_values,
            )
            for tab in normalized_tabs:
                self._clear_values(
                    access_token,
                    spreadsheet_id,
                    tab["title"],
                    existing_clear_cells.get(tab["title"], _tab_clear_cells(tab)),
                    secret_values=response_secret_values,
                )
        else:
            raise GoogleSheetsClientError(f"unsupported destination mode: {mode}")

        for tab in normalized_tabs:
            self._write_values(
                access_token,
                spreadsheet_id,
                tab,
                secret_values=response_secret_values,
            )

        return {
            "spreadsheet_id": spreadsheet_id,
            "spreadsheet_url": spreadsheet_url,
            "updated_tabs": [
                {
                    "title": tab["title"],
                    "sheet_id": tab.get("sheet_id"),
                    "row_count": len(tab["rows"]),
                    "column_count": len(tab["columns"]),
                }
                for tab in normalized_tabs
            ],
        }

    def _refresh_access_token(self, connection: Mapping[str, Any]) -> str:
        refresh_token = str(connection.get("refresh_token") or "").strip()
        if not refresh_token:
            raise GoogleSheetsClientError("connected account has no refresh token")
        response = self._request(
            "POST",
            self._token_url,
            action="refresh Google access token",
            secret_values=(self._client_secret, refresh_token),
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        payload = self._json_response(
            response,
            "refresh Google access token",
            secret_values=(self._client_secret, refresh_token),
        )
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise GoogleSheetsClientError("Google token response omitted access_token")
        return access_token

    def _create_spreadsheet(
        self,
        access_token: str,
        *,
        title: str,
        sheet_titles: list[str],
        secret_values: tuple[str, ...],
    ) -> tuple[str, str]:
        response = self._request(
            "POST",
            f"{self._sheets_api_base}/v4/spreadsheets",
            action="create Google spreadsheet",
            secret_values=secret_values,
            headers=_auth_headers(access_token),
            json={
                "properties": {"title": title},
                "sheets": [
                    {"properties": {"title": sheet_title}}
                    for sheet_title in sheet_titles
                ],
            },
        )
        payload = self._json_response(
            response,
            "create Google spreadsheet",
            secret_values=secret_values,
        )
        spreadsheet_id = str(payload.get("spreadsheetId") or "").strip()
        if not spreadsheet_id:
            raise GoogleSheetsClientError(
                "Google spreadsheet create response omitted spreadsheetId"
            )
        spreadsheet_url = str(
            payload.get("spreadsheetUrl")
            or GOOGLE_SPREADSHEET_URL.format(spreadsheet_id=spreadsheet_id)
        )
        return spreadsheet_id, spreadsheet_url

    def _spreadsheet_metadata(
        self,
        access_token: str,
        spreadsheet_id: str,
        *,
        secret_values: tuple[str, ...],
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"{self._sheets_api_base}/v4/spreadsheets/{quote(spreadsheet_id, safe='')}",
            action="load Google spreadsheet metadata",
            secret_values=secret_values,
            headers=_auth_headers(access_token),
            params={"includeGridData": "false"},
        )
        return self._json_response(
            response,
            "load Google spreadsheet metadata",
            secret_values=secret_values,
        )

    def _ensure_existing_tabs(
        self,
        access_token: str,
        spreadsheet_id: str,
        *,
        metadata: Mapping[str, Any],
        sheet_titles: list[str],
        secret_values: tuple[str, ...],
    ) -> None:
        existing = {
            str((sheet.get("properties") or {}).get("title") or "")
            for sheet in metadata.get("sheets") or []
            if isinstance(sheet, Mapping)
        }
        missing = [title for title in sheet_titles if title not in existing]
        if not missing:
            return
        response = self._request(
            "POST",
            f"{self._sheets_api_base}/v4/spreadsheets/"
            f"{quote(spreadsheet_id, safe='')}:batchUpdate",
            action="add Google spreadsheet tabs",
            secret_values=secret_values,
            headers=_auth_headers(access_token),
            json={
                "requests": [
                    {"addSheet": {"properties": {"title": title}}} for title in missing
                ]
            },
        )
        self._json_response(
            response,
            "add Google spreadsheet tabs",
            secret_values=secret_values,
        )

    def _clear_values(
        self,
        access_token: str,
        spreadsheet_id: str,
        sheet_title: str,
        cells: str,
        *,
        secret_values: tuple[str, ...],
    ) -> None:
        response = self._request(
            "POST",
            f"{self._sheets_api_base}/v4/spreadsheets/"
            f"{quote(spreadsheet_id, safe='')}/values/"
            f"{quote(_a1_range(sheet_title, cells), safe='')}:clear",
            action="clear Google spreadsheet tab values",
            secret_values=secret_values,
            headers=_auth_headers(access_token),
            json={},
        )
        self._json_response(
            response,
            "clear Google spreadsheet tab values",
            secret_values=secret_values,
        )

    def _write_values(
        self,
        access_token: str,
        spreadsheet_id: str,
        tab: Mapping[str, Any],
        *,
        secret_values: tuple[str, ...],
    ) -> None:
        response = self._request(
            "PUT",
            f"{self._sheets_api_base}/v4/spreadsheets/"
            f"{quote(spreadsheet_id, safe='')}/values/"
            f"{quote(_a1_range(str(tab['title']), 'A1'), safe='')}",
            action="write Google spreadsheet tab values",
            secret_values=secret_values,
            headers=_auth_headers(access_token),
            params={"valueInputOption": "RAW"},
            json={"values": [tab["columns"], *tab["rows"]]},
        )
        self._json_response(
            response,
            "write Google spreadsheet tab values",
            secret_values=secret_values,
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        action: str,
        secret_values: tuple[str, ...],
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            return self._http.request(method, url, **kwargs)
        except Exception as exc:
            safe = safe_error(
                "google_sheets_transport_error",
                exc,
                secret_values=secret_values,
                max_chars=800,
            )
            raise GoogleSheetsClientError(
                f"Google API transport failed to {action}: {safe.detail}"
            ) from None

    @staticmethod
    def _json_response(
        response: httpx.Response,
        action: str,
        *,
        secret_values: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            raise GoogleSheetsClientError(
                f"Google API response for {action} was not JSON"
            ) from None
        if response.status_code >= 400:
            message = _google_error_message(payload) or response.text
            safe = safe_error(
                "google_sheets_provider_error",
                message,
                secret_values=secret_values,
                # Leave room for this exception's stable contextual prefix
                # while keeping the whole raised string under the shared
                # 1,000-character error budget.
                max_chars=800,
            )
            raise GoogleSheetsClientError(
                f"Google API failed to {action}: {safe.detail}"
            )
        return payload if isinstance(payload, dict) else {}


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _google_error_message(payload: Mapping[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        return str(message) if message else ""
    return str(error) if error else ""


def normalize_google_sheets_destination(
    destination: Mapping[str, Any],
) -> dict[str, str]:
    """The effective destination fields consumed by the provider client."""

    mode = str(destination.get("mode") or "")
    if mode == "new_spreadsheet":
        return {
            "mode": mode,
            "spreadsheet_title": str(
                destination.get("spreadsheet_title") or "Frisket export"
            ),
        }
    if mode == "update_existing":
        return {
            "mode": mode,
            "spreadsheet_id": str(destination.get("spreadsheet_id") or "").strip(),
        }
    return {"mode": mode}


def normalize_google_sheets_tabs(
    tabs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The exact JSON-safe tab payload sent to Google Sheets.

    Effect identity consumes this same projection so retry safety cannot drift
    from provider-call normalization.
    """

    normalized: list[dict[str, Any]] = []
    for index, tab in enumerate(tabs, start=1):
        normalized.append(
            {
                "sheet_id": tab.get("sheet_id"),
                "title": _sheet_title(str(tab.get("title") or ""), index=index),
                "columns": [_cell_value(value) for value in tab.get("columns") or []],
                "rows": [
                    [_cell_value(value) for value in row]
                    for row in (tab.get("rows") or [])
                ],
            }
        )
    titles = _unique_titles([tab["title"] for tab in normalized])
    for tab, title in zip(normalized, titles, strict=True):
        tab["title"] = title
    return normalized


def _sheet_title(title: str, *, index: int) -> str:
    cleaned = _INVALID_SHEET_TITLE_CHARS.sub("_", title).strip().strip("'")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = f"Sheet {index}"
    return cleaned[:_MAX_SHEET_TITLE_LEN]


def _unique_titles(titles: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for title in titles:
        candidate = title
        suffix = 2
        while candidate in seen:
            marker = f" ({suffix})"
            candidate = f"{title[: _MAX_SHEET_TITLE_LEN - len(marker)]}{marker}"
            suffix += 1
        seen.add(candidate)
        out.append(candidate)
    return out


def _cell_value(value: Any) -> Any:
    """Render a typed cell value for a Google Sheets RAW write.

    Numbers and booleans are sent as native JSON values so ``valueInputOption=RAW``
    lands them as real numbers/booleans in the tab; strings pass through; complex
    values (dict/list, e.g. a non-media JSON column) are JSON-stringified; None
    becomes an empty cell.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _tab_clear_cells(tab: Mapping[str, Any]) -> str:
    column_count = max(1, len(tab.get("columns") or []))
    row_count = max(1, len(tab.get("rows") or []) + 1)
    return f"A1:{_column_label(column_count)}{row_count}"


def _existing_tab_clear_cells(metadata: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for sheet in metadata.get("sheets") or []:
        if not isinstance(sheet, Mapping):
            continue
        properties = sheet.get("properties")
        if not isinstance(properties, Mapping):
            continue
        title = str(properties.get("title") or "").strip()
        grid = properties.get("gridProperties")
        if not title or not isinstance(grid, Mapping):
            continue
        row_count = _positive_int(grid.get("rowCount"))
        column_count = _positive_int(grid.get("columnCount"))
        if row_count is not None and column_count is not None:
            out[title] = f"A1:{_column_label(column_count)}{row_count}"
    return out


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _column_label(index: int) -> str:
    out = ""
    value = max(1, int(index))
    while value:
        value, remainder = divmod(value - 1, 26)
        out = chr(ord("A") + remainder) + out
    return out


def _a1_range(sheet_title: str, cells: str) -> str:
    escaped = sheet_title.replace("'", "''")
    return f"'{escaped}'!{cells}"
