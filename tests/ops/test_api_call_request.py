"""Pure request-builder for the API-call op: structured header/param/cookie/
form pairs plus a JSON/raw/form body, turned into ``safe_request`` kwargs
against an already-bound ``render`` callable. No I/O, no secrets, no HTTP --
the security surface under test is the boundary encoding: CRLF header
injection blocked, JSON body built structurally (parse-then-substitute, never
string-concatenated), and ordered query/header/form pairs preserved.
"""

from __future__ import annotations

import json

import pytest

from frisket.ops.api_call_request import (
    CookieInjection,
    HeaderInjection,
    InvalidJsonBodyTemplate,
    build_cookies,
    build_headers,
    build_json_body,
    build_query_params,
    build_request_kwargs,
    render_pairs,
)


def _render(value: str):
    """A fake render: substitutes the single token ``{{v}}`` with ``value``,
    leaving everything else (including other ``{{...}}`` text) untouched --
    same inert, non-re-evaluating contract as ``render_request_field``."""

    def render(template: str) -> str:
        return template.replace("{{v}}", value)

    return render


class TestRenderPairs:
    def test_renders_names_and_values(self):
        render = _render("42")
        result = render_pairs([("id", "{{v}}"), ("static", "x")], render)
        assert result == [("id", "42"), ("static", "x")]

    def test_duplicate_names_are_preserved_in_order(self):
        def render(t):
            return t

        result = render_pairs([("k", "first"), ("k", "second")], render)
        assert result == [("k", "first"), ("k", "second")]

    def test_empty_pairs_yields_empty_list(self):
        assert render_pairs([], lambda t: t) == []


class TestBuildHeaders:
    def test_renders_pairs(self):
        render = _render("sekret")
        assert build_headers([("Authorization", "Bearer {{v}}")], render) == [
            ("Authorization", "Bearer sekret")
        ]

    def test_crlf_in_value_raises_header_injection(self):
        render = _render("evil\r\nX-Injected: true")
        with pytest.raises(HeaderInjection):
            build_headers([("X-Custom", "{{v}}")], render)

    def test_lf_only_in_value_raises(self):
        render = _render("evil\nX-Injected: true")
        with pytest.raises(HeaderInjection):
            build_headers([("X-Custom", "{{v}}")], render)

    def test_crlf_in_name_raises(self):
        render = _render("X-Custom\r\nX-Injected")
        with pytest.raises(HeaderInjection):
            build_headers([("{{v}}", "value")], render)

    def test_other_control_char_raises(self):
        render = _render("bad\x00value")
        with pytest.raises(HeaderInjection):
            build_headers([("X-Custom", "{{v}}")], render)

    def test_ordinary_value_passes(self):
        def render(t):
            return t

        assert build_headers([("X-Custom", "fine value")], render) == [
            ("X-Custom", "fine value")
        ]

    def test_duplicate_header_names_are_preserved(self):
        assert build_headers(
            [("X-Tag", "first"), ("X-Tag", "second")], lambda value: value
        ) == [("X-Tag", "first"), ("X-Tag", "second")]


class TestBuildQueryParams:
    def test_renders_literally_no_manual_encoding(self):
        # httpx's params= does the URL-encoding; this builder must not
        # pre-encode, or values would be double-encoded.
        render = _render("a b&c=d")
        result = build_query_params([("q", "{{v}}")], render)
        assert result == [("q", "a b&c=d")]

    def test_duplicate_query_names_are_preserved(self):
        assert build_query_params(
            [("tag", "one"), ("tag", "two")], lambda value: value
        ) == [("tag", "one"), ("tag", "two")]


class TestBuildCookies:
    def test_renders_pairs_into_dict(self):
        render = _render("abc123")
        assert build_cookies([("session", "{{v}}")], render) == {"session": "abc123"}

    def test_semicolon_in_value_rejected_no_cookie_injection(self):
        # httpx does not encode cookie values; a bare ';' would forge a second pair
        render = _render("benign; session=attacker")
        with pytest.raises(CookieInjection):
            build_cookies([("pref", "{{v}}")], render)

    @pytest.mark.parametrize("bad", ["a\r\nb", "a\x00b", "a b", 'a"b', "a,b", "a\\b"])
    def test_illegal_cookie_octets_in_value_rejected(self, bad):
        with pytest.raises(CookieInjection):
            build_cookies([("session", "{{v}}")], _render(bad))

    def test_illegal_cookie_name_rejected(self):
        with pytest.raises(CookieInjection):
            build_cookies([("{{v}}", "ok")], _render("bad;name"))

    def test_duplicate_cookie_name_rejected_instead_of_last_value_winning(self):
        with pytest.raises(CookieInjection, match="duplicate cookie name"):
            build_cookies(
                [("session", "tenantA"), ("session", "tenantB")],
                lambda value: value,
            )

    def test_rendered_cookie_name_collision_is_rejected(self):
        with pytest.raises(CookieInjection, match="duplicate cookie name"):
            build_cookies(
                [("session", "tenantA"), ("{{v}}", "tenantB")],
                _render("session"),
            )

    def test_cookie_names_remain_case_sensitive(self):
        assert build_cookies(
            [("session", "lower"), ("Session", "upper")],
            lambda value: value,
        ) == {"session": "lower", "Session": "upper"}


class TestBuildJsonBody:
    def test_leaf_with_quote_brace_backslash_is_escaped_not_injected(self):
        value = 'a"b\\c{{x}}'
        render = _render(value)
        body = build_json_body('{"greeting": "{{v}}"}', render)
        # round-trips to exactly the rendered string -- no structural
        # change, and {{x}} inside the value is never re-expanded.
        assert json.loads(body) == {"greeting": value}
        assert body == json.dumps({"greeting": value})

    def test_numbers_bools_null_keep_their_types(self):
        def render(t):
            return t

        body = build_json_body(
            '{"count": 5, "active": true, "missing": null, "ratio": 1.5}',
            render,
        )
        assert json.loads(body) == {
            "count": 5,
            "active": True,
            "missing": None,
            "ratio": 1.5,
        }

    def test_templated_number_field_stays_a_string(self):
        # "templated leaves are strings; literal structure sets types" --
        # {"count": "{{v}}"} must not become a JSON number.
        render = _render("5")
        body = build_json_body('{"count": "{{v}}"}', render)
        parsed = json.loads(body)
        assert parsed == {"count": "5"}
        assert isinstance(parsed["count"], str)

    def test_object_keys_are_not_rendered(self):
        render = _render("RENDERED")
        body = build_json_body('{"{{v}}": "value"}', render)
        # the key text passes through untouched -- only values are templated
        assert json.loads(body) == {"{{v}}": "value"}

    def test_nested_objects_and_arrays(self):
        render = _render("X")
        template = '{"a": {"b": ["{{v}}", 1, {"c": "{{v}}"}]}}'
        body = build_json_body(template, render)
        assert json.loads(body) == {"a": {"b": ["X", 1, {"c": "X"}]}}

    def test_invalid_json_template_raises(self):
        with pytest.raises(InvalidJsonBodyTemplate):
            build_json_body("{not valid json", lambda t: t)

    @pytest.mark.parametrize(
        "body",
        [
            '{"score": NaN}',
            '{"score": Infinity}',
            '{"score": -Infinity}',
            '{"score": 1e999}',
        ],
    )
    def test_non_finite_json_number_raises(self, body):
        with pytest.raises(InvalidJsonBodyTemplate):
            build_json_body(body, lambda value: value)

    @pytest.mark.parametrize(
        "body",
        [
            '{"role": "user", "role": "admin"}',
            '{"request": {"role": "user", "role": "admin"}}',
        ],
    )
    def test_duplicate_object_key_raises(self, body):
        with pytest.raises(InvalidJsonBodyTemplate, match="duplicate JSON object key"):
            build_json_body(body, lambda value: value)

    def test_excessive_json_nesting_raises(self):
        # Deep enough for the recursive leaf substitution to reject even on a
        # platform whose C JSON decoder accepts this shape.
        body = "[" * 1_000 + '"value"' + "]" * 1_000
        with pytest.raises(InvalidJsonBodyTemplate, match="nesting"):
            build_json_body(body, lambda value: value)

    def test_top_level_array(self):
        render = _render("X")
        body = build_json_body('["{{v}}", 1, null]', render)
        assert json.loads(body) == ["X", 1, None]


class TestBuildRequestKwargs:
    def test_body_mode_none_has_no_body_kwarg(self):
        def render(t):
            return t

        kwargs = build_request_kwargs(
            {"method": "GET", "url": "https://example.com", "body_mode": "none"},
            render,
        )
        assert "content" not in kwargs
        assert "data" not in kwargs
        assert "json" not in kwargs
        assert kwargs["method"] == "GET"
        assert kwargs["url"] == "https://example.com"
        assert kwargs["headers"] == []
        assert kwargs["params"] == []
        assert kwargs["cookies"] == {}

    def test_url_and_pairs_are_rendered(self):
        render = _render("77")
        kwargs = build_request_kwargs(
            {
                "method": "GET",
                "url": "https://example.com/{{v}}",
                "headers": [("X-Id", "{{v}}")],
                "query_params": [("id", "{{v}}")],
                "cookies": [("sess", "{{v}}")],
                "body_mode": "none",
            },
            render,
        )
        assert kwargs["url"] == "https://example.com/77"
        assert kwargs["headers"] == [("X-Id", "77")]
        assert kwargs["params"] == [("id", "77")]
        assert kwargs["cookies"] == {"sess": "77"}

    def test_form_mode_uses_data_kwarg(self):
        render = _render("bob")
        kwargs = build_request_kwargs(
            {
                "method": "POST",
                "url": "https://example.com",
                "body_mode": "form",
                "form_body": [("username", "{{v}}")],
            },
            render,
        )
        assert kwargs["data"] == [("username", "bob")]
        assert "content" not in kwargs
        assert "json" not in kwargs

    def test_form_mode_preserves_duplicate_names(self):
        kwargs = build_request_kwargs(
            {
                "method": "POST",
                "url": "https://example.com",
                "body_mode": "form",
                "form_body": [("tag", "one"), ("tag", "two")],
            },
            lambda value: value,
        )
        assert kwargs["data"] == [("tag", "one"), ("tag", "two")]

    def test_json_mode_sets_content_and_default_content_type(self):
        render = _render("42")
        kwargs = build_request_kwargs(
            {
                "method": "POST",
                "url": "https://example.com",
                "body_mode": "json",
                "body": '{"id": "{{v}}"}',
            },
            render,
        )
        assert json.loads(kwargs["content"]) == {"id": "42"}
        assert ("content-type", "application/json") in kwargs["headers"]
        assert "data" not in kwargs
        assert "json" not in kwargs

    def test_json_mode_does_not_override_existing_content_type_header(self):
        def render(t):
            return t

        kwargs = build_request_kwargs(
            {
                "method": "POST",
                "url": "https://example.com",
                "headers": [("Content-Type", "application/vnd.api+json")],
                "body_mode": "json",
                "body": "{}",
            },
            render,
        )
        assert kwargs["headers"] == [("Content-Type", "application/vnd.api+json")]

    def test_raw_mode_uses_content_type_field(self):
        render = _render("raw-body")
        kwargs = build_request_kwargs(
            {
                "method": "POST",
                "url": "https://example.com",
                "body_mode": "raw",
                "body": "{{v}}",
                "content_type": "text/plain",
            },
            render,
        )
        assert kwargs["content"] == "raw-body"
        assert ("content-type", "text/plain") in kwargs["headers"]
        assert "data" not in kwargs
        assert "json" not in kwargs

    def test_raw_mode_explicit_header_wins_over_content_type_field(self):
        def render(t):
            return t

        kwargs = build_request_kwargs(
            {
                "method": "POST",
                "url": "https://example.com",
                "headers": [("Content-Type", "text/xml")],
                "body_mode": "raw",
                "body": "<x/>",
                "content_type": "text/plain",
            },
            render,
        )
        assert kwargs["headers"] == [("Content-Type", "text/xml")]

    def test_unknown_body_mode_raises(self):
        with pytest.raises(ValueError):
            build_request_kwargs(
                {"method": "GET", "url": "https://example.com", "body_mode": "bogus"},
                lambda t: t,
            )

    def test_header_crlf_injection_still_blocked_via_kwargs_builder(self):
        render = _render("evil\r\nX-Injected: true")
        with pytest.raises(HeaderInjection):
            build_request_kwargs(
                {
                    "method": "GET",
                    "url": "https://example.com",
                    "headers": [("X-Custom", "{{v}}")],
                    "body_mode": "none",
                },
                render,
            )
