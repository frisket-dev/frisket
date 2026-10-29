"""Pure spaCy entity schema and extraction, without host application imports."""

from typing import Any, Callable

# OntoNotes 5 NER tag set (the label set `en_core_web_sm`/`en_core_web_trf`
# ship) -> a lowercase canonical type. PERSON/ORG/GPE/LOC route through the
# same words the shared alias table in `ops/entities.py` already knows
# (person/organization/location); the rest are OntoNotes-only concepts that
# alias table has no opinion on, so they pass through as their own lowercase
# type via `ops.entities.canonicalize_entity_type`'s fallback -- this map
# exists only to give GPE/LOC/PERSON/ORG the SAME canonical spelling
# gliner/llm produce, not to relabel everything.
SPACY_ONTONOTES_TYPES: dict[str, str] = {
    "PERSON": "person",
    "ORG": "organization",
    "GPE": "location",
    "LOC": "location",
}

# Every raw tag an `en_core_web_*` pipeline can emit. spaCy's schema is CLOSED:
# it is a trained tagger, not a zero-shot extractor, so a requested type
# outside this set can never match and the run would write `[]` into every row
# -- indistinguishable from clean data. This is the list `map.ner` validates
# `labels` against when engine="spacy".
SPACY_ONTONOTES_TAGS: tuple[str, ...] = (
    "PERSON",
    "NORP",
    "FAC",
    "ORG",
    "GPE",
    "LOC",
    "PRODUCT",
    "EVENT",
    "WORK_OF_ART",
    "LAW",
    "LANGUAGE",
    "DATE",
    "TIME",
    "PERCENT",
    "MONEY",
    "QUANTITY",
    "ORDINAL",
    "CARDINAL",
)

# The canonical types spaCy can actually produce, derived by the SAME
# tag -> canonical transform `run_spacy_ner` applies to a live span, so a
# validator built on this can never accept a label the extractor would drop.
SPACY_CANONICAL_TYPES: frozenset[str] = frozenset(
    SPACY_ONTONOTES_TYPES.get(tag, tag.lower()) for tag in SPACY_ONTONOTES_TAGS
)


def extract_entities(
    nlp: Callable[[str], Any], texts: list[str], wanted: set[str] | None = None
) -> list[list[dict[str, Any]]]:
    """Pure, injectable-pipeline core (HARD RULE: tests use a tiny fake/mocked
    pipeline for this logic, never a real download). `nlp` is anything
    callable as `nlp(text) -> doc` where `doc.ents` yields objects with
    `.text`/`.label_`/`.start_char`/`.end_char` (a real spaCy `Language`, or a
    fake with that shape). spaCy assigns no per-span confidence, so `score`
    is honestly `None` rather than a fabricated number."""
    results: list[list[dict[str, Any]]] = []
    for text in texts:
        entities: list[dict[str, Any]] = []
        if text and text.strip():
            doc = nlp(text)
            for ent in doc.ents:
                raw_label = str(ent.label_)
                canonical = SPACY_ONTONOTES_TYPES.get(raw_label, raw_label.lower())
                if wanted is not None and canonical not in wanted:
                    continue
                entities.append(
                    {
                        "text": str(ent.text),
                        "type": canonical,
                        "start": int(ent.start_char),
                        "end": int(ent.end_char),
                        "score": None,
                    }
                )
        results.append(entities)
    return results
