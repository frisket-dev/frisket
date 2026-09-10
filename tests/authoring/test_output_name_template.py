"""Unit coverage for ``frisket.output_names``.

Pins: token formatting incl. zero-padding, stem extraction from filenames
w/ dots/unicode, the sanitization table, collision determinism, and
template-validation errors (unknown token = loud error, never an empty
string).
"""

from __future__ import annotations

import pytest

from frisket.authoring.output_names import (
    MAX_OUTPUT_NAME_COMPONENT_LENGTH,
    OutputNameTemplateError,
    dedupe_output_name,
    format_output_name,
    sanitize_output_name,
    source_stem,
)


# --- token formatting -------------------------------------------------


def test_zero_padded_integer_tokens() -> None:
    assert (
        format_output_name("{row:03d}_{table:03d}.csv", {"row": 1, "table": 12})
        == "001_012.csv"
    )


def test_wide_row_number_is_not_truncated() -> None:
    assert format_output_name("{row:03d}.csv", {"row": 1234}) == "1234.csv"


def test_string_tokens_pass_through() -> None:
    assert (
        format_output_name(
            "{source_stem}_{column}.csv",
            {"source_stem": "alpha", "column": "vendor"},
        )
        == "alpha_vendor.csv"
    )


def test_date_token_is_a_plain_string_passthrough() -> None:
    assert format_output_name("{date}.csv", {"date": "2026-07-07"}) == "2026-07-07.csv"


def test_repeated_token_reused_is_fine() -> None:
    assert format_output_name("{row}-{row}.csv", {"row": 3}) == "3-3.csv"


# --- unknown / malformed tokens: loud errors, never empty strings ------


def test_unknown_token_raises_loudly() -> None:
    with pytest.raises(OutputNameTemplateError) as excinfo:
        format_output_name("{nope}.csv", {"row": 1})
    assert "nope" in str(excinfo.value)
    assert excinfo.value.token == "nope"


def test_attribute_access_token_rejected() -> None:
    with pytest.raises(OutputNameTemplateError):
        format_output_name("{row.__class__}.csv", {"row": 1})


def test_index_access_token_rejected() -> None:
    with pytest.raises(OutputNameTemplateError):
        format_output_name("{row[0]}.csv", {"row": [1, 2]})


def test_positional_field_rejected() -> None:
    with pytest.raises(OutputNameTemplateError):
        format_output_name("{}.csv", {"row": 1})


def test_empty_template_rejected() -> None:
    with pytest.raises(OutputNameTemplateError):
        format_output_name("", {"row": 1})
    with pytest.raises(OutputNameTemplateError):
        format_output_name("   ", {"row": 1})


def test_non_string_template_rejected() -> None:
    with pytest.raises(OutputNameTemplateError):
        format_output_name(None, {"row": 1})  # type: ignore[arg-type]


# --- stem extraction from filenames w/ dots/unicode --------------------


def test_source_stem_simple() -> None:
    assert source_stem("report.csv") == "report"


def test_source_stem_multi_dot_keeps_all_but_last_extension() -> None:
    assert source_stem("report.final.csv") == "report.final"


def test_source_stem_no_extension() -> None:
    assert source_stem("README") == "README"


def test_source_stem_dotfile_with_no_other_dot_is_unchanged() -> None:
    assert source_stem(".env") == ".env"


def test_source_stem_leading_dot_with_extension() -> None:
    assert source_stem(".hidden.tar") == ".hidden"


def test_source_stem_strips_directory_components() -> None:
    assert source_stem("some/dir/report.csv") == "report"
    assert source_stem("some\\windows\\dir\\report.csv") == "report"


def test_source_stem_unicode_filename() -> None:
    assert source_stem("café-résumé.pdf") == "café-résumé"
    assert source_stem("日本語ファイル.pdf") == "日本語ファイル"


def test_source_stem_requires_non_empty_filename() -> None:
    with pytest.raises(OutputNameTemplateError):
        source_stem("")


def test_source_stem_rejects_bare_path_separator() -> None:
    with pytest.raises(OutputNameTemplateError):
        source_stem("some/dir/")


# --- sanitization table --------------------------------------------------


def test_sanitize_replaces_path_separators_with_underscore_join() -> None:
    # Tokens that smuggle path separators collapse into one flat component
    # rather than escaping into a directory structure.
    assert sanitize_output_name("a/b/c.csv") == "a_b_c.csv"
    assert sanitize_output_name("a\\b\\c.csv") == "a_b_c.csv"


def test_sanitize_strips_dot_dot_and_dot_segments() -> None:
    assert sanitize_output_name("../../etc/passwd") == "etc_passwd"


def test_sanitize_replaces_reserved_characters() -> None:
    assert sanitize_output_name('a:b*c?d"e<f>g|h.csv') == "a_b_c_d_e_f_g_h.csv"


def test_sanitize_strips_control_characters() -> None:
    assert sanitize_output_name("a\x00b\x1fc.csv") == "a_b_c.csv"


def test_sanitize_strips_trailing_dots_and_spaces() -> None:
    assert sanitize_output_name("name.csv...  ") == "name.csv"


def test_sanitize_avoids_reserved_windows_stems() -> None:
    assert sanitize_output_name("CON.csv") == "_CON.csv"
    assert sanitize_output_name("con.csv") == "_con.csv"
    assert sanitize_output_name("COM1.csv") == "_COM1.csv"
    assert sanitize_output_name("NUL") == "_NUL"


def test_sanitize_non_reserved_stem_is_untouched() -> None:
    assert sanitize_output_name("CONSTANT.csv") == "CONSTANT.csv"


def test_sanitize_empty_result_raises() -> None:
    with pytest.raises(OutputNameTemplateError):
        sanitize_output_name("")
    with pytest.raises(OutputNameTemplateError):
        sanitize_output_name("   ")
    with pytest.raises(OutputNameTemplateError):
        sanitize_output_name("../..")


def test_sanitize_caps_length_preserving_extension() -> None:
    long_name = ("x" * 400) + ".csv"
    sanitized = sanitize_output_name(long_name)
    assert len(sanitized.encode("utf-8")) <= MAX_OUTPUT_NAME_COMPONENT_LENGTH
    assert sanitized.endswith(".csv")


def test_sanitize_caps_length_does_not_split_multibyte_codepoint() -> None:
    long_name = ("日" * 200) + ".csv"
    sanitized = sanitize_output_name(long_name)
    assert len(sanitized.encode("utf-8")) <= MAX_OUTPUT_NAME_COMPONENT_LENGTH
    # Must decode cleanly -- a split codepoint would raise.
    sanitized.encode("utf-8").decode("utf-8")
    assert sanitized.endswith(".csv")


# --- collision determinism ------------------------------------------------


def test_dedupe_returns_name_unchanged_when_unused() -> None:
    assert dedupe_output_name("report.csv", set()) == "report.csv"


def test_dedupe_appends_suffix_on_collision() -> None:
    used = {"report.csv"}
    assert dedupe_output_name("report.csv", used) == "report-2.csv"


def test_dedupe_increments_deterministically_never_overwrites() -> None:
    used = {"report.csv", "report-2.csv", "report-3.csv"}
    assert dedupe_output_name("report.csv", used) == "report-4.csv"


def test_dedupe_without_extension() -> None:
    used = {"report"}
    assert dedupe_output_name("report", used) == "report-2"


def test_dedupe_is_deterministic_across_repeated_calls() -> None:
    used = {"a.csv"}
    first = dedupe_output_name("a.csv", used)
    second = dedupe_output_name("a.csv", used)
    assert first == second == "a-2.csv"


# --- template validation errors (end-to-end through format_output_name) --


def test_format_output_name_sanitizes_rendered_result() -> None:
    assert (
        format_output_name("{column}.csv", {"column": "vendor/amount"})
        == "vendor_amount.csv"
    )


def test_format_output_name_rejects_template_that_sanitizes_to_empty() -> None:
    with pytest.raises(OutputNameTemplateError):
        format_output_name("{column}", {"column": "   "})
