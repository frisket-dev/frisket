"""The cluster-key template dialect: a single-source-value substituter with a
small blessed transform set (before/after/lower), pipe-with-one-arg only, no
nesting or expressions. Unknown transforms and malformed tokens are loud
KeyTemplateErrors; an empty template is passthrough.
"""

from __future__ import annotations

import pytest

from frisket.authoring.templates import (
    KeyTemplateError,
    render_value_key,
    validate_key_template,
)


class TestRenderTransforms:
    def test_bare_value_is_identity(self):
        assert render_value_key("{{value}}", "President of Honduras") == (
            "President of Honduras"
        )

    def test_lower(self):
        assert render_value_key("{{value|lower}}", "PRESIDENT") == "president"

    def test_before_separator_present(self):
        assert render_value_key('{{value|before:" of "}}', "President of Honduras") == (
            "President"
        )

    def test_before_separator_absent_passes_through(self):
        # the value has no " of " -> unchanged, so "President" collides with the
        # before-key of "President of Honduras".
        assert render_value_key('{{value|before:" of "}}', "President") == "President"

    def test_after_separator_present(self):
        assert render_value_key('{{value|after:" of "}}', "President of Honduras") == (
            "Honduras"
        )

    def test_after_separator_absent_passes_through(self):
        assert render_value_key('{{value|after:" of "}}', "President") == "President"

    def test_only_first_separator_occurrence_splits(self):
        assert render_value_key(
            '{{value|before:" of "}}', "King of the Hill of Fame"
        ) == ("King")
        assert render_value_key(
            '{{value|after:" of "}}', "King of the Hill of Fame"
        ) == ("the Hill of Fame")

    def test_quotes_preserve_separator_whitespace(self):
        # quoted " of " keeps its surrounding spaces (word boundary); the
        # unquoted "of" is the bare substring — a different split point.
        assert render_value_key('{{value|before:" of "}}', "food of gods") == "food"
        assert render_value_key("{{value|before:of}}", "food of gods") == "food "

    def test_literal_text_around_token_is_kept(self):
        assert render_value_key("role:{{value|lower}}", "CEO") == "role:ceo"

    def test_empty_template_is_passthrough(self):
        assert render_value_key("", "President of Honduras") == "President of Honduras"
        assert render_value_key(None, "x") == "x"

    def test_none_value_renders_empty(self):
        assert render_value_key("{{value}}", None) == ""


class TestValidation:
    def test_valid_templates(self):
        for tpl in (
            "{{value}}",
            "{{value|lower}}",
            '{{value|before:" of "}}',
            '{{value|after:" of "}}',
            "{{value|before:of}}",
        ):
            validate_key_template(tpl)  # no raise

    def test_unknown_transform_is_error(self):
        with pytest.raises(KeyTemplateError):
            validate_key_template("{{value|reverse}}")

    def test_unknown_token_name_is_error(self):
        with pytest.raises(KeyTemplateError):
            validate_key_template("{{column}}")

    def test_before_without_arg_is_error(self):
        with pytest.raises(KeyTemplateError):
            validate_key_template("{{value|before}}")

    def test_before_with_empty_arg_is_error(self):
        with pytest.raises(KeyTemplateError):
            validate_key_template('{{value|before:""}}')

    def test_lower_with_arg_is_error(self):
        with pytest.raises(KeyTemplateError):
            validate_key_template("{{value|lower:x}}")

    def test_template_without_value_token_is_error(self):
        # a constant key (no {{value}}) would collapse every row into one
        # cluster — reject it rather than silently over-merge.
        with pytest.raises(KeyTemplateError):
            validate_key_template("President")

    def test_render_also_rejects_unknown_transform(self):
        # render enforces the same grammar, not only the standalone validator.
        with pytest.raises(KeyTemplateError):
            render_value_key("{{value|reverse}}", "x")
