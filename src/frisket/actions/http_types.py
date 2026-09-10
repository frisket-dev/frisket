"""Semantic HTTP request and narrow host-owned request capability."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from pydantic import Field, JsonValue, field_validator
from frisket.actions.types import ActionParams, InputReference, Row
from frisket.authoring.request_templates import scan_request_tokens
from frisket.ops.api_call_request import strict_json_loads
from frisket.contracts.actions.schemas._validators import (
    validate_positive_finite_number,
    validate_positive_finite_rate,
)

MAX_API_CALL_TIMEOUT_SECONDS = 120.0


def _pairs(value: Any) -> list[tuple[str, str]]:
    """Normalize stored name/value pairs (tuples or 2-item lists) to tuples."""
    out: list[tuple[str, str]] = []
    for item in value or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            out.append((str(item[0]), str(item[1])))
    return out


def _json_body_leaf_templates(body_text: str) -> list[str]:
    """The string leaves of a json-mode body — exactly what ``build_json_body``
    renders (object keys and non-string leaves are never templated). A body
    that doesn't parse yet contributes no column deps; it errors at execute."""
    try:
        parsed = strict_json_loads(body_text)
    except (json.JSONDecodeError, ValueError):
        return []
    out: list[str] = []
    stack: list[Any] = [parsed]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            stack.extend(node.values())  # values only — keys aren't rendered
        elif isinstance(node, list):
            stack.extend(node)
    return out


def _request_templates(spec: Mapping[str, Any]) -> list[str]:
    """Every templated string the request ACTUALLY renders, in lockstep with
    ``build_request_kwargs``: the URL and each header/query/cookie name AND
    value always; form pairs only in form mode; the body only in json/raw mode
    (json scans string leaves only, never object keys). content_type is used
    literally, not rendered, so it is not scanned. Keeping this list matched to
    execution keeps the precheck, provenance, and secret resolution from
    depending on a column an inactive field could never use."""
    body_mode = spec.get("body_mode", "none")
    templates: list[str] = [str(spec.get("url") or "")]
    for field in ("headers", "query_params", "cookies"):
        for name, value in _pairs(spec.get(field)):
            templates.append(name)
            templates.append(value)
    if body_mode == "form":
        for name, value in _pairs(spec.get("form_body")):
            templates.append(name)
            templates.append(value)
    elif body_mode == "raw":
        templates.append(str(spec.get("body") or ""))
    elif body_mode == "json":
        templates.extend(_json_body_leaf_templates(str(spec.get("body") or "")))
    return templates


def request_referenced_columns(spec: Mapping[str, Any]) -> list[str]:
    """The `{{column}}` tokens referenced anywhere in the request spec, in
    first-seen order. `{{secret.NAME}}` tokens are excluded. Authoritative for
    both semantic reference discovery and actual-argument validation, so
    admission and execution agree about which columns the request reads."""
    return list(scan_request_tokens(_request_templates(spec)).columns)


class HttpRequest(ActionParams):
    """An inert templated request; secrets resolve only in the admitted host."""

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str = Field(min_length=1)
    headers: list[tuple[str, str]] = Field(default_factory=list)
    query_params: list[tuple[str, str]] = Field(default_factory=list)
    form_body: list[tuple[str, str]] = Field(default_factory=list)
    cookies: list[tuple[str, str]] = Field(default_factory=list)
    body_mode: Literal["none", "form", "json", "raw"] = "none"
    body: str = ""
    content_type: str | None = None
    timeout: float = Field(default=30.0, gt=0, le=MAX_API_CALL_TIMEOUT_SECONDS)
    max_requests_per_second: float | None = Field(default=None, gt=0)
    follow_redirects: bool = True

    @field_validator("method", mode="before")
    @classmethod
    def _method(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("body_mode", mode="before")
    @classmethod
    def _body_mode(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("timeout")
    @classmethod
    def _timeout(cls, value: float) -> float:
        return validate_positive_finite_number(value)

    @field_validator("max_requests_per_second", mode="before")
    @classmethod
    def _blank_rate(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("max_requests_per_second")
    @classmethod
    def _rate(cls, value: float | None) -> float | None:
        return None if value is None else validate_positive_finite_rate(value)

    def references(self) -> tuple[InputReference, ...]:
        return tuple(
            InputReference(name, None)
            for name in request_referenced_columns(self.model_dump())
        )


class HttpRequester(Protocol):
    async def request_json(
        self, request: HttpRequest, row: Row
    ) -> dict[str, JsonValue]:
        """Send the actual request with admitted row values; return a JSON object."""
        ...
