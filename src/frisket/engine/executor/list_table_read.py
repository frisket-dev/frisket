"""Snapshot admission and item identity for list-table producers."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext, ExitStack, closing
from typing import Any, Sequence
from weakref import WeakSet, WeakKeyDictionary

from frisket.actions.types import (
    ListColumnSource,
    ListCopiedColumn,
    ListItem,
    ListParentColumn,
    ListTableSource,
    NamedListSource,
    RowSource,
    TableError,
)
from frisket.contracts.action import Receipt
from frisket.engine.executor.action_support import _json_schema_error
from frisket.engine.executor.import_read_guard import require_action_sheet_readable
from frisket.engine.executor.recordsets import (
    DERIVE_TABLE_FROM_LIST,
    is_feedable_named_result_ref,
    json_schema_equal,
)
from frisket.engine.store.evidence import find_item_citation
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore


class AdmittedListTableReader:
    def __init__(self, project: Any, action_kind: str = "derive.table_from_list"):
        self.project = project
        self.action_kind = action_kind
        self.sources: WeakSet[RowSource] = WeakSet()
        self.item_associations: WeakKeyDictionary[RowSource, dict[str, Any]] = (
            WeakKeyDictionary()
        )
        self._stages = []
        self.parent_sheet_id: int | None = None
        self.source_ai_generated = False
        self.copied_columns: tuple[ListCopiedColumn, ...] = ()
        self.facts: list[dict[str, Any]] = []
        self.require_complete = False
        self.consumed_captures: list[dict[str, Any]] = []

    def read(
        self,
        source: ListTableSource,
        *,
        item_schema: dict[str, Any] | None = None,
        parent_columns: Sequence[ListParentColumn] = (),
    ) -> Sequence[ListItem]:
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
        require_action_sheet_readable(self.project, source.sheet_id)
        if self.parent_sheet_id not in (None, source.sheet_id):
            raise TableError(
                "invalid_input_ref", "A list table must have one parent sheet"
            )

        from frisket.engine.executor.table_read_stage import ListItemStage
        from frisket.engine.store.consumed_sources import (
            capture_retained_results,
        )

        from frisket.engine.store.consumed_source_pages import (
            iter_consumed_source_pages,
        )

        snapshot = (
            nullcontext(self.project)
            if self.project.db.in_transaction
            else self.project.read_snapshot()
        )
        stage = ListItemStage(self, source.sheet_id)
        self._stages.append(stage)
        try:
            with snapshot as project, ExitStack() as reads:
                sheet = project.db.execute(
                    "SELECT id FROM sheets WHERE id=? AND hidden=0", (source.sheet_id,)
                ).fetchone()
                column = project.db.execute(
                    "SELECT * FROM columns WHERE id=? AND sheet_id=? AND active=1",
                    (source.column_id, source.sheet_id),
                ).fetchone()
                if sheet is None or column is None or column["type"] != "json":
                    raise TableError(
                        "invalid_input_ref",
                        "Source must be a JSON column on a visible sheet",
                    )
                copied_columns, copied_source_facts = [], []
                for requested in parent_columns:
                    parent_column = project.db.execute(
                        "SELECT id,name,type,format,ai_generated FROM columns WHERE id=? AND sheet_id=? AND active=1",
                        (requested.column_id, source.sheet_id),
                    ).fetchone()
                    if parent_column is None or requested.column_id == source.column_id:
                        raise TableError(
                            "invalid_input_ref",
                            "Selected parent column must be an active non-source column on the list's sheet",
                            details={"column_id": requested.column_id},
                        )
                    copied_columns.append(
                        ListCopiedColumn(
                            name=requested.output_name,
                            type=str(parent_column["type"]),
                            format=parent_column["format"],
                            ai_generated=bool(parent_column["ai_generated"]),
                        )
                    )
                    copied_source_facts.append(
                        {
                            **requested.model_dump(mode="json"),
                            "source_name": str(parent_column["name"]),
                            "type": str(parent_column["type"]),
                            "format": parent_column["format"],
                            "ai_generated": bool(parent_column["ai_generated"]),
                        }
                    )
                receipt_id, source_ref = None, None
                if isinstance(source, NamedListSource):
                    assert item_schema is not None
                    row_ids, receipt_id, source_ref = self._named_source(
                        project, source, item_schema
                    )
                    self.consumed_captures.append(
                        capture_retained_results(
                            project,
                            source.sheet_id,
                            source.column_id,
                            source.run_id,
                            row_ids,
                        )
                    )
                    pages = self._named_pages(project, source, row_ids, parent_columns)
                else:
                    pages = iter_consumed_source_pages(
                        project,
                        source.sheet_id,
                        [source.column_id, *[c.column_id for c in parent_columns]],
                        captures=self.consumed_captures,
                    )
                pages = reads.enter_context(closing(pages))
                source_digest, parent_digest = hashlib.sha256(), hashlib.sha256()
                source_digest.update(b"[")
                parent_digest.update(b"[")
                row_ids = []
                for page, values, refs in pages:
                    positions = {
                        int(r[0]): int(r[1])
                        for r in project.db.execute(
                            "SELECT id,position FROM rows WHERE id IN (SELECT value FROM json_each(?))",
                            (json.dumps(page),),
                        )
                    }
                    for row_id in page:
                        current_ref = refs.get(source.column_id, {}).get(row_id, {})
                        if self.require_complete and (
                            current_ref.get("outcome") not in (None, "completed")
                            or current_ref.get("validity") == "invalid"
                        ):
                            raise TableError(
                                "invalid_input_ref",
                                "Source list is unavailable; refresh cannot infer that its previous items disappeared",
                                details={
                                    "row_id": row_id,
                                    "reason": "upstream_unavailable",
                                },
                            )
                        value = values[source.column_id].get(row_id)
                        parents = {
                            c.output_name: values.get(c.column_id, {}).get(row_id)
                            for c in parent_columns
                        }
                        if row_ids:
                            source_digest.update(b",")
                            parent_digest.update(b",")
                        source_digest.update(
                            json.dumps(
                                [row_id, value],
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ).encode()
                        )
                        parent_digest.update(
                            json.dumps(
                                [row_id, parents],
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ).encode()
                        )
                        row_ids.append(row_id)
                        if value is None and isinstance(source, ListColumnSource):
                            continue
                        if not isinstance(value, list):
                            raise TableError(
                                "source_not_list",
                                "Source cells must be lists",
                                details={"row_id": row_id},
                            )
                        for index, item in enumerate(value):
                            if (
                                item_schema is not None
                                and (error := _json_schema_error(item, item_schema))
                                is not None
                            ):
                                raise TableError(
                                    "invalid_item_schema",
                                    "Source item does not match item_schema",
                                    details={
                                        "row_id": row_id,
                                        "item_index": index,
                                        "error": error,
                                    },
                                )
                            value_hash = (
                                "sha256:"
                                + hashlib.sha256(
                                    json.dumps(item, sort_keys=True).encode("utf-8")
                                ).hexdigest()
                            )
                            stage.add(
                                row_id,
                                item,
                                parents,
                                {
                                    "source_sheet_id": source.sheet_id,
                                    "source_column_id": source.column_id,
                                    "source_row_id": row_id,
                                    "item_index": index,
                                    "evidence_link": find_item_citation(
                                        project,
                                        row_id=row_id,
                                        column_id=source.column_id,
                                        item_index=index,
                                        value_hash=value_hash,
                                        run_id=source.run_id
                                        if isinstance(source, NamedListSource)
                                        else current_ref.get("run_id"),
                                    ),
                                },
                                position=positions[row_id],
                            )
                source_digest.update(b"]")
                parent_digest.update(b"]")
                fact = {
                    "kind": "list_table_read",
                    "source": source.model_dump(mode="json", by_alias=True),
                    "source_row_ids": row_ids,
                    "item_count": len(stage),
                    "source_values_hash": "sha256:" + source_digest.hexdigest(),
                    "source_ai_generated": bool(column["ai_generated"]),
                }
                if parent_columns:
                    fact.update(
                        parent_columns=copied_source_facts,
                        parent_source_row_ids=row_ids,
                        parent_values_hash="sha256:" + parent_digest.hexdigest(),
                    )
                if receipt_id is not None and source_ref is not None:
                    fact.update(
                        source_receipt_id=receipt_id, named_result=dict(source_ref)
                    )
                self.parent_sheet_id = source.sheet_id
                self.source_ai_generated = bool(column["ai_generated"])
                self.copied_columns = tuple(copied_columns)
                self.facts.append(fact)
            stage.seal()
            return stage
        except BaseException:
            stage.close()
            raise

    def close(self):
        for stage in self._stages:
            stage.close()
        self._stages.clear()

    def _named_pages(self, project, source, row_ids, parent_columns):
        from frisket.engine.store.consumed_sources import read_consumed_source

        for offset in range(0, len(row_ids), 256):
            page = row_ids[offset : offset + 256]
            refs = {}
            values = {}
            if parent_columns:
                _, values, capture = read_consumed_source(
                    project,
                    source.sheet_id,
                    [c.column_id for c in parent_columns],
                    row_ids=page,
                    scope_intent="selected",
                    refs_out=refs,
                )
                self.consumed_captures.append(capture)
            rows = RunResultStore(project).decoded_result_rows(
                [(source.run_id, rid, source.column_id) for rid in page]
            )
            results = {}
            for rid in page:
                row = rows.get((source.run_id, rid, source.column_id))
                if row is None or row["error"] is not None:
                    raise TableError(
                        "invalid_input_ref",
                        "Source result cells are missing or errored",
                        details={"row_id": rid},
                    )
                results[rid] = row["value"]
            values[source.column_id] = results
            yield page, values, refs

    @staticmethod
    def _named_source(
        project: Any, source: NamedListSource, item_schema: dict[str, Any]
    ) -> tuple[list[int], str, dict[str, Any]]:
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
        return list(row_ids), receipt.receipt_id, source_ref
