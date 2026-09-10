"""The composite request-field resolver: scans multiple HTTP request fields
(URL, headers, params, body, cookies) for `{{column}}` and `{{secret.NAME}}`
tokens, splitting the two, and renders a field against row values plus
already-resolved secrets in one inert, non-re-evaluating pass.
"""

from __future__ import annotations

import pytest

from frisket.authoring.request_templates import (
    MissingSecret,
    RequestTokens,
    render_request_field,
    scan_request_tokens,
)


class TestScanRequestTokens:
    def test_splits_columns_and_secrets_across_fields(self):
        tokens = scan_request_tokens(
            [
                "https://api.example.com/{{id}}",
                "Bearer {{secret.API_KEY}}",
                "user={{username}}&key={{secret.API_KEY}}",
            ]
        )
        assert tokens == RequestTokens(columns=["id", "username"], secrets=["API_KEY"])

    def test_dedups_and_preserves_first_seen_order_across_all_fields(self):
        tokens = scan_request_tokens(
            [
                "{{b}} {{a}} {{secret.Y}}",
                "{{a}} {{c}} {{secret.X}} {{secret.Y}}",
            ]
        )
        assert tokens.columns == ["b", "a", "c"]
        assert tokens.secrets == ["Y", "X"]

    def test_secret_tokens_excluded_from_columns(self):
        tokens = scan_request_tokens(["{{secret.TOKEN}}"])
        assert tokens.columns == []
        assert tokens.secrets == ["TOKEN"]

    def test_empty_secret_name_is_skipped(self):
        tokens = scan_request_tokens(["{{secret.}}"])
        assert tokens.columns == []
        assert tokens.secrets == []

    def test_no_tokens_yields_empty_lists(self):
        tokens = scan_request_tokens(["https://example.com/static", ""])
        assert tokens == RequestTokens(columns=[], secrets=[])


class TestRenderRequestField:
    def test_substitutes_columns_and_secrets(self):
        rendered = render_request_field(
            "https://api.example.com/{{id}}?auth={{secret.API_KEY}}",
            {"id": "42"},
            {"API_KEY": "sekret"},
        )
        assert rendered == "https://api.example.com/42?auth=sekret"

    def test_missing_column_renders_empty_string(self):
        assert render_request_field("{{missing}}", {}, {}) == ""

    def test_missing_secret_raises_missing_secret(self):
        with pytest.raises(MissingSecret) as exc_info:
            render_request_field("{{secret.API_KEY}}", {}, {})
        assert exc_info.value.name == "API_KEY"

    def test_inserted_value_is_not_re_expanded(self):
        # a row value that itself looks like a template must be inserted
        # literally, never scanned again for {{...}} tokens.
        rendered = render_request_field(
            "{{payload}}",
            {"payload": "literal {{other}} stays literal"},
            {},
        )
        assert rendered == "literal {{other}} stays literal"

    def test_inserted_secret_value_is_not_re_expanded(self):
        rendered = render_request_field(
            "{{secret.TOKEN}}",
            {},
            {"TOKEN": "{{id}} looks templated but is not"},
        )
        assert rendered == "{{id}} looks templated but is not"

    def test_special_characters_inserted_literally_no_encoding(self):
        # boundary encoding (URL/JSON/form) is the caller's job, not the
        # resolver's -- values pass through byte-for-byte.
        rendered = render_request_field(
            "q={{q}}",
            {"q": "a b&c=d/e?f#g"},
            {},
        )
        assert rendered == "q=a b&c=d/e?f#g"

    def test_none_column_value_renders_empty_string(self):
        assert render_request_field("{{x}}", {"x": None}, {}) == ""

    def test_non_string_column_value_uses_format_template_value(self):
        # dict/list values go through JSON; everything else through str() --
        # same rules as render_column_template.
        rendered = render_request_field("{{n}} {{obj}}", {"n": 5, "obj": {"a": 1}}, {})
        assert rendered == '5 {"a": 1}'
