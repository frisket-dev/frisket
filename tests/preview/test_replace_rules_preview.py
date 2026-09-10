"""Read-only replace-rules preview contract.

Pins the shared rule engine's semantics (ordered first-match-wins;
contains/exact/regex via Python ``re.search``; casefold when case-insensitive;
whole-cell set-to with null targets; MISSING source cells — null, empty, or
whitespace-only, the column-values preview's `missing` fact — never matched)
and the preview payload: per-rule counts over the full column, the no-match
unmatched,
an optional test-value trace, and the same ``value_hash`` staleness anchor the
commit validates — all with NO receipt, NO op, NO column written.
"""

from __future__ import annotations

from itertools import count

import pytest

from frisket.preview.cluster import source_value_hash
from frisket.actions.resolve import ReplaceRule
from frisket.preview.replace_rules import (
    REPLACE_RULES_PREVIEW_SCHEMA_VERSION,
    ReplaceRulesPreviewError,
    compile_replace_rules,
    evaluate_replace_rules,
    resolve_replace_rules_preview,
)
from frisket.engine.store import Project


_SEED_COUNTER = count()


def _seed(tmp_path, values, name="org"):
    project = Project.create(tmp_path / f"t{next(_SEED_COUNTER)}.frisket", name="t")
    sheet_id = project.add_sheet("data")
    cols = {name: project.add_column(sheet_id, name)}
    project.add_rows(sheet_id, [{name: value} for value in values], cols)
    return project, sheet_id


def _compiled(*rule_dicts):
    return compile_replace_rules(
        [ReplaceRule.model_validate(rule) for rule in rule_dicts]
    )


# ---------------------------------------------------------------------------
# shared evaluator semantics


def test_contains_is_substring_and_casefolds_by_default():
    rules = _compiled({"match": "contains", "pattern": "ACME", "target": "Acme"})
    assert evaluate_replace_rules("big acme corp", rules) == (0, "Acme")
    # casefold, not lower: "ß".casefold() == "ss"
    rules = _compiled({"match": "contains", "pattern": "strasse", "target": "St"})
    assert evaluate_replace_rules("Hauptstraße", rules) == (0, "St")


def test_contains_case_sensitive_requires_exact_case():
    rules = _compiled(
        {
            "match": "contains",
            "pattern": "ACME",
            "target": "Acme",
            "case_sensitive": True,
        }
    )
    assert evaluate_replace_rules("big acme corp", rules) == (None, "big acme corp")
    assert evaluate_replace_rules("big ACME corp", rules) == (0, "Acme")


def test_exact_is_whole_string_equality_both_case_modes():
    insensitive = _compiled({"match": "exact", "pattern": "N/A", "target": None})
    assert evaluate_replace_rules("n/a", insensitive) == (0, None)
    # substring is NOT enough for exact
    assert evaluate_replace_rules("n/a today", insensitive) == (None, "n/a today")
    sensitive = _compiled(
        {"match": "exact", "pattern": "N/A", "target": None, "case_sensitive": True}
    )
    assert evaluate_replace_rules("n/a", sensitive) == (None, "n/a")
    assert evaluate_replace_rules("N/A", sensitive) == (0, None)


def test_regex_uses_python_re_search_semantics():
    # re.search (not fullmatch/anchor): pattern may hit mid-string; \b and
    # IGNORECASE are Python semantics — the commit uses this same engine.
    rules = _compiled({"match": "regex", "pattern": r"\bcorp\b", "target": "Corp"})
    assert evaluate_replace_rules("ACME Corp", rules) == (0, "Corp")
    assert evaluate_replace_rules("corporation", rules) == (None, "corporation")
    sensitive = _compiled(
        {
            "match": "regex",
            "pattern": r"^glo\w+",
            "target": "Globex",
            "case_sensitive": True,
        }
    )
    assert evaluate_replace_rules("globex ltd", sensitive) == (0, "Globex")
    assert evaluate_replace_rules("GLOBEX ltd", sensitive) == (None, "GLOBEX ltd")


def test_first_match_wins_in_list_order():
    rules = _compiled(
        {"match": "contains", "pattern": "acme corp", "target": "First"},
        {"match": "contains", "pattern": "acme", "target": "Second"},
    )
    assert evaluate_replace_rules("ACME Corp", rules) == (0, "First")
    reordered = _compiled(
        {"match": "contains", "pattern": "acme", "target": "Second"},
        {"match": "contains", "pattern": "acme corp", "target": "First"},
    )
    assert evaluate_replace_rules("ACME Corp", reordered) == (0, "Second")


def test_null_target_and_unmatched_policies():
    rules = _compiled({"match": "exact", "pattern": "n/a", "target": None})
    assert evaluate_replace_rules("n/a", rules) == (0, None)
    assert evaluate_replace_rules("keep me", rules, "keep") == (None, "keep me")
    assert evaluate_replace_rules("null me", rules, "null") == (None, None)


def test_null_source_cells_are_never_matched_and_stay_null():
    # even a catch-all regex under unmatched="null" cannot touch a null cell
    rules = _compiled({"match": "regex", "pattern": ".*", "target": "X"})
    assert evaluate_replace_rules(None, rules, "keep") == (None, None)
    assert evaluate_replace_rules(None, rules, "null") == (None, None)


def test_blank_and_whitespace_cells_are_missing_never_matched():
    # missing = null OR empty OR whitespace-only (the column-values preview's
    # "missing" fact): never matched, never policy-touched — even by a
    # catch-all regex that would match the empty string
    rules = _compiled({"match": "regex", "pattern": ".*", "target": "X"})
    for cell in ("", "   ", "\t\n"):
        assert evaluate_replace_rules(cell, rules, "keep") == (None, None)
        assert evaluate_replace_rules(cell, rules, "null") == (None, None)


# ---------------------------------------------------------------------------
# preview payload

ROWS = ["Acme Inc", "ACME Corp", "globex ltd", "n/a", None, "Banana"]
RULES = [
    {"match": "contains", "pattern": "acme", "target": "Acme"},
    {"match": "exact", "pattern": "n/a", "target": None},
    {"match": "regex", "pattern": r"^glo\w+", "target": "Globex"},
]


def test_preview_counts_full_column_and_returns_commit_anchor(tmp_path):
    project, sheet_id = _seed(tmp_path, ROWS)
    payload = resolve_replace_rules_preview(
        project, sheet_id=sheet_id, input_column="org", rules=RULES
    )
    assert payload["schema_version"] == REPLACE_RULES_PREVIEW_SCHEMA_VERSION
    assert payload["sheet_id"] == sheet_id
    assert payload["total_rows"] == 6  # null row included in the column fact
    assert payload["rule_counts"] == [
        {"index": 0, "matched_rows": 2, "matched_values": 2},
        {"index": 1, "matched_rows": 1, "matched_values": 1},
        {"index": 2, "matched_rows": 1, "matched_values": 1},
    ]
    # the null cell is excluded from the unmatched: only "Banana" misses
    assert payload["unmatched_rows"] == 1
    assert payload["test_result"] is None
    # the anchor is the FULL-column source_value_hash the commit validates
    expected_hash = source_value_hash(
        project,
        sheet_id,
        payload["column_id"],
        [int(row_id) for row_id in project.visible_row_ids(sheet_id)],
    )
    assert payload["value_hash"] == expected_hash
    project.close()


def test_preview_first_match_wins_counts_overlapping_rules_once(tmp_path):
    project, sheet_id = _seed(tmp_path, ["ACME Corp", "acme inc", "other"])
    payload = resolve_replace_rules_preview(
        project,
        sheet_id=sheet_id,
        input_column="org",
        rules=[
            {"match": "contains", "pattern": "acme corp", "target": "First"},
            {"match": "contains", "pattern": "acme", "target": "Second"},
        ],
    )
    # "ACME Corp" matches both patterns but is counted ONLY under rule 0
    assert payload["rule_counts"] == [
        {"index": 0, "matched_rows": 1, "matched_values": 1},
        {"index": 1, "matched_rows": 1, "matched_values": 1},
    ]
    assert payload["unmatched_rows"] == 1
    project.close()


def test_preview_is_read_only(tmp_path):
    project, sheet_id = _seed(tmp_path, ROWS)
    ops_before = len(project.history())
    receipts_before = int(
        project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
    )
    resolve_replace_rules_preview(
        project, sheet_id=sheet_id, input_column="org", rules=RULES, test_value="x"
    )
    assert len(project.history()) == ops_before
    assert (
        int(project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0])
        == receipts_before
    )
    assert [c["name"] for c in project.columns(sheet_id)] == ["org"]
    project.close()


def test_preview_test_value_trace_matched_and_unmatched(tmp_path):
    project, sheet_id = _seed(tmp_path, ROWS)
    matched = resolve_replace_rules_preview(
        project,
        sheet_id=sheet_id,
        input_column="org",
        rules=RULES,
        test_value="THE ACME COMPANY",
    )
    assert matched["test_result"] == {"matched_rule_index": 0, "output": "Acme"}
    nulled = resolve_replace_rules_preview(
        project,
        sheet_id=sheet_id,
        input_column="org",
        rules=RULES,
        test_value="N/A",
    )
    assert nulled["test_result"] == {"matched_rule_index": 1, "output": None}
    kept = resolve_replace_rules_preview(
        project,
        sheet_id=sheet_id,
        input_column="org",
        rules=RULES,
        unmatched="keep",
        test_value="nothing here",
    )
    assert kept["test_result"] == {
        "matched_rule_index": None,
        "output": "nothing here",
    }
    null_policy = resolve_replace_rules_preview(
        project,
        sheet_id=sheet_id,
        input_column="org",
        rules=RULES,
        unmatched="null",
        test_value="nothing here",
    )
    assert null_policy["test_result"] == {"matched_rule_index": None, "output": None}
    project.close()


def test_preview_excludes_blank_and_whitespace_cells_like_nulls(tmp_path):
    """Blank/whitespace-only cells are MISSING: excluded from every rule
    count AND from the no-match unmatched, exactly like null cells — the
    commit passes them through as null, so the preview must not offer them
    to any rule or policy."""
    project, sheet_id = _seed(tmp_path, ["Acme Inc", "", "   ", None, "Banana"])
    payload = resolve_replace_rules_preview(
        project, sheet_id=sheet_id, input_column="org", rules=RULES
    )
    assert payload["total_rows"] == 5  # missing rows still count in the fact
    assert payload["rule_counts"][0] == {
        "index": 0,
        "matched_rows": 1,
        "matched_values": 1,
    }
    assert payload["unmatched_rows"] == 1  # only "Banana"
    project.close()


def test_preview_allows_empty_rules_for_the_opening_drawer(tmp_path):
    project, sheet_id = _seed(tmp_path, ROWS)
    payload = resolve_replace_rules_preview(
        project, sheet_id=sheet_id, input_column="org", rules=[]
    )
    assert payload["rule_counts"] == []
    assert payload["unmatched_rows"] == 5  # non-null rows only
    assert payload["value_hash"].startswith("sha256:")
    project.close()


# ---------------------------------------------------------------------------
# validation errors


def _error(tmp_path, **overrides):
    project, sheet_id = _seed(tmp_path, ROWS)
    kwargs = {
        "sheet_id": sheet_id,
        "input_column": "org",
        "rules": RULES,
        "unmatched": "keep",
        "test_value": None,
    }
    kwargs.update(overrides)
    with pytest.raises(ReplaceRulesPreviewError) as excinfo:
        resolve_replace_rules_preview(project, **kwargs)
    project.close()
    return excinfo.value


def test_preview_invalid_regex(tmp_path):
    exc = _error(
        tmp_path, rules=[{"match": "regex", "pattern": "(unclosed", "target": "x"}]
    )
    assert exc.code == "invalid_regex"
    assert exc.field == "rules[0].pattern"


def test_preview_invalid_rule_shapes_are_invalid_params(tmp_path):
    assert _error(tmp_path, rules="nope").code == "invalid_params"
    assert _error(tmp_path, rules=["nope"]).code == "invalid_params"
    assert (
        _error(tmp_path, rules=[{"match": "vibes", "pattern": "x", "target": "y"}]).code
        == "invalid_params"
    )
    # blank target is invalid (None is the explicit null target)
    assert (
        _error(
            tmp_path, rules=[{"match": "exact", "pattern": "x", "target": "   "}]
        ).code
        == "invalid_params"
    )


def test_preview_missing_sheet_and_column_are_invalid_input_ref(tmp_path):
    missing_sheet = _error(tmp_path, sheet_id=999)
    assert missing_sheet.code == "invalid_input_ref"
    assert missing_sheet.field == "sheet_id"
    missing_column = _error(tmp_path, input_column="missing")
    assert missing_column.code == "invalid_input_ref"
    assert missing_column.field == "input_column"
    bad_type = _error(tmp_path, sheet_id="1")
    assert bad_type.code == "invalid_input_ref"


def test_preview_bad_unmatched_and_test_value_are_invalid_params(tmp_path):
    assert _error(tmp_path, unmatched="maybe").code == "invalid_params"
    assert _error(tmp_path, test_value=42).code == "invalid_params"
