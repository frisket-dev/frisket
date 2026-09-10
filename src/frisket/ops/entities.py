"""Unified `map.ner` entity output contract.

Every engine (spacy/gliner/llm) emits its own native shape internally
(GLiNER: `{text,label,start,end,score}`; spaCy: OntoNotes tags; an LLM:
whatever field names the entities-preset schema declares). This module is the
ONE place that canonicalizes any of those into the unified row-cell contract:

    {text, type, start, end, score, fingerprint?, metadata?}

`type` replaces the old `label` key: one concept, one name. `_LABEL_ALIASES`
below is the single table making spaCy's `GPE`, GLiNER's `location`, and an
LLM's `place` land on the same canonical string; anything it does not know
falls back to a lowercased, underscored raw label (DATE, MONEY, ...), so
canonicalization is total -- every entity gets a `type`.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any

# Engine-native label -> canonical entity type. `gpe` is load-bearing: spaCy
# emits GPE for cities/countries/states and `run_spacy_ner` compares the
# REQUESTED types against the EMITTED canonical type, so without this entry a
# user asking for GPE matches nothing and the run looks like clean empty data.
_LABEL_ALIASES = {
    "people": "person",
    "person": "person",
    "persons": "person",
    "org": "organization",
    "organisation": "organization",
    "organization": "organization",
    "organizations": "organization",
    "place": "location",
    "gpe": "location",
    "loc": "location",
    "location": "location",
    "locations": "location",
    "address": "address",
    "addresses": "address",
    "street address": "address",
}


def _normalize_label(label: str) -> str:
    return re.sub(r"[\s_-]+", " ", str(label).strip().lower())


# What an extraction engine or LLM is asked to produce. `fingerprint` is
# deliberately absent: it is server-derived, and asking a model for a
# comparison key invites it to invent one.
ENTITY_MODEL_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "type": {"type": "string"},
        "start": {"type": "integer"},
        "end": {"type": "integer"},
        "score": {"type": ["number", "null"]},
    },
}

# What we STORE. Shared by output_fields() across all three engines so
# `map.ner`'s output column schema never drifts from what
# canonicalize_entities() actually produces. `score` is nullable: spaCy has no
# per-span confidence and reports it honestly as null rather than a fabricated
# number.
# deepcopy, not a spread of the same value dicts: a future in-place schema
# walker adding e.g. additionalProperties to the stored schema would
# otherwise reach through the shared property objects and re-merge exactly
# what this split separates.
ENTITY_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **copy.deepcopy(ENTITY_MODEL_ITEM_SCHEMA["properties"]),
        "fingerprint": {"type": "string"},
        "metadata": {"type": "object"},
    },
}

ENTITY_OUTPUT_ARRAY_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": ENTITY_ITEM_SCHEMA,
}

# Types whose surfaces are numeric or temporal. Grouping "March 2022" with
# "2022 March" would be wrong, and token-sorting a money amount is
# meaningless, so these carry no fingerprint and the Mentions panel groups
# them by exact text instead.
UNFINGERPRINTED_ENTITY_TYPES = frozenset(
    {"date", "time", "percent", "quantity", "ordinal", "cardinal", "money"}
)

# NFKD decomposes the typographic ligatures (fi, ffl, ...) and casefold turns
# the sharp s into "ss"; these are the letter-ligatures Unicode does not take
# apart on its own.
_LIGATURES = {"æ": "ae", "œ": "oe"}

# A closed set, applied to `organization` surfaces only. Deliberately tiny: it
# exists so "ACME Corp." and "Acme Corporation" answer the same question, not
# to normalize company names in general. "Acme Inc" and "Acme LLC" are
# different companies and must stay apart.
_ORGANIZATION_PHRASE_ALIASES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("limited", "liability", "company"), "llc"),
)
_ORGANIZATION_TOKEN_ALIASES = {
    "corporation": "corp",
    "incorporated": "inc",
    "company": "co",
}


def canonicalize_entity_type(raw_label: str) -> str:
    """One raw engine label -> one canonical `type`.

    Anything `_LABEL_ALIASES` does not know (spaCy's DATE/MONEY/... OntoNotes
    tags, or an LLM-invented type) is lowercased and space-normalized rather
    than dropped -- canonicalization never discards a type the engine
    actually reported.
    """
    cleaned = (raw_label or "").strip()
    if not cleaned:
        return "entity"
    canonical = _LABEL_ALIASES.get(_normalize_label(cleaned))
    if canonical:
        return canonical
    return "_".join(cleaned.lower().split())


def _apply_organization_aliases(tokens: list[str]) -> list[str]:
    """Phrase aliases run first: "limited liability company" must become `llc`
    rather than being eaten token-wise into "... co"."""
    out: list[str] = []
    index = 0
    while index < len(tokens):
        matched = False
        for phrase, replacement in _ORGANIZATION_PHRASE_ALIASES:
            if tuple(tokens[index : index + len(phrase)]) == phrase:
                out.append(replacement)
                index += len(phrase)
                matched = True
                break
        if matched:
            continue
        token = tokens[index]
        out.append(_ORGANIZATION_TOKEN_ALIASES.get(token, token))
        index += 1
    return out


def entity_fingerprint(entity_type: str, text: str) -> str | None:
    """A deliberately boring equality key over one entity surface.

    Combines only mechanical spelling variation -- case, diacritics,
    punctuation, whitespace, token order, duplicate tokens, `&`/`and`,
    ligatures, and the small organization-suffix set above. It is NOT identity
    resolution and NOT fuzzy matching: `Acme` and OCR'd `Acrne` stay apart,
    and two different people named John Smith necessarily share one key,
    because this groups surfaces rather than people.

    Token order is normalized away only for the tokens that reach the sort.
    The organization phrase/suffix aliases run BEFORE sorting and match
    tokens in ORDER, so a reordered surface can miss an alias its ordered
    spelling would have hit: `"Limited Liability Company"` keys as `llc`
    while `"Company Liability Limited"` keys as `co liability limited`. That
    is the intended trade -- the aliases are phrase rewrites, and matching
    them order-independently would fold unrelated names together.

    `entity_type` is a GATE, not just an alias selector: it returns None for
    EVERY type in `UNFINGERPRINTED_ENTITY_TYPES` (numeric/temporal surfaces,
    which the Mentions panel groups by exact text instead), so
    `entity_fingerprint("date", "March 2022")` is None, not `"2022 march"`.
    It also returns None when there is nothing to key on, so an empty bucket
    can never exist either way. Tokens are sorted, never characters --
    character/anagram keys over-merge unrelated names.

    This algorithm is immutable and unversioned; it is part of the stored
    entity-column contract. Changing it is a breaking format change that
    requires rebuilding entity columns, never a mixed-algorithm read path.
    """
    if entity_type in UNFINGERPRINTED_ENTITY_TYPES:
        return None
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = stripped.casefold()
    for ligature, expansion in _LIGATURES.items():
        folded = folded.replace(ligature, expansion)
    # `&` only becomes a word when there is a word for it to join. Otherwise
    # "&" and "@&#" would each key as `and` and share a bucket with the
    # literal word -- a punctuation-only surface must never produce a group.
    if any(ch.isalnum() for ch in folded):
        folded = folded.replace("&", " and ")
    cleaned = "".join(ch if ch.isalnum() else " " for ch in folded)
    tokens = cleaned.split()
    if entity_type == "organization":
        tokens = _apply_organization_aliases(tokens)
    if not tokens:
        return None
    return " ".join(sorted(set(tokens)))


def canonicalize_entity(entity: dict[str, Any]) -> dict[str, Any] | None:
    """One raw entity dict (`label` or `type` key, either accepted) -> the
    unified stored shape. Returns None for an entry missing the load-bearing
    fields (text/start/end) rather than emitting a half-populated span."""
    text = entity.get("text")
    start = entity.get("start")
    end = entity.get("end")
    if (
        not isinstance(text, str)
        or isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
    ):
        return None
    raw_type = (
        entity.get("type") if entity.get("type") is not None else entity.get("label")
    )
    canonical_type = canonicalize_entity_type(str(raw_type or ""))
    out: dict[str, Any] = {
        "text": text,
        "type": canonical_type,
        "start": start,
        "end": end,
        "score": entity.get("score")
        if isinstance(entity.get("score"), (int, float))
        and not isinstance(entity.get("score"), bool)
        else None,
    }
    # Type first, then the key: the organization suffix aliases and the
    # unfingerprinted numeric-temporal set are both keyed off the CANONICAL
    # type, so fingerprinting a raw engine label would silently mis-group.
    fingerprint = entity_fingerprint(canonical_type, text)
    if fingerprint:
        out["fingerprint"] = fingerprint
    if raw_type is not None and out["type"] != str(raw_type).strip().lower():
        out["metadata"] = {"raw_label": str(raw_type)}
    else:
        existing_metadata = entity.get("metadata")
        if (
            isinstance(existing_metadata, dict)
            and existing_metadata.get("raw_label") is not None
        ):
            out["metadata"] = {"raw_label": str(existing_metadata["raw_label"])}
    return out


def canonicalize_entities(
    entities: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Canonicalize a whole per-row entity list, in order, dropping malformed
    entries rather than raising -- one bad item from an engine should not fail
    the whole row."""
    if not entities:
        return []
    out: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        canonical = canonicalize_entity(entity)
        if canonical is not None:
            out.append(canonical)
    return out
