"""Composite template resolver for multi-field HTTP request specs.

An API-call op has several request fields (URL, headers, query params, body,
cookies) that each may contain `{{column}}` tokens AND `{{secret.NAME}}`
tokens. Plain `column_template_names` / `render_column_template`
(``templates.py``) know nothing of the `secret.` dialect and would treat
`{{secret.NAME}}` as a column literally named ``secret.NAME``. This module
wraps those primitives with one resolver that scans every field, splits
column tokens from secret tokens, and renders a field against row values plus
already-resolved secret values.

Inert by construction, same as ``templates.py``: rendering is a single
non-evaluating substitution pass — no expressions, no re-scanning inserted
text. Secret values are the caller's problem to fetch and never touch this
module's own state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from frisket.authoring.templates import BRACE_TEMPLATE_RE, format_template_value

SECRET_PREFIX = "secret."


class MissingSecret(Exception):
    """Raised by ``render_request_field`` when a ``{{secret.NAME}}`` token has
    no entry in ``secret_values``. ``name`` is the NAME portion (without the
    ``secret.`` prefix) so callers can report which credential is missing.
    """

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"missing secret: {name!r}")


@dataclass
class RequestTokens:
    """Result of scanning a set of request fields: unique, first-seen-order
    column names and secret names, combined across all fields (not per-field).
    """

    columns: list[str]
    secrets: list[str]


def _split_token(raw: str) -> tuple[bool, str]:
    """Classify one stripped ``{{...}}`` body: ``secret.NAME`` -> (True,
    NAME); anything else -> (False, raw). A token is a secret iff its
    stripped name starts with the ``secret.`` prefix.
    """
    name = raw.strip()
    if name.startswith(SECRET_PREFIX):
        return True, name[len(SECRET_PREFIX) :].strip()
    return False, name


def scan_request_tokens(templates: Iterable[str]) -> RequestTokens:
    """Scan every request field for `{{...}}` tokens, separating column names
    from `{{secret.NAME}}` tokens.

    Feeds the op's ``source_columns`` (the columns) and its
    required-credentials list (the secrets). ``{{secret.}}`` — an empty
    NAME — is skipped: it is neither a usable column nor a nameable secret.
    """
    columns: list[str] = []
    secrets: list[str] = []
    for template in templates:
        text = str(template or "")
        for match in BRACE_TEMPLATE_RE.finditer(text):
            is_secret, name = _split_token(match.group(1))
            if is_secret:
                if name and name not in secrets:
                    secrets.append(name)
            elif name and name not in columns:
                columns.append(name)
    return RequestTokens(columns=columns, secrets=secrets)


def render_request_field(
    template: Any,
    row_values: Mapping[str, Any],
    secret_values: Mapping[str, str],
) -> str:
    """Render one request field against row values and resolved secrets in a
    single inert pass over `{{...}}` tokens.

    ``{{secret.NAME}}`` resolves from ``secret_values`` (raises
    ``MissingSecret`` if absent). Any other token resolves from
    ``row_values`` via ``format_template_value`` — a missing column renders
    as ``""``, matching ``render_column_template``. Substitution never
    re-scans inserted text, so a value that itself contains ``{{...}}`` is
    inserted literally. No URL/JSON/form encoding happens here — that is the
    caller's per-field job at the request-building boundary.
    """
    text = str(template or "")

    def _sub(match: re.Match[str]) -> str:
        is_secret, name = _split_token(match.group(1))
        if is_secret:
            if name not in secret_values:
                raise MissingSecret(name)
            return secret_values[name]
        return format_template_value(row_values.get(name))

    return BRACE_TEMPLATE_RE.sub(_sub, text)
