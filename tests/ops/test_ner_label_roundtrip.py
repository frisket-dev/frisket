"""The label/filter round-trip guard.

Every label path in NER extraction fails the same way when it fails: it
silently returns nothing, which is indistinguishable from "this text contains
no entities". A user cannot tell a broken filter from clean data. This module
asserts that for every selectable type, a request either MATCHES the entity
the engine emitted or raises -- never quietly empties.

Uses a fake pipeline, so it runs without spaCy or any model download.
"""

from __future__ import annotations

import pytest

from frisket.ops.entities import canonicalize_entity_type
from frisket.ops.spacy_ner import (
    SPACY_ONTONOTES_TYPES,
    requested_spacy_types,
    run_spacy_ner,
)

# The full OntoNotes 5 tag set `en_core_web_sm` can emit. A user picking any
# of these must get results back.
ONTONOTES_TAGS = (
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


class _Ent:
    def __init__(self, text: str, label: str) -> None:
        self.text = text
        self.label_ = label
        self.start_char = 0
        self.end_char = len(text)


class _Doc:
    def __init__(self, ents: list[_Ent]) -> None:
        self.ents = ents


def _pipeline_emitting(tag: str):
    def nlp(text: str) -> _Doc:
        return _Doc([_Ent(text, tag)])

    return nlp


@pytest.mark.parametrize("tag", ONTONOTES_TAGS)
def test_requesting_a_raw_tag_matches_what_the_engine_emits(tag: str) -> None:
    """The C4a guard. `GPE` shipped broken precisely here: the emit side
    canonicalized it to `location` while the request side fell through to the
    lowercase fallback `gpe`, so every span was dropped."""
    out = run_spacy_ner(_pipeline_emitting(tag), ["Springfield"], labels=[tag])
    assert len(out[0]) == 1, f"requesting {tag} silently matched nothing"


@pytest.mark.parametrize("tag", ONTONOTES_TAGS)
def test_requesting_the_canonical_name_matches_what_the_engine_emits(
    tag: str,
) -> None:
    """The chips render canonical names, not raw tags, so the canonical
    spelling is what actually reaches `labels` from the form."""
    canonical = SPACY_ONTONOTES_TYPES.get(tag, tag.lower())
    out = run_spacy_ner(_pipeline_emitting(tag), ["Springfield"], labels=[canonical])
    assert len(out[0]) == 1, f"requesting {canonical} silently matched nothing"


@pytest.mark.parametrize("tag", ONTONOTES_TAGS)
def test_request_and_emit_sides_agree_on_the_canonical_spelling(tag: str) -> None:
    """The asymmetry itself, asserted directly: whatever the emit side
    produces for a tag must be what the request side canonicalizes it to."""
    emitted = SPACY_ONTONOTES_TYPES.get(tag, tag.lower())
    requested = requested_spacy_types([tag])
    assert requested == {emitted}
    assert canonicalize_entity_type(tag) == emitted


def test_gpe_and_loc_collapse_to_one_concept() -> None:
    """Both mean `location`, which is why the form offers one chip, not two."""
    assert requested_spacy_types(["GPE"]) == requested_spacy_types(["LOC"])
    assert requested_spacy_types(["GPE"]) == {"location"}


def test_a_filter_never_drops_every_span_for_a_type_it_asked_for() -> None:
    """Whole-set sweep: request all 18 at once and get all 18 back."""

    def nlp(text: str) -> _Doc:
        return _Doc([_Ent(f"e{i}", tag) for i, tag in enumerate(ONTONOTES_TAGS)])

    labels = [SPACY_ONTONOTES_TYPES.get(t, t.lower()) for t in ONTONOTES_TAGS]
    out = run_spacy_ner(nlp, ["text"], labels=labels)
    assert len(out[0]) == len(ONTONOTES_TAGS)


def test_unknown_label_is_the_one_honest_empty() -> None:
    """A type the engine cannot emit yields nothing -- correct, and the reason
    the form offers a fixed list instead of a text box. `address` is the
    canonical trap: it is in the alias table, so it looks valid, but spaCy has
    no address type and never will."""
    out = run_spacy_ner(_pipeline_emitting("PERSON"), ["Ada"], labels=["address"])
    assert out[0] == []
    assert requested_spacy_types(["address"]) == {"address"}
    assert "address" not in set(SPACY_ONTONOTES_TYPES.values())
