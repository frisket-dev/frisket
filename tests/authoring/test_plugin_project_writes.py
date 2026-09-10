from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

CONTRACTS_MODULE = "frisket.contracts.plugin_write_plan"
APPLY_MODULE = "frisket.engine.executor.plugin_write_apply"


def _module(name: str):
    """Import ``name`` only when it exists; a missing module is a semantic red
    (AssertionError), not a bare ImportError that would classify as a setup error."""
    assert importlib.util.find_spec(name) is not None, (
        f"{name} is not authored yet — the WritePlan contract seam is unimplemented"
    )
    return importlib.import_module(name)


def _attr(module, name: str):
    assert hasattr(module, name), f"{module.__name__}.{name} is unimplemented"
    return getattr(module, name)


def _make_project(tmp_path):
    from frisket.engine.store.project import Project

    return Project.create(tmp_path / "writes.frisket", name="writes")


# --------------------------------------------------------------------------- #
# 1. The closed WritePlan schema: three ops, plan-local refs, unknown-field
#    rejection and schema-declared bounds.
# --------------------------------------------------------------------------- #


def test_write_plan_schema_is_closed_three_ops_with_plan_local_refs() -> None:
    mod = _module(CONTRACTS_MODULE)

    schema_version = _attr(mod, "WRITE_PLAN_SCHEMA_VERSION")
    assert schema_version == "frisket.plugin_write_plan.v1"

    WritePlan = _attr(mod, "WritePlan")
    CreateSheetOp = _attr(mod, "CreateSheetOp")
    CreateColumnOp = _attr(mod, "CreateColumnOp")
    AppendRowsOp = _attr(mod, "AppendRowsOp")

    # Exactly the three additive ops — no mutation/delete op is smuggled in.
    op_literal = lambda cls: cls.model_fields["op"].default  # noqa: E731
    assert {
        op_literal(CreateSheetOp),
        op_literal(CreateColumnOp),
        op_literal(AppendRowsOp),
    } == {
        "create_sheet",
        "create_column",
        "append_rows",
    }

    # Plan-local refs are first-class fields; host ids are absent from the child schema.
    assert "sheet_ref" in CreateSheetOp.model_fields
    assert {"sheet_ref", "column_ref"} <= set(CreateColumnOp.model_fields)
    assert "sheet_ref" in AppendRowsOp.model_fields
    for cls in (CreateSheetOp, CreateColumnOp, AppendRowsOp, WritePlan):
        fields = set(cls.model_fields)
        assert "sheet_id" not in fields and "column_id" not in fields, (
            f"{cls.__name__} leaks a host id into the child-authored schema"
        )

    # extra='forbid' everywhere: an unknown field is a loud rejection.
    with pytest.raises(Exception):
        WritePlan.model_validate(
            {
                "schema_version": schema_version,
                "ops": [{"op": "create_sheet", "sheet_ref": "s1", "name": "People"}],
                "unexpected_field": True,
            }
        )
    with pytest.raises(Exception):
        CreateSheetOp.model_validate(
            {"op": "create_sheet", "sheet_ref": "s1", "name": "People", "rogue": 1}
        )

    # A minimal valid plan round-trips.
    plan = WritePlan.model_validate(
        {
            "schema_version": schema_version,
            "ops": [
                {"op": "create_sheet", "sheet_ref": "s1", "name": "People"},
                {
                    "op": "create_column",
                    "sheet_ref": "s1",
                    "column_ref": "c1",
                    "name": "full_name",
                },
                {"op": "append_rows", "sheet_ref": "s1", "rows": [{"c1": "Ada"}]},
            ],
        }
    )
    assert len(plan.ops) == 3


def test_write_plan_bounds_are_schema_declared_constants() -> None:
    mod = _module(CONTRACTS_MODULE)
    for const in (
        "MAX_OPS_PER_PLAN",
        "MAX_ROWS_PER_APPEND",
        "MAX_TOTAL_CELLS",
        "MAX_SIDECAR_BYTES",
        "MAX_PLAN_SIDECAR_BYTES",
    ):
        value = _attr(mod, const)
        assert isinstance(value, int) and value > 0, f"{const} must be a positive bound"

    WritePlan = _attr(mod, "WritePlan")
    schema_version = _attr(mod, "WRITE_PLAN_SCHEMA_VERSION")
    over = _attr(mod, "MAX_OPS_PER_PLAN") + 1
    with pytest.raises(Exception):
        WritePlan.model_validate(
            {
                "schema_version": schema_version,
                "ops": [
                    {"op": "create_sheet", "sheet_ref": f"s{i}", "name": f"S{i}"}
                    for i in range(over)
                ],
            }
        )


# --------------------------------------------------------------------------- #
# 2. Host applier: capability gating, collisions, atomicity, undo, receipts
#    including plugin identity, consent, and transaction boundaries.
# --------------------------------------------------------------------------- #


def test_undeclared_capability_is_a_loud_rejection(tmp_path) -> None:
    apply_mod = _module(APPLY_MODULE)
    contracts = _module(CONTRACTS_MODULE)
    apply_write_plan = _attr(apply_mod, "apply_write_plan")
    writes_cap = _attr(contracts, "PROJECT_WRITES_CAPABILITY")
    assert writes_cap == "plugin:project_writes"

    project = _make_project(tmp_path)
    plan = {
        "schema_version": _attr(contracts, "WRITE_PLAN_SCHEMA_VERSION"),
        "ops": [{"op": "create_sheet", "sheet_ref": "s1", "name": "People"}],
    }
    # No project_writes capability declared → must refuse loudly, write nothing.
    with pytest.raises(Exception):
        apply_write_plan(
            project,
            plan,
            plugin_id="frisket.ftm",
            manifest_sha="deadbeef",
            declared_capabilities=("plugin:trusted_local_backend",),
        )
    assert project.sheets() == []


def test_collision_hard_fails_and_applies_nothing(tmp_path) -> None:
    apply_mod = _module(APPLY_MODULE)
    contracts = _module(CONTRACTS_MODULE)
    apply_write_plan = _attr(apply_mod, "apply_write_plan")
    writes_cap = _attr(contracts, "PROJECT_WRITES_CAPABILITY")

    project = _make_project(tmp_path)
    project.add_sheet("People")  # pre-existing name
    before = {row["name"] for row in project.sheets()}

    plan = {
        "schema_version": _attr(contracts, "WRITE_PLAN_SCHEMA_VERSION"),
        "ops": [{"op": "create_sheet", "sheet_ref": "s1", "name": "People"}],
    }
    with pytest.raises(Exception):
        apply_write_plan(
            project,
            plan,
            plugin_id="frisket.ftm",
            manifest_sha="deadbeef",
            declared_capabilities=(writes_cap,),
        )
    assert {row["name"] for row in project.sheets()} == before


def test_atomicity_nth_invalid_op_applies_nothing(tmp_path) -> None:
    """Disposition #2: a plan whose later op is invalid leaves ZERO partial state —
    proven by store inspection (no sheet, no op row, op_cursor unmoved)."""
    apply_mod = _module(APPLY_MODULE)
    contracts = _module(CONTRACTS_MODULE)
    apply_write_plan = _attr(apply_mod, "apply_write_plan")
    writes_cap = _attr(contracts, "PROJECT_WRITES_CAPABILITY")

    project = _make_project(tmp_path)
    cursor_before = project.get_meta("op_cursor")
    op_rows_before = project.db.execute("SELECT COUNT(*) AS n FROM ops").fetchone()["n"]

    # Second op appends to a column ref that was never created → invalid mid-plan.
    plan = {
        "schema_version": _attr(contracts, "WRITE_PLAN_SCHEMA_VERSION"),
        "ops": [
            {"op": "create_sheet", "sheet_ref": "s1", "name": "People"},
            {"op": "append_rows", "sheet_ref": "s1", "rows": [{"missing_ref": "x"}]},
        ],
    }
    with pytest.raises(Exception):
        apply_write_plan(
            project,
            plan,
            plugin_id="frisket.ftm",
            manifest_sha="deadbeef",
            declared_capabilities=(writes_cap,),
        )
    assert project.sheets() == [], "the created sheet must have been rolled back"
    assert project.get_meta("op_cursor") == cursor_before
    assert (
        project.db.execute("SELECT COUNT(*) AS n FROM ops").fetchone()["n"]
        == op_rows_before
    )


def test_success_applies_atomically_records_one_undoable_op_and_a_receipt(
    tmp_path,
) -> None:
    apply_mod = _module(APPLY_MODULE)
    contracts = _module(CONTRACTS_MODULE)
    apply_write_plan = _attr(apply_mod, "apply_write_plan")
    writes_cap = _attr(contracts, "PROJECT_WRITES_CAPABILITY")

    project = _make_project(tmp_path)
    plan = {
        "schema_version": _attr(contracts, "WRITE_PLAN_SCHEMA_VERSION"),
        "ops": [
            {"op": "create_sheet", "sheet_ref": "s1", "name": "People"},
            {
                "op": "create_column",
                "sheet_ref": "s1",
                "column_ref": "c_name",
                "name": "full_name",
            },
            {
                "op": "append_rows",
                "sheet_ref": "s1",
                "rows": [{"c_name": "Ada"}, {"c_name": "Alan"}],
            },
        ],
    }
    op_rows_before = project.db.execute("SELECT COUNT(*) AS n FROM ops").fetchone()["n"]
    receipt = apply_write_plan(
        project,
        plan,
        plugin_id="frisket.ftm",
        manifest_sha="cafef00d",
        declared_capabilities=(writes_cap,),
    )

    # Applied: one sheet, one column, two rows.
    sheets = project.sheets()
    assert [row["name"] for row in sheets] == ["People"]
    sheet_id = sheets[0]["id"]
    assert [c["name"] for c in project.columns(sheet_id)] == ["full_name"]
    assert project.row_count(sheet_id) == 2

    # Undo is ONE step: exactly one new op row, and it removes everything the plan created.
    op_rows_after = project.db.execute("SELECT COUNT(*) AS n FROM ops").fetchone()["n"]
    assert op_rows_after == op_rows_before + 1

    # Receipt carries {plugin id, manifest sha, plan hash, counts}.
    def field(obj, name):
        return obj[name] if isinstance(obj, dict) else getattr(obj, name)

    assert field(receipt, "plugin_id") == "frisket.ftm"
    assert field(receipt, "manifest_sha") == "cafef00d"
    plan_hash = field(receipt, "plan_hash")
    assert isinstance(plan_hash, str) and plan_hash
    counts = field(receipt, "counts")
    counts = counts if isinstance(counts, dict) else vars(counts)
    assert (
        counts.get("sheets") == 1
        and counts.get("columns") == 1
        and counts.get("rows") == 2
    )


def test_plan_hash_is_stable_over_reordered_json_keys() -> None:
    apply_mod = _module(APPLY_MODULE)
    contracts = _module(CONTRACTS_MODULE)
    plan_hash = _attr(apply_mod, "plan_hash")
    schema_version = _attr(contracts, "WRITE_PLAN_SCHEMA_VERSION")
    a = {
        "schema_version": schema_version,
        "ops": [{"op": "create_sheet", "sheet_ref": "s1", "name": "P"}],
    }
    b = {
        "ops": [{"name": "P", "sheet_ref": "s1", "op": "create_sheet"}],
        "schema_version": schema_version,
    }
    assert plan_hash(a) == plan_hash(b)
