from __future__ import annotations

from frisket.server.services.import_inference import infer_column_type


# --- infer_column_type: only classify a column numeric when int()/float() would
# hand back the digits the user typed.  Everything below is a shape that
# int()/float() silently rewrites, and every one of them is an identifier
# a journalist imports: ZIP and FIPS codes, case numbers, E.164 phone
# numbers, account numbers longer than a JSON double can hold.

_IDENTIFIER_COLUMNS: list[tuple[str, list[str], str]] = [
    ("zip", ["01234", "02138"], "leading zero: 01234 is not 1234"),
    ("fips", ["06075", "01001"], "leading zero"),
    ("case_no", ["00042", "00043"], "leading zero"),
    ("phone", ["+12125551234", "+13475550000"], "E.164 '+' is not a sign"),
    ("plus_int", ["+5", "+12"], "int() drops the '+'"),
    ("unit", ["1e5", "2E3"], "exponent notation is not a spelling we keep"),
    ("account", ["123456789012345678", "123456789012345679"], "beyond 2**53"),
    ("literal", ["1_000", "2_000"], "Python literal syntax is not CSV"),
    ("missing", ["nan", "inf"], "float() invents NaN/Infinity"),
    ("trailing_dot", ["5.", "6."], "not a numeral we round-trip"),
]

_NUMERIC_COLUMNS: list[tuple[str, list[str], str]] = [
    ("score", ["1", "42"], "integer"),
    ("year", ["1999", "2026"], "integer"),
    ("count", ["0", "7"], "integer"),
    ("delta", ["-5", "-12"], "integer"),
    ("grouped", ["1,234", "5,678"], "integer"),
    ("price", ["1.50", "2.00"], "number"),
    ("ratio", ["0.5", "-0.25"], "number"),
    ("share", [".5", ".25"], "number"),
    ("padded", [" 12 ", " 13 "], "integer"),
]


def test_sniff_type_keeps_identifier_shaped_columns_as_text() -> None:
    observed = {
        name: infer_column_type(name, values) for name, values, _ in _IDENTIFIER_COLUMNS
    }
    assert observed == {name: "text" for name, _, _ in _IDENTIFIER_COLUMNS}, (
        "these shapes lose data when coerced: "
        + "; ".join(f"{name}: {why}" for name, _, why in _IDENTIFIER_COLUMNS)
    )


def test_sniff_type_still_recognizes_real_numbers() -> None:
    observed = {
        name: infer_column_type(name, values) for name, values, _ in _NUMERIC_COLUMNS
    }
    assert observed == {name: expected for name, _, expected in _NUMERIC_COLUMNS}


def test_sniff_type_honors_the_semicolon_decimal_convention() -> None:
    assert (
        infer_column_type("price", ["1.234,56", "9,50"], decimal_separator=",")
        == "number"
    )
    assert (
        infer_column_type("count", ["1.234", "9"], decimal_separator=",") == "integer"
    )
    assert infer_column_type("zip", ["01234", "02138"], decimal_separator=",") == "text"
