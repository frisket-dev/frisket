from __future__ import annotations

import pytest

from frisket.engine.executor.action_support import _extract_json_path


@pytest.mark.parametrize(
    ("value", "path", "expected"),
    [
        ({"a": 1, "b": [1, 2, 3]}, "$", {"a": 1, "b": [1, 2, 3]}),
        ({"a": {"b": {"c": 42}}}, "$.a.b.c", 42),
        ({"items": [10, 20, 30]}, "$.items[0]", 10),
        ({"items": [10, 20, 30]}, "$.items[2]", 30),
        ({"a": {"b": [{"c": "x"}, {"c": "y"}, {"c": "z"}]}}, "$.a.b[2].c", "z"),
        ({"arr": [1, 2, 3]}, "$.arr[-1]", 3),
        ({"arr": [1, 2, 3]}, "$.arr[-3]", 1),
        ({"a": [[1, 2], [3, 4]]}, "$.a[0][1]", 2),
        ({"a": [[1, 2], [3, 4]]}, "$.a[1][0]", 3),
    ],
)
def test_extract_json_path_resolves_nested_dicts_and_indices(
    value: dict, path: str, expected: object
) -> None:
    assert _extract_json_path(value, path) == (expected, True)


@pytest.mark.parametrize(
    ("value", "path"),
    [
        ({"a": {"b": 1}}, "$.a.missing"),
        ({"a": {"b": 1}}, "$.missing"),
        ({"items": [1, 2, 3]}, "$.items[3]"),
        ({"items": [1, 2, 3]}, "$.items[-4]"),
        ({"a": {"b": 1}}, "$.a[0]"),
        ({"a": 5}, "$.a[0]"),
    ],
)
def test_extract_json_path_missing_or_out_of_range_is_not_found(
    value: dict, path: str
) -> None:
    assert _extract_json_path(value, path) == (None, False)


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
