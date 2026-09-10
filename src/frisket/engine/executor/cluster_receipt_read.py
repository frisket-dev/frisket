"""Snapshot admission and publication fences for completed cluster receipts."""

import json
from contextlib import nullcontext
from typing import Any

from frisket.actions.core import _ProjectAction
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.entity_types import (
    ClusterReceiptSource,
    EntityCluster,
    EntityVariant,
)
from frisket.actions.types import RowSource, TableError
from frisket.engine.executor.action_receipts import _receipt_ops_are_applied
from frisket.engine.store.artifact_timeline import canonical_json_hash
from frisket.engine.store.receipts import ReceiptStore
from frisket.sdk.replay import output_column_value_hash, output_columns_replay_error


def _published_clusters(project: Any, receipt_id: str):
    """Read actual capability facts tied to this producer's accepted output."""
    from frisket.actions.cluster_types import ValueClusterer

    receipt = ReceiptStore(project).parsed_by_id(receipt_id)
    if receipt is None or receipt.status != "completed":
        raise TableError(
            "invalid_input_ref", "Source must be a completed cluster receipt"
        )
    try:
        terminal = ACTION_REGISTRY.get(receipt.action_kind).definition.run
    except KeyError as exc:
        raise TableError(
            "invalid_input_ref", "Cluster producer is not registered"
        ) from exc
    if not isinstance(terminal, _ProjectAction) or terminal.capabilities != (
        ValueClusterer,
    ):
        raise TableError(
            "invalid_input_ref", "Source was not produced by a value clusterer"
        )
    try:
        run = project.db.execute(
            "SELECT runs.action_kind,runs.status,runs.op_id,ops.spec "
            "FROM runs JOIN ops ON ops.id=runs.op_id WHERE runs.id=?",
            (receipt.run_id,),
        ).fetchone()
        facts = [
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "value_clusters"
        ]
        if (
            run is None
            or run["action_kind"] != receipt.action_kind
            or run["status"] != "completed"
            or run["op_id"] not in receipt.op_ids
            or not _receipt_ops_are_applied(project, receipt.op_ids)
            or len(facts) != 1
        ):
            raise ValueError("Cluster publication identity changed")
        fact = facts[0]
        provenance = json.loads(run["spec"] or "{}")
        persisted = (
            provenance.get("value_clusters_result")
            if isinstance(provenance, dict)
            else None
        )
        if persisted != fact or fact.get("run_id") != receipt.run_id:
            raise ValueError(
                "Cluster receipt differs from the actual capability result"
            )
        source, output = fact["source"], fact["output"]
        sheet_id, column_id, row_ids = (
            source["sheet_id"],
            source["column_id"],
            source["row_ids"],
        )
        if (
            type(sheet_id) is not int
            or type(column_id) is not int
            or not isinstance(row_ids, list)
            or any(type(row) is not int or row <= 0 for row in row_ids)
            or project.visible_row_ids(sheet_id) != row_ids
            or project.db.execute(
                "SELECT 1 FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
            ).fetchone()
            is None
        ):
            raise ValueError("Cluster source rows changed")
        column = project.db.execute(
            "SELECT name,type FROM columns WHERE id=? AND sheet_id=? AND hidden=0",
            (column_id, sheet_id),
        ).fetchone()
        if column is None or any(
            column[key] != source[key] for key in ("name", "type")
        ):
            raise ValueError("Cluster source column changed")
        if (
            output_column_value_hash(
                project, sheet_id=sheet_id, column_id=column_id, row_ids=row_ids
            )
            != source["value_hash"]
        ):
            raise ValueError("Cluster source values changed")
        published = [
            item
            for item in receipt.outputs
            if item.ref.get("kind") == "map_result_column"
            and item.ref.get("column_id") == output["column_id"]
        ]
        if len(published) != 1 or any(
            published[0].ref.get(key) != value
            for key, value in {
                **output,
                "sheet_id": sheet_id,
                "run_id": receipt.run_id,
            }.items()
        ):
            raise ValueError("Cluster output association changed")
        if error := output_columns_replay_error(
            project,
            receipt.model_copy(update={"outputs": published}),
            output_kind="map_result_column",
            action_kind=receipt.action_kind,
            scope="expected",
            expected_row_ids=row_ids,
            allow_empty_rows=True,
            compare_declared_format=True,
        ):
            raise ValueError(error.message)
        values = project.get_values(sheet_id, output["column_id"])
        if fact["canonical_values"] != [
            {"row_id": row, "value": values.get(row)} for row in row_ids
        ]:
            raise ValueError("Published canonical values changed")
    except (KeyError, TypeError, ValueError) as exc:
        raise TableError("stale_replay", str(exc)) from exc
    return receipt, fact


def validate_cluster_receipt_fence(project: Any, fact: dict[str, Any]) -> None:
    receipt_id = fact.get("receipt_id")
    if not isinstance(receipt_id, str):
        raise TableError("stale_replay", "The admitted cluster receipt is missing")
    try:
        receipt, published = _published_clusters(project, receipt_id)
    except TableError as exc:
        raise TableError(
            "stale_replay", "The admitted cluster receipt or source values changed"
        ) from exc
    if canonical_json_hash(receipt.model_dump(mode="json")) != fact.get("receipt_hash"):
        raise TableError(
            "stale_replay", "The admitted cluster receipt or source values changed"
        )
    source = published["source"]
    if (
        any(fact.get(key) != source.get(key) for key in ("sheet_id", "column_id"))
        or fact.get("source_value_hash") != source.get("value_hash")
        or fact.get("column_name") != source.get("name")
    ):
        raise TableError(
            "stale_replay", "The admitted cluster source reference changed"
        )


class AdmittedClusterReceiptReader:
    def __init__(self, project: Any):
        self.project = project
        self.sources: set[RowSource] = set()
        self.source_roles: dict[RowSource, str] = {}
        self.parent_sheet_id: int | None = None
        self.facts: list[dict[str, Any]] = []

    def read(self, source: ClusterReceiptSource) -> tuple[EntityCluster, ...]:
        if not isinstance(source, ClusterReceiptSource):
            raise TableError("invalid_input_ref", "Expected a cluster receipt source")
        context = (
            nullcontext(self.project)
            if self.project.db.in_transaction
            else self.project.read_snapshot()
        )
        with context as project:
            receipt, published = _published_clusters(project, source.receipt_id)
            column = published["source"]
            sheet_id, column_id = column["sheet_id"], column["column_id"]
            if self.parent_sheet_id not in (None, sheet_id):
                raise TableError(
                    "invalid_input_ref", "Cluster receipt source references disagree"
                )
            values = project.get_values(sheet_id, column_id)
            groups = published.get("clusters")
            if not isinstance(groups, list):
                raise TableError("invalid_input_ref", "Cluster receipt lacks groups")
            result = []
            admitted = {}
            seen = set()
            for group in groups:
                if not isinstance(group, dict):
                    raise TableError("invalid_input_ref", "Invalid cluster group")
                key, canonical, size = (
                    group.get("key"),
                    group.get("canonical"),
                    group.get("size"),
                )
                row_ids, variants = group.get("row_ids"), group.get("values")
                if (
                    not isinstance(key, str)
                    or key in seen
                    or not isinstance(canonical, str)
                    or type(size) is not int
                    or size < 0
                    or not isinstance(row_ids, list)
                    or any(
                        type(row) is not int or row <= 0 or row not in values
                        for row in row_ids
                    )
                    or len(set(row_ids)) != len(row_ids)
                    or len(row_ids) != size
                    or not isinstance(variants, list)
                ):
                    raise TableError("invalid_input_ref", "Invalid cluster membership")
                seen.add(key)
                prepared_variants = []
                seen_variants = set()
                for variant in variants:
                    if not isinstance(variant, dict):
                        raise TableError("invalid_input_ref", "Invalid cluster variant")
                    text, count = variant.get("value"), variant.get("count")
                    if (
                        not isinstance(text, str)
                        or text in seen_variants
                        or type(count) is not int
                        or count < 0
                    ):
                        raise TableError("invalid_input_ref", "Invalid cluster variant")
                    seen_variants.add(text)
                    members = tuple(
                        row for row in row_ids if str(values[row]).strip() == text
                    )
                    if len(members) != count:
                        raise TableError(
                            "stale_replay",
                            "Source variant rows no longer match the cluster receipt",
                        )
                    prepared_variants.append(
                        EntityVariant(value=text, count=count, row_ids=members)
                    )
                if sum(variant.count for variant in prepared_variants) != size:
                    raise TableError(
                        "stale_replay",
                        "Source mention count no longer matches the cluster receipt",
                    )
                tokens = tuple(
                    admitted.setdefault(row, RowSource(sheet_id, row))
                    for row in row_ids
                )
                result.append(
                    EntityCluster(
                        key, canonical, tuple(prepared_variants), size, tokens
                    )
                )
            self.parent_sheet_id = sheet_id
            self.sources.update(admitted.values())
            self.source_roles.update(
                {token: "aggregate_source" for token in admitted.values()}
            )
            self.facts.append(
                {
                    "kind": "cluster_receipt_source",
                    "receipt_id": receipt.receipt_id,
                    "receipt_hash": canonical_json_hash(
                        receipt.model_dump(mode="json")
                    ),
                    "sheet_id": sheet_id,
                    "column_id": column_id,
                    "column_name": column.get("name"),
                    "source_value_hash": column.get("value_hash"),
                }
            )
            return tuple(result)
