"""Column-value template rendering for recipe specs.

Canonical user-authored syntax is ``{{column_name}}``.

Inert by design: this module SUBSTITUTES, it never evaluates. There are no
expressions, no arithmetic, no nesting, no function calls — a token is a name
lookup and (for the cluster-key dialect below) at most one blessed one-argument
string transform from a fixed allow-list. Anything outside that tiny grammar is
a validation error, never silently-executed authored input. Keep it that way:
the safety of every recipe/spec that renders user text through here rests on
this file having no way to *run* what it reads.
"""

from __future__ import annotations

import json
import re
from typing import Any

BRACE_TEMPLATE_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

# --- cluster-key dialect ---------------------------------------------------
# A one-token-source dialect used by the cluster-values "cluster key" input: a
# template rendered against a SINGLE source value, addressed as ``{{value}}``,
# with an optional pipe transform: ``{{value|lower}}``,
# ``{{value|before:" of "}}``, ``{{value|after:" of "}}``. Pipe-with-one-arg
# only — no nesting, no chaining, no expressions. Everything else is a
# KeyTemplateError.
KEY_TEMPLATE_TOKEN = "value"
# transform name -> whether it takes a separator argument
KEY_TEMPLATE_TRANSFORMS: dict[str, bool] = {
    "lower": False,
    "before": True,
    "after": True,
}
_KEY_TOKEN_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)


class KeyTemplateError(ValueError):
    """An invalid cluster-key template: unknown token, unknown transform, or a
    malformed/absent separator argument. A ``ValueError`` so contract params
    validators map it to their canonical ``invalid_params`` code and the
    preview layer to ``ClusterPreviewError('invalid_params', ...)`` — one
    grammar, each caller stamps its own canonical code."""


def _unwrap_arg(arg: str) -> str:
    """Strip surrounding whitespace then, if the remainder is wrapped in
    matching single or double quotes, unwrap them — quotes are how an author
    expresses a separator with meaningful leading/trailing spaces (``" of "``)
    that the surrounding strip would otherwise eat."""
    trimmed = arg.strip()
    if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in "\"'":
        return trimmed[1:-1]
    return trimmed


def _parse_key_token(inner: str) -> tuple[str, str | None, str | None]:
    """Split a token body ``value|before:" of "`` into (name, transform, arg).
    ``arg`` is the RAW text after the first ``:`` (quote handling deferred to
    application). No pipe -> (name, None, None)."""
    if "|" in inner:
        name_part, transform_part = inner.split("|", 1)
    else:
        name_part, transform_part = inner, None
    name = name_part.strip()
    if transform_part is None:
        return name, None, None
    if ":" in transform_part:
        tname, arg = transform_part.split(":", 1)
    else:
        tname, arg = transform_part, None
    return name, tname.strip(), arg


def _validate_key_token(inner: str) -> None:
    """Parse-check one ``{{...}}`` body, raising KeyTemplateError on any token
    outside the dialect."""
    name, transform, arg = _parse_key_token(inner)
    if name != KEY_TEMPLATE_TOKEN:
        raise KeyTemplateError(
            f"cluster key only knows {{{{{KEY_TEMPLATE_TOKEN}}}}}, not {name!r}"
        )
    if transform is None:
        return
    if transform not in KEY_TEMPLATE_TRANSFORMS:
        raise KeyTemplateError(
            f"unknown cluster-key transform {transform!r}; allowed: "
            f"{', '.join(sorted(KEY_TEMPLATE_TRANSFORMS))}"
        )
    takes_arg = KEY_TEMPLATE_TRANSFORMS[transform]
    if takes_arg:
        if arg is None or not _unwrap_arg(arg):
            raise KeyTemplateError(
                f"cluster-key transform {transform!r} needs a separator, "
                f'e.g. {{{{value|{transform}:" of "}}}}'
            )
    elif arg is not None:
        raise KeyTemplateError(f"cluster-key transform {transform!r} takes no argument")


def validate_key_template(template: Any) -> None:
    """Validate a cluster-key template, raising KeyTemplateError on anything
    outside the dialect. A non-empty template MUST contain at least one
    ``{{value}}`` token — a template with none renders the same constant key for
    every row (one giant meaningless cluster), which is always a mistake."""
    text = "" if template is None else str(template)
    tokens = _KEY_TOKEN_RE.findall(text)
    if not tokens:
        raise KeyTemplateError("cluster key must reference {{value}} at least once")
    for inner in tokens:
        _validate_key_token(inner)


def _apply_key_transform(value: str, transform: str | None, arg: str | None) -> str:
    if transform is None:
        return value
    if transform == "lower":
        return value.lower()
    # before / after split on the first occurrence of the separator; when the
    # separator is ABSENT the value passes through unchanged (so "President"
    # under before:" of " stays "President" and clusters with the stem of
    # "President of Honduras").
    sep = _unwrap_arg(arg or "")
    idx = value.find(sep)
    if idx == -1:
        return value
    if transform == "before":
        return value[:idx]
    return value[idx + len(sep) :]  # after


def render_value_key(template: Any, value: Any) -> str:
    """Render a cluster-key template against a SINGLE source value. An empty /
    whitespace-only template is passthrough (returns the value unchanged), so
    "no key" means cluster on the original form. Raises KeyTemplateError on a
    token outside the dialect (same grammar as ``validate_key_template``)."""
    source = "" if value is None else str(value)
    text = "" if template is None else str(template)
    if not text.strip():
        return source

    def _sub(match: re.Match[str]) -> str:
        name, transform, arg = _parse_key_token(match.group(1))
        if name != KEY_TEMPLATE_TOKEN:
            raise KeyTemplateError(
                f"cluster key only knows {{{{{KEY_TEMPLATE_TOKEN}}}}}, not {name!r}"
            )
        if transform is not None and transform not in KEY_TEMPLATE_TRANSFORMS:
            raise KeyTemplateError(f"unknown cluster-key transform {transform!r}")
        return _apply_key_transform(source, transform, arg)

    return _KEY_TOKEN_RE.sub(_sub, text)


def column_template_names(template: Any) -> list[str]:
    """Return template column names in first-seen order.

    ``{{column}}`` is canonical and supports arbitrary column names.
    """
    text = str(template or "")
    out: list[str] = []
    for match in BRACE_TEMPLATE_RE.finditer(text):
        name = match.group(1).strip()
        if name and name not in out:
            out.append(name)
    return out


def format_template_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def render_column_template(template: Any, row_values: dict[str, Any]) -> str:
    """Render a template against row values without re-rendering insertions."""
    text = str(template or "")
    out: list[str] = []
    i = 0

    while i < len(text):
        if text.startswith("{{", i):
            close = text.find("}}", i + 2)
            if close != -1:
                name = text[i + 2 : close].strip()
                out.append(format_template_value(row_values.get(name)))
                i = close + 2
                continue

        out.append(text[i])
        i += 1

    return "".join(out)
