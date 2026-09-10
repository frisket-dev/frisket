"""Snapshot admission and item identity for list-table producers."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from typing import Any

from frisket.actions.types import (
    ListColumnSource,
    ListItem,
    ListTableSource,
    NamedListSource,
    RowSource,
    TableError,
)
from frisket.contracts.action import Receipt
from frisket.engine.executor.action_support import _json_schema_error
from frisket.engine.executor.recordsets import (
    DERIVE_TABLE_FROM_LIST,
    is_feedable_named_result_ref,
    json_schema_equal,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.evidence import find_item_evidence_link


class AdmittedListTableReader:
    def __init__(self, project: Any, action_kind: str = "derive.table_from_list"):
        self.project = project
        self.action_kind = action_kind
        self.sources: set[RowSource] = set()
        self.item_associations: dict[RowSource, dict[str, Any]] = {}
        self.parent_sheet_id: int | None = None
        self.source_ai_generated = False
        self.facts: list[dict[str, Any]] = []

    def read(
        self, source: ListTableSource, *, item_schema: dict[str, Any] | None = None
    ) -> tuple[ListItem, ...]:
        if not isinstance(source, (ListColumnSource, NamedListSource)):
            raise TableError("invalid_input_ref", "Expected an admitted list source")
        if isinstance(source, NamedListSource):
            if not isinstance(item_schema, dict) or not isinstance(
                item_schema.get("type"), str
            ):
                raise TableError(
                    "invalid_item_schema", "Named results require item_schema"
                )
        elif item_schema is not None:
            raise TableError(
                "invalid_item_schema", "Live columns do not accept item_schema"
            )
        if self.parent_sheet_id not in (None, source.sheet_id):
            raise TableError(
                "invalid_input_ref", "A list table must have one parent sheet"
            )

        # Refresh already owns a write transaction and must read its own snapshot.
        snapshot = (
            nullcontext(self.project)
            if self.project.db.in_transaction
            else self.project.read_snapshot()
        )
        with snapshot as project:
            sheet = project.db.execute(
                "SELECT id FROM sheets WHERE id=? AND hidden=0", (source.sheet_id,)
            ).fetchone()
            column = project.db.execute(
                "SELECT * FROM columns WHERE id=? AND sheet_id=?",
                (source.column_id, source.sheet_id),
            ).fetchone()
            if sheet is None or column is None or column["type"] != "json":
                raise TableError(
                    "invalid_input_ref",
                    "Source must be a JSON column on a visible sheet",
                )
            receipt_id = None
            source_ref = None
            value_refs = {}
            if isinstance(source, NamedListSource):
                row_ids, values, receipt_id, source_ref = self._named_values(
                    project, source, item_schema
                )
            else:
                row_ids = project.visible_row_ids(source.sheet_id)
                values, value_refs = project.get_values_with_refs(
                    source.sheet_id, source.column_id
                )

            entries = []
            associations = {}
            for row_id in row_ids:
                value = values.get(row_id)
                if value is None and isinstance(source, ListColumnSource):
                    continue
                if not isinstance(value, list):
                    raise TableError(
                        "source_not_list",
                        "Source cells must be lists",
                        details={"row_id": row_id},
                    )
                for index, item in enumerate(value):
                    if item_schema is not None:
                        error = _json_schema_error(item, item_schema)
                        if error is not None:
                            raise TableError(
                                "invalid_item_schema",
                                "Source item does not match item_schema",
                                details={
                                    "row_id": row_id,
                                    "item_index": index,
                                    "error": error,
                                },
                            )
                    token = RowSource(sheet_id=source.sheet_id, row_id=row_id)
                    entries.append(ListItem(value=item, source=token))
                    # Match the extract writer's exact item-hash encoding.
                    # Manual edits have no run authority, but unchanged items
                    # can still retain matching same-index/value grounding.
                    value_hash = (
                        "sha256:"
                        + hashlib.sha256(
                            json.dumps(item, sort_keys=True).encode("utf-8")
                        ).hexdigest()
                    )
                    associations[token] = {
                        "source_sheet_id": source.sheet_id,
                        "source_column_id": source.column_id,
                        "source_row_id": row_id,
                        "item_index": index,
                        "evidence_link": find_item_evidence_link(
                            project,
                            row_id=row_id,
                            column_id=source.column_id,
                            item_index=index,
                            value_hash=value_hash,
                            run_id=source.run_id
                            if isinstance(source, NamedListSource)
                            else value_refs.get(row_id, {}).get("run_id"),
                        ),
                    }
            if not entries and isinstance(source, ListColumnSource):
                raise TableError("invalid_input_ref", "Source column has no list items")
            try:
                encoded = json.dumps(
                    [(row_id, values.get(row_id)) for row_id in row_ids],
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            except (ValueError, TypeError) as exc:
                raise TableError(
                    "invalid_item_schema", "Source values must be JSON"
                ) from exc
            fact = {
                "kind": "list_table_read",
                "source": source.model_dump(mode="json", by_alias=True),
                "source_row_ids": row_ids,
                "item_count": len(entries),
                "source_values_hash": "sha256:" + hashlib.sha256(encoded).hexdigest(),
                "source_ai_generated": bool(column["ai_generated"]),
            }
            if receipt_id is not None:
                fact["source_receipt_id"] = receipt_id
                fact["named_result"] = dict(source_ref)
            self.parent_sheet_id = source.sheet_id
            self.source_ai_generated = bool(column["ai_generated"])
            self.sources.update(associations)
            self.item_associations.update(associations)
            self.facts.append(fact)
            return tuple(entries)

    @staticmethod
    def _named_values(
        project: Any, source: NamedListSource, item_schema: dict[str, Any]
    ) -> tuple[list[int], dict[int, Any], str, dict[str, Any]]:
        run = project.db.execute(
            "SELECT * FROM runs WHERE id=? AND sheet_id=? AND status='completed'",
            (source.run_id, source.sheet_id),
        ).fetchone()
        if run is None:
            raise TableError(
                "invalid_input_ref", "Source run must be completed on the source sheet"
            )
        receipt = None
        source_ref = None
        try:
            producer_spec = json.loads(run["params"] or "{}")
        except (TypeError, ValueError):
            producer_spec = None
        if not isinstance(producer_spec, dict):
            producer_spec = None
        for body in ReceiptStore(project).bodies_for_run_status(
            source.run_id, "completed"
        ):
            candidate = Receipt.model_validate_json(body)
            if int(run["op_id"]) not in candidate.op_ids:
                continue
            producer_kind = candidate.action_kind
            producer_params_hash = candidate.params_hash
            for item in candidate.evidence:
                if (
                    item.ref.get("kind") == "backfill_source_generation"
                    and item.ref.get("successor_run_id") == source.run_id
                    and item.ref.get("source_action_kind") == run["action_kind"]
                ):
                    producer_kind = str(run["action_kind"])
                    producer_params_hash = item.ref.get("successor_request_hash")
                    break
            for output in candidate.outputs:
                if is_feedable_named_result_ref(
                    output.ref,
                    target=DERIVE_TABLE_FROM_LIST,
                    receipt_action_kind=producer_kind,
                    sheet_id=source.sheet_id,
                    column_id=source.column_id,
                    run_id=source.run_id,
                    op_id=int(run["op_id"]),
                    route=source.route,
                    schema_name=source.schema_name,
                    producer_spec=producer_spec,
                    producer_params_hash=producer_params_hash,
                ):
                    receipt, source_ref = candidate, output.ref
                    break
            if receipt is not None:
                break
        if receipt is None or source_ref is None:
            raise TableError(
                "invalid_input_ref",
                "Source receipt does not allow list-table derivation",
            )
        if not json_schema_equal(source_ref.get("item_schema"), item_schema):
            raise TableError(
                "invalid_item_schema",
                "item_schema differs from the admitted named result",
            )
        row_ids = source_ref["row_ids"]
        if any(type(row_id) is not int or row_id <= 0 for row_id in row_ids) or len(
            row_ids
        ) != len(set(row_ids)):
            raise TableError(
                "invalid_input_ref", "Source receipt row membership is invalid"
            )
        existing = {
            int(row["id"])
            for row in project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=?", (source.sheet_id,)
            ).fetchall()
        }
        if not set(row_ids) <= existing:
            raise TableError(
                "invalid_input_ref", "Source receipt rows do not belong to its sheet"
            )
        values = {}
        rows = project.db.execute(
            "SELECT row_id, value, error FROM results WHERE run_id=? AND column_id=?",
            (source.run_id, source.column_id),
        ).fetchall()
        wanted = set(row_ids)
        errored = []
        for row in rows:
            row_id = int(row["row_id"])
            if row_id not in wanted:
                continue
            if row["error"] is not None:
                errored.append(row_id)
            else:
                values[row_id] = (
                    json.loads(row["value"]) if row["value"] is not None else None
                )
        missing = sorted(wanted - set(values) - set(errored))
        if missing or errored:
            raise TableError(
                "invalid_input_ref",
                "Source result cells are missing or errored",
                details={"missing": missing, "errored": sorted(errored)},
            )
        return list(row_ids), values, receipt.receipt_id, source_ref
