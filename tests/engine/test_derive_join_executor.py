from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

from executor_harness import CatalogEntry, ExecutorCase, Gate
from frisket.engine.store import Project


PROJECT_ID = "project-derive-join"
_KEYGEN = itertools.count(1)


def _seed_states_and_population(project_path: Path) -> dict[str, Any]:
    """1:1 key columns with a left-only and a right-only key (no fan-out)."""
    project = Project.create(project_path, name="Derive Join")
    try:
        return _populate_states_and_population(project)
    finally:
        project.close()


def _populate_states_and_population(project: Project) -> dict[str, Any]:
    left_id = project.add_sheet("States")
    left_cols = {
        "state_fips": project.add_column(left_id, "state_fips", type="integer"),
        "state_name": project.add_column(left_id, "state_name", type="text"),
    }
    left_rows = project.add_rows(
        left_id,
        [
            {"state_fips": 1, "state_name": "Alabama"},
            {"state_fips": 2, "state_name": "Alaska"},
            {"state_fips": 6, "state_name": "California"},
            {"state_fips": 99, "state_name": "Nowhere"},  # left-only key
        ],
        left_cols,
    )
    right_id = project.add_sheet("Population")
    right_cols = {
        "state_fips": project.add_column(right_id, "state_fips", type="integer"),
        "population": project.add_column(right_id, "population", type="integer"),
    }
    right_rows = project.add_rows(
        right_id,
        [
            {"state_fips": 1, "population": 5024279},
            {"state_fips": 2, "population": 733391},
            {"state_fips": 6, "population": 39538223},
            {"state_fips": 72, "population": 3285874},  # right-only key (PR)
        ],
        right_cols,
    )
    return {
        "left_sheet_id": left_id,
        "right_sheet_id": right_id,
        "left_row_ids": left_rows,
        "right_row_ids": right_rows,
        "left_cols": left_cols,
        "right_cols": right_cols,
    }


def _join_action(
    *,
    left_sheet_id: int,
    right_sheet_id: int,
    join_keys: list[dict[str, Any]] | None = None,
    how: str = "inner",
    indicator: bool = False,
    target_sheet_name: str = "Joined",
    idempotency_key: str | None = None,
    confirmation: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    if join_keys is None:
        join_keys = [{"left_column": "state_fips", "right_column": "state_fips"}]
    if idempotency_key is None:
        idempotency_key = f"derive_join@sha256:{next(_KEYGEN)}"
    params: dict[str, Any] = {
        "right": {"sheet_id": right_sheet_id},
        "join_keys": join_keys,
        "how": how,
        "indicator": indicator,
    }
    params.update(extra)
    return {
        "action_id": "derive.join",
        "scope": {"kind": "sheet_rows", "sheet_id": left_sheet_id},
        "sheet_name": target_sheet_name,
        "confirmation": confirmation,
        "params": params,
        "idempotency_key": idempotency_key,
    }


def _run(project: Project, action: dict[str, Any]):
    from frisket.engine.executor import actions as executor_actions

    return executor_actions.run_action_spec(
        project,
        action,
        project_id=PROJECT_ID,
    )


def _sheet_id_by_name(project: Project, name: str) -> int:
    row = project.db.execute(
        "SELECT id FROM sheets WHERE name=? AND hidden=0", (name,)
    ).fetchone()
    assert row is not None, f"sheet {name!r} not found"
    return int(row["id"])


def _materialized_table(project: Project, sheet_id: int) -> dict[str, Any]:
    columns = [
        (str(c["name"]), str(c["type"]), int(c["id"]))
        for c in project.columns(sheet_id)
    ]
    row_ids = project.visible_row_ids(sheet_id)
    values_by_col = {
        name: project.get_values(sheet_id, col_id) for name, _t, col_id in columns
    }
    rows = [
        {name: values_by_col[name].get(rid) for name, _t, _c in columns}
        for rid in row_ids
    ]
    return {
        "column_names": [name for name, _t, _c in columns],
        "column_types": {name: t for name, t, _c in columns},
        "rows": rows,
        "row_ids": row_ids,
    }


def _receipt(project: Project, receipt_id: str):
    import json

    from frisket.contracts.action import Receipt

    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    assert row is not None
    return Receipt.model_validate(json.loads(row["body"]))


def _receipt_ref(receipt, kind: str) -> dict[str, Any] | None:
    for item in [*receipt.inputs, *receipt.outputs, *receipt.evidence]:
        ref = item.ref
        if isinstance(ref, dict) and ref.get("kind") == kind:
            return ref
    return None


# --------------------------------------------------------------------------
# Shared lifecycle (catalog, gates, inner-join primary, idempotent replay)
# --------------------------------------------------------------------------


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    return _populate_states_and_population(project)


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _join_action(
        left_sheet_id=seeded["left_sheet_id"],
        right_sheet_id=seeded["right_sheet_id"],
        how="inner",
        target_sheet_name="Inner",
        idempotency_key="derive_join@sha256:harness-inner",
    )


def _same_sheet_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _join_action(
        left_sheet_id=seeded["left_sheet_id"],
        right_sheet_id=seeded["left_sheet_id"],
        idempotency_key="derive_join@sha256:same-sheet",
    )


def _duplicate_sheet_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _join_action(
        left_sheet_id=seeded["left_sheet_id"],
        right_sheet_id=seeded["right_sheet_id"],
        how="inner",
        target_sheet_name="Inner",
        idempotency_key="derive_join@sha256:duplicate-name",
    )


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    del result
    table = _materialized_table(project, _sheet_id_by_name(project, "Inner"))
    assert table["column_names"] == ["state_fips", "state_name", "population"]
    fips = sorted(row["state_fips"] for row in table["rows"])
    assert fips == [1, 2, 6]
    # coalesced key keeps the (integer) source type
    assert table["column_types"]["state_fips"] == "integer"


CASES = [
    ExecutorCase(
        kind="derive.join",
        request_style="typed",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            side_effects=frozenset(
                {
                    "create_sheet",
                    "create_columns",
                    "create_rows",
                    "write_op",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_input_ref",
                    "duplicate_sheet_name",
                    "stale_replay",
                    "join_fanout_requires_confirmation",
                    "idempotency_conflict",
                }
            ),
            cost_policy_kind="none",
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate(
                "invalid_input_ref_same_sheet",
                _same_sheet_action,
                "invalid_input_ref",
            ),
            Gate(
                "duplicate_sheet_name",
                _duplicate_sheet_action,
                "duplicate_sheet_name",
                after_primary_run=True,
            ),
        ),
        expect_counts={
            "sheets": 1,
            "columns": 3,
            "rows": 3,
            "ops": 1,
            "receipts": 1,
        },
        check_state=_check_state,
    )
]


# --------------------------------------------------------------------------
# Validation contract (param-schema uniques beyond the same-sheet gate)
# --------------------------------------------------------------------------


def test_validation_rejects_empty_keys_and_unknown_how() -> None:
    from frisket.actions.system import validate_root_action

    no_keys = _join_action(left_sheet_id=3, right_sheet_id=4, join_keys=[])
    assert validate_root_action(no_keys).ok is False

    cross = _join_action(left_sheet_id=3, right_sheet_id=4, how="cross")
    assert validate_root_action(cross).ok is False


def test_exact_join_rejects_other_predicates() -> None:
    from frisket.actions.system import validate_root_action

    # Equality is intrinsic; no alternate predicate is supported.
    ok = _join_action(
        left_sheet_id=3,
        right_sheet_id=4,
        join_keys=[{"left_column": "a", "right_column": "b"}],
    )
    assert validate_root_action(ok).ok is True

    bad = _join_action(
        left_sheet_id=3,
        right_sheet_id=4,
        join_keys=[
            {"left_column": "a", "right_column": "b", "predicate": "intersects"}
        ],
    )
    assert validate_root_action(bad).ok is False


# --------------------------------------------------------------------------
# The remaining how= modes (pandas-parity row sets over public FIPS data;
# inner is the harness primary)
# --------------------------------------------------------------------------


def test_left_join_keeps_unmatched_left(tmp_path: Path) -> None:
    seed = _seed_states_and_population(tmp_path / "p.frisket")
    project = Project(tmp_path / "p.frisket")
    try:
        result = _run(
            project,
            _join_action(
                left_sheet_id=seed["left_sheet_id"],
                right_sheet_id=seed["right_sheet_id"],
                how="left",
                target_sheet_name="Left",
            ),
        )
        assert result.status == "completed", result.errors
        table = _materialized_table(project, _sheet_id_by_name(project, "Left"))
        assert len(table["rows"]) == 4  # >= nL_total; 1:1 here
        by_fips = {row["state_fips"]: row for row in table["rows"]}
        assert set(by_fips) == {1, 2, 6, 99}
        assert by_fips[99]["population"] is None  # unmatched-left → right cols null
    finally:
        project.close()


def test_right_join_keeps_unmatched_right(tmp_path: Path) -> None:
    seed = _seed_states_and_population(tmp_path / "p.frisket")
    project = Project(tmp_path / "p.frisket")
    try:
        result = _run(
            project,
            _join_action(
                left_sheet_id=seed["left_sheet_id"],
                right_sheet_id=seed["right_sheet_id"],
                how="right",
                target_sheet_name="Right",
            ),
        )
        assert result.status == "completed", result.errors
        table = _materialized_table(project, _sheet_id_by_name(project, "Right"))
        by_fips = {row["state_fips"]: row for row in table["rows"]}
        assert set(by_fips) == {1, 2, 6, 72}
        assert by_fips[72]["state_name"] is None  # unmatched-right → left cols null
    finally:
        project.close()


def test_outer_join_is_union(tmp_path: Path) -> None:
    seed = _seed_states_and_population(tmp_path / "p.frisket")
    project = Project(tmp_path / "p.frisket")
    try:
        result = _run(
            project,
            _join_action(
                left_sheet_id=seed["left_sheet_id"],
                right_sheet_id=seed["right_sheet_id"],
                how="outer",
                indicator=True,
                target_sheet_name="Outer",
            ),
        )
        assert result.status == "completed", result.errors
        table = _materialized_table(project, _sheet_id_by_name(project, "Outer"))
        assert len(table["rows"]) == 5  # 3 both + 1 left_only + 1 right_only
        by_fips = {row["state_fips"]: row for row in table["rows"]}
        assert set(by_fips) == {1, 2, 6, 99, 72}
        # indicator values (pandas categorical parity)
        merge_by_fips = {f: r["_merge"] for f, r in by_fips.items()}
        assert merge_by_fips[1] == "both"
        assert merge_by_fips[99] == "left_only"
        assert merge_by_fips[72] == "right_only"
        assert {r["_merge"] for r in table["rows"]} == {
            "both",
            "left_only",
            "right_only",
        }
    finally:
        project.close()


# --------------------------------------------------------------------------
# Duplicate-key fan-out (property-style row-count assertions)
# --------------------------------------------------------------------------


def _seed_fanout(project_path: Path) -> dict[str, Any]:
    project = Project.create(project_path, name="Fanout")
    try:
        left_id = project.add_sheet("Left")
        left_cols = {"k": project.add_column(left_id, "k", type="integer")}
        # key 6 appears twice, key 1 once
        project.add_rows(left_id, [{"k": 6}, {"k": 6}, {"k": 1}], left_cols)
        right_id = project.add_sheet("Right")
        right_cols = {"k": project.add_column(right_id, "k", type="integer")}
        # key 6 appears 3x, key 1 twice
        project.add_rows(
            right_id, [{"k": 6}, {"k": 6}, {"k": 6}, {"k": 1}, {"k": 1}], right_cols
        )
        return {"left_sheet_id": left_id, "right_sheet_id": right_id}
    finally:
        project.close()


def test_duplicate_key_fanout_row_counts(tmp_path: Path) -> None:
    seed = _seed_fanout(tmp_path / "p.frisket")
    project = Project(tmp_path / "p.frisket")
    try:
        result = _run(
            project,
            _join_action(
                left_sheet_id=seed["left_sheet_id"],
                right_sheet_id=seed["right_sheet_id"],
                join_keys=[{"left_column": "k", "right_column": "k"}],
                how="inner",
                target_sheet_name="Fanned",
            ),
        )
        assert result.status == "completed", result.errors
        table = _materialized_table(project, _sheet_id_by_name(project, "Fanned"))
        # inner rows == Σ nL(k)*nR(k) = 6:(2*3) + 1:(1*2) = 8
        assert len(table["rows"]) == 8
        ks = sorted(row["k"] for row in table["rows"])
        assert ks == [1, 1, 6, 6, 6, 6, 6, 6]
        source = _receipt_ref(
            _receipt(project, result.receipt_id), "joined_tables_source"
        )
        assert source is not None
        stats = source["stats"]
        assert stats["both"] == 8
        assert stats["max_fanout"] == 6
    finally:
        project.close()


def test_fanout_guard_requires_confirmation(tmp_path: Path) -> None:
    seed = _seed_fanout(tmp_path / "p.frisket")
    project = Project(tmp_path / "p.frisket")
    try:
        blocked = _run(
            project,
            _join_action(
                left_sheet_id=seed["left_sheet_id"],
                right_sheet_id=seed["right_sheet_id"],
                join_keys=[{"left_column": "k", "right_column": "k"}],
                how="inner",
                target_sheet_name="Guarded",
                max_output_rows=5,
            ),
        )
        # The fan-out guard uses the unified confirmation envelope and surfaces
        # as needs_confirmation
        # (HTTP 402), the SAME envelope the model-cost gate uses, NOT a plain
        # failed/400. The generic marker rides the ActionError.
        assert blocked.status == "needs_confirmation"
        assert blocked.errors
        err = blocked.errors[0]
        assert err.needs_confirmation is True
        assert err.code == "join_fanout_requires_confirmation"
        assert err.field == "confirmation"
        assert err.details.get("estimated_rows") == 8
        assert err.details.get("max_output_rows") == 5
        assert err.details.get("top_fanout_keys")
        promise_set_hash = err.details.get("promise_set_hash")
        assert isinstance(promise_set_hash, str)
        assert len(promise_set_hash) == 64
        # nothing materialized on the block
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM sheets WHERE name='Guarded'"
            ).fetchone()[0]
            == 0
        )

        bare_confirmed = _run(
            project,
            _join_action(
                left_sheet_id=seed["left_sheet_id"],
                right_sheet_id=seed["right_sheet_id"],
                join_keys=[{"left_column": "k", "right_column": "k"}],
                how="inner",
                target_sheet_name="Guarded",
                max_output_rows=5,
                confirmation="wrong-token",
            ),
        )
        assert bare_confirmed.status == "needs_confirmation"

        confirmed = _run(
            project,
            _join_action(
                left_sheet_id=seed["left_sheet_id"],
                right_sheet_id=seed["right_sheet_id"],
                join_keys=[{"left_column": "k", "right_column": "k"}],
                how="inner",
                target_sheet_name="Guarded",
                max_output_rows=5,
                confirmation=promise_set_hash,
            ),
        )
        assert confirmed.status == "completed", confirmed.errors
        table = _materialized_table(project, _sheet_id_by_name(project, "Guarded"))
        assert len(table["rows"]) == 8
    finally:
        project.close()


# --------------------------------------------------------------------------
# Typed key equality: nulls never match, numeric coercion, text exact
# --------------------------------------------------------------------------


def test_nulls_never_match_including_null_null(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "p.frisket", name="Nulls")
    try:
        left_id = project.add_sheet("L")
        left_cols = {
            "k": project.add_column(left_id, "k", type="integer"),
            "ln": project.add_column(left_id, "ln", type="text"),
        }
        project.add_rows(
            left_id,
            [{"k": 6, "ln": "six"}, {"ln": "blank-left"}],  # 2nd row: null key
            left_cols,
        )
        right_id = project.add_sheet("R")
        right_cols = {
            "k": project.add_column(right_id, "k", type="integer"),
            "rn": project.add_column(right_id, "rn", type="text"),
        }
        project.add_rows(
            right_id,
            [{"k": 6, "rn": "SIX"}, {"rn": "blank-right"}],  # 2nd row: null key
            right_cols,
        )
    finally:
        project.close()

    project = Project(tmp_path / "p.frisket")
    try:
        # inner: the two null-keyed rows must NOT match each other (null != null)
        inner = _run(
            project,
            _join_action(
                left_sheet_id=left_id,
                right_sheet_id=right_id,
                join_keys=[{"left_column": "k", "right_column": "k"}],
                how="inner",
                target_sheet_name="NullInner",
            ),
        )
        assert inner.status == "completed", inner.errors
        table = _materialized_table(project, _sheet_id_by_name(project, "NullInner"))
        assert len(table["rows"]) == 1  # only k=6
        assert table["rows"][0]["k"] == 6

        # outer: null-left becomes left_only, null-right becomes right_only
        outer = _run(
            project,
            _join_action(
                left_sheet_id=left_id,
                right_sheet_id=right_id,
                join_keys=[{"left_column": "k", "right_column": "k"}],
                how="outer",
                indicator=True,
                target_sheet_name="NullOuter",
            ),
        )
        assert outer.status == "completed", outer.errors
        table = _materialized_table(project, _sheet_id_by_name(project, "NullOuter"))
        merges = sorted(row["_merge"] for row in table["rows"])
        assert merges == ["both", "left_only", "right_only"]
        source = _receipt_ref(
            _receipt(project, outer.receipt_id), "joined_tables_source"
        )
        assert source is not None
        stats = source["stats"]
        assert stats["both"] == 1
        assert stats["left_only"] == 1
        assert stats["right_only"] == 1
    finally:
        project.close()


def test_numeric_keys_coerce_one_equals_one_point_zero(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "p.frisket", name="Numeric")
    try:
        left_id = project.add_sheet("L")
        left_cols = {"k": project.add_column(left_id, "k", type="integer")}
        project.add_rows(left_id, [{"k": 6}], left_cols)  # integer 6
        right_id = project.add_sheet("R")
        right_cols = {"k": project.add_column(right_id, "k", type="number")}
        project.add_rows(right_id, [{"k": 6.0}], right_cols)  # number 6.0
    finally:
        project.close()

    project = Project(tmp_path / "p.frisket")
    try:
        result = _run(
            project,
            _join_action(
                left_sheet_id=left_id,
                right_sheet_id=right_id,
                join_keys=[{"left_column": "k", "right_column": "k"}],
                how="inner",
                target_sheet_name="NumJoin",
            ),
        )
        assert result.status == "completed", result.errors
        table = _materialized_table(project, _sheet_id_by_name(project, "NumJoin"))
        assert len(table["rows"]) == 1  # 1 == 1.0
        # integer + number widen to number for the coalesced key
        assert table["column_types"]["k"] == "number"
    finally:
        project.close()


def test_text_keys_are_case_sensitive_exact(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "p.frisket", name="Text")
    try:
        left_id = project.add_sheet("L")
        left_cols = {"name": project.add_column(left_id, "name", type="text")}
        project.add_rows(left_id, [{"name": "Washington"}], left_cols)
        right_id = project.add_sheet("R")
        right_cols = {"name": project.add_column(right_id, "name", type="text")}
        project.add_rows(right_id, [{"name": "washington"}], right_cols)
    finally:
        project.close()

    project = Project(tmp_path / "p.frisket")
    try:
        result = _run(
            project,
            _join_action(
                left_sheet_id=left_id,
                right_sheet_id=right_id,
                join_keys=[{"left_column": "name", "right_column": "name"}],
                how="inner",
                target_sheet_name="TextJoin",
            ),
        )
        assert result.status == "completed", result.errors
        table = _materialized_table(project, _sheet_id_by_name(project, "TextJoin"))
        assert len(table["rows"]) == 0  # exact, case-sensitive → no match
    finally:
        project.close()


# --------------------------------------------------------------------------
# Multi-key AND semantics + suffix collisions
# --------------------------------------------------------------------------


def test_multi_key_and_semantics(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "p.frisket", name="MultiKey")
    try:
        left_id = project.add_sheet("Counties")
        left_cols = {
            "state": project.add_column(left_id, "state", type="integer"),
            "county": project.add_column(left_id, "county", type="integer"),
            "cname": project.add_column(left_id, "cname", type="text"),
        }
        project.add_rows(
            left_id,
            [
                {"state": 6, "county": 1, "cname": "Alameda"},
                {"state": 6, "county": 37, "cname": "Los Angeles"},
                {"state": 1, "county": 1, "cname": "Autauga"},
            ],
            left_cols,
        )
        right_id = project.add_sheet("CountyPop")
        right_cols = {
            "state": project.add_column(right_id, "state", type="integer"),
            "county": project.add_column(right_id, "county", type="integer"),
            "pop": project.add_column(right_id, "pop", type="integer"),
        }
        project.add_rows(
            right_id,
            [
                {"state": 6, "county": 1, "pop": 1682353},
                {"state": 6, "county": 37, "pop": 10014009},
                {"state": 1, "county": 37, "pop": 999},  # (1,37) not on the left
            ],
            right_cols,
        )
    finally:
        project.close()

    project = Project(tmp_path / "p.frisket")
    try:
        result = _run(
            project,
            _join_action(
                left_sheet_id=left_id,
                right_sheet_id=right_id,
                join_keys=[
                    {"left_column": "state", "right_column": "state"},
                    {"left_column": "county", "right_column": "county"},
                ],
                how="inner",
                target_sheet_name="MK",
            ),
        )
        assert result.status == "completed", result.errors
        table = _materialized_table(project, _sheet_id_by_name(project, "MK"))
        # only (6,1) and (6,37) match on BOTH keys
        assert len(table["rows"]) == 2
        pairs = sorted((row["state"], row["county"]) for row in table["rows"])
        assert pairs == [(6, 1), (6, 37)]
    finally:
        project.close()


def test_non_key_collision_gets_suffixed(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "p.frisket", name="Collide")
    try:
        left_id = project.add_sheet("L")
        left_cols = {
            "k": project.add_column(left_id, "k", type="integer"),
            "name": project.add_column(left_id, "name", type="text"),
        }
        project.add_rows(left_id, [{"k": 6, "name": "left-cal"}], left_cols)
        right_id = project.add_sheet("R")
        right_cols = {
            "k": project.add_column(right_id, "k", type="integer"),
            "name": project.add_column(right_id, "name", type="text"),
        }
        project.add_rows(right_id, [{"k": 6, "name": "right-cal"}], right_cols)
    finally:
        project.close()

    project = Project(tmp_path / "p.frisket")
    try:
        result = _run(
            project,
            _join_action(
                left_sheet_id=left_id,
                right_sheet_id=right_id,
                join_keys=[{"left_column": "k", "right_column": "k"}],
                how="inner",
                target_sheet_name="Collided",
            ),
        )
        assert result.status == "completed", result.errors
        table = _materialized_table(project, _sheet_id_by_name(project, "Collided"))
        assert table["column_names"] == ["k", "name_left", "name_right"]
        row = table["rows"][0]
        assert row["name_left"] == "left-cal"
        assert row["name_right"] == "right-cal"
    finally:
        project.close()


# --------------------------------------------------------------------------
# Lineage membership, receipt evidence, stale_replay
# --------------------------------------------------------------------------


def test_two_sided_membership_and_receipt_evidence(tmp_path: Path) -> None:
    seed = _seed_states_and_population(tmp_path / "p.frisket")
    project = Project(tmp_path / "p.frisket")
    try:
        result = _run(
            project,
            _join_action(
                left_sheet_id=seed["left_sheet_id"],
                right_sheet_id=seed["right_sheet_id"],
                how="outer",
                target_sheet_name="Lineage",
            ),
        )
        assert result.status == "completed", result.errors
        op_id = result.op_ids[0]
        roles = [
            r["role"]
            for r in project.db.execute(
                "SELECT role FROM materialized_row_sources WHERE op_id=?", (op_id,)
            ).fetchall()
        ]
        # both source and target membership roles present
        assert "join_left" in roles
        assert "join_right" in roles
        # source sheet ids reach BOTH parents (multi-parent staleness plumbing)
        source_sheets = {
            int(r["source_sheet_id"])
            for r in project.db.execute(
                "SELECT DISTINCT source_sheet_id FROM materialized_row_sources WHERE op_id=?",
                (op_id,),
            ).fetchall()
        }
        assert source_sheets == {seed["left_sheet_id"], seed["right_sheet_id"]}

        receipt = _receipt(project, result.receipt_id)
        source = _receipt_ref(receipt, "joined_tables_source")
        assert source is not None
        stats = source["stats"]
        assert stats is not None
        assert stats["both"] == 3
        assert stats["left_only"] == 1
        assert stats["right_only"] == 1
        assert stats["matched_pairs"] == 3
        # key column provenance recorded
        keys_ref = _receipt_ref(receipt, "joined_tables_source")
        assert keys_ref is not None
        assert keys_ref["how"] == "outer"
    finally:
        project.close()


def test_stale_replay_after_parent_cell_edit(tmp_path: Path) -> None:
    seed = _seed_states_and_population(tmp_path / "p.frisket")
    project = Project(tmp_path / "p.frisket")
    try:
        action = _join_action(
            left_sheet_id=seed["left_sheet_id"],
            right_sheet_id=seed["right_sheet_id"],
            how="inner",
            target_sheet_name="Stale",
            idempotency_key="derive_join@sha256:stale-fixed",
        )
        first = _run(project, action)
        assert first.status == "completed", first.errors

        # edit a source key cell on the left parent → the recorded join is stale
        project.apply_edits(
            [
                {
                    "row_id": seed["left_row_ids"][0],
                    "column_id": seed["left_cols"]["state_fips"],
                    "value": 55,
                }
            ]
        )
        replay = _run(project, action)
        assert replay.status == "failed"
        assert replay.errors
        assert replay.errors[0].code == "stale_replay"
    finally:
        project.close()


def test_join_replay_rejects_stale_materialized_membership(tmp_path: Path) -> None:
    seed = _seed_states_and_population(tmp_path / "p.frisket")
    project = Project(tmp_path / "p.frisket")
    try:
        action = _join_action(
            left_sheet_id=seed["left_sheet_id"],
            right_sheet_id=seed["right_sheet_id"],
            how="inner",
            target_sheet_name="MaterializedStale",
            idempotency_key="derive_join@sha256:stale-materialized",
        )
        first = _run(project, action)
        assert first.status == "completed", first.errors
        rows_output = next(output for output in first.outputs if output.kind == "rows")
        assert rows_output.row_ids

        project.db.execute(
            "UPDATE rows SET hidden=1 WHERE id=?", (int(rows_output.row_ids[0]),)
        )
        project.db.commit()

        replay = _run(project, action)
        assert replay.status == "failed"
        assert replay.errors
        assert replay.errors[0].code == "stale_replay"
    finally:
        project.close()
