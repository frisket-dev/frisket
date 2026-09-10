"""Typed whole-column cleanup action and its deterministic transforms."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from pydantic import field_validator

from frisket.actions.core import ActionCategory, action, map_batch
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    DynamicOutput,
    Outcome,
    RowResult,
    Rows,
)
from frisket.ops.cluster_fingerprint import fingerprint

_WS = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Edge punctuation the OPTIONAL strip_edge_punct transform and the always-on
# DETECTION copy trim from the leading/trailing edges. Whitespace is handled by
# the always-on floor (stage 0) separately, so this set is punctuation only.
_PUNCT_EDGE = "\"'`.,;:|"
_DEFAULT_NULL_TOKENS = "n/a,na,null,none,unknown,unk,tbd,-,--,?"
_ABBREVIATIONS = {
    "assoc": "association",
    "assn": "association",
    "co": "company",
    "corp": "corporation",
    "dept": "department",
    "div": "division",
    "inc": "incorporated",
    "intl": "international",
    "natl": "national",
    "org": "organization",
    "univ": "university",
}
_STOPWORDS = {"and", "for", "in", "of", "on", "the", "to"}
_ACRONYMS = {"ai", "api", "cdc", "cia", "dc", "doi", "fbi", "foia", "irs", "nyc", "usa"}

# Unicode punctuation normalization (stage 4). NFKC (stage 0) does NOT fold these
# — they are not compatibility-equivalent — so this is a genuinely distinct
# transform.
_UNICODE_PUNCT = {
    "“": '"',
    "”": '"',
    "„": '"',
    "‘": "'",
    "’": "'",
    "‚": "'",
    "–": "-",
    "—": "-",
    "―": "-",
    "−": "-",
    "…": "...",
}
_UNICODE_PUNCT_RE = re.compile("|".join(re.escape(k) for k in _UNICODE_PUNCT))

# Numeric-only thousands-separator commas: a comma flanked by a digit on the
# left and a run of exactly three digits (then a non-digit or end) on the right.
# This deliberately does NOT touch prose commas or "Last, First" commas.
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?:\D|$))")

_CURRENCY_SYMBOLS = "$€£¥₹₩¢₡₪₽₴"
_CURRENCY_CODES = frozenset(
    {"USD", "EUR", "GBP", "JPY", "CNY", "INR", "KRW", "CAD", "AUD", "CHF", "MXN", "BRL"}
)


@dataclass
class _PreparedValue:
    display: Any
    key: str | None
    kind: str
    expanded: bool


class CleanableColumn(ColumnRef[str]):
    accepted_column_types: ClassVar[tuple[str, ...]] = ("text", "category", "link")


class CleanColumnParams(ActionParams):
    source: CleanableColumn
    case: Literal["keep", "smart_title", "title", "upper", "lower"] = "smart_title"
    null_tokens: str = _DEFAULT_NULL_TOKENS
    blank_null_tokens: bool = True
    lowercase_emails: bool = True
    normalize_us_phone: bool = True
    expand_abbreviations: bool = True
    reorder_person_name: bool = True
    canonicalize_duplicates: bool = True
    strip_edge_punct: bool = False
    normalize_unicode_punct: bool = False
    remove_thousands_separators: bool = False
    remove_all_commas: bool = False
    make_numeric: bool = False

    @field_validator("null_tokens")
    @classmethod
    def _nonblank_null_tokens(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("null tokens must be non-empty")
        return value


def _parse_null_tokens(raw: str) -> frozenset[str]:
    return frozenset(
        token.casefold() for item in raw.split(",") if (token := item.strip())
    )


def _clean_base(value: Any) -> str:
    """Always-on floor: NFKC + control-char strip + whitespace collapse + trim.

    Edge PUNCTUATION is intentionally NOT stripped here (that is the optional
    strip_edge_punct transform, and detection uses its own stripped copy)."""
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text).replace("\xa0", " ")
    text = _CONTROL.sub(" ", text)
    text = _WS.sub(" ", text).strip()
    return text


def _prepare_value(
    value: Any,
    params: CleanColumnParams,
    null_tokens: frozenset[str],
) -> _PreparedValue:
    base = _clean_base(value)
    # Detection copy: edge punctuation stripped regardless of the visible
    # strip_edge_punct toggle, so "N/A." / "a@b.com." are recognized.
    detect = base.strip(_PUNCT_EDGE)

    if params.blank_null_tokens and (not detect or detect.casefold() in null_tokens):
        return _PreparedValue(
            display=None,
            key=None,
            kind="null",
            expanded=False,
        )

    if params.lowercase_emails:
        emailish = detect.removeprefix("mailto:").strip()
        if _EMAIL.match(emailish):
            display = emailish.lower()
            return _PreparedValue(
                display=display,
                key=f"email:{display}",
                kind="email",
                expanded=False,
            )

    if params.normalize_us_phone:
        phone = _phone(detect)
        if phone is not None:
            return _PreparedValue(
                display=phone,
                key=f"phone:{phone}",
                kind="phone",
                expanded=False,
            )

    # --- text path (ordered, individually gated) ---------------------------
    text = base

    if params.strip_edge_punct:
        text = text.strip(_PUNCT_EDGE).strip()

    if params.make_numeric:
        number, status = _make_numeric(text)
        if status == "parsed":
            return _PreparedValue(
                display=number,
                key=None,
                kind="numeric",
                expanded=False,
            )
        if status == "ambiguous":
            # Short-circuit: the row-local typed failure is published later.
            return _PreparedValue(
                display=text,
                key=None,
                kind="text",
                expanded=False,
            )

    if params.remove_thousands_separators:
        text = _THOUSANDS.sub("", text)

    if params.normalize_unicode_punct:
        text = _UNICODE_PUNCT_RE.sub(lambda m: _UNICODE_PUNCT[m.group()], text)

    if params.reorder_person_name:
        reordered = _person_comma_reorder(text)
        if reordered is not None:
            text = reordered

    if params.remove_all_commas:
        text = text.replace(",", "")
        text = _WS.sub(" ", text).strip()

    expanded = False
    if params.expand_abbreviations:
        text, expanded = _expand_abbreviations(text)

    text = _apply_case(text, params.case)
    text = _WS.sub(" ", text).strip()

    key = fingerprint(_key_text(text))
    return _PreparedValue(
        display=text,
        key=f"text:{key}" if key else None,
        kind="text",
        expanded=expanded,
    )


def _phone(text: str) -> str | None:
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    if re.search(r"[A-Za-z]", text):
        return None
    return f"+1{digits}"


def _person_comma_reorder(text: str) -> str | None:
    if "," not in text:
        return None
    left, right, *rest = [part.strip() for part in text.split(",")]
    if rest or not left or not right:
        return None
    if len(left.split()) > 3 or len(right.split()) > 3:
        return None
    return f"{right} {left}"


def _expand_abbreviations(text: str) -> tuple[str, bool]:
    """Expand `&`→and and the common-abbreviation set, preserving per-word
    edge punctuation. Casing is applied separately by ``_apply_case``."""
    expanded = False
    words: list[str] = []
    for raw_word in re.split(r"(\s+)", text.replace("&", " and ")):
        if not raw_word or raw_word.isspace():
            words.append(raw_word)
            continue
        prefix_len = len(raw_word) - len(raw_word.lstrip(".,;:/\\()[]{}"))
        suffix_start = len(raw_word.rstrip(".,;:/\\()[]{}"))
        prefix = raw_word[:prefix_len]
        suffix = raw_word[suffix_start:]
        clean = raw_word[prefix_len:suffix_start]
        lower = clean.casefold().rstrip(".")
        replacement = _ABBREVIATIONS.get(lower)
        if replacement:
            clean = replacement
            suffix = suffix.lstrip(".")
            expanded = True
        words.append(prefix + clean + suffix)
    text = _WS.sub(" ", "".join(words)).strip()
    return text, expanded


def _apply_case(text: str, case: str) -> str:
    if case == "keep":
        return text
    if case == "upper":
        return text.upper()
    if case == "lower":
        return text.lower()
    if case == "title":
        return _title_case(text)
    # smart_title
    words: list[str] = []
    for raw_word in re.split(r"(\s+)", text):
        if not raw_word or raw_word.isspace():
            words.append(raw_word)
            continue
        prefix_len = len(raw_word) - len(raw_word.lstrip(".,;:/\\()[]{}"))
        suffix_start = len(raw_word.rstrip(".,;:/\\()[]{}"))
        prefix = raw_word[:prefix_len]
        suffix = raw_word[suffix_start:]
        core = raw_word[prefix_len:suffix_start]
        words.append(
            prefix
            + _smart_word(core, is_first=not any(w.strip() for w in words))
            + suffix
        )
    return "".join(words)


def _title_case(text: str) -> str:
    def _cap(word: str) -> str:
        return word[:1].upper() + word[1:].lower() if word else word

    return "".join(
        part if part.isspace() else _cap(part) for part in re.split(r"(\s+)", text)
    )


def _smart_word(word: str, *, is_first: bool) -> str:
    if not word:
        return word
    lower = word.casefold()
    if lower in _ACRONYMS:
        return lower.upper()
    if not is_first and lower in _STOPWORDS:
        return lower
    if word.isupper() or word.islower():
        return lower.capitalize()
    return word[0].upper() + word[1:]


def _make_numeric(text: str) -> tuple[Any, str]:
    """Strict, conservative English-locale numeric parser.

    Contract: decimal separator is ``.``, grouping separator is ``,`` (or a
    space). We NEVER guess a non-en interpretation. ANY ambiguity - a comma used
    as a decimal (``1.234,56``), a malformed group (``1,23.45``, ``1,23,456``),
    two amounts / embedded text (``USD 1 CAD 2``, ``Model X-100``) - returns
    ``(None, "ambiguous")`` so the typed action publishes a row-local failure.
    A value with no digits at all -> ``(None, "skip")`` (not applicable).
    Success returns ``(int|float, "parsed")``.
    """
    s = text.strip()
    if not s:
        return None, "skip"
    if not any(ch.isdigit() for ch in s):
        return None, "skip"

    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1].strip()

    # Strip currency codes/symbols to a SPACE so distinct amounts do not fuse
    # ("USD 1 CAD 2" -> " 1  2 ", rejected below as multiple tokens).
    for code in _CURRENCY_CODES:
        s = re.sub(rf"(?<![A-Za-z]){code}(?![A-Za-z])", " ", s, flags=re.IGNORECASE)
    s = "".join(" " if ch in _CURRENCY_SYMBOLS else ch for ch in s)
    s = s.strip()

    if s[:1] in {"+", "-"}:
        if s[0] == "-":
            neg = not neg
        s = s[1:].strip()
    if not s:
        return None, "skip"
    if not any(ch.isdigit() for ch in s):
        return None, "ambiguous"

    # Internal whitespace is acceptable ONLY as thousands grouping (fr locale);
    # anything else is two tokens / stray text -> ambiguous.
    if " " in s:
        if re.fullmatch(r"\d{1,3}( \d{3})+", s):
            s = s.replace(" ", "")
        else:
            return None, "ambiguous"

    if not re.fullmatch(r"[0-9.,]+", s):
        # Number-ish but carrying leftover text/punctuation ("Model X-100", "3-5").
        return None, "ambiguous"

    has_comma = "," in s
    has_dot = "." in s
    if has_comma and has_dot:
        # en only: every comma precedes the single decimal dot, and the integer
        # part is cleanly comma-grouped. A comma AFTER the dot is European -> reject.
        if s.rfind(",") > s.rfind("."):
            return None, "ambiguous"
        if s.count(".") > 1:
            return None, "ambiguous"
        int_part, frac = s.split(".", 1)
        if not re.fullmatch(r"\d{1,3}(,\d{3})+", int_part) or "," in frac:
            return None, "ambiguous"
        num = f"{int_part.replace(',', '')}.{frac}"
    elif has_comma:
        if re.fullmatch(r"\d{1,3}(,\d{3})+", s):
            num = s.replace(",", "")
        else:
            # A lone/malformed comma group is decimal-vs-grouping ambiguous.
            return None, "ambiguous"
    elif has_dot:
        if s.count(".") > 1 or re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
            # Multiple dots, or a dot-grouped integer (European) -> ambiguous.
            return None, "ambiguous"
        num = s
    else:
        num = s

    if not re.fullmatch(r"\d+(\.\d+)?", num):
        return None, "ambiguous"
    value: Any = float(num) if "." in num else int(num)
    if neg:
        value = -value
    return value, "parsed"


def _key_text(text: str) -> str:
    """Matching key for duplicate canonicalization.

    Always folds abbreviations regardless of the visible
    ``expand_abbreviations`` toggle."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", " and ")
    tokens = []
    for token in re.sub(r"[^\w\s]", " ", text.casefold()).split():
        tokens.append(_ABBREVIATIONS.get(token.rstrip("."), token))
    return " ".join(tokens)


def _canonical(items: list[_PreparedValue]) -> Any:
    displays = [item.display for item in items if item.display is not None]
    if not displays:
        return None
    counts = Counter(displays)
    # Prefer frequent, then richer expanded forms, then longer surface. Keeps
    # "New York Department of Health" over "New York Dept Health" deterministically.
    return sorted(
        counts,
        key=lambda display: (
            -counts[display],
            -int(any(i.display == display and i.expanded for i in items)),
            -len(str(display)),
            str(display),
        ),
    )[0]


def _clean_column_outputs(params: CleanColumnParams) -> dict[str, Any]:
    value_type = int | float | None if params.make_numeric else str | None
    return {"cleaned": Outcome[value_type]}


def clean_column(
    params: CleanColumnParams,
    rows: Rows,
) -> dict[int, RowResult[DynamicOutput]]:
    null_tokens = _parse_null_tokens(params.null_tokens)
    prepared = {
        row_id: _prepare_value(params.source.read(row), params, null_tokens)
        for row_id, row in rows.items()
    }
    groups: dict[str, list[_PreparedValue]] = {}
    if params.canonicalize_duplicates:
        for item in prepared.values():
            if item.key:
                groups.setdefault(item.key, []).append(item)
    canonical_by_key = {key: _canonical(items) for key, items in groups.items()}

    results: dict[int, RowResult[DynamicOutput]] = {}
    for row_id, item in prepared.items():
        if params.make_numeric and item.display is not None and item.kind != "numeric":
            outcome = Outcome.failed(
                "invalid_numeric_value",
                "The source value could not be parsed as a number.",
            )
        else:
            value = canonical_by_key.get(item.key or "", item.display)
            outcome = Outcome.ok(value)
        results[row_id] = RowResult(
            output=DynamicOutput({"cleaned": outcome}),
        )
    return results


CLEAN_COLUMN = action(
    examples=(CleanColumnParams(source="source"),),
    name="clean_column",
    title="Clean one text column",
    description="Normalize values in a text-like column while preserving the source.",
    category=ActionCategory.CLEANUP,
    run=map_batch(
        clean_column,
        dynamic_outputs=_clean_column_outputs,
    ),
)
