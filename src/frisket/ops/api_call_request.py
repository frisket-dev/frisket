"""Pure request-builder for the API-call op (Step 1 of ``map.api_call``).

Turns a request spec (structured header/param/cookie/form pairs, plus a body
mode) into the keyword args ``safe_request`` (``netguard.py``) wants. No I/O:
no secret lookup, no HTTP, no knowledge of frisket ops or ``Recipe``. The
caller binds ``render_request_field`` (or anything with the same
``str -> str`` shape) into a one-arg ``render`` and passes that in.

Structured pairs, not textareas: headers/query_params/form_body/cookies are
each a ``list[tuple[str, str]]`` of (name-template, value-template) — the
frontend does textarea<->pairs translation, this module never sees raw
"Name: value" text.

Boundary encoding lives here, one rule per body mode:
- headers: rendered, then rejected outright if CR/LF/control chars appear
  (CRLF header injection).
- query params: rendered literally as ordered pairs; ``safe_request`` URL-encodes
  them and appends them to any query already present in the rendered URL.
- cookies: rendered literally into a dict for httpx's ``cookies=`` handling.
- form body: rendered as ordered pairs for ``data=`` (``safe_request``
  form-encodes while preserving duplicate names).
- json body: the *structural* safe-builder — parse-then-substitute-leaves,
  never string-concatenate into a JSON template.
- raw body: rendered literally; the caller owns the bytes.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Callable, Iterable

RenderFn = Callable[[str], str]

Pairs = Iterable[tuple[str, str]]

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")

# RFC 6265: a cookie name is an HTTP token; a cookie-octet excludes controls,
# whitespace, and the delimiters below. httpx does NOT encode cookie values
# (unlike params=), so a rendered value like ``x; session=attacker`` would
# forge a second cookie pair — these guards reject that at the boundary.
_COOKIE_NAME_BAD_RE = re.compile(r'[\x00-\x20\x7f()<>@,;:\\"/\[\]?={}]')
_COOKIE_VALUE_BAD_RE = re.compile(r'[\x00-\x20\x7f",;\\]')

_BODY_MODES = {"none", "form", "json", "raw"}


class HeaderInjection(ValueError):
    """A rendered header name or value contains CR, LF, or another control
    character. Raised instead of silently forwarding — CRLF in a header lets
    a templated row value inject extra header lines or split the request.
    """


class CookieInjection(ValueError):
    """A rendered cookie name or value carries a delimiter (``;``), control
    char, or other cookie-octet-illegal byte. Raised because httpx forwards
    cookie values unencoded, so a bare ``;`` in a value would inject an
    additional cookie pair (e.g. shadowing an auth/tenant cookie).
    """


class InvalidJsonBodyTemplate(ValueError):
    """The json-mode body template text is not valid JSON on its own, before
    any ``{{...}}`` substitution. Templating only ever replaces leaf string
    *contents*, so the template must already parse.
    """


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number is not allowed: {value}")
    return parsed


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, value in pairs:
        if key in parsed:
            raise ValueError(f"duplicate JSON object key is not allowed: {key!r}")
        parsed[key] = value
    return parsed


def strict_json_loads(value: str | bytes | bytearray) -> Any:
    """Parse unambiguous standards-compliant JSON.

    Python's default decoder accepts the non-JSON literals ``NaN`` and
    ``Infinity`` and also turns an overflowing exponent such as ``1e999`` into
    infinity. It also silently keeps the last value when an object repeats a
    key. Request templates and persisted response objects must stay valid and
    unambiguous, so both paths share this stricter decoder.
    """

    try:
        return json.loads(
            value,
            parse_constant=_reject_json_constant,
            parse_float=_parse_finite_json_float,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except RecursionError as exc:
        # A response can fit comfortably under the byte cap yet contain enough
        # nested arrays/objects to exceed the decoder's recursion limit. Keep
        # that attacker-controlled shape on the ordinary invalid-JSON path
        # instead of letting it escape the row boundary.
        raise ValueError("JSON nesting is too deep") from exc


def render_pairs(pairs: Pairs, render: RenderFn) -> list[tuple[str, str]]:
    """Render each (name, value) pair's name and value through ``render``.

    The result intentionally remains an ordered list rather than a dict so
    repeated header, query, and form names survive through HTTP encoding.
    """
    return [(render(name), render(value)) for name, value in pairs]


def build_headers(pairs: Pairs, render: RenderFn) -> list[tuple[str, str]]:
    """Render header pairs, then reject any rendered name or value carrying
    a CR, LF, or other control character (CRLF-injection guard)."""
    rendered = render_pairs(pairs, render)
    for name, value in rendered:
        if _CONTROL_CHAR_RE.search(name) or _CONTROL_CHAR_RE.search(value):
            raise HeaderInjection(
                f"header {name!r} name or value contains a control character"
            )
    return rendered


def build_query_params(pairs: Pairs, render: RenderFn) -> list[tuple[str, str]]:
    """Render query-param pairs for ``safe_request(params=...)``.

    Rendered literally and kept ordered — no manual URL-encoding. The
    transport performs boundary encoding and preserves duplicate names.
    """
    return render_pairs(pairs, render)


def build_cookies(pairs: Pairs, render: RenderFn) -> dict[str, str]:
    """Render cookie pairs, then reject any rendered name/value that isn't a
    legal RFC 6265 cookie token/octet (cookie-injection guard).

    httpx does not encode ``cookies=`` values, so a rendered value carrying a
    ``;`` (or CR/LF/NUL) could otherwise forge extra cookie pairs on the wire.
    """
    rendered_pairs = render_pairs(pairs, render)
    rendered: dict[str, str] = {}
    for name, value in rendered_pairs:
        if not name or _COOKIE_NAME_BAD_RE.search(name):
            raise CookieInjection(f"cookie name {name!r} is not a legal token")
        if _COOKIE_VALUE_BAD_RE.search(value):
            raise CookieInjection(
                f"cookie {name!r} value contains an illegal cookie-octet"
            )
        if name in rendered:
            # The HTTP boundary accepts a mapping, so retaining either value
            # would silently change a request that supplied both cookies.
            raise CookieInjection(f"duplicate cookie name is ambiguous: {name!r}")
        rendered[name] = value
    return rendered


def _substitute_leaves(node: Any, render: RenderFn) -> Any:
    """Walk a parsed-JSON structure, replacing string leaves with
    ``render(leaf)``. Numbers/bools/None and object keys pass through
    untouched — only string *values* are templated, and the result of
    rendering a leaf always stays a string."""
    if isinstance(node, str):
        return render(node)
    if isinstance(node, dict):
        return {key: _substitute_leaves(value, render) for key, value in node.items()}
    if isinstance(node, list):
        return [_substitute_leaves(item, render) for item in node]
    return node


def build_json_body(body_text: str, render: RenderFn) -> str:
    """The structural safe-builder for json-mode bodies.

    Parses ``body_text`` as JSON, substitutes ``render`` into every string
    leaf (object keys and non-string leaves untouched), then re-serializes.
    Because substitution happens on an already-parsed structure and the
    result is re-escaped by ``json.dumps``, a rendered value containing
    ``"``, ``{{...}}``, or ``\\`` cannot alter the JSON structure or get
    re-expanded — it lands as a correctly-escaped string.
    """
    try:
        parsed = strict_json_loads(body_text)
    except (json.JSONDecodeError, ValueError) as e:
        raise InvalidJsonBodyTemplate(f"body template is not valid JSON: {e}") from e
    try:
        substituted = _substitute_leaves(parsed, render)
        return json.dumps(substituted)
    except RecursionError as exc:
        raise InvalidJsonBodyTemplate("body template JSON nesting is too deep") from exc


def _has_content_type(headers: Iterable[tuple[str, str]]) -> bool:
    return any(name.lower() == "content-type" for name, _value in headers)


def build_request_kwargs(spec: dict[str, Any], render: RenderFn) -> dict[str, Any]:
    """Assemble ``safe_request`` kwargs from a request spec.

    ``spec`` keys: ``method``, ``url``, ``headers``, ``query_params``,
    ``body_mode`` (``"none"|"form"|"json"|"raw"``), ``body``, ``form_body``,
    ``cookies``, ``content_type``. Body handling by mode:

    - ``none`` — no body kwarg.
    - ``form`` — ``data=`` from the rendered ``form_body`` pairs (httpx
      form-encodes).
    - ``json`` — ``content=build_json_body(...)``, plus a default
      ``content-type: application/json`` header unless the caller already
      set one (any casing).
    - ``raw`` — ``content=render(body)`` literally; ``content-type`` comes
      from ``spec["content_type"]`` unless an explicit header already set
      one.
    """
    body_mode = spec.get("body_mode", "none")
    if body_mode not in _BODY_MODES:
        raise ValueError(f"unknown body_mode: {body_mode!r}")

    headers = build_headers(spec.get("headers") or [], render)
    params = build_query_params(spec.get("query_params") or [], render)
    cookies = build_cookies(spec.get("cookies") or [], render)

    kwargs: dict[str, Any] = {
        "method": spec.get("method", "GET"),
        "url": render(spec["url"]),
        "headers": headers,
        "params": params,
        "cookies": cookies,
    }

    if body_mode == "form":
        kwargs["data"] = render_pairs(spec.get("form_body") or [], render)
    elif body_mode == "json":
        kwargs["content"] = build_json_body(spec.get("body") or "", render)
        if not _has_content_type(headers):
            headers.append(("content-type", "application/json"))
    elif body_mode == "raw":
        kwargs["content"] = render(spec.get("body") or "")
        content_type = spec.get("content_type")
        if not _has_content_type(headers) and content_type:
            headers.append(("content-type", content_type))

    return kwargs
