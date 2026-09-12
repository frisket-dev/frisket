"""Engine-declared language contract.

A single, engine-generic declaration shape advertised in
``ui_hints.engines[*].language`` and consumed identically by transcribe today
and by the OCR / translate programs later. The declaration tells the form which
language control to render (nothing / a single Auto-first picker / an inline
multi-select) and whether the engine can report a detected language.

The wire param every consuming action carries is ``language: list[str] | None``
(always a list post-canonicalization; empty = auto). ``canonicalize_languages``
is the one canonicalizer so that ``absent`` / ``null`` / ``[]`` / ``["auto"]``
all collapse to the same effective value — this is what lets the idempotency
hash and receipts record one canonical form instead of hashing
four spellings of "auto" as four different requests.

This module is deliberately dependency-light (only the contract base) so the
contract-validation layer, the launcher-safe catalog-hints projection, and the
ops recipe can all import it without pulling heavy runtime deps.
"""

from __future__ import annotations

from typing import Any, Literal

from frisket.contracts.actions.schemas._base import ContractModel, StrictString

# The sentinel the UI uses for "detect automatically". It is NEVER a member of
# a canonical language list and NEVER a `choices` entry — the empty list is
# the wire representation of auto; "auto" only ever appears as the
# declaration's `default`.
AUTO_SENTINEL = "auto"

LanguageMode = Literal["auto_only", "fixed", "single", "multi"]


class LanguageChoice(ContractModel):
    value: StrictString  # ISO-639 code (never "auto")
    label: StrictString


class LanguageDeclaration(ContractModel):
    """Per-engine language capability, engine-generic.

    - ``auto_only``  — the engine ignores any language hint (renders no
      control). Distinct from ``fixed``: auto_only genuinely auto-detects.
    - ``fixed``      — the engine only ever does ``fixed_language``. UI can
      name that language honestly; no control.
    - ``single``     — one language OR Auto. UI renders one Auto-first picker.
    - ``multi``      — a SET of expected languages. UI renders a multi-select.

    ``detects`` gates the ``detected_language`` output column: an engine that
    cannot report a detected language emits no such column, rather
    than a constant-value lie.

    ``allows_auto`` gates whether the ``single`` picker offers an Auto option: a
    pair-based engine with no language identification (Opus-MT) can accept ONE
    explicit source language but cannot auto-detect, so it declares
    ``allows_auto=False`` — the UI drops the Auto-first entry and the params
    model rejects an empty/auto selection as ``invalid_language_selection``
    rather than silently defaulting to auto. Only meaningful for ``single``;
    ``auto_only`` is auto-by-definition and ``fixed``/``multi`` carry no Auto
    entry to gate.
    """

    mode: LanguageMode
    default: StrictString = AUTO_SENTINEL
    # Present only when the engine only ever produces one language.
    fixed_language: StrictString | None = None
    # None only for auto_only / fixed (no per-language control to populate).
    choices: list[LanguageChoice] | None = None
    detects: bool = False
    # False only for a single-mode engine that requires an EXPLICIT source and
    # cannot auto-detect (Opus-MT: pair-based, no LID). Default True preserves
    # every existing (transcribe) declaration unchanged.
    allows_auto: bool = True


def canonicalize_languages(value: Any) -> list[str]:
    """Collapse any accepted `language` spelling to ONE canonical list.

    Rules: absent/null/[]/["auto"] → [] (auto); blanks dropped;
    de-duplicated and SORTED (order is declared non-semantic). Always returns a
    list, so the post-validation field value is uniform and hashes
    deterministically.

    ONLY a wholly-auto request (empty, or every member the "auto" sentinel) is
    the auto []. A MIXED sentinel+code list (e.g. ``["auto","en"]``) is NOT
    silently coerced to ``["en"]`` — the "auto" sentinel is KEPT so downstream
    validation rejects it loudly (auto is not a language code), rather than
    guessing the caller's intent.

    A non-string member is left in place so the model's field typing
    (``list[StrictString]``) raises the normal validation error rather than this
    helper silently swallowing it.
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return value  # let strict typing reject non-list inputs downstream
    items: list[Any] = []
    for item in value:
        if isinstance(item, str):
            stripped = item.strip()
            if not stripped:
                continue  # blanks are noise, not a signal
            items.append(stripped)
        else:
            items.append(item)  # preserved for downstream type rejection
    # Wholly-auto (or empty) request => auto.
    if all(item == AUTO_SENTINEL for item in items):
        return []
    seen: set[str] = set()
    out: list[Any] = []
    for item in items:
        if isinstance(item, str):
            if item in seen:
                continue
            seen.add(item)
        out.append(item)
    if all(isinstance(item, str) for item in out):
        out.sort()
    return out


def choices_from_codes(codes: dict[str, str]) -> list[LanguageChoice]:
    """Build a `choices` list (code -> English label), sorted by label."""
    return [
        LanguageChoice(value=code, label=label)
        for code, label in sorted(codes.items(), key=lambda kv: kv[1].lower())
    ]


__all__ = [
    "AUTO_SENTINEL",
    "LanguageChoice",
    "LanguageDeclaration",
    "LanguageMode",
    "canonicalize_languages",
    "choices_from_codes",
]
