"""clean_column per-transform goldens and ordering-interaction tests. Each row
exercises one transform in isolation (all others at a neutral
setting) plus the load-bearing ordering collisions.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from frisket.actions.cleanup import CleanColumnParams, _make_numeric, clean_column
from frisket.actions.types import Outcome, Row, Rows


SOURCE = "col"


def _clean(value, **params):
    outcome = _outcomes([value], **params)[0]
    assert outcome.status == "ok"
    return outcome.value


def _clean_all(values, **params):
    outcomes = _outcomes(values, **params)
    assert all(outcome.status == "ok" for outcome in outcomes)
    return [outcome.value for outcome in outcomes]


def _outcomes(values, **params):
    params.pop("profile_version", None)
    definition = CleanColumnParams(source=SOURCE, **params)
    rows = Rows({i: Row({SOURCE: value}) for i, value in enumerate(values)})
    results = clean_column(definition, rows)
    return [results[i].output.root["cleaned"] for i in range(len(values))]


# Neutral "new-style, everything off, keep case" baseline the single-transform
# rows switch ONE flag on against.
OFF = dict(
    case="keep",
    blank_null_tokens=False,
    lowercase_emails=False,
    normalize_us_phone=False,
    expand_abbreviations=False,
    reorder_person_name=False,
    canonicalize_duplicates=False,
    strip_edge_punct=False,
    normalize_unicode_punct=False,
    remove_thousands_separators=False,
    remove_all_commas=False,
    make_numeric=False,
)


def _with(**overrides):
    return {**OFF, **overrides}


# --- always-on floor -------------------------------------------------------


def test_floor_trims_whitespace_but_not_edge_punct_by_default():
    # NFKC + control + whitespace collapse + whitespace-trim only; edge punct is
    # NOT stripped unless strip_edge_punct is on.
    assert _clean("  NEW   YORK  ", **OFF) == "NEW YORK"
    assert _clean('"quoted."', **OFF) == '"quoted."'


def test_floor_nfkc_folds_compatibility_forms():
    assert _clean("ﬁ", **OFF) == "fi"


# --- null-token blanking ---------------------------------------------------


def test_blank_null_tokens_on_blanks():
    assert _clean("N/A", **_with(blank_null_tokens=True)) is None


def test_blank_null_tokens_off_keeps_value():
    assert _clean("N/A", **_with(blank_null_tokens=False)) == "N/A"


def test_null_detection_uses_edge_punct_stripped_copy():
    # detection-vs-display split: "N/A." blanks even with strip_edge_punct OFF.
    assert _clean("N/A.", **_with(blank_null_tokens=True)) is None


# --- email -----------------------------------------------------------------


def test_lowercase_emails_on():
    assert _clean("REPORTER@EXAMPLE.COM", **_with(lowercase_emails=True)) == (
        "reporter@example.com"
    )


def test_email_detection_uses_edge_punct_stripped_copy():
    assert _clean("a@B.Com.", **_with(lowercase_emails=True)) == "a@b.com"


def test_lowercase_emails_off_falls_to_text_and_cases():
    assert (
        _clean(
            "REPORTER@EXAMPLE.COM", **_with(lowercase_emails=False, case="smart_title")
        )
        == "Reporter@example.com"
    )


# --- US phone --------------------------------------------------------------


def test_normalize_us_phone_on():
    assert _clean("(202) 555-0173", **_with(normalize_us_phone=True)) == "+12025550173"


def test_normalize_us_phone_drops_leading_country_code():
    assert _clean("1-202-555-0173", **_with(normalize_us_phone=True)) == "+12025550173"


# --- make_numeric ----------------------------------------------------------


def test_make_numeric_currency_and_separators():
    assert _clean("$1,299.00", **_with(make_numeric=True)) == 1299.0


def test_make_numeric_parentheses_negative():
    assert _clean("(1,234.50)", **_with(make_numeric=True)) == -1234.5


def test_make_numeric_plain_integer():
    assert _clean("12,345", **_with(make_numeric=True)) == 12345


def test_make_numeric_ambiguous_is_a_typed_row_failure():
    outcome = _outcomes(["1,23,456"], **_with(make_numeric=True))[0]
    assert outcome == Outcome.failed(
        "invalid_numeric_value",
        "The source value could not be parsed as a number.",
    )


def test_make_numeric_embedded_text_is_ambiguous():
    assert _outcomes(["Model X-100"], **_with(make_numeric=True))[0].status == "failed"


@pytest.mark.parametrize("value", ["1.234,56", "1,23.45", "USD 1 CAD 2", "1,23,456"])
def test_make_numeric_rejects_ambiguous_locale_forms(value):
    assert _outcomes([value], **_with(make_numeric=True))[0].status == "failed"


def test_make_numeric_ambiguous_short_circuits_later_transforms():
    outcome = _outcomes(
        ["1,23,456"], **_with(make_numeric=True, remove_all_commas=True)
    )[0]
    assert outcome.status == "failed"


def test_make_numeric_non_numeric_is_a_typed_row_failure():
    assert _outcomes(["hello world"], **_with(make_numeric=True))[0].status == "failed"


def test_make_numeric_unit_helper():
    assert _make_numeric("$1,299.00") == (1299.0, "parsed")
    assert _make_numeric("(1,234.50)") == (-1234.5, "parsed")
    assert _make_numeric("1 234 567") == (1234567, "parsed")
    assert _make_numeric("1,23,456") == (None, "ambiguous")
    assert _make_numeric("1.234,56") == (None, "ambiguous")
    assert _make_numeric("1,23.45") == (None, "ambiguous")
    assert _make_numeric("USD 1 CAD 2") == (None, "ambiguous")
    assert _make_numeric("Model X-100") == (None, "ambiguous")
    assert _make_numeric("hello") == (None, "skip")
    assert _make_numeric("42") == (42, "parsed")


# --- thousands separators (numeric-only) -----------------------------------


def test_remove_thousands_separators_numeric_only():
    assert _clean("12,345", **_with(remove_thousands_separators=True)) == "12345"


def test_remove_thousands_does_not_eat_prose_comma():
    assert (
        _clean("Smith, Jon", **_with(remove_thousands_separators=True)) == "Smith, Jon"
    )


# --- unicode punctuation ---------------------------------------------------


def test_normalize_unicode_punct():
    assert (
        _clean("“quoted” — dash…", **_with(normalize_unicode_punct=True))
        == '"quoted" - dash...'
    )


# --- strip edge punctuation ------------------------------------------------


def test_strip_edge_punct_on():
    assert _clean('"quoted."', **_with(strip_edge_punct=True)) == "quoted"


# --- Last, First reorder ---------------------------------------------------


def test_reorder_person_name():
    assert _clean("Smith, Jon", **_with(reorder_person_name=True)) == "Jon Smith"


def test_reorder_before_remove_thousands_no_collision():
    # The headline ordering hazard: numeric-only thousands removal must NOT eat
    # the "Last, First" comma.
    assert (
        _clean(
            "Smith, Jon",
            **_with(reorder_person_name=True, remove_thousands_separators=True),
        )
        == "Jon Smith"
    )


def test_remove_all_commas_runs_after_reorder():
    # remove_all_commas is ordered AFTER reorder, so the name still reorders and
    # then any remaining commas go.
    assert (
        _clean("Smith, Jon", **_with(reorder_person_name=True, remove_all_commas=True))
        == "Jon Smith"
    )
    assert _clean("a, b, c", **_with(remove_all_commas=True)) == "a b c"


# --- abbreviations ---------------------------------------------------------


def test_expand_abbreviations_under_keep_case():
    assert (
        _clean(
            "NEW YORK dept. of health", **_with(expand_abbreviations=True, case="keep")
        )
        == "NEW YORK department of health"
    )


def test_expand_abbreviations_ampersand():
    assert _clean("Black & White", **_with(expand_abbreviations=True)) == (
        "Black and White"
    )


# --- case dropdown ---------------------------------------------------------


def test_case_title_forces_every_word():
    assert _clean("acme corp", **_with(case="title")) == "Acme Corp"


def test_case_upper_lower():
    assert _clean("acme corp", **_with(case="upper")) == "ACME CORP"
    assert _clean("ACME CORP", **_with(case="lower")) == "acme corp"


def test_case_smart_title_preserves_mixed_case():
    assert _clean("McDonald", **_with(case="smart_title")) == "McDonald"


def test_case_title_lowercases_mixed_case_guarding_the_distinction():
    assert _clean("McDonald", **_with(case="title")) == "Mcdonald"


# --- duplicate canonicalization --------------------------------------------


def test_canonicalize_duplicates_on_merges_variants():
    out = _clean_all(
        ["New York Dept of Health", "New York Department of Health"],
        **_with(
            canonicalize_duplicates=True, expand_abbreviations=True, case="smart_title"
        ),
    )
    assert out == ["New York Department of Health", "New York Department of Health"]


def test_canonicalize_duplicates_off_keeps_each_display():
    # With canonicalize OFF each row keeps its own expanded display (both expand
    # to the same text here, but no cross-row merge step runs).
    out = _clean_all(
        ["New York Dept of Health", "New York Department of Health"],
        **_with(
            canonicalize_duplicates=False, expand_abbreviations=True, case="smart_title"
        ),
    )
    assert out == ["New York Department of Health", "New York Department of Health"]
    # off vs on differ when the surfaces stay distinct:
    out2 = _clean_all(
        ["acme", "acme", "ACME"],
        **_with(canonicalize_duplicates=False, case="keep"),
    )
    assert out2 == ["acme", "acme", "ACME"]
    out3 = _clean_all(
        ["acme", "acme", "ACME"],
        **_with(canonicalize_duplicates=True, case="keep"),
    )
    assert out3 == ["acme", "acme", "acme"]


# --- default vector (reproduces today's smart_title) -----------------------


def test_default_vector_reproduces_smart_title():
    assert _clean("  NEW YORK dept. of health ") == "New York Department of Health"


def test_explicit_default_case_resolves_default_vector():
    assert _clean("  NEW YORK dept. of health ", case="smart_title") == (
        "New York Department of Health"
    )


def test_transform_bools_apply_directly():
    assert _clean("$50", case="smart_title", make_numeric=True) == 50


@pytest.mark.parametrize("case", ["keep", "title", "upper", "lower", "smart_title"])
def test_new_style_case_values_accepted(case):
    # Smoke: every rendered case value runs without error.
    assert _clean("acme corp", **_with(case=case)) is not None


def test_params_reject_deleted_profile_and_unknown_case():
    with pytest.raises(ValidationError):
        CleanColumnParams(source="amount", profile_version=2)
    with pytest.raises(ValidationError):
        CleanColumnParams(source="amount", case="preserve")
