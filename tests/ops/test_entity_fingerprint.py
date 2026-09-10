"""Contract cases for the stored entity-surface fingerprint.

The fingerprint is an immutable, unversioned part of the `entity_mentions`
column contract: changing what these cases assert is a breaking format change
that requires rebuilding every entity column, not a test update.
"""

from __future__ import annotations

import pytest

from frisket.ops.entities import (
    UNFINGERPRINTED_ENTITY_TYPES,
    canonicalize_entity,
    entity_fingerprint,
)


@pytest.mark.parametrize(
    ("entity_type", "left", "right"),
    [
        ("organization", "ACME Corp.", "Acme Corporation"),
        ("organization", "Acme Limited Liability Company", "Acme LLC"),
        ("organization", "A&B Co", "A and B Company"),
        ("person", "Smith, Jon", "Jon Smith"),
        ("person", "José Núñez", "Jose Nunez"),
        ("person", "  Jon   Smith  ", "jon smith"),
        ("person", "Smith Smith", "Smith"),
        ("location", "New York, NY", "ny new york"),
    ],
)
def test_deterministic_surface_variants_combine(
    entity_type: str, left: str, right: str
) -> None:
    assert entity_fingerprint(entity_type, left) == entity_fingerprint(
        entity_type, right
    )
    assert entity_fingerprint(entity_type, left) is not None


@pytest.mark.parametrize(
    ("entity_type", "left", "right"),
    [
        # Different companies, not spelling variants.
        ("organization", "Acme Inc", "Acme LLC"),
        # OCR damage is NOT corrected: v1 promises no fuzzy matching.
        ("organization", "Acme", "Acrne"),
        ("person", "Bob Smith", "Robert Smith"),
        # Token sorting must not become character/anagram sorting.
        ("person", "Silent", "Listen"),
        # The organization suffix table applies to organizations only.
        ("person", "Acme Corporation", "Acme Corp"),
    ],
)
def test_distinct_surfaces_stay_apart(entity_type: str, left: str, right: str) -> None:
    assert entity_fingerprint(entity_type, left) != entity_fingerprint(
        entity_type, right
    )


@pytest.mark.parametrize("entity_type", sorted(UNFINGERPRINTED_ENTITY_TYPES))
def test_numeric_temporal_types_have_no_fingerprint(entity_type: str) -> None:
    """These group by exact text instead; token-sorting a date or an amount
    would merge "March 2022" with "2022 March"."""
    assert entity_fingerprint(entity_type, "March 2022") is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "...",
        "!!! ---",
        "​",
        "\t\n",
        "()[]{}",
        # `&` is the trap: it is the one punctuation character that rewrites
        # to a word, so an all-punctuation surface containing it could key as
        # `and` and share a bucket with the literal word.
        "&",
        "&&&",
        "@&#",
        "-&-",
        " & ",
    ],
)
def test_empty_or_punctuation_only_never_creates_a_bucket(text: str) -> None:
    assert entity_fingerprint("organization", text) is None
    assert entity_fingerprint("person", text) is None


def test_ampersand_still_joins_real_words() -> None:
    """The punctuation-only guard must not disarm the `&`/`and` merge."""
    assert entity_fingerprint("organization", "A&B") == entity_fingerprint(
        "organization", "A and B"
    )


def test_dotted_acronyms_do_not_combine_with_undotted_ones() -> None:
    """A specified consequence, pinned so it cannot drift silently: punctuation
    becomes whitespace, so `D.C.` tokenizes to `d` + `c` and does not meet
    `DC`. Collapsing them would mean guessing at acronyms, which is the class
    of fuzzy rule this key deliberately excludes."""
    assert entity_fingerprint("location", "Washington, D.C.") == "c d washington"
    assert entity_fingerprint("location", "Washington DC") == "dc washington"


def test_fingerprint_is_stable_and_order_independent() -> None:
    first = entity_fingerprint("organization", "ACME Corp.")
    assert first == entity_fingerprint("organization", "ACME Corp.")
    assert first == "acme corp"
    # Tokens are sorted, so surface word order cannot change the key.
    assert entity_fingerprint("person", "Ada Byron Lovelace") == entity_fingerprint(
        "person", "Lovelace Ada Byron"
    )


def test_canonicalize_entity_stores_the_key_for_mergeable_types() -> None:
    entity = canonicalize_entity(
        {"text": "ACME Corp.", "label": "ORG", "start": 0, "end": 10}
    )
    assert entity is not None
    assert entity["type"] == "organization"
    assert entity["fingerprint"] == "acme corp"


def test_canonicalize_entity_omits_the_key_rather_than_storing_empty() -> None:
    for raw_label, text in [("DATE", "March 2022"), ("ORG", "...")]:
        entity = canonicalize_entity(
            {"text": text, "label": raw_label, "start": 0, "end": len(text)}
        )
        assert entity is not None
        assert "fingerprint" not in entity


def test_fingerprint_is_derived_from_the_canonical_type_not_the_raw_label() -> None:
    """`corporation -> corp` is keyed off the CANONICAL type, so an engine
    emitting a raw `ORG` label must still get the organization aliases."""
    raw = canonicalize_entity(
        {"text": "Acme Corporation", "label": "ORG", "start": 0, "end": 16}
    )
    canonical = canonicalize_entity(
        {"text": "Acme Corp", "type": "organization", "start": 0, "end": 9}
    )
    assert raw is not None and canonical is not None
    assert raw["fingerprint"] == canonical["fingerprint"]


def test_fingerprint_presence_is_a_function_of_the_canonical_type_only() -> None:
    """Preview/filter parity depends on this: one column can never hold the
    same (type, text) both WITH and WITHOUT a fingerprint.

    The Mentions preview groups a fingerprinted item under its fingerprint
    key and an unfingerprinted one under its exact text. If both spellings of
    one item could coexist in a column, the text group's own {type, text}
    selector would match BOTH items and the group's row_count would stop
    equaling the row count of the filter it emits. `canonicalize_entity` is a
    pure function of (type, text) and fingerprint presence follows only from
    the canonical type, which is what makes that state unreachable. Breaking
    this determinism breaks preview/filter parity.
    """
    cases = [
        ("organization", "Acme Corp"),
        ("person", "Ada Lovelace"),
        ("date", "March 2022"),
        ("money", "$4,000"),
        ("location", "New York"),
        ("organization", "..."),
    ]
    for entity_type, text in cases:
        entities = [
            canonicalize_entity(
                {
                    "text": text,
                    "type": entity_type,
                    "start": start,
                    "end": start + len(text),
                    "score": score,
                }
            )
            for start, score in ((0, None), (17, 0.9), (204, 0.1))
        ]
        assert all(entity is not None for entity in entities)
        presence = {"fingerprint" in entity for entity in entities}
        assert len(presence) == 1, (entity_type, text)
        keys = {entity.get("fingerprint") for entity in entities}
        assert len(keys) == 1, (entity_type, text)
        # ... and presence is decided by the canonical type alone.
        assert ("fingerprint" in entities[0]) is (
            entity_type not in UNFINGERPRINTED_ENTITY_TYPES
            and entity_fingerprint(entity_type, text) is not None
        )
