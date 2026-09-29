from frisket.engine.store.text_quote_match import quote_ranges


def test_quote_ranges_returns_literal_and_whitespace_equivalent_repeats():
    source = "Ada launched\t a rocket. Ada launched\r\n\u00a0a rocket."
    quote = "Ada launched a rocket."

    ranges = quote_ranges(source, quote)

    assert [source[start:end] for start, end in ranges] == [
        "Ada launched\t a rocket.",
        "Ada launched\r\n\u00a0a rocket.",
    ]


def test_quote_ranges_preserves_original_unicode_codepoint_offsets():
    source = "🚀 Préface — Ada Ada"

    assert quote_ranges(source, "Ada") == [(12, 15), (16, 19)]


def test_quote_ranges_does_not_apply_fuzzy_or_case_matching():
    assert quote_ranges("Ada-Lovelace ADA", "Ada Lovelace") == []
