"""Per-output result generations and rebuildable per-cell publication heads."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from frisket.engine.store.current_cells import (
    refresh_current_cell_pairs,
    refresh_current_cells,
)

PUBLICATION_EFFECTS = frozenset({"publish_value", "publish_null", "publish_error"})
WRITE_MODES = frozenset({"create", "replace_scope"})
TERMINAL_DISPOSITIONS = frozenset({"completed", "partial", "failed", "cancelled"})

PENDING_DESCRIPTOR_REPLACEMENT_KEY = "pending_descriptor_replacement"
UNPUBLISHED_GENERATIONS_KEY = "unpublished_generation_columns"

_SQLITE_ID_CHUNK_SIZE = 900


class ResultGenerationError(RuntimeError):
    """Base error for a refused result-generation transition."""


class GenerationClaimError(ResultGenerationError):
    """The caller does not hold the exact active output claim."""


class GenerationDeclarationConflict(ResultGenerationError):
    """A run/output was already declared with different immutable facts."""


class GenerationSealedError(ResultGenerationError):
    """A terminal output generation cannot be resumed or rewritten."""


class GenerationStateError(ResultGenerationError):
    """A generation transition is incompatible with its current state."""


@dataclass(frozen=True)
class RunOutputGeneration:
    run_id: int
    column_id: int
    output_role: str
    compatibility_key: str
    write_mode: str
    state: str
    # The claim which first declared the generation. It is audit context, not
    # permanent writer authority: crash recovery may hold a successor claim.
    claim_token: str
    expected_base_run_id: int | None
    terminal_disposition: str | None
    declared_at: str
    sealed_at: str | None


@dataclass(frozen=True)
class PublishedCellHead:
    run_id: int
    row_id: int
    column_id: int
    publication_effect: str
    value: Any
    error: str | None
    error_code: str | None
    outcome: str
    review_state: str
    confidence: float | None
    justification: str | None
    op_id: int


def decode_published_result_value(
    publication_effect: str, encoded_value: str | None
) -> Any:
    """Decode one exact result without null/error fallback semantics."""

    if publication_effect not in PUBLICATION_EFFECTS:
        raise ValueError(f"unknown publication effect {publication_effect!r}")
    if publication_effect != "publish_value":
        return None
    if encoded_value is None:
        raise ValueError("publish_value result has no encoded value")
    return json.loads(encoded_value)


def _chunks(values: list[int]) -> Iterator[list[int]]:
    for start in range(0, len(values), _SQLITE_ID_CHUNK_SIZE):
        yield values[start : start + _SQLITE_ID_CHUNK_SIZE]


def _generation_publication_predicate(*, generation_alias: str, op_alias: str) -> str:
    return (
        f"{generation_alias}.state IN ('active','sealed') "
        f"AND {op_alias}.status='applied' AND NOT EXISTS ("
        "SELECT 1 FROM json_each(COALESCE(json_extract("
        f"{op_alias}.undo_info,'$.{UNPUBLISHED_GENERATIONS_KEY}'),'[]')) unpublished "
        f"WHERE CAST(unpublished.value AS INTEGER)={generation_alias}.column_id)"
    )


class ResultGenerationStore:
    """Declare, publish, read, and rebuild generation-managed result heads."""

    def __init__(self, project: Any):
        self.project = project

    @property
    def db(self) -> sqlite3.Connection:
        return self.project.db

    @contextmanager
    def _write_scope(self, *, commit: bool) -> Iterator[None]:
        started_transaction = not self.db.in_transaction
        if started_transaction:
            self.db.execute("BEGIN IMMEDIATE")
        savepoint = f"result_generation_{uuid.uuid4().hex}"
        self.db.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
        except BaseException:
            self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            if started_transaction:
                self.db.rollback()
            raise
        self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
        if commit:
            self.db.commit()

    @staticmethod
    def _generation_from_row(row: sqlite3.Row) -> RunOutputGeneration:
        return RunOutputGeneration(
            run_id=int(row["run_id"]),
            column_id=int(row["column_id"]),
            output_role=str(row["output_role"]),
            compatibility_key=str(row["compatibility_key"]),
            write_mode=str(row["write_mode"]),
            state=str(row["state"]),
            claim_token=str(row["claim_token"]),
            expected_base_run_id=(
                None
                if row["expected_base_run_id"] is None
                else int(row["expected_base_run_id"])
            ),
            terminal_disposition=(
                None
                if row["terminal_disposition"] is None
                else str(row["terminal_disposition"])
            ),
            declared_at=str(row["declared_at"]),
            sealed_at=None if row["sealed_at"] is None else str(row["sealed_at"]),
        )

    def get_binding(self, run_id: int, column_id: int) -> RunOutputGeneration | None:
        row = self.db.execute(
            "SELECT * FROM run_output_generations WHERE run_id=? AND column_id=?",
            (int(run_id), int(column_id)),
        ).fetchone()
        return None if row is None else self._generation_from_row(row)

    def bindings_for_run(self, run_id: int) -> list[RunOutputGeneration]:
        rows = self.db.execute(
            "SELECT * FROM run_output_generations WHERE run_id=? "
            "ORDER BY output_role, column_id",
            (int(run_id),),
        ).fetchall()
        return [self._generation_from_row(row) for row in rows]

    def bindings_for_op(self, op_id: int) -> list[RunOutputGeneration]:
        rows = self.db.execute(
            "SELECT generation.* FROM run_output_generations generation "
            "JOIN runs run ON run.id=generation.run_id "
            "WHERE run.op_id=? ORDER BY generation.run_id, "
            "generation.output_role, generation.column_id",
            (int(op_id),),
        ).fetchall()
        return [self._generation_from_row(row) for row in rows]

    def column_ids_for_op(self, op_id: int) -> frozenset[int]:
        return frozenset(
            binding.column_id for binding in self.bindings_for_op(int(op_id))
        )

    def is_generation_managed(self, column_id: int) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM run_output_generations WHERE column_id=? LIMIT 1",
                (int(column_id),),
            ).fetchone()
            is not None
        )

    def latest_applied_run_id(self, column_id: int) -> int | None:
        """Newest applied generation for presentation-family metadata.

        This is deliberately not a value pointer. Exact cell heads remain the
        authority for managed values; callers use this run only to group an
        output family or associate it with an in-flight run.
        """

        row = self.db.execute(
            "SELECT generation.run_id FROM run_output_generations generation "
            "JOIN runs run ON run.id=generation.run_id "
            "JOIN ops op ON op.id=run.op_id "
            "WHERE generation.column_id=? AND op.status='applied' "
            "ORDER BY generation.run_id DESC LIMIT 1",
            (int(column_id),),
        ).fetchone()
        return None if row is None else int(row["run_id"])

    @staticmethod
    def _operation_info(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        try:
            value = json.loads(row["undo_info"] or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _run_operation(self, run_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT op.id,op.spec,op.undo_info,op.status FROM runs run "
            "JOIN ops op ON op.id=run.op_id WHERE run.id=?",
            (int(run_id),),
        ).fetchone()

    @staticmethod
    def _pending_descriptor_changes(
        info: dict[str, Any],
    ) -> dict[str, dict[str, dict[str, Any]]]:
        raw = info.get(PENDING_DESCRIPTOR_REPLACEMENT_KEY)
        if not isinstance(raw, dict):
            return {}
        changes: dict[str, dict[str, dict[str, Any]]] = {}
        for column_id, value in raw.items():
            if (
                str(column_id).isdecimal()
                and isinstance(value, dict)
                and isinstance(value.get("before"), dict)
                and isinstance(value.get("after"), dict)
                and isinstance(value["before"].get("type"), str)
                and isinstance(value["after"].get("type"), str)
            ):
                changes[str(column_id)] = {
                    "before": {
                        "type": str(value["before"]["type"]),
                        "format": value["before"].get("format"),
                        "semantic_type": value["before"].get("semantic_type"),
                    },
                    "after": {
                        "type": str(value["after"]["type"]),
                        "format": value["after"].get("format"),
                        "semantic_type": value["after"].get("semantic_type"),
                    },
                }
        return changes

    def latest_descriptor_replacement_op_id(self, column_id: int) -> int | None:
        """Latest applied whole-column descriptor boundary for edit precedence."""

        rows = self.db.execute(
            "SELECT DISTINCT op.id,op.undo_info FROM run_output_generations generation "
            "JOIN runs run ON run.id=generation.run_id "
            "JOIN ops op ON op.id=run.op_id "
            "WHERE generation.column_id=? AND generation.state='sealed' "
            "AND op.status='applied' ORDER BY op.id DESC",
            (int(column_id),),
        ).fetchall()
        key = str(int(column_id))
        for row in rows:
            info = self._operation_info(row)
            after_types = info.get("column_types_after")
            after_formats = info.get("column_formats_after")
            after_semantics = info.get("column_semantic_types_after")
            if any(
                isinstance(values, dict) and key in values
                for values in (after_types, after_formats, after_semantics)
            ):
                return int(row["id"])
        return None

    def require_effect_batch_publishable(
        self, run_id: int, batch: Collection[dict[str, Any]]
    ) -> None:
        """Refuse result effects whose generation journal is not publishable."""

        column_ids = sorted(
            {
                int(item["column_id"])
                for item in batch
                if item.get("publication_effect") is not None
            }
        )
        for column_id in column_ids:
            row = self.db.execute(
                "SELECT generation.state, op.status AS op_status "
                "FROM run_output_generations generation "
                "JOIN runs run ON run.id=generation.run_id "
                "JOIN ops op ON op.id=run.op_id "
                "WHERE generation.run_id=? AND generation.column_id=?",
                (int(run_id), column_id),
            ).fetchone()
            if row is None:
                raise GenerationStateError(
                    f"run {run_id} output column {column_id} is undeclared"
                )
            if str(row["state"]) == "sealed":
                raise GenerationSealedError(
                    f"run {run_id} output column {column_id} is sealed"
                )
            if str(row["op_status"]) != "applied":
                raise GenerationStateError(
                    f"run {run_id} cannot publish while its op is {row['op_status']!r}"
                )

    def _require_active_claim(
        self,
        *,
        run_id: int,
        column_id: int,
        claim_token: str,
        require_current_name: bool = True,
    ) -> sqlite3.Row:
        if not isinstance(claim_token, str) or not claim_token:
            raise GenerationClaimError("generation mutation supplied no claim token")
        row = self.db.execute(
            "SELECT claim.expected_current_run_id, claim.output_name, "
            "claim.action_kind, column.name AS column_name, "
            "(SELECT MAX(head.run_id) FROM cell_result_heads head "
            " WHERE head.column_id=column.id) AS current_head_run_id, "
            "run.status AS run_status, op.status AS op_status "
            "FROM output_column_claims claim "
            "JOIN columns column ON column.id=claim.column_id "
            "JOIN runs run ON run.id=claim.run_id "
            "JOIN ops op ON op.id=run.op_id "
            "WHERE claim.claim_token=? AND claim.run_id=? AND claim.column_id=? "
            "AND claim.status='active'",
            (claim_token, int(run_id), int(column_id)),
        ).fetchone()
        if row is None:
            raise GenerationClaimError(
                f"claim token {claim_token!r} does not own run {run_id} "
                f"output column {column_id}"
            )
        if require_current_name and str(row["output_name"]) != str(row["column_name"]):
            raise GenerationClaimError(
                f"claim token {claim_token!r} no longer names run {run_id} "
                f"output column {column_id}"
            )
        if str(row["op_status"]) != "applied":
            raise GenerationStateError(
                f"run {run_id} cannot publish while its op is {row['op_status']!r}"
            )
        return row

    @staticmethod
    def _require_pointer_fence(
        binding: RunOutputGeneration, *, current_head_run_id: object
    ) -> None:
        current = None if current_head_run_id is None else int(current_head_run_id)
        if binding.write_mode == "create":
            # Preparation mirrors a newly created output column to its owning
            # run before the bound generation is declared.
            if current not in (None, binding.run_id):
                raise GenerationClaimError(
                    f"fresh output column {binding.column_id} moved to run {current}"
                )
            return
        if current != binding.expected_base_run_id:
            raise GenerationClaimError(
                f"replacement output column {binding.column_id} moved from expected "
                f"base {binding.expected_base_run_id} to {current}"
            )

    def declare(
        self,
        run_id: int,
        column_id: int,
        *,
        output_role: str,
        compatibility_key: str,
        write_mode: str,
        claim_token: str,
        target_descriptor: dict[str, Any] | None = None,
        defer_publication: bool = False,
        commit: bool = True,
    ) -> RunOutputGeneration:
        """Freeze one run/output declaration before its first result write.

        Exact recovery declarations are idempotent while open. A successor
        output claim may recover the same declaration, but descriptors, mode,
        and original base pointer cannot change. Sealed generations reject
        resume.
        """

        run_id = int(run_id)
        column_id = int(column_id)
        output_role = str(output_role).strip()
        compatibility_key = str(compatibility_key).strip()
        if not output_role:
            raise ValueError("output_role must not be empty")
        if not compatibility_key:
            raise ValueError("compatibility_key must not be empty")
        if write_mode not in WRITE_MODES:
            raise ValueError(f"unsupported generation write_mode {write_mode!r}")

        with self._write_scope(commit=commit):
            existing = self.get_binding(run_id, column_id)
            claim = self._require_active_claim(
                run_id=run_id,
                column_id=column_id,
                claim_token=claim_token,
            )
            column = self.db.execute(
                "SELECT type,format,semantic_type FROM columns WHERE id=?", (column_id,)
            ).fetchone()
            if column is None:
                raise GenerationDeclarationConflict(
                    f"output column {column_id} disappeared before declaration"
                )
            current_descriptor = {
                "type": str(column["type"]),
                "format": column["format"],
                "semantic_type": column["semantic_type"],
            }
            declared_descriptor = (
                dict(current_descriptor)
                if target_descriptor is None
                else {
                    "type": str(target_descriptor["type"]),
                    "format": target_descriptor.get("format"),
                    "semantic_type": target_descriptor.get("semantic_type"),
                }
            )
            descriptor_change = current_descriptor != declared_descriptor
            operation = self._run_operation(run_id)
            operation_info = self._operation_info(operation)
            pending_changes = self._pending_descriptor_changes(operation_info)
            pending_change = pending_changes.get(str(column_id))
            if existing is not None:
                expected_open_state = (
                    "staged"
                    if defer_publication or write_mode == "replace_scope"
                    else "active"
                )
                if (
                    existing.output_role != output_role
                    or existing.compatibility_key != compatibility_key
                    or existing.write_mode != write_mode
                    or (
                        existing.state != "sealed"
                        and existing.state != expected_open_state
                    )
                ):
                    raise GenerationDeclarationConflict(
                        f"run {run_id} output column {column_id} was already "
                        "declared with different immutable facts"
                    )
                self._require_pointer_fence(
                    existing, current_head_run_id=claim["current_head_run_id"]
                )
                if descriptor_change:
                    expected_change = {
                        "before": current_descriptor,
                        "after": declared_descriptor,
                    }
                    if pending_change != expected_change:
                        raise GenerationDeclarationConflict(
                            f"run {run_id} recovered with changed pending descriptor "
                            f"replacement for column {column_id}"
                        )
                elif pending_change is not None:
                    raise GenerationDeclarationConflict(
                        f"run {run_id} recovered after column {column_id} descriptor drift"
                    )
                if existing.state == "sealed":
                    raise GenerationSealedError(
                        f"run {run_id} output column {column_id} is sealed"
                    )
                return existing

            if str(claim["run_status"]) != "running":
                raise GenerationStateError(
                    f"run {run_id} is {claim['run_status']!r}, not 'running'"
                )
            preexisting_result = self.db.execute(
                "SELECT 1 FROM results WHERE run_id=? LIMIT 1",
                (run_id,),
            ).fetchone()
            if preexisting_result is not None:
                raise GenerationDeclarationConflict(
                    f"run {run_id} output declaration set is already frozen because "
                    f"results were written before column {column_id} was declared"
                )
            publication_started = self.db.execute(
                "SELECT 1 FROM run_output_generations sibling "
                "WHERE sibling.run_id=? AND sibling.state='sealed' LIMIT 1",
                (run_id,),
            ).fetchone()
            if publication_started is not None:
                raise GenerationDeclarationConflict(
                    f"run {run_id} output declaration set is already frozen by "
                    "publication"
                )
            expected_base_run_id = (
                None
                if claim["expected_current_run_id"] is None
                else int(claim["expected_current_run_id"])
            )
            managed_history = self.db.execute(
                "SELECT run_id,compatibility_key FROM run_output_generations "
                "WHERE column_id=? ORDER BY run_id DESC LIMIT 1",
                (column_id,),
            ).fetchone()
            if write_mode == "create" and (
                expected_base_run_id not in (None, run_id)
                or managed_history is not None
            ):
                raise GenerationDeclarationConflict(
                    f"fresh output column {column_id} already has publication history"
                )
            prior_managed = (
                None
                if expected_base_run_id is None
                else self.get_binding(expected_base_run_id, column_id)
            )
            if write_mode == "replace_scope" and prior_managed is not None:
                if prior_managed.state != "sealed":
                    raise GenerationDeclarationConflict(
                        f"base generation ({expected_base_run_id}, {column_id}) "
                        "is not sealed"
                    )
                if (
                    prior_managed.compatibility_key != compatibility_key
                    and not descriptor_change
                ):
                    raise GenerationDeclarationConflict(
                        f"base generation ({expected_base_run_id}, {column_id}) "
                        "has a different compatibility key"
                    )
            # Exact per-cell heads are publication authority. The claim's
            # expected value is only their acquisition-time high-water mark;
            # columns.current_run_id is never consulted by this fence.
            descriptor_boundary_op_id = self.latest_descriptor_replacement_op_id(
                column_id
            )
            incompatible = self.db.execute(
                "SELECT generation.run_id FROM run_output_generations generation "
                "JOIN runs run ON run.id=generation.run_id "
                "JOIN ops op ON op.id=run.op_id "
                "WHERE generation.column_id=? AND generation.compatibility_key<>? "
                "AND "
                + _generation_publication_predicate(
                    generation_alias="generation", op_alias="op"
                )
                + (" AND run.op_id>=?" if descriptor_boundary_op_id is not None else "")
                + " LIMIT 1",
                (
                    (column_id, compatibility_key, descriptor_boundary_op_id)
                    if descriptor_boundary_op_id is not None
                    else (column_id, compatibility_key)
                ),
            ).fetchone()
            if incompatible is not None and not descriptor_change:
                raise GenerationDeclarationConflict(
                    f"output column {column_id} has incompatible managed history "
                    f"at run {int(incompatible['run_id'])}"
                )
            state = (
                "staged"
                if defer_publication or write_mode == "replace_scope"
                else "active"
            )
            if descriptor_change:
                if write_mode != "replace_scope":
                    raise GenerationDeclarationConflict(
                        "a descriptor-changing output must replace an existing generation"
                    )
                if operation is None or str(operation["status"]) != "applied":
                    raise GenerationStateError(
                        f"run {run_id} has no applied operation for descriptor replacement"
                    )
                try:
                    operation_spec = json.loads(operation["spec"] or "{}")
                except (TypeError, ValueError):
                    operation_spec = {}
                if (
                    not isinstance(operation_spec, dict)
                    or operation_spec.get("replace_existing") is not True
                    or "row_ids" in operation_spec
                ):
                    raise GenerationDeclarationConflict(
                        "descriptor-changing replacement requires an explicit all-rows request"
                    )
                if (
                    self.db.execute(
                        "SELECT 1 FROM cells WHERE column_id=? LIMIT 1", (column_id,)
                    ).fetchone()
                    is not None
                ):
                    raise GenerationDeclarationConflict(
                        f"output column {column_id} has protected source values"
                    )
            try:
                self.db.execute(
                    "INSERT INTO run_output_generations "
                    "(run_id,column_id,output_role,compatibility_key,write_mode,"
                    "state,claim_token,expected_base_run_id) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        column_id,
                        output_role,
                        compatibility_key,
                        write_mode,
                        state,
                        claim_token,
                        expected_base_run_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise GenerationDeclarationConflict(
                    f"run {run_id} output declaration conflicts with another role "
                    "or column"
                ) from exc
            created = self.get_binding(run_id, column_id)
            if created is None:  # pragma: no cover - insert/read lock backstop
                raise RuntimeError("declared output generation vanished")
            self._require_pointer_fence(
                created, current_head_run_id=claim["current_head_run_id"]
            )
            if descriptor_change:
                if pending_change is not None and pending_change != {
                    "before": current_descriptor,
                    "after": declared_descriptor,
                }:
                    raise GenerationDeclarationConflict(
                        f"run {run_id} changed its pending descriptor replacement"
                    )
                pending_changes[str(column_id)] = {
                    "before": current_descriptor,
                    "after": declared_descriptor,
                }
                operation_info[PENDING_DESCRIPTOR_REPLACEMENT_KEY] = pending_changes
                self.db.execute(
                    "UPDATE ops SET undo_info=? WHERE id=?",
                    (json.dumps(operation_info), int(operation["id"])),
                )
            return created

    def _upsert_head_uncommitted(
        self, *, run_id: int, row_id: int, column_id: int
    ) -> int:
        cursor = self.db.execute(
            "INSERT INTO cell_result_heads (column_id,row_id,run_id) "
            "SELECT column_id,row_id,run_id FROM results "
            "WHERE run_id=? AND row_id=? AND column_id=? "
            "AND publication_effect IS NOT NULL "
            "ON CONFLICT(column_id,row_id) DO UPDATE SET run_id=excluded.run_id "
            "WHERE excluded.run_id > cell_result_heads.run_id",
            (int(run_id), int(row_id), int(column_id)),
        )
        return int(cursor.rowcount)

    def _project_written_results_uncommitted(
        self,
        run_id: int,
        batch: Collection[dict[str, Any]],
        *,
        claim_token: str | None,
    ) -> int:
        """Managed write sink hook; caller already owns the result transaction."""

        keys = {
            (int(item["row_id"]), int(item["column_id"]))
            for item in batch
            if item.get("publication_effect") is not None
        }
        if not keys:
            return 0
        active_keys: list[tuple[int, int]] = []
        for row_id, column_id in sorted(keys):
            binding = self.get_binding(int(run_id), column_id)
            if binding is None:
                # The schema trigger should have refused the result insert;
                # keep a precise store error if a custom SQLite build did not.
                raise GenerationStateError(
                    f"run {run_id} output column {column_id} is undeclared"
                )
            if binding.state == "active":
                active_keys.append((row_id, column_id))
            elif binding.state != "staged":
                raise GenerationSealedError(
                    f"run {run_id} output column {column_id} is sealed"
                )
        if not active_keys:
            return 0
        if claim_token is None:
            raise GenerationClaimError("active result publication has no claim token")
        # RunResultStore already fenced every batch column under this claim;
        # this method is inside that same savepoint and only projects the exact
        # rows whose effect was just written.
        projected = sum(
            self._upsert_head_uncommitted(
                run_id=int(run_id), row_id=row_id, column_id=column_id
            )
            for row_id, column_id in active_keys
        )
        refresh_current_cell_pairs(self.db, active_keys)
        return projected

    def _upsert_generation_heads_uncommitted(
        self, *, run_id: int, column_id: int
    ) -> int:
        cursor = self.db.execute(
            "INSERT INTO cell_result_heads (column_id,row_id,run_id) "
            "SELECT column_id,row_id,run_id FROM results "
            "WHERE run_id=? AND column_id=? AND publication_effect IS NOT NULL "
            "ON CONFLICT(column_id,row_id) DO UPDATE SET run_id=excluded.run_id "
            "WHERE excluded.run_id > cell_result_heads.run_id",
            (int(run_id), int(column_id)),
        )
        return int(cursor.rowcount)

    def _has_complete_publication_effects(
        self, run_id: int, column_ids: Collection[int]
    ) -> bool:
        if (
            self.db.execute(
                "SELECT 1 FROM run_scopes WHERE run_id=?", (int(run_id),)
            ).fetchone()
            is None
        ):
            return False
        return not any(
            self.db.execute(
                "SELECT 1 FROM run_rows scope WHERE scope.run_id=? "
                "AND NOT EXISTS (SELECT 1 FROM results result "
                "WHERE result.run_id=scope.run_id AND result.row_id=scope.row_id "
                "AND result.column_id=? AND result.publication_effect IS NOT NULL) "
                "LIMIT 1",
                (int(run_id), int(column_id)),
            ).fetchone()
            is not None
            for column_id in column_ids
        )

    def _finish_pending_descriptor_replacement(
        self,
        *,
        run_id: int,
        publish: bool,
    ) -> set[int]:
        """Promote or abandon the descriptor half of a staged replacement."""

        operation = self._run_operation(run_id)
        info = self._operation_info(operation)
        pending = self._pending_descriptor_changes(info)
        if not pending or operation is None:
            return set()
        changed_column_ids = {int(column_id) for column_id in pending}
        if publish:
            for column_id, change in pending.items():
                current = self.db.execute(
                    "SELECT type,format,semantic_type FROM columns WHERE id=?",
                    (int(column_id),),
                ).fetchone()
                observed = (
                    None
                    if current is None
                    else {
                        "type": str(current["type"]),
                        "format": current["format"],
                        "semantic_type": current["semantic_type"],
                    }
                )
                if observed != change["before"]:
                    raise GenerationClaimError(
                        f"replacement output column {column_id} changed descriptor "
                        "before publication"
                    )
            for column_id, change in pending.items():
                self.db.execute(
                    "UPDATE columns SET type=?,format=?,semantic_type=? WHERE id=?",
                    (
                        change["after"]["type"],
                        change["after"]["format"],
                        change["after"]["semantic_type"],
                        int(column_id),
                    ),
                )
                info.setdefault("column_types", {}).setdefault(
                    column_id, change["before"]["type"]
                )
                info.setdefault("column_types_after", {})[column_id] = change["after"][
                    "type"
                ]
                info.setdefault("column_formats", {}).setdefault(
                    column_id, change["before"]["format"]
                )
                info.setdefault("column_formats_after", {})[column_id] = change[
                    "after"
                ]["format"]
                info.setdefault("column_semantic_types", {}).setdefault(
                    column_id, change["before"]["semantic_type"]
                )
                info.setdefault("column_semantic_types_after", {})[column_id] = change[
                    "after"
                ]["semantic_type"]

            # Columns born beside the replacement were hidden when the group
            # was declared. Reveal each to its frozen declared visibility in
            # the same transaction as descriptor and head publication.
            created = {int(column_id) for column_id in info.get("created_columns", ())}
            if created:
                claims = self.db.execute(
                    "SELECT column_id,details FROM output_column_claims "
                    "WHERE run_id=? AND status='active'",
                    (int(run_id),),
                ).fetchall()
                visibility: dict[int, bool] = {}
                for claim in claims:
                    if claim["column_id"] is None:
                        continue
                    try:
                        details = json.loads(claim["details"] or "{}")
                    except (TypeError, ValueError):
                        details = {}
                    for field in details.get("output_fields", ()):
                        if not isinstance(field, dict):
                            continue
                        column = self.db.execute(
                            "SELECT id FROM columns WHERE id=? AND name=?",
                            (int(claim["column_id"]), field.get("name")),
                        ).fetchone()
                        if column is not None:
                            visibility[int(column["id"])] = bool(field.get("hidden"))
                for column_id in created:
                    if column_id in visibility:
                        self.db.execute(
                            "UPDATE columns SET hidden=? WHERE id=?",
                            (int(visibility[column_id]), column_id),
                        )
        info.pop(PENDING_DESCRIPTOR_REPLACEMENT_KEY, None)
        self.db.execute(
            "UPDATE ops SET undo_info=? WHERE id=?",
            (json.dumps(info), int(operation["id"])),
        )
        return changed_column_ids if publish else set()

    def _record_unpublished_generations(
        self, *, run_id: int, column_ids: Collection[int]
    ) -> None:
        operation = self._run_operation(run_id)
        if operation is None or not column_ids:
            return
        info = self._operation_info(operation)
        unpublished = {
            int(column_id) for column_id in info.get(UNPUBLISHED_GENERATIONS_KEY, ())
        }
        unpublished.update(int(column_id) for column_id in column_ids)
        info[UNPUBLISHED_GENERATIONS_KEY] = sorted(unpublished)
        self.db.execute(
            "UPDATE ops SET undo_info=? WHERE id=?",
            (json.dumps(info), int(operation["id"])),
        )

    def seal(
        self,
        run_id: int,
        column_ids: Collection[int],
        *,
        claim_token: str,
        terminal_disposition: str,
        allow_stale_output_name: bool = False,
        publish: bool = True,
        commit: bool = True,
    ) -> int:
        """Terminalize outputs, optionally aborting a coupled publication.

        Aborted generations retain their checkpoints but never become heads.
        Already sealed members cannot be published by a later retry.
        """

        if terminal_disposition not in TERMINAL_DISPOSITIONS:
            raise ValueError(
                f"unsupported terminal disposition {terminal_disposition!r}"
            )
        ordered_column_ids = sorted({int(column_id) for column_id in column_ids})
        with self._write_scope(commit=commit):
            declared_bindings = self.bindings_for_run(int(run_id))
            declared_column_ids = sorted(
                binding.column_id for binding in declared_bindings
            )
            if ordered_column_ids != declared_column_ids:
                raise GenerationStateError(
                    f"run {run_id} seal requires its complete declared output set "
                    f"{declared_column_ids}, not {ordered_column_ids}"
                )
            if not ordered_column_ids:
                return 0
            open_bindings: list[RunOutputGeneration] = []
            for column_id in ordered_column_ids:
                binding = self.get_binding(int(run_id), column_id)
                if binding is None:
                    raise GenerationStateError(
                        f"run {run_id} output column {column_id} is undeclared"
                    )
                if binding.state == "sealed":
                    if binding.terminal_disposition != terminal_disposition:
                        raise GenerationStateError(
                            f"run {run_id} output column {column_id} was sealed as "
                            f"{binding.terminal_disposition!r}"
                        )
                    continue
                claim = self._require_active_claim(
                    run_id=int(run_id),
                    column_id=column_id,
                    claim_token=claim_token,
                    require_current_name=not allow_stale_output_name,
                )
                self._require_pointer_fence(
                    binding, current_head_run_id=claim["current_head_run_id"]
                )
                open_bindings.append(binding)

            # Sealing is idempotent but its original publish decision is not
            # mutable: a recovery call cannot publish an aborted generation
            # or retroactively mark a published one as unpublished.
            if not open_bindings:
                return 0

            operation = self._run_operation(int(run_id))
            operation_info = self._operation_info(operation)
            pending_descriptor_changes = self._pending_descriptor_changes(
                operation_info
            )
            publish_generation = bool(publish)
            if pending_descriptor_changes and publish_generation:
                publish_generation = terminal_disposition in {
                    "completed",
                    "partial",
                } and self._has_complete_publication_effects(
                    int(run_id), declared_column_ids
                )

            for binding in open_bindings:
                updated = self.db.execute(
                    "UPDATE run_output_generations SET state='sealed', "
                    "terminal_disposition=?, sealed_at=datetime('now') "
                    "WHERE run_id=? AND column_id=? AND state IN ('active','staged')",
                    (
                        terminal_disposition,
                        int(run_id),
                        binding.column_id,
                    ),
                )
                if updated.rowcount != 1:  # pragma: no cover - write lock backstop
                    raise GenerationStateError(
                        f"run {run_id} output column {binding.column_id} "
                        "changed while sealing"
                    )

            changed_descriptors = self._finish_pending_descriptor_replacement(
                run_id=int(run_id),
                publish=publish_generation and bool(open_bindings),
            )
            if not publish_generation:
                self._record_unpublished_generations(
                    run_id=int(run_id), column_ids=declared_column_ids
                )
                return 0
            for column_id in changed_descriptors:
                self.db.execute(
                    "DELETE FROM cell_result_heads WHERE column_id=?",
                    (int(column_id),),
                )
            projected = sum(
                self._upsert_generation_heads_uncommitted(
                    run_id=int(run_id), column_id=binding.column_id
                )
                for binding in open_bindings
            )
            refresh_current_cells(
                self.db,
                column_ids={
                    *changed_descriptors,
                    *(binding.column_id for binding in open_bindings),
                },
            )
            return projected

    def read_cell_heads(
        self, column_id: int, row_ids: Collection[int] | None = None
    ) -> dict[int, PublishedCellHead]:
        ordered_row_ids = (
            None if row_ids is None else sorted({int(row_id) for row_id in row_ids})
        )
        if ordered_row_ids == []:
            return {}
        batches: Iterable[list[int] | None]
        batches = [None] if ordered_row_ids is None else _chunks(ordered_row_ids)
        heads: dict[int, PublishedCellHead] = {}
        for row_id_batch in batches:
            params: list[int] = [int(column_id)]
            row_filter = ""
            if row_id_batch is not None:
                placeholders = ",".join("?" for _ in row_id_batch)
                row_filter = f" AND head.row_id IN ({placeholders})"
                params.extend(row_id_batch)
            rows = self.db.execute(
                "SELECT head.run_id,head.row_id,head.column_id,"
                "result.publication_effect,result.value,result.error,"
                "result.error_code,result.outcome,result.review_state,"
                "result.confidence,result.justification,run.op_id "
                "FROM cell_result_heads head "
                "JOIN results result ON result.run_id=head.run_id "
                "AND result.row_id=head.row_id "
                "AND result.column_id=head.column_id "
                "JOIN runs run ON run.id=head.run_id "
                "WHERE head.column_id=?" + row_filter + " ORDER BY head.row_id",
                params,
            ).fetchall()
            for row in rows:
                effect = str(row["publication_effect"])
                row_id = int(row["row_id"])
                heads[row_id] = PublishedCellHead(
                    run_id=int(row["run_id"]),
                    row_id=row_id,
                    column_id=int(row["column_id"]),
                    publication_effect=effect,
                    value=decode_published_result_value(effect, row["value"]),
                    error=None if row["error"] is None else str(row["error"]),
                    error_code=(
                        None if row["error_code"] is None else str(row["error_code"])
                    ),
                    outcome=str(row["outcome"]),
                    review_state=str(row["review_state"]),
                    confidence=(
                        None if row["confidence"] is None else float(row["confidence"])
                    ),
                    justification=(
                        None
                        if row["justification"] is None
                        else str(row["justification"])
                    ),
                    op_id=int(row["op_id"]),
                )
        return heads

    def origin_run_ids(
        self,
        column_id: int,
        row_ids: Collection[int] | None = None,
        *,
        limit: int = 2,
    ) -> list[int]:
        if limit < 1:
            raise ValueError("origin limit must be positive")
        ordered_row_ids = (
            None if row_ids is None else sorted({int(row_id) for row_id in row_ids})
        )
        if ordered_row_ids == []:
            return []
        if ordered_row_ids is None:
            rows = self.db.execute(
                "SELECT DISTINCT run_id FROM cell_result_heads "
                "WHERE column_id=? ORDER BY run_id DESC LIMIT ?",
                (int(column_id), int(limit)),
            ).fetchall()
            return [int(row["run_id"]) for row in rows]

        origins: set[int] = set()
        for row_id_batch in _chunks(ordered_row_ids):
            placeholders = ",".join("?" for _ in row_id_batch)
            rows = self.db.execute(
                "SELECT DISTINCT run_id FROM cell_result_heads "
                f"WHERE column_id=? AND row_id IN ({placeholders}) "
                "ORDER BY run_id DESC LIMIT ?",
                (int(column_id), *row_id_batch, int(limit)),
            ).fetchall()
            origins.update(int(row["run_id"]) for row in rows)
        return sorted(origins, reverse=True)[:limit]

    def has_mixed_origins(
        self, column_id: int, row_ids: Collection[int] | None = None
    ) -> bool:
        return len(self.origin_run_ids(column_id, row_ids, limit=2)) > 1

    def rebuild_heads(
        self,
        column_ids: Collection[int] | None = None,
        *,
        commit: bool = True,
    ) -> int:
        """Rebuild heads from applied journal state and immutable effects."""

        ordered_column_ids = (
            None
            if column_ids is None
            else sorted({int(column_id) for column_id in column_ids})
        )
        if ordered_column_ids == []:
            return 0
        with self._write_scope(commit=commit):
            affected_column_ids = (
                [
                    int(row["column_id"])
                    for row in self.db.execute(
                        "SELECT DISTINCT column_id FROM cell_result_heads "
                        "UNION SELECT DISTINCT column_id FROM run_output_generations"
                    ).fetchall()
                ]
                if ordered_column_ids is None
                else ordered_column_ids
            )
            params: tuple[int, ...] = ()
            filter_sql = ""
            if ordered_column_ids is None:
                self.db.execute("DELETE FROM cell_result_heads")
            else:
                placeholders = ",".join("?" for _ in ordered_column_ids)
                params = tuple(ordered_column_ids)
                self.db.execute(
                    f"DELETE FROM cell_result_heads WHERE column_id IN ({placeholders})",
                    params,
                )
                filter_sql = f" AND result.column_id IN ({placeholders})"

            self.db.execute(
                "WITH ranked AS ("
                "SELECT result.column_id,result.row_id,result.run_id,"
                "ROW_NUMBER() OVER ("
                "PARTITION BY result.column_id,result.row_id "
                "ORDER BY result.run_id DESC"
                ") AS precedence "
                "FROM results result "
                "JOIN run_output_generations generation "
                "ON generation.run_id=result.run_id "
                "AND generation.column_id=result.column_id "
                "JOIN runs run ON run.id=generation.run_id "
                "JOIN ops op ON op.id=run.op_id "
                "WHERE result.publication_effect IS NOT NULL "
                "AND "
                + _generation_publication_predicate(
                    generation_alias="generation", op_alias="op"
                )
                + filter_sql
                + ") "
                "INSERT INTO cell_result_heads (column_id,row_id,run_id) "
                "SELECT column_id,row_id,run_id FROM ranked WHERE precedence=1",
                params,
            )
            boundary_columns = affected_column_ids
            for column_id in boundary_columns:
                boundary_op_id = self.latest_descriptor_replacement_op_id(column_id)
                if boundary_op_id is None:
                    continue
                self.db.execute(
                    "DELETE FROM cell_result_heads WHERE column_id=? AND run_id IN ("
                    "SELECT run.id FROM runs run WHERE run.op_id<?)",
                    (int(column_id), int(boundary_op_id)),
                )
            if affected_column_ids:
                refresh_current_cells(self.db, column_ids=affected_column_ids)
            if ordered_column_ids is None:
                count = self.db.execute(
                    "SELECT COUNT(*) FROM cell_result_heads"
                ).fetchone()[0]
            else:
                placeholders = ",".join("?" for _ in ordered_column_ids)
                count = self.db.execute(
                    f"SELECT COUNT(*) FROM cell_result_heads "
                    f"WHERE column_id IN ({placeholders})",
                    tuple(ordered_column_ids),
                ).fetchone()[0]
            return int(count)
