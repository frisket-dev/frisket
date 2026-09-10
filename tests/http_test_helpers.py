from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from fastapi.testclient import TestClient

from frisket.engine.jobs import Worker

ACTION_SCHEMA_VERSION = "frisket.action.v2"


def drain_queue(client: TestClient, *, worker_id: str = "v1-http-test-worker") -> None:
    ws = client.app.state.workspace
    worker = Worker(ws.queue, ws.registry, worker_id=worker_id, poll_interval=0.01)
    while worker.run_once():
        pass


def _copy_fields(source: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: source[field] for field in fields if field in source}


def v1_action_from_canonical_run_spec(
    spec: dict[str, Any],
    *,
    confirmed: bool = True,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if "recipe" in spec:
        raise ValueError("canonical action helper refuses recipe-shaped specs")
    kind = spec.get("action_kind")
    if not isinstance(kind, str) or "." not in kind or kind != kind.strip():
        raise ValueError("canonical action helper requires a clean dotted action_kind")
    row_scope: dict[str, Any] | None = None
    typed_scope: dict[str, Any] | None = None
    if kind == "map.classify":
        capabilities = ["project:write", "model:complete"]
        params = _copy_fields(
            spec,
            (
                "sheet_id",
                "input_columns",
                "input_template",
                "model",
                "context",
                "fields",
                "include_justification",
                "include_confidence",
                "row_ids",
            ),
        )
        params["confirmed"] = confirmed
    elif kind == "map.summarize":
        params = _copy_fields(
            spec,
            (
                "model",
                "preset",
                "instruction",
                "context",
            ),
        )
        params["source"] = (
            {"text": spec["input_template"]}
            if spec.get("input_template")
            else list(spec.get("input_columns") or [])
        )
        typed_scope = {"kind": "sheet_rows", "sheet_id": int(spec["sheet_id"])}
        if spec.get("row_ids") is not None:
            typed_scope["row_ids"] = list(spec["row_ids"])
    elif kind == "map.template":
        params = {"template": {"text": spec["template"]}}
        typed_scope = {"kind": "sheet_rows", "sheet_id": int(spec["sheet_id"])}
        if spec.get("row_ids") is not None:
            typed_scope["row_ids"] = list(spec["row_ids"])
    elif kind == "map.regex_extract":
        params = _copy_fields(
            spec,
            (
                "input_columns",
                "pattern",
                "all_matches",
                "group",
                "timeout_seconds",
            ),
        )
        typed_scope = {"kind": "sheet_rows", "sheet_id": int(spec["sheet_id"])}
        if spec.get("row_ids") is not None:
            typed_scope["row_ids"] = list(spec["row_ids"])
    elif kind == "media.ytdlp_download":
        capabilities = ["project:write", "external:media_download"]
        params = _copy_fields(
            spec,
            (
                "sheet_id",
                "input_columns",
                "row_ids",
                "media_type",
                "output_name",
                "format_selector",
            ),
        )
        # media.ytdlp_download is UNPRICEABLE (CostPolicy kind="unknown"): a
        # row-supplied media URL has no readable meter, so every run gates on
        # an unknown cost and the launch carries the consent.
        params["confirmed"] = confirmed
    elif kind == "map.python":
        output_name = str(spec.get("output_name") or "python_result")
        output_type = str(spec.get("output_type") or "text")
        params = {
            **_copy_fields(spec, ("input_columns", "code")),
            "return_schema": {
                "type": "string" if output_type == "text" else output_type
            },
            "output_routes": [
                {
                    "name": output_name,
                    "path": "$",
                    "target": {
                        "kind": "column",
                        "type": output_type,
                    },
                }
            ],
        }
        typed_scope = {"kind": "sheet_rows", "sheet_id": int(spec["sheet_id"])}
        if spec.get("row_ids") is not None:
            typed_scope["row_ids"] = list(spec["row_ids"])
    else:
        raise ValueError(f"unsupported action_kind for v1 action helper: {kind!r}")

    if idempotency_key is None:
        encoded = json.dumps(
            {"kind": kind, "params": params, "nonce": uuid.uuid4().hex},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        idempotency_key = f"test-{kind}@sha256:{digest}"
    if typed_scope is not None:
        logical_output = {
            "map.template": "rendered",
            "map.summarize": "summary",
            "map.python": str(spec.get("output_name") or "python_result"),
        }.get(kind, "extracted")
        return {
            "action_id": kind,
            "scope": typed_scope,
            "params": params,
            "output_names": {
                logical_output: str(spec.get("output_name") or logical_output)
            },
            "idempotency_key": idempotency_key,
        }
    action = {
        "schema_version": ACTION_SCHEMA_VERSION,
        "kind": kind,
        "capabilities": capabilities,
        "params": params,
        "idempotency_key": idempotency_key,
    }
    if row_scope is not None:
        action["row_scope"] = row_scope
    return action


def post_canonical_run_spec_as_v1_action(
    client: TestClient,
    project_id: str,
    spec: dict[str, Any],
    *,
    confirmed: bool = True,
    idempotency_key: str | None = None,
):
    # ``confirmed=True`` is a convenience for tests that are about the run,
    # not the confirmation protocol.  It must still exercise the real two-step
    # protocol: first obtain the server-authored exact context hash, then echo
    # that hash with the same action/idempotency identity.  A bare boolean is
    # deliberately never manufactured as consent here.
    action = v1_action_from_canonical_run_spec(
        spec,
        confirmed=False,
        idempotency_key=idempotency_key,
    )
    if confirmed:
        return post_v1_action_with_exact_confirmation(client, project_id, action)
    return client.post(f"/api/projects/{project_id}/actions/v1/run", json=action)


def queued_python_run_spec(
    sheet_id: int,
    input_column: str,
    output_name: str,
    *,
    row_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Small deterministic queued action for queue/liveness integration tests."""

    spec: dict[str, Any] = {
        "action_kind": "map.python",
        "sheet_id": sheet_id,
        "input_columns": [input_column],
        "code": f"result = row[{input_column!r}]",
        "output_name": output_name,
        "output_type": "text",
    }
    if row_ids is not None:
        spec["row_ids"] = row_ids
    return spec


def confirmation_hash_from_action_result(body: dict[str, Any]) -> str | None:
    """Return the exact confirmation echo from a v1 action 402, if present."""

    errors = body.get("errors")
    if not isinstance(errors, list) or not errors:
        return None
    first = errors[0]
    if not isinstance(first, dict):
        return None
    details = first.get("details")
    if not isinstance(details, dict):
        return None
    value = details.get("promise_set_hash")
    return value if isinstance(value, str) and value else None


def post_v1_action_with_exact_confirmation(
    client: TestClient,
    project_id: str,
    action: dict[str, Any],
):
    """Post an action, then answer an exact 402 challenge when one exists.

    This is for integration tests whose subject is downstream execution. Tests
    of the gate itself should continue making an explicit one-shot request.
    """

    challenge_action = {
        **action,
        "params": dict(action.get("params") or {}),
    }
    if "confirmed" in challenge_action["params"]:
        challenge_action["params"]["confirmed"] = False
    challenge_action["params"].pop("consented_promise_set_hash", None)
    challenge_action.pop("confirmation", None)
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=challenge_action,
    )
    if response.status_code != 402:
        return response
    promise_set_hash = confirmation_hash_from_action_result(response.json())
    if promise_set_hash is None:
        # Do not turn a non-exact legacy challenge into blanket consent.  The
        # caller gets the original refusal and can fix the missing protocol.
        return response
    if "action_id" in challenge_action:
        retry_action = {**challenge_action, "confirmation": promise_set_hash}
    else:
        retry_action = {
            **challenge_action,
            "params": {
                **challenge_action["params"],
                "confirmed": True,
                "consented_promise_set_hash": promise_set_hash,
            },
        }
    return client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=retry_action,
    )


def map_extract_action(
    sheet_id: int,
    fields: list[dict[str, Any]],
    *,
    input_columns: list[str] | None = None,
    model: str = "anthropic/claude-haiku-4-5",
    instruction: str = "",
    confirmed: bool = True,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    params = {
        "sheet_id": sheet_id,
        "input_columns": list(input_columns or []),
        "model": model,
        "instruction": instruction,
        "fields": list(fields),
        "confirmed": confirmed,
    }
    if idempotency_key is None:
        idempotency_key = f"test-map.extract@sha256:{uuid.uuid4().hex}"
    return {
        "schema_version": ACTION_SCHEMA_VERSION,
        "kind": "map.extract",
        "capabilities": ["project:write", "model:complete"],
        "params": params,
        "idempotency_key": idempotency_key,
    }


def post_map_extract_as_v1_action(
    client: TestClient,
    project_id: str,
    sheet_id: int,
    fields: list[dict[str, Any]],
    *,
    input_columns: list[str] | None = None,
    model: str = "anthropic/claude-haiku-4-5",
    instruction: str = "",
    confirmed: bool = True,
    idempotency_key: str | None = None,
):
    return client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=map_extract_action(
            sheet_id,
            fields,
            input_columns=input_columns,
            model=model,
            instruction=instruction,
            confirmed=confirmed,
            idempotency_key=idempotency_key,
        ),
    )


def named_result_source_from_output(output: dict[str, Any]) -> dict[str, Any]:
    ref = output.get("ref") or {}
    route = str(ref.get("route") or output.get("name") or "")
    schema = str(ref.get("schema") or f"{route}_list")
    return {
        "kind": "named_result",
        "sheet_id": int(ref.get("sheet_id") or output["sheet_id"]),
        "column_id": int(ref.get("column_id") or output["column_id"]),
        "run_id": int(ref["run_id"]),
        "route": route,
        "schema": schema,
    }


def derive_table_from_list_action(
    source: dict[str, Any],
    *,
    target_sheet_name: str,
    item_schema: dict[str, Any],
    columns: list[dict[str, Any]],
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    params = {
        "source": dict(source),
        "item_schema": dict(item_schema),
        "columns": list(columns),
    }
    if idempotency_key is None:
        idempotency_key = f"test-derive.table_from_list@sha256:{uuid.uuid4().hex}"
    return {
        "action_id": "derive.table_from_list",
        "scope": {"kind": "project"},
        "sheet_name": target_sheet_name,
        "params": params,
        "idempotency_key": idempotency_key,
    }


def post_derive_table_from_list_as_v1_action(
    client: TestClient,
    project_id: str,
    source: dict[str, Any],
    *,
    target_sheet_name: str,
    item_schema: dict[str, Any],
    columns: list[dict[str, Any]],
    idempotency_key: str | None = None,
):
    return client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=derive_table_from_list_action(
            source,
            target_sheet_name=target_sheet_name,
            item_schema=item_schema,
            columns=columns,
            idempotency_key=idempotency_key,
        ),
    )


def row_add_action(
    sheet_id: int,
    cells: dict[str, Any] | None = None,
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    params = {
        "sheet_id": sheet_id,
        "cells": dict(cells or {}),
    }
    if idempotency_key is None:
        idempotency_key = f"test-row.add@sha256:{uuid.uuid4().hex}"
    return {
        "action_id": "row.add",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": idempotency_key,
    }


def post_row_add_as_v1_action(
    client: TestClient,
    project_id: str,
    sheet_id: int,
    cells: dict[str, Any] | None = None,
    *,
    idempotency_key: str | None = None,
):
    return client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=row_add_action(
            sheet_id,
            cells,
            idempotency_key=idempotency_key,
        ),
    )


def column_add_action(
    sheet_id: int,
    name: str,
    *,
    type: str = "text",
    position: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "sheet_id": sheet_id,
        "name": name,
        "type": type,
    }
    if position is not None:
        params["position"] = position
    if idempotency_key is None:
        idempotency_key = f"test-column.add@sha256:{uuid.uuid4().hex}"
    return {
        "action_id": "column.add",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": idempotency_key,
    }


def post_column_add_as_v1_action(
    client: TestClient,
    project_id: str,
    sheet_id: int,
    name: str,
    *,
    type: str = "text",
    position: int | None = None,
    idempotency_key: str | None = None,
):
    return client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=column_add_action(
            sheet_id,
            name,
            type=type,
            position=position,
            idempotency_key=idempotency_key,
        ),
    )


def cell_edit_action(
    edits: list[dict[str, Any]],
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if idempotency_key is None:
        idempotency_key = f"test-cell.edit@sha256:{uuid.uuid4().hex}"
    return {
        "action_id": "cell.edit",
        "scope": {"kind": "project"},
        "params": {"edits": list(edits)},
        "idempotency_key": idempotency_key,
    }


def post_cell_edit_as_v1_action(
    client: TestClient,
    project_id: str,
    edits: list[dict[str, Any]],
    *,
    idempotency_key: str | None = None,
):
    return client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=cell_edit_action(edits, idempotency_key=idempotency_key),
    )


def review_decision_action(
    *,
    run_id: int,
    row_id: int,
    column_id: int,
    decision: str,
    value: Any = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "run_id": run_id,
        "row_id": row_id,
        "column_id": column_id,
        "decision": decision,
    }
    if decision == "edit":
        params["value"] = value
    if idempotency_key is None:
        idempotency_key = f"test-review.decision@sha256:{uuid.uuid4().hex}"
    return {
        "action_id": "review.decision",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": idempotency_key,
    }


def post_review_decision_as_v1_action(
    client: TestClient,
    project_id: str,
    *,
    run_id: int,
    row_id: int,
    column_id: int,
    decision: str,
    value: Any = None,
    idempotency_key: str | None = None,
):
    return client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=review_decision_action(
            run_id=run_id,
            row_id=row_id,
            column_id=column_id,
            decision=decision,
            value=value,
            idempotency_key=idempotency_key,
        ),
    )


def column_patch_action(
    column_id: int,
    patch: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if idempotency_key is None:
        idempotency_key = f"test-column.patch@sha256:{uuid.uuid4().hex}"
    return {
        "action_id": "column.patch",
        "scope": {"kind": "project"},
        "params": {"column_id": column_id, "format": patch.get("format")},
        "idempotency_key": idempotency_key,
    }


def post_column_patch_as_v1_action(
    client: TestClient,
    project_id: str,
    column_id: int,
    patch: dict[str, Any],
    *,
    idempotency_key: str | None = None,
):
    return client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=column_patch_action(
            column_id,
            patch,
            idempotency_key=idempotency_key,
        ),
    )


def column_set_type_action(
    column_id: int,
    column_type: str,
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if idempotency_key is None:
        idempotency_key = f"test-column.set_type@sha256:{uuid.uuid4().hex}"
    return {
        "action_id": "column.set_type",
        "scope": {"kind": "project"},
        "params": {"column_id": column_id, "type": column_type},
        "idempotency_key": idempotency_key,
    }


def post_column_set_type_as_v1_action(
    client: TestClient,
    project_id: str,
    column_id: int,
    column_type: str,
    *,
    idempotency_key: str | None = None,
):
    return client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=column_set_type_action(
            column_id,
            column_type,
            idempotency_key=idempotency_key,
        ),
    )


def operation_step_action(
    kind: str,
    *,
    expected_op_id: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if kind not in {"operation.undo", "operation.redo"}:
        raise ValueError(f"unsupported operation action kind: {kind!r}")
    if idempotency_key is None:
        idempotency_key = f"test-{kind}@sha256:{uuid.uuid4().hex}"
    params: dict[str, Any] = {}
    if expected_op_id is not None:
        params["expected_op_id"] = expected_op_id
    return {
        "action_id": kind,
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": idempotency_key,
    }


def post_operation_step_as_v1_action(
    client: TestClient,
    project_id: str,
    kind: str,
    *,
    expected_op_id: int | None = None,
    idempotency_key: str | None = None,
):
    return client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=operation_step_action(
            kind,
            expected_op_id=expected_op_id,
            idempotency_key=idempotency_key,
        ),
    )


def post_operation_undo_as_v1_action(
    client: TestClient,
    project_id: str,
    *,
    expected_op_id: int | None = None,
    idempotency_key: str | None = None,
):
    return post_operation_step_as_v1_action(
        client,
        project_id,
        "operation.undo",
        expected_op_id=expected_op_id,
        idempotency_key=idempotency_key,
    )


def post_operation_redo_as_v1_action(
    client: TestClient,
    project_id: str,
    *,
    expected_op_id: int | None = None,
    idempotency_key: str | None = None,
):
    return post_operation_step_as_v1_action(
        client,
        project_id,
        "operation.redo",
        expected_op_id=expected_op_id,
        idempotency_key=idempotency_key,
    )


def cluster_values_action(
    sheet_id: int,
    column: str,
    *,
    min_size: int = 2,
    key_template: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "source": column,
        "method": "fingerprint",
        "min_size": min_size,
    }
    if key_template is not None:
        params["key_template"] = key_template
    if idempotency_key is None:
        idempotency_key = f"test-cluster.values@sha256:{uuid.uuid4().hex}"
    return {
        "action_id": "cluster.values",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "output_names": {"canonical": f"{column}_canonical"},
        "params": params,
        "idempotency_key": idempotency_key,
    }


def post_cluster_values_as_v1_action(
    client: TestClient,
    project_id: str,
    sheet_id: int,
    column: str,
    *,
    min_size: int = 2,
    key_template: str | None = None,
    idempotency_key: str | None = None,
):
    body = cluster_values_action(
        sheet_id,
        column,
        min_size=min_size,
        key_template=key_template,
        idempotency_key=idempotency_key,
    )
    path = f"/api/projects/{project_id}/actions/v1/run"
    response = client.post(path, json=body)
    if response.status_code == 200 and response.json()["status"] == "queued":
        drain_queue(client)
        return client.post(path, json=body)
    return response


def resolve_entities_action(
    source_receipt_id: str,
    *,
    target_sheet_name: str = "Entities",
    cluster_keys: list[str] | None = None,
    canonical_overrides: dict[str, str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "source": {"kind": "cluster_values", "receipt_id": source_receipt_id},
    }
    if cluster_keys is not None:
        params["cluster_keys"] = list(cluster_keys)
    if canonical_overrides is not None:
        params["canonical_overrides"] = dict(canonical_overrides)
    if idempotency_key is None:
        idempotency_key = f"test-resolve.entities@sha256:{uuid.uuid4().hex}"
    return {
        "action_id": "resolve.entities",
        "scope": {"kind": "project"},
        "sheet_name": target_sheet_name,
        "output_names": {
            key: key
            for key in ("entity", "key", "variants", "mentions", "source_variants")
        },
        "params": params,
        "idempotency_key": idempotency_key,
    }


def post_resolve_entities_as_v1_action(
    client: TestClient,
    project_id: str,
    source_receipt_id: str,
    *,
    target_sheet_name: str = "Entities",
    cluster_keys: list[str] | None = None,
    canonical_overrides: dict[str, str] | None = None,
    idempotency_key: str | None = None,
):
    return client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=resolve_entities_action(
            source_receipt_id,
            target_sheet_name=target_sheet_name,
            cluster_keys=cluster_keys,
            canonical_overrides=canonical_overrides,
            idempotency_key=idempotency_key,
        ),
    )


def join_semantic_action(
    sheet_id: int,
    target_sheet_id: int,
    *,
    input_columns: list[str] | None = None,
    target_column: str = "company",
    output_name: str = "semantic_match",
    child_sheet_name: str = "Semantic Links",
    match_threshold: float = 0.70,
    confident_threshold: float = 0.85,
    carry_columns: list[str] | None = None,
    confirmed: bool = True,
    consented_promise_set_hash: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    params = {
        "sheet_id": sheet_id,
        "input_columns": list(input_columns or ["donor"]),
        "target_sheet_id": target_sheet_id,
        "target_column": target_column,
        "output_name": output_name,
        "match_threshold": match_threshold,
        "confident_threshold": confident_threshold,
        "child_sheet_name": child_sheet_name,
        "confirmed": confirmed,
    }
    if carry_columns is not None:
        params["carry_columns"] = list(carry_columns)
    if consented_promise_set_hash is not None:
        params["consented_promise_set_hash"] = consented_promise_set_hash
    if idempotency_key is None:
        idempotency_key = f"test-join.semantic@sha256:{uuid.uuid4().hex}"
    return {
        "schema_version": ACTION_SCHEMA_VERSION,
        "kind": "join.semantic",
        "capabilities": ["project:write", "model:embed"],
        "params": params,
        "idempotency_key": idempotency_key,
    }


def post_join_semantic_as_v1_action(
    client: TestClient,
    project_id: str,
    sheet_id: int,
    target_sheet_id: int,
    *,
    input_columns: list[str] | None = None,
    target_column: str = "company",
    output_name: str = "semantic_match",
    child_sheet_name: str = "Semantic Links",
    match_threshold: float = 0.70,
    confident_threshold: float = 0.85,
    carry_columns: list[str] | None = None,
    confirmed: bool = True,
    consented_promise_set_hash: str | None = None,
    idempotency_key: str | None = None,
):
    return client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=join_semantic_action(
            sheet_id,
            target_sheet_id,
            input_columns=input_columns,
            target_column=target_column,
            output_name=output_name,
            child_sheet_name=child_sheet_name,
            match_threshold=match_threshold,
            confident_threshold=confident_threshold,
            carry_columns=carry_columns,
            confirmed=confirmed,
            consented_promise_set_hash=consented_promise_set_hash,
            idempotency_key=idempotency_key,
        ),
    )
