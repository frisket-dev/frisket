"""Shared executor helpers for feedable record-set outputs."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any
from frisket.authoring.json_schema_paths import (
    named_result_schema_for_path as named_result_schema_for_path,
)


DERIVE_TABLE_FROM_LIST = "derive.table_from_list"


@dataclass(frozen=True)
class NamedResultCapability:
    may_feed: frozenset[str]


FEEDABLE_NAMED_RESULT_PRODUCERS: dict[str, NamedResultCapability] = {
    "map.python": NamedResultCapability(may_feed=frozenset({DERIVE_TABLE_FROM_LIST})),
    "map.extract": NamedResultCapability(may_feed=frozenset({DERIVE_TABLE_FROM_LIST})),
    "media.extract_pdf_tables": NamedResultCapability(
        may_feed=frozenset({DERIVE_TABLE_FROM_LIST})
    ),
    "media.extract_faces": NamedResultCapability(
        may_feed=frozenset({DERIVE_TABLE_FROM_LIST})
    ),
    "media.video_frames": NamedResultCapability(
        may_feed=frozenset({DERIVE_TABLE_FROM_LIST})
    ),
    "map.ner": NamedResultCapability(may_feed=frozenset({DERIVE_TABLE_FROM_LIST})),
}


def json_schema_equal(left: Any, right: Any) -> bool:
    try:
        return _json_stable(left) == _json_stable(right)
    except (TypeError, ValueError):
        return False


def feedable_named_result_ref(
    *,
    source_action_kind: str,
    sheet_id: int,
    column_id: int,
    run_id: int,
    op_id: int,
    route: str,
    schema_name: str,
    schema_json: dict[str, Any],
    row_ids: list[int],
    may_feed: list[str] | tuple[str, ...],
    path: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "kind": "named_result",
        "sheet_id": sheet_id,
        "column_id": column_id,
        "run_id": run_id,
        "op_id": op_id,
        "route": route,
        "schema": schema_name,
        "schema_json": copy.deepcopy(schema_json),
        "item_schema": copy.deepcopy(_item_schema_for(schema_json)),
        "source_action_kind": source_action_kind,
        "row_ids": [int(row_id) for row_id in row_ids],
        "may_feed": list(may_feed),
    }
    if path is not None:
        ref["path"] = path
    if extra:
        ref.update(copy.deepcopy(extra))
    return ref


def is_feedable_named_result_ref(
    ref: dict[str, Any],
    *,
    receipt_action_kind: str,
    sheet_id: int,
    column_id: int,
    run_id: int,
    op_id: int,
    route: str,
    schema_name: str,
    target: str = DERIVE_TABLE_FROM_LIST,
    producer_spec: dict[str, Any] | None = None,
    producer_params_hash: str | None = None,
) -> bool:
    capability = FEEDABLE_NAMED_RESULT_PRODUCERS.get(receipt_action_kind)
    if capability is None:
        from frisket.actions.registry import ACTION_REGISTRY

        try:
            producer = ACTION_REGISTRY.get(receipt_action_kind)
        except KeyError:
            producer = None
        fields = (
            getattr(producer.definition.run, "output_fields", ()) if producer else ()
        )
        if producer is not None and producer_spec is not None:
            from frisket.actions.core import has_dynamic_outputs
            from frisket.engine.executor.map_rows_action import (
                bound_typed_program_request_from_runner_spec,
                typed_request_hash,
            )

            if has_dynamic_outputs(producer.definition.run):
                # Model receipts deliberately omit semantic Params. Rebind the
                # private run spec, then require the receipt's admitted identity
                # before trusting a Params-dependent declaration or rename.
                try:
                    bound = bound_typed_program_request_from_runner_spec(producer_spec)
                    if (
                        bound is None
                        or bound.action.action_id != receipt_action_kind
                        or typed_request_hash(bound) != producer_params_hash
                    ):
                        return False
                    fields = tuple(
                        field
                        for field in bound.output_fields or ()
                        if field.materialized_name(bound.request.output_names) == route
                    )
                except (TypeError, ValueError):
                    return False
        for field in fields:
            declaration = field.named_result
            if (
                declaration is not None
                and ref.get("output_key") == field.key
                and schema_name == declaration.schema_name
                and json_schema_equal(ref.get("schema_json"), dict(field.schema))
            ):
                capability = NamedResultCapability(frozenset(declaration.may_feed))
                break
    if capability is None and schema_name == "pdf_table_rows":
        from frisket.actions.pdf_table_types import PdfTablesReader
        from frisket.actions.registry import ACTION_REGISTRY

        try:
            producer = ACTION_REGISTRY.get(receipt_action_kind)
        except KeyError:
            producer = None
        if producer is not None and PdfTablesReader in getattr(
            producer.definition.run, "capabilities", ()
        ):
            capability = NamedResultCapability(frozenset({DERIVE_TABLE_FROM_LIST}))
    if capability is None and schema_name in {"frames", "faces"}:
        from frisket.actions.row_media_types import FrameExtractor, FaceExtractor
        from frisket.actions.registry import ACTION_REGISTRY

        try:
            producer = ACTION_REGISTRY.get(receipt_action_kind)
        except KeyError:
            producer = None
        required = FrameExtractor if schema_name == "frames" else FaceExtractor
        if producer is not None and required in getattr(
            producer.definition.run, "capabilities", ()
        ):
            capability = NamedResultCapability(frozenset({DERIVE_TABLE_FROM_LIST}))
    if capability is None or target not in capability.may_feed:
        return False
    if target not in (ref.get("may_feed") or []):
        return False
    if ref.get("source_action_kind") != receipt_action_kind:
        return False
    if (
        ref.get("kind") != "named_result"
        or ref.get("sheet_id") != sheet_id
        or ref.get("column_id") != column_id
        or ref.get("run_id") != run_id
        or ref.get("op_id") != op_id
        or ref.get("route") != route
        or ref.get("schema") != schema_name
    ):
        return False
    schema_json = ref.get("schema_json")
    item_schema = ref.get("item_schema")
    if not isinstance(schema_json, dict) or schema_json.get("type") != "array":
        return False
    if not isinstance(item_schema, dict):
        return False
    if not json_schema_equal(item_schema, _item_schema_for(schema_json)):
        return False
    row_ids = ref.get("row_ids")
    if not isinstance(row_ids, list) or any(
        not isinstance(row_id, int) for row_id in row_ids
    ):
        return False
    return True


def _item_schema_for(schema_json: dict[str, Any]) -> dict[str, Any]:
    item_schema = schema_json.get("items")
    if not isinstance(item_schema, dict):
        return {}
    projected = copy.deepcopy(item_schema)
    reference = projected.get("$ref")
    definitions = schema_json.get("$defs", {})
    if (
        isinstance(definitions, dict)
        and isinstance(reference, str)
        and reference.startswith("#/$defs/")
    ):
        definition = definitions.get(reference.removeprefix("#/$defs/"))
        if isinstance(definition, dict):
            projected = {**copy.deepcopy(definition), **projected}
            del projected["$ref"]
    if "$defs" in schema_json:
        projected["$defs"] = copy.deepcopy(schema_json["$defs"])
    return projected


def _json_stable(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
