from __future__ import annotations

import pytest
from pydantic import ValidationError

from frisket.actions.resolve import (
    CombineParams,
    FillMissingParams,
    ReplaceParams,
    SubstituteParams,
    _fill_values,
    combine_values,
    replace_values,
    substitute_values,
)
from frisket.actions.system import root_action_catalog
from frisket.actions.types import Row, Rows


def _rows(values: list[object]) -> Rows:
    return Rows(
        {index: Row({"source": value}) for index, value in enumerate(values, 1)}
    )


def _values(results) -> list[object]:
    return [result.output.cleaned for result in results.values()]


def test_resolve_catalog_is_truthful_about_atomic_scope_and_runtime_fill_type() -> None:
    entries = {entry.kind: entry for entry in root_action_catalog().actions}

    for action_id in (
        "resolve.substitute",
        "resolve.replace",
        "resolve.combine",
        "resolve.fill_missing",
    ):
        entry = entries[action_id]
        assert entry.execution_mode == "atomic_column_transform"
        assert entry.row_scope_policy.selectors == ["all_rows"]
        assert "write_column_transform_op" in entry.side_effects
        assert "write_map_op" not in entry.side_effects
    fill_hints = entries["resolve.fill_missing"].ui_hints
    assert fill_hints["logical_outputs"] == [
        {"key": "cleaned", "column_type": "runtime"}
    ]
    fill_schema = entries["resolve.fill_missing"].input_schema
    assert fill_schema["properties"]["method"]["default"] == "down"
    assert "form_params" not in fill_hints
    assert "dynamic_output_types" not in fill_hints


def test_substitute_matches_exact_values_and_preserves_missing_cells() -> None:
    params = SubstituteParams(
        source="source",
        mapping={" 1 ": " padded ", "1": "one", "0": None},
        unmatched="null",
    )

    assert _values(
        substitute_values(params, _rows([" 1 ", "1", "0", "x", " ", None]))
    ) == [
        "padded",
        "one",
        None,
        None,
        None,
        None,
    ]


@pytest.mark.parametrize("key", ["", " ", "\t"])
def test_substitute_rejects_impossible_missing_value_keys(key: str) -> None:
    with pytest.raises(ValidationError, match="keys must not be blank"):
        SubstituteParams(source="source", mapping={key: "target"})


def test_replace_uses_ordered_python_rules_and_whole_cell_targets() -> None:
    params = ReplaceParams(
        source="source",
        rules=[
            {"match": "contains", "pattern": "acme", "target": "Acme"},
            {"match": "regex", "pattern": r"^glo\w+", "target": "Globex"},
            {"match": "exact", "pattern": "n/a", "target": None},
        ],
    )

    assert _values(
        replace_values(
            params,
            _rows(["Acme Inc", "ACME Corp", "globex", "n/a", "other", " "]),
        )
    ) == ["Acme", "Acme", "Globex", None, "other", None]


def test_replace_rejects_invalid_regex_and_blank_target() -> None:
    with pytest.raises(ValidationError, match="invalid regex pattern"):
        ReplaceParams(
            source="source",
            rules=[{"match": "regex", "pattern": "[", "target": "x"}],
        )
    with pytest.raises(ValidationError, match="target must not be blank"):
        ReplaceParams(
            source="source",
            rules=[{"match": "exact", "pattern": "x", "target": " "}],
        )


def test_replace_is_ordered_and_can_match_case_sensitively() -> None:
    params = ReplaceParams(
        source="source",
        rules=[
            {
                "match": "contains",
                "pattern": "acme",
                "target": "case-sensitive",
                "case_sensitive": True,
            },
            {"match": "regex", "pattern": r"^acme", "target": "regex"},
            {"match": "contains", "pattern": "acme", "target": "fallback"},
        ],
    )

    assert _values(replace_values(params, _rows(["acme inc", "ACME inc"]))) == [
        "case-sensitive",
        "regex",
    ]


def test_combine_groups_exact_members_and_applies_unmatched_policy() -> None:
    params = CombineParams(
        source="source",
        groups=[
            {"canonical": " Acme ", "members": ["ACME", "Acme, Inc."]},
            {"canonical": "Other", "members": ["misc"]},
        ],
        unmatched="value",
        unmatched_value=" Unknown ",
    )

    assert _values(
        combine_values(
            params,
            _rows(["ACME", "Acme, Inc.", "misc", "new", " ", None]),
        )
    ) == ["Acme", "Acme", "Other", "Unknown", None, None]


@pytest.mark.parametrize(("unmatched", "expected"), [("keep", "new"), ("null", None)])
def test_combine_keep_and_null_unmatched_policies(
    unmatched: str, expected: object
) -> None:
    params = CombineParams(
        source="source",
        groups=[{"canonical": "Known", "members": ["known"]}],
        unmatched=unmatched,
    )

    assert _values(combine_values(params, _rows(["new"]))) == [expected]


@pytest.mark.parametrize("member", ["", " ", "\n"])
def test_combine_rejects_impossible_missing_value_members(member: str) -> None:
    with pytest.raises(ValidationError, match="members must not be blank"):
        CombineParams(
            source="source",
            groups=[{"canonical": "Known", "members": [member]}],
        )


def test_combine_rejects_cross_group_duplicates_and_incoherent_unmatched_value() -> (
    None
):
    with pytest.raises(ValidationError, match="unique across groups"):
        CombineParams(
            source="source",
            groups=[
                {"canonical": "A", "members": ["x"]},
                {"canonical": "B", "members": ["x"]},
            ],
        )
    with pytest.raises(ValidationError, match="members must be unique"):
        CombineParams(
            source="source",
            groups=[{"canonical": "A", "members": ["x", "x"]}],
        )
    with pytest.raises(ValidationError, match="required only"):
        CombineParams(
            source="source",
            groups=[{"canonical": "A", "members": ["x"]}],
            unmatched="value",
        )


@pytest.mark.parametrize(
    ("method", "values", "expected"),
    [
        ("down", [None, "a", None, "b", None], [None, "a", "a", "b", "b"]),
        ("up", [None, "a", None, "b", None], ["a", "a", "b", "b", None]),
        ("mean", [1, None, "bad", 3], [1, 2, "bad", 3]),
        ("median", [1, None, 4, 9], [1, 4, 4, 9]),
        ("mode", ["b", "a", None, "a", "b", None], ["b", "a", "b", "a", "b", "b"]),
    ],
)
def test_fill_methods(
    method: str, values: list[object], expected: list[object]
) -> None:
    params = FillMissingParams(source="source", method=method)

    assert (
        list(_fill_values(params, _rows(values), source_type="number").values())
        == expected
    )


def test_fill_mode_tie_uses_first_value_and_empty_aggregate_stays_missing() -> None:
    mode = FillMissingParams(source="source", method="mode")
    mean = FillMissingParams(source="source", method="mean")

    assert list(
        _fill_values(mode, _rows(["b", "a", None]), source_type="text").values()
    ) == ["b", "a", "b"]
    assert list(
        _fill_values(mean, _rows(["bad", None]), source_type="number").values()
    ) == ["bad", None]


def test_fill_blank_policy_and_numeric_zero_literal() -> None:
    preserve_blank = FillMissingParams(
        source="source", method="value", fill_value="0", treat_blank_as_missing=False
    )
    fill_blank = preserve_blank.model_copy(update={"treat_blank_as_missing": True})

    assert list(
        _fill_values(preserve_blank, _rows([" ", None]), source_type="integer").values()
    ) == [" ", 0]
    assert list(
        _fill_values(fill_blank, _rows([" ", None]), source_type="integer").values()
    ) == [0, 0]
