"""The typed regex action preserves the established capture-group behavior."""

from frisket.actions.extract import REGEX_EXTRACT, RegexExtractParams, regex_extract
from frisket.actions.types import Row


def _run(spec: dict, text: str):
    params = RegexExtractParams(
        input_columns=["body"],
        pattern=spec["pattern"],
        group=spec.get("group"),
        all_matches=spec.get("all_matches", False),
    )
    return regex_extract(params, Row({"body": text})).output.root


def _columns(spec: dict) -> list[str]:
    params = RegexExtractParams(
        input_columns=["body"],
        pattern=spec["pattern"],
        group=spec.get("group"),
        all_matches=spec.get("all_matches", False),
    )
    return [field.key for field in REGEX_EXTRACT.run.resolve_output_fields(params)]


def test_first_capture_group_is_extracted_by_default():
    out = _run(
        {"pattern": r"\$([\d,]+(?:\.\d{2})?)", "output_name": "amount"},
        "the fine was $1,200.50 total",
    )
    assert out == {"extracted": "1,200.50"}


def test_no_group_falls_back_to_whole_match():
    out = _run(
        {"pattern": r"\$[\d,]+", "output_name": "amount"},
        "the fine was $1,200 total",
    )
    assert out == {"extracted": "$1,200"}


def test_explicit_group_is_respected():
    out = _run(
        {"pattern": r"(\w+)@(\w+)", "output_name": "domain", "group": 2},
        "reach me at alice@example",
    )
    assert out == {"extracted": "example"}


def test_digit_string_group_from_generated_form_is_coerced_to_integer():
    params = RegexExtractParams(
        input_columns=["body"],
        pattern=r"(\w+)@(\w+)",
        group="2",
    )

    assert params.group == 2
    assert regex_extract(params, Row({"body": "alice@example"})).output.root == {
        "extracted": "example"
    }


def test_all_matches_uses_capture_group_too():
    out = _run(
        {"pattern": r"#(\w+)", "output_name": "tags", "all_matches": True},
        "tagged #alpha and #beta",
    )
    assert out == {"extracted": ["alpha", "beta"]}


def test_multiple_groups_expand_into_numbered_columns():
    spec = {"pattern": r"(\d{4})-(\d{2})-(\d{2})", "output_name": "date"}
    assert _columns(spec) == ["extracted_1", "extracted_2", "extracted_3"]
    out = _run(spec, "filed on 2026-07-05 sharp")
    assert out == {
        "extracted_1": "2026",
        "extracted_2": "07",
        "extracted_3": "05",
    }


def test_multiple_groups_no_match_yields_none_per_column():
    spec = {"pattern": r"(\d+)x(\d+)", "output_name": "dim"}
    assert _columns(spec) == ["extracted_1", "extracted_2"]
    assert _run(spec, "no dimensions here") == {
        "extracted_1": None,
        "extracted_2": None,
    }


def test_explicit_group_keeps_single_column_even_with_many_groups():
    spec = {"pattern": r"(\w+)@(\w+)", "output_name": "who", "group": 2}
    assert _columns(spec) == ["extracted"]
    assert _run(spec, "alice@example") == {"extracted": "example"}
