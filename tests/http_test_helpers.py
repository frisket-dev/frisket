from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.engine.jobs import Worker
from frisket.engine.jobs.runs import register_project_run_handler
from frisket.engine.jobs.worker import HandlerRegistry
from frisket.engine.store.media_blobs import (
    media_cell,
    owned_media_metadata_document,
)
from frisket.execution.pricing_policy import (
    EstimateFacts,
    RatedEstimate,
    install_pricing_policy,
)
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
from frisket.sdk.ops import transcribe_engines

ACTION_SCHEMA_VERSION = "frisket.action.v2"


def connected_client_receive() -> Callable[[], Awaitable[dict[str, Any]]]:
    """ASGI ``receive`` for a client that sends one empty request and stays connected.

    A server's ``receive`` blocks once the request is delivered. File responses
    watch it for a disconnect while they send, so a fake that returns at once
    never lets the transfer run.
    """
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            await asyncio.Event().wait()
        delivered = True
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive


def drain_queue(client: TestClient, *, worker_id: str = "v1-http-test-worker") -> None:
    ws = client.app.state.workspace
    worker = Worker(ws.queue, ws.registry, worker_id=worker_id, poll_interval=0.01)
    while worker.run_once():
        pass


def run_worker_until_job_terminal(
    worker: Worker,
    queue: Any,
    job_id: int,
    *,
    max_steps: int = 16,
) -> Any:
    """Run bounded queue quanta until one exact job reaches a terminal state."""

    observed: list[str] = []
    for _ in range(max_steps):
        target = queue.get(job_id)
        if target is None:
            raise AssertionError(f"target job {job_id} disappeared")
        observed.append(str(target.status))
        if target.status in {"done", "failed", "cancelled"}:
            return target
        if not worker.run_once():
            raise AssertionError(
                f"worker became idle before target job {job_id} terminalized; "
                f"observed={observed}"
            )
    target = queue.get(job_id)
    raise AssertionError(
        f"target job {job_id} did not terminalize in {max_steps} worker steps; "
        f"status={None if target is None else target.status!r}; observed={observed}"
    )


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
        # Preserve an explicit echo for deployments that install a tariff.
        # The local downloader itself has no provider purchase.
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
    expected_change_id: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if kind not in {"change.undo", "change.redo"}:
        raise ValueError(f"unsupported operation action kind: {kind!r}")
    if idempotency_key is None:
        idempotency_key = f"test-{kind}@sha256:{uuid.uuid4().hex}"
    params: dict[str, Any] = {}
    if expected_change_id is not None:
        params["expected_change_id"] = expected_change_id
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
    expected_change_id: int | None = None,
    idempotency_key: str | None = None,
):
    return client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=operation_step_action(
            kind,
            expected_change_id=expected_change_id,
            idempotency_key=idempotency_key,
        ),
    )


def post_operation_undo_as_v1_action(
    client: TestClient,
    project_id: str,
    *,
    expected_change_id: int | None = None,
    idempotency_key: str | None = None,
):
    return post_operation_step_as_v1_action(
        client,
        project_id,
        "change.undo",
        expected_change_id=expected_change_id,
        idempotency_key=idempotency_key,
    )


def post_operation_redo_as_v1_action(
    client: TestClient,
    project_id: str,
    *,
    expected_change_id: int | None = None,
    idempotency_key: str | None = None,
):
    return post_operation_step_as_v1_action(
        client,
        project_id,
        "change.redo",
        expected_change_id=expected_change_id,
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


class AboveLimitPricingPolicy:
    """A deployment tariff that makes routed audio-consent pins billable."""

    policy_id = "test.r70.above-limit.v1"

    def rate(self, facts: EstimateFacts) -> RatedEstimate:
        return RatedEstimate(
            billed_cost=2_000_001,
            provider_cost=facts.provider_cost,
            lane="cost_plus",
            policy_id=self.policy_id,
        )


def install_audio_consent_gateway_env(monkeypatch: Any) -> None:
    """Configure a claim-bearing models-gateway environment with billable pricing."""
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")
    install_pricing_policy(AboveLimitPricingPolicy())


def stub_audio_transcription_engine(monkeypatch: Any) -> list[str]:
    """Stub the transcription engine boundary while keeping execution routing real."""
    calls: list[str] = []

    async def fake_run_engine(
        engine, path, spec, ctx, *, should_cancel=None, transport=None, media=None
    ):
        calls.append(engine)
        return transcribe_engines.TranscriptionEngineResult(
            output={
                "text": "stubbed transcript",
                "segments": [],
                "language": None,
                "cost": 0.0,
            },
            model_calls=(),
        )

    monkeypatch.setattr(transcribe_engines, "run_transcription_engine", fake_run_engine)
    return calls


def seed_audio_project(
    client: TestClient, *, name: str = "behavioral pin"
) -> tuple[str, Any, int]:
    pid = client.post("/api/projects", json={"name": name}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("S")
    col = project.add_column(sheet_id, "media", type="audio")
    blob = project.add_blob(
        b"RIFFxxxxWAVEfmt ",
        filename="a.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 2.0, "kind": "audio"}
        ),
    )
    project.add_rows(
        sheet_id,
        [{"media": media_cell(blob, mime="audio/wav", filename="a.wav")}],
        {"media": col},
    )
    drain_queue(client, worker_id="r70-setup")
    return pid, project, sheet_id


def add_audio_row(
    project: Any, sheet_id: int, *, duration_seconds: float = 2.0
) -> None:
    col = next(c for c in project.columns(sheet_id) if c["name"] == "media")
    name = f"b{duration_seconds}.wav"
    blob = project.add_blob(
        b"RIFFyyyyWAVEfmt " + str(duration_seconds).encode(),
        filename=name,
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": duration_seconds, "kind": "audio"}
        ),
    )
    project.add_rows(
        sheet_id,
        [{"media": media_cell(blob, mime="audio/wav", filename=name)}],
        {"media": col["id"]},
    )


def transcribe_action(sheet_id: int, *, key: str) -> dict[str, Any]:
    return {
        "action_id": "media.transcribe",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "output_names": {"text": "transcript", "segments": "transcript_segments"},
        "params": {
            "source": "media",
            "engine": "moss",
        },
        "idempotency_key": key,
    }


def launch_consented_audio_run(
    client: TestClient, pid: str, project: Any, sheet_id: int, *, key: str
) -> tuple[int, str]:
    """Post an unconfirmed transcribe action, echo the exact 402 hash, and queue it."""
    action = transcribe_action(sheet_id, key=key)
    gated = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=action,
    )
    assert gated.status_code == 402, gated.text
    body = gated.json()
    assert body["status"] == "needs_confirmation"
    details = body["errors"][0]["details"]
    assert {claim["field"] for claim in details["claims"]} == {"egress_class"}, details[
        "claims"
    ]
    set_hash = details["promise_set_hash"]
    assert set_hash

    action["confirmation"] = set_hash
    queued = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=action,
    )
    assert queued.status_code == 200, queued.text
    launched = queued.json()
    assert launched["status"] == "queued"
    receipt_id = launched["receipt_id"]
    assert receipt_id
    run_id = int(
        project.db.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()[
            "id"
        ]
    )
    return run_id, receipt_id


def drain_one_project_run_job(client: TestClient, *, worker_id: str) -> bool:
    """Drain one queued ``project.run`` job through the real worker handler."""
    registry = HandlerRegistry()
    register_project_run_handler(
        registry,
        workspace_root=client.app.state.workspace.root,
        router=ModelRouter(model_call_policy=LOCAL_PURCHASE_POLICY),
    )
    worker = Worker(client.app.state.workspace.queue, registry, worker_id=worker_id)
    return worker.run_once()


def run_row(project: Any, run_id: int) -> Any:
    return project.db.execute(
        "SELECT status, params FROM runs WHERE id=?", (run_id,)
    ).fetchone()
