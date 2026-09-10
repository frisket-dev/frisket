from __future__ import annotations

from frisket.engine.executor.action_support import _extract_json_path


def test_whole_value_returns_input_unchanged() -> None:
    value = {"a": 1, "b": [1, 2, 3]}
    assert _extract_json_path(value, "$") == (value, True)


def test_dotted_path_over_nested_dicts() -> None:
    value = {"a": {"b": {"c": 42}}}
    assert _extract_json_path(value, "$.a.b.c") == (42, True)


def test_missing_dict_key_is_not_found() -> None:
    value = {"a": {"b": 1}}
    assert _extract_json_path(value, "$.a.missing") == (None, False)
    assert _extract_json_path(value, "$.missing") == (None, False)


def test_array_index_on_list_field() -> None:
    value = {"items": [10, 20, 30]}
    assert _extract_json_path(value, "$.items[0]") == (10, True)
    assert _extract_json_path(value, "$.items[2]") == (30, True)


def test_array_index_after_dotted_path() -> None:
    value = {"a": {"b": [{"c": "x"}, {"c": "y"}, {"c": "z"}]}}
    assert _extract_json_path(value, "$.a.b[2].c") == ("z", True)


def test_negative_index_from_end() -> None:
    value = {"arr": [1, 2, 3]}
    assert _extract_json_path(value, "$.arr[-1]") == (3, True)
    assert _extract_json_path(value, "$.arr[-3]") == (1, True)


def test_chained_index_suffixes() -> None:
    value = {"a": [[1, 2], [3, 4]]}
    assert _extract_json_path(value, "$.a[0][1]") == (2, True)
    assert _extract_json_path(value, "$.a[1][0]") == (3, True)


def test_out_of_range_index_is_not_found() -> None:
    value = {"items": [1, 2, 3]}
    assert _extract_json_path(value, "$.items[3]") == (None, False)


def test_negative_out_of_range_index_is_not_found() -> None:
    value = {"items": [1, 2, 3]}
    assert _extract_json_path(value, "$.items[-4]") == (None, False)


def test_indexing_non_list_is_not_found() -> None:
    assert _extract_json_path({"a": {"b": 1}}, "$.a[0]") == (None, False)
    assert _extract_json_path({"a": 5}, "$.a[0]") == (None, False)


def test_malformed_bracket_syntax_fails_closed() -> None:
    # A typo must not silently resolve to a shorter path.
    value = {"a": [1, 2, 3]}
    for bad in ("$.a[", "$.a[0", "$.a[0]junk", "$.a[0][1", "$.a[]", "$.a[b]"):
        assert _extract_json_path(value, bad) == (None, False), bad


def test_literal_bracket_bearing_key_preserved() -> None:
    # Exact dict keys win, so a key that literally contains brackets is still
    # addressable (backward-compat with pre-indexing map.python routes).
    assert _extract_json_path({"a[0]": "literal"}, "$.a[0]") == ("literal", True)
    assert _extract_json_path({"a[b]": 7}, "$.a[b]") == (7, True)
    # exact literal key wins over indexing a same-prefixed list sibling
    both = {"a[0]": "keyed", "a": ["indexed"]}
    assert _extract_json_path(both, "$.a[0]") == ("keyed", True)
    # but with no literal key, the index applies to the list
    assert _extract_json_path({"a": ["indexed"]}, "$.a[0]") == ("indexed", True)
