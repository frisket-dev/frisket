"""Shared output-name template formatter.

A small, declarative naming util for actions that mint many output
filenames from a template + per-artifact context: ``{row:03d}``,
``{source_stem}``, ``{table:02d}``, ``{column}``, ``{date}`` are the named
tokens the current consumers use, but any key present in the caller's
``context`` mapping is a valid token -- referencing a token that is NOT in
the context is a loud :class:`OutputNameTemplateError`, never a silent
empty string.

Rule-of-three consumers (named in the task manifest): export.column_tables
(this lane), video_frames' output files (retrofit later), the searchable-PDF
``{output}_pdf`` blobs (hOCR receipt task adopts on merge).

Templates are restricted to simple named-field references
(``{token}``/``{token:format_spec}``) -- no attribute access
(``{token.attr}``), no indexing (``{token[0]}``), no positional/auto-numbered
fields (``{}``/``{0}``). This is the declarative-params guardrail applied to
naming: a template is data, never an expression.
"""

from __future__ import annotations

import re
import string
from collections.abc import Mapping
from typing import Any

__all__ = [
    "MAX_OUTPUT_NAME_COMPONENT_LENGTH",
    "OutputNameTemplateError",
    "dedupe_output_name",
    "format_output_name",
    "sanitize_output_name",
    "source_stem",
]


class OutputNameTemplateError(ValueError):
    """A template/context/sanitization error in output-name formatting.

    Always raised loudly (never swallowed into an empty-string name).
    """

    def __init__(self, message: str, *, token: str | None = None) -> None:
        super().__init__(message)
        self.token = token


# Simple named-field tokens only: letters/digits/underscore, must not start
# with a digit. No dots, brackets, or empty names (which would be Python
# str.format's auto-numbering / positional-arg forms).
_TOKEN_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Windows reserved device stems (case-insensitive); sanitizing these avoids
# producing an unopenable filename on a Windows destination filesystem even
# though the project mostly runs on POSIX.
_RESERVED_STEMS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Path separators, Windows-reserved punctuation, and control characters are
# never valid inside a single filename component.
_UNSAFE_CHAR_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

MAX_OUTPUT_NAME_COMPONENT_LENGTH = 200


class _StrictTemplateFormatter(string.Formatter):
    """A ``string.Formatter`` that only allows simple named-field lookups.

    Rejects attribute/index access, positional args, and unknown tokens with
    a loud :class:`OutputNameTemplateError` instead of ``string.Formatter``'s
    default ``KeyError``/``AttributeError``/empty-string behavior.
    """

    def __init__(self, context: Mapping[str, Any]) -> None:
        self._context = context

    def get_field(self, field_name: str, args: Any, kwargs: Any) -> tuple[Any, str]:
        if not _TOKEN_NAME_RE.match(field_name):
            raise OutputNameTemplateError(
                f"output name template token {field_name!r} is not a plain "
                "field name (attribute/index/positional access is not "
                "allowed in a declarative template)",
                token=field_name,
            )
        if field_name not in self._context:
            raise OutputNameTemplateError(
                f"output name template references unknown token {{{field_name}}}",
                token=field_name,
            )
        return self._context[field_name], field_name

    def get_value(self, key: Any, args: Any, kwargs: Any) -> Any:
        # Reached only for positional/auto-numbered fields ("{}" / "{0}"),
        # which get_field's regex never produces a named lookup for -- this
        # is the auto-numbering entry point string.Formatter uses instead.
        raise OutputNameTemplateError(
            "output name template positional/auto-numbered fields are not "
            "allowed; use a named token like {row}",
            token=str(key),
        )

    def check_unused_args(
        self, used_args: Any, args: Any, kwargs: Any
    ) -> None:  # pragma: no cover - no-op override
        return None


def source_stem(filename: str) -> str:
    """Return ``filename`` without its final extension, unicode-safe.

    Strips any directory components first (a stem is a name component, not a
    path). Multi-dot names keep everything but the LAST extension
    (``"report.final.csv"`` -> ``"report.final"``); a leading-dot dotfile
    with no other dot (``".env"``) is returned unchanged rather than emptied.
    """
    if not isinstance(filename, str) or not filename.strip():
        raise OutputNameTemplateError("source_stem requires a non-empty filename")
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if not name:
        raise OutputNameTemplateError("source_stem requires a filename, not a path")
    if "." not in name.lstrip("."):
        return name
    lstripped = len(name) - len(name.lstrip("."))
    leading_dots, rest = name[:lstripped], name[lstripped:]
    if "." not in rest:
        return name
    stem, _, _ext = rest.rpartition(".")
    return leading_dots + stem


def format_output_name(template: str, context: Mapping[str, Any]) -> str:
    """Render ``template`` against ``context`` and sanitize the result.

    ``template`` uses Python format-spec syntax restricted to simple named
    tokens present in ``context`` (e.g. ``"{row:03d}_{table:03d}.csv"``).
    Zero-padding, width, and other standard format specs work exactly as
    ``str.format`` since the underlying value (e.g. an ``int`` for ``row``)
    is passed straight through to ``format()``.

    Raises :class:`OutputNameTemplateError` for: a non-string/empty
    template, a token not present in ``context``, a non-simple token
    (attribute/index/positional access), or a template that sanitizes to an
    empty name. Never returns an empty string silently.
    """
    if not isinstance(template, str) or not template.strip():
        raise OutputNameTemplateError("output name template must be a non-empty string")
    formatter = _StrictTemplateFormatter(context)
    try:
        rendered = formatter.vformat(template, (), {})
    except OutputNameTemplateError:
        raise
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise OutputNameTemplateError(
            f"output name template {template!r} is invalid: {exc}"
        ) from exc
    return sanitize_output_name(rendered)


def sanitize_output_name(name: str) -> str:
    """Sanitize a rendered name into one safe filesystem name component.

    Collapses any path separators the rendered tokens introduced (this is
    always a single filename component, never a path), replaces reserved/
    control characters, strips trailing dots/spaces (a Windows quirk),
    avoids reserved device stems, and caps the byte length without splitting
    a multi-byte unicode codepoint.
    """
    if not isinstance(name, str) or not name:
        raise OutputNameTemplateError("output name sanitizes to an empty name")
    parts = [
        part
        for part in name.replace("\\", "/").split("/")
        if part not in ("", ".", "..")
    ]
    if not parts:
        raise OutputNameTemplateError("output name sanitizes to an empty name")
    flat = "_".join(parts)
    cleaned = _UNSAFE_CHAR_RE.sub("_", flat).strip(" .")
    if not cleaned:
        raise OutputNameTemplateError("output name sanitizes to an empty name")
    stem, dot, ext = cleaned.rpartition(".")
    if dot:
        if stem and stem.upper() in _RESERVED_STEMS:
            cleaned = f"_{stem}.{ext}"
    elif cleaned.upper() in _RESERVED_STEMS:
        cleaned = f"_{cleaned}"
    return _truncate_component(cleaned, MAX_OUTPUT_NAME_COMPONENT_LENGTH)


def _truncate_component(name: str, max_length: int) -> str:
    encoded = name.encode("utf-8")
    if len(encoded) <= max_length:
        return name
    stem, dot, ext = name.rpartition(".")
    suffix = f".{ext}" if dot and ext else ""
    suffix_bytes = len(suffix.encode("utf-8"))
    budget = max(max_length - suffix_bytes, 1)
    base = stem if dot and ext else name
    truncated = base.encode("utf-8")[:budget]
    # Never split a multi-byte codepoint: drop trailing bytes until valid.
    while truncated:
        try:
            decoded = truncated.decode("utf-8")
            break
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    else:
        decoded = ""
    result = f"{decoded}{suffix}"
    return result or "_"


def dedupe_output_name(name: str, used_names: set[str]) -> str:
    """Return ``name``, or a deterministic ``-2``/``-3``/... suffixed variant.

    Never silently overwrites: the FIRST unused candidate in ascending
    suffix order is returned. Does not mutate ``used_names`` -- callers add
    the returned name once they've committed to it.
    """
    if name not in used_names:
        return name
    stem, dot, ext = name.rpartition(".")
    base = stem if dot else name
    suffix_ext = f".{ext}" if dot else ""
    counter = 2
    while True:
        candidate = f"{base}-{counter}{suffix_ext}"
        if candidate not in used_names:
            return candidate
        counter += 1
