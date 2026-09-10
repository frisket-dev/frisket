"""Closed WritePlan schema for project-scoped plugin writes (plugin-project-writes-v1).

BINDING SPEC: the public plugin-write contract in this module
dispositions (win conflicts). A trusted bundled plugin that declares
``plugin:project_writes`` returns a WritePlan from its (out-of-process) handler; the
host validates the whole plan against this closed schema and applies it atomically
(see frisket.executor.plugin_write_apply).

V1 is ADDITIVE-ONLY: the op vocabulary is exactly three ops — create a sheet,
create a column, append rows — and nothing mutates existing cells/columns/sheets, so
provenance/undo stay trivial (new objects, attributed wholesale). Ops carry
plan-local ``sheet_ref``/``column_ref`` handles (disposition #1); host row ids NEVER
appear in the child-authored schema and later ops target refs, never bare names or
ids. Every model is ``extra='forbid'`` so an unknown field is a loud rejection, and
the bounds below are schema-declared module constants, not hidden literals
(disposition #7).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

WRITE_PLAN_SCHEMA_VERSION = "frisket.plugin_write_plan.v1"

# Capabilities declared in the light manifest's ``requires.capabilities`` and
# checked by the host before any write/read surface is constructed.
PROJECT_WRITES_CAPABILITY = "plugin:project_writes"
PROJECT_READS_CAPABILITY = "plugin:project_reads"

# Schema-declared bounds. Exported as constants so the host, the
# frozen check, and any future manifest surface all agree on one source of truth.
MAX_OPS_PER_PLAN = 2000
MAX_ROWS_PER_APPEND = 5000
MAX_TOTAL_CELLS = 500_000
# Per-sidecar and per-plan byte caps for the scratch-file row handoff. The
# applier core stays inline-JSON for v1; the caps are declared here so the
# subprocess sidecar path enforces one agreed bound when it lands.
MAX_SIDECAR_BYTES = 64 * 1024 * 1024
MAX_PLAN_SIDECAR_BYTES = 256 * 1024 * 1024

_REF_MAX = 200
_NAME_MAX = 200


class _ClosedModel(BaseModel):
    """Every WritePlan model forbids unknown fields — a stray key is a loud
    validation error, never silently dropped."""

    model_config = ConfigDict(extra="forbid")


class CreateSheetOp(_ClosedModel):
    op: Literal["create_sheet"] = "create_sheet"
    sheet_ref: str = Field(min_length=1, max_length=_REF_MAX)
    name: str = Field(min_length=1, max_length=_NAME_MAX)


class CreateColumnOp(_ClosedModel):
    op: Literal["create_column"] = "create_column"
    sheet_ref: str = Field(min_length=1, max_length=_REF_MAX)
    column_ref: str = Field(min_length=1, max_length=_REF_MAX)
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    type: str = Field(default="text", min_length=1, max_length=100)
    format: str | None = None
    # Additive-only v1 still lets a plugin declare a column as hidden technical
    # metadata (e.g. the FtM importer's ``_ftm_*`` provenance columns), matching
    # the host's own hidden-column column so imports round-trip faithfully.
    hidden: bool = False


class AppendRowsOp(_ClosedModel):
    op: Literal["append_rows"] = "append_rows"
    sheet_ref: str = Field(min_length=1, max_length=_REF_MAX)
    # Each row maps plan-local column_refs -> JSON-serializable values.
    rows: list[dict[str, Any]] = Field(
        default_factory=list, max_length=MAX_ROWS_PER_APPEND
    )


WriteOp = Annotated[
    Union[CreateSheetOp, CreateColumnOp, AppendRowsOp],
    Field(discriminator="op"),
]


class WritePlan(_ClosedModel):
    schema_version: Literal["frisket.plugin_write_plan.v1"] = WRITE_PLAN_SCHEMA_VERSION
    ops: list[WriteOp] = Field(default_factory=list, max_length=MAX_OPS_PER_PLAN)

    @model_validator(mode="after")
    def _check_referential_integrity(self) -> "WritePlan":
        """Refs are unique and defined-before-use; append rows only target
        column_refs created earlier in the SAME plan; names are unique within the
        plan; the total appended-cell count stays under the declared cap.

        This is pure (no store): live-store name collisions are checked by the
        applier. A referential error here means ``apply_write_plan`` raises before
        any write, so a plan whose later op is invalid applies nothing."""
        sheet_refs: set[str] = set()
        sheet_names: set[str] = set()
        column_refs: set[str] = set()
        column_names_by_sheet: dict[str, set[str]] = {}
        total_cells = 0
        for op in self.ops:
            if isinstance(op, CreateSheetOp):
                if op.sheet_ref in sheet_refs:
                    raise ValueError(f"duplicate sheet_ref in plan: {op.sheet_ref}")
                if op.name in sheet_names:
                    raise ValueError(f"duplicate sheet name in plan: {op.name}")
                sheet_refs.add(op.sheet_ref)
                sheet_names.add(op.name)
                column_names_by_sheet[op.sheet_ref] = set()
            elif isinstance(op, CreateColumnOp):
                if op.sheet_ref not in sheet_refs:
                    raise ValueError(
                        f"create_column references unknown sheet_ref: {op.sheet_ref}"
                    )
                if op.column_ref in column_refs:
                    raise ValueError(f"duplicate column_ref in plan: {op.column_ref}")
                names = column_names_by_sheet[op.sheet_ref]
                if op.name in names:
                    raise ValueError(
                        f"duplicate column name '{op.name}' in sheet_ref {op.sheet_ref}"
                    )
                column_refs.add(op.column_ref)
                names.add(op.name)
            elif isinstance(op, AppendRowsOp):
                if op.sheet_ref not in sheet_refs:
                    raise ValueError(
                        f"append_rows references unknown sheet_ref: {op.sheet_ref}"
                    )
                # append_rows may only target column_refs created for this sheet
                # earlier in the plan (the key is a ref, never a name or host id).
                sheet_column_refs = _column_refs_for_sheet(self.ops, op.sheet_ref)
                for row in op.rows:
                    for key in row:
                        if key not in sheet_column_refs:
                            raise ValueError(
                                f"append_rows references unknown column_ref: {key}"
                            )
                    total_cells += len(row)
        if total_cells > MAX_TOTAL_CELLS:
            raise ValueError(f"plan exceeds MAX_TOTAL_CELLS ({MAX_TOTAL_CELLS})")
        return self


def _column_refs_for_sheet(ops: list[WriteOp], sheet_ref: str) -> set[str]:
    """The column_refs created for ``sheet_ref`` by create_column ops earlier in
    the plan (append_rows may only target these)."""
    return {
        op.column_ref
        for op in ops
        if isinstance(op, CreateColumnOp) and op.sheet_ref == sheet_ref
    }
