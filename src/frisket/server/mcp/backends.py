"""Backends for the frisket MCP tools: one tool surface, two bindings.

- :class:`LocalBackend` — canonical action-service calls against a workspace
  directory. It reuses :class:`frisket.server.workspace.Workspace` and
  :class:`frisket.server.services.action_runs.ActionRunService`, the same
  ingress the HTTP API is built on, so there is no business-logic fork and
  `frisket mcp <dir>` needs no running server.
- :class:`HostedBackend` — a thin httpx client over the hosted /api/*
  surface, authenticated with an org-scoped PAT
  (`Authorization: Bearer frisket_pat_*`). All caps/guards/auth are the
  hosted server's; this class only maps tool calls to endpoints and
  normalizes responses to the same shapes LocalBackend returns.

Cost-gate contract (both backends): a gated run NEVER surfaces as an
exception blob. It becomes a structured ``needs_confirmation`` response
carrying the complete estimate, gate details, and ``promise_set_hash``.
``estimate`` is USD: the figure this deployment would BILL where a pricing
policy has rated the run. ``estimate: null`` means the price is UNKNOWN —
the model is not in pricing_data.json, the deployment's pricing policy
declined to price the run, or an old/malformed envelope carried no complete
rating. An unrated provider figure never becomes a billed quote. A retry must
echo the exact hash; a bare confirmation boolean is not consent to whatever
scope happens to exist at retry time.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

import httpx

from frisket.ai.llm import ModelRouter
from frisket.engine.jobs import Worker
from frisket.engine.runner.confirmation_context import quoted_usd
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace

# mirror GET /sheets/{id}/data's server-side cap — sheets can be 100k rows,
# a tool call must never return one wholesale
READ_SHEET_MAX_LIMIT = 1000

_ACTION_SCHEMA_VERSION = "frisket.action.v2"
_ACTION_RESULT_SCHEMA_VERSION = "frisket.action_result.v1"
_ACTION_IN_FLIGHT_STATUSES = {"queued", "running"}
_CONFIRMATION_ERROR_CODES = {
    "model_cost_requires_confirmation",
    "external_cost_requires_confirmation",
}


def cost_gate_response(
    estimate: Any,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    promise_set_hash: str | None = None,
    retry_tool: str = "run_action",
) -> dict:
    """The structured needs_confirmation shape both backends emit for a
    cost-gated run.

    Action results carry the full estimate mapping inside error details while
    the local runner also exposes its scalar ``cost`` for compatibility. Keep
    both: automation can continue reading ``estimate`` as USD, while a human
    approval surface can show the exact rows/rate/claims it is authorizing.
    ``estimate=None`` means UNKNOWN price, not zero.

    The scalar is the BILLED figure — ``quoted_usd``, the same three-state
    selector every other money gate compares and quotes with. Reading the
    envelope's ``cost`` here published the PROVIDER's number to the agent
    under a deployment tariff, and published a confident number for a price
    the policy explicitly declined to give.
    """
    gate_details = dict(details or {})
    estimate_payload = gate_details.get("estimate", estimate)
    estimate_details: dict[str, Any] | None
    if isinstance(estimate_payload, dict):
        estimate_details = dict(estimate_payload)
        try:
            estimate_usd = quoted_usd(estimate_details)
        except (TypeError, ValueError):
            # An envelope whose money facts do not project (a hosted server on
            # a shape this build cannot read, a ``ConsentQuoteRefused``) is
            # UNKNOWN, which gates exactly as an unpriced run does. Never a
            # confident figure, and never an exception blob: this surface's
            # contract is that a gated run always returns needs_confirmation.
            estimate_usd = None
    else:
        estimate_details = None
        estimate_usd = estimate_payload
    if "estimate" not in gate_details:
        gate_details["estimate"] = estimate_details or estimate_usd
    promise_hash = promise_set_hash or gate_details.get("promise_set_hash")
    if promise_hash is not None:
        gate_details["promise_set_hash"] = promise_hash
    return {
        "status": "needs_confirmation",
        "run_id": None,
        "estimate": estimate_usd,
        "estimate_known": estimate_usd is not None,
        "estimate_details": estimate_details,
        "details": gate_details,
        "promise_set_hash": promise_hash,
        "message": message,
        "hint": (
            f"call {retry_tool} again with confirmed=true and "
            "consented_promise_set_hash set to this response's "
            "promise_set_hash; do not change anything else about the call"
        ),
    }


def _clamp_page(offset: int, limit: int) -> tuple[int, int]:
    return max(0, offset), max(0, min(limit, READ_SHEET_MAX_LIMIT))


def _mcp_idempotency_key(action: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in action.items()
        if key not in {"idempotency_key", "confirmation"}
    }
    params = dict(payload.get("params") or {})
    params.pop("confirmed", None)
    params.pop("consented_promise_set_hash", None)
    payload["params"] = params
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    action_id = action.get("action_id", action.get("kind", "action"))
    return f"mcp.run_action.{action_id}@sha256:{digest}"


def _action_payload(
    action: dict,
    *,
    confirmed: bool,
    consented_promise_set_hash: str | None,
) -> dict[str, Any]:
    action_id = action.get("action_id")
    if isinstance(action_id, str):
        from frisket.actions.system import typed_action_for_request

        prepared = dict(action)
        # These are invocation authority supplied by the MCP tool call, never
        # reusable intent accepted from the caller's opaque action object.
        prepared.pop("confirmation", None)
        if confirmed and consented_promise_set_hash is not None:
            prepared["confirmation"] = consented_promise_set_hash
        if not prepared.get("idempotency_key"):
            prepared["idempotency_key"] = _mcp_idempotency_key(prepared)
        # Bind the request to its sole registry owner here. Project-bound
        # semantic references remain the action service's responsibility.
        try:
            typed_action_for_request(prepared)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid_action_request: {exc}") from exc
        return prepared

    if action.get("schema_version") != _ACTION_SCHEMA_VERSION:
        raise ValueError(
            "MCP run_action requires schema_version="
            f"{_ACTION_SCHEMA_VERSION!r}; missing and unknown versions are refused"
        )
    raise ValueError(f"unknown canonical action kind: {action.get('kind')!r}")


def _refuse_queued_action_job(action: dict[str, Any]) -> None:
    """Fail before either MCP backend dispatches work it cannot observe."""

    kind = action.get("action_id", action.get("kind"))
    if not isinstance(kind, str):
        return
    from frisket.engine.executor.action_dispatch import placement_for_kind
    from frisket.engine.executor.action_specs import PlacementPolicy

    if placement_for_kind(kind) is PlacementPolicy.QUEUED_ACTION_JOB:
        raise ValueError(
            "queued_action_job_not_supported: MCP run_action cannot dispatch "
            f"{kind!r} because this tool has no action-job receipt polling "
            "contract; use the HTTP/browser surface"
        )


def _cost_gate_from_action_result(
    body: dict[str, Any], *, retry_tool: str = "run_action"
) -> dict:
    errors = body.get("errors") or []
    error = errors[0] if errors else {}
    details = error.get("details") or {}
    return cost_gate_response(
        details.get("estimate"),
        error.get("message", ""),
        details=details,
        retry_tool=retry_tool,
    )


def _action_result_needs_confirmation(body: dict[str, Any]) -> bool:
    if body.get("status") == "needs_confirmation":
        return True
    return any(
        error.get("code") in _CONFIRMATION_ERROR_CODES
        for error in body.get("errors") or []
    )


def _action_result_from_response_body(body: object) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        return None
    if body.get("schema_version") == _ACTION_RESULT_SCHEMA_VERSION:
        return body
    detail = body.get("detail")
    if isinstance(detail, dict):
        return _action_result_from_response_body(detail)
    if isinstance(detail, str):
        try:
            parsed = json.loads(detail)
        except ValueError:
            return None
        return _action_result_from_response_body(parsed)
    return None


def _has_terminal_receipt(body: dict[str, Any]) -> bool:
    receipt_id = body.get("receipt_id")
    return (
        body.get("run_id") is None
        and body.get("status") in {"completed", "partial", "failed", "cancelled"}
        and isinstance(receipt_id, str)
        and bool(receipt_id)
    )


def _started_from_action_result(body: dict[str, Any]) -> dict:
    status = body.get("status")
    run_id = body.get("run_id")
    if run_id is None:
        if status == "completed" or _has_terminal_receipt(body):
            return {
                "status": status,
                "done": True,
                "receipt_id": body.get("receipt_id"),
                "outputs": body.get("outputs") or [],
                "value": body.get("value"),
                "errors": body.get("errors") or [],
                "warnings": body.get("warnings") or [],
            }
        raise ValueError("frisket API action result did not include a run_id")
    return {
        "status": "started",
        "run_id": run_id,
        "done": status not in _ACTION_IN_FLIGHT_STATUSES,
    }


def _backfill_action_payload(
    sheet_id: int,
    column: str,
    row_ids: list[int] | None,
    *,
    confirmed: bool,
    consented_promise_set_hash: str | None,
) -> dict[str, Any]:
    """The canonical typed run.backfill request both backends dispatch.

    Deliberately NOT run_action's content-hash idempotency key: identical
    backfill params at two different times create different scoped generations
    (the target-row scope is server-derived and moves as rows fail or arrive), and
    a reused key would replay the FIRST invocation's receipt as a silent
    no-op instead of doing the new work. A fresh key per tool call matches
    the web client's per-attempt key. The challenge->confirm pair needs no
    shared key — a gated attempt persists nothing, and consent binds through
    the echoed promise_set_hash, never the key.
    """
    scope: dict[str, Any] = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = list(row_ids)
    # The hash is its own explicit tool parameter, exactly like run_action's:
    # It authorizes execution only when the caller explicitly approves it.
    return {
        "action_id": "run.backfill",
        "scope": scope,
        "params": {"column": column},
        "output_names": {},
        "idempotency_key": f"mcp.backfill_run:{uuid.uuid4().hex}",
        **(
            {"confirmation": consented_promise_set_hash}
            if confirmed and consented_promise_set_hash is not None
            else {}
        ),
    }


def _backfill_tool_response(body: dict[str, Any]) -> dict:
    """Normalize a run.backfill action result to the tool's answer shape.

    requested_row_ids are the rows the fresh successor generation actually ran
    (the only rows that could bill); filled_row_ids are the subset that now
    holds a durable result. Every other cell keeps its exact published head."""
    if _action_result_needs_confirmation(body):
        return _cost_gate_from_action_result(body, retry_tool="backfill_run")
    ref = next(
        (
            dict(output.get("ref") or {})
            for output in body.get("outputs") or []
            if output.get("kind") == "run_backfill"
        ),
        {},
    )
    requested = [int(row_id) for row_id in ref.get("requested_row_ids") or []]
    filled = [int(row_id) for row_id in ref.get("filled_row_ids") or []]
    run_id = body.get("run_id") or ref.get("run_id")
    response: dict[str, Any] = {
        "status": body.get("status"),
        "run_id": run_id,
        "receipt_id": body.get("receipt_id"),
        "requested_row_ids": requested,
        "filled_row_ids": filled,
        "filled": int(ref.get("filled", len(filled))),
    }
    errors = [
        {
            "code": error.get("code"),
            "message": error.get("message"),
            "field": error.get("field"),
        }
        for error in body.get("errors") or []
    ]
    if errors:
        response["errors"] = errors
        response["message"] = errors[0]["message"]
    else:
        response["message"] = (
            f"created scoped run {run_id}: ran {len(requested)} row(s) and "
            f"filled {len(filled)}; every other cell kept its published "
            "generation and was not re-bought"
        )
    return response


class LocalBackend:
    """Direct binding: the MCP process owns the workspace (stdio, keyless)."""

    def __init__(self, root: str | Path, router: ModelRouter | None = None):
        self.ws = Workspace(Path(root), router)
        self._tasks: set[asyncio.Task[Any]] = set()

    def list_projects(self) -> list[dict]:
        return self.ws.list()

    def list_sheets(self, project_id: str) -> list[dict]:
        from frisket.server.services.projects import ProjectLifecycleService

        return ProjectLifecycleService(self.ws).list_sheets(project_id)

    def read_sheet(
        self, project_id: str, sheet_id: int, offset: int = 0, limit: int = 100
    ) -> dict:
        p = self.ws.get(project_id)
        offset, limit = _clamp_page(offset, limit)
        cols = p.columns(sheet_id)
        if not cols:
            raise ValueError(f"no sheet '{sheet_id}' in project '{project_id}'")
        rows = p.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? AND hidden=0 ORDER BY position LIMIT ? OFFSET ?",
            (sheet_id, limit, offset),
        ).fetchall()
        row_ids = [r["id"] for r in rows]
        cells: dict[int, dict[str, Any]] = {rid: {} for rid in row_ids}
        for c in cols:
            vals = p.get_values(sheet_id, c["id"], row_ids=row_ids)
            for rid in row_ids:
                cells[rid][c["name"]] = vals.get(rid)
        return {
            "sheet_id": sheet_id,
            "total": p.row_count(sheet_id),
            "offset": offset,
            "limit": limit,
            "columns": [
                {
                    "id": c["id"],
                    "name": c["name"],
                    "type": c["type"],
                    "ai_generated": bool(c["ai_generated"]),
                }
                for c in cols
            ],
            "rows": [{"id": rid, "cells": cells[rid]} for rid in row_ids],
        }

    def search(self, project_id: str, query: str, limit: int = 20) -> list[dict]:
        from frisket.search import search_project

        return search_project(self.ws.get(project_id), query, limit=limit)

    async def run_action(
        self,
        project_id: str,
        action: dict,
        confirmed: bool = False,
        consented_promise_set_hash: str | None = None,
    ) -> dict:
        try:
            prepared = _action_payload(
                action,
                confirmed=confirmed,
                consented_promise_set_hash=consented_promise_set_hash,
            )
        except ValueError as exc:
            raise ValueError(f"invalid_action_spec: {exc}") from exc
        _refuse_queued_action_job(prepared)
        from frisket.engine.executor.queued_actions import queued_v1_action_request

        service = ActionRunService(self.ws)
        if queued_v1_action_request(prepared) is not None:
            # Queue publication is synchronous and contains no provider
            # egress. Keeping it on the workspace-owning thread also avoids
            # handing the local queue's SQLite connection to a second thread.
            response = service.run_action(project_id, prepared)
        else:
            response = await asyncio.to_thread(
                service.run_action,
                project_id,
                prepared,
            )
        body = response.payload
        if _action_result_needs_confirmation(body):
            return _cost_gate_from_action_result(body)
        if response.status_code >= 400 and not _has_terminal_receipt(body):
            errors = body.get("errors") or []
            error = errors[0] if errors else {}
            code = str(error.get("code") or "invalid_action_spec")
            message = str(error.get("message") or "the action spec was refused")
            raise ValueError(f"{code}: {message}")
        started = _started_from_action_result(body)
        if body.get("status") == "queued":
            worker = Worker(self.ws.queue, self.ws.registry)
            task = asyncio.create_task(
                asyncio.to_thread(worker.run_forever, drain=True)
            )
            self._tasks.add(task)
            task.add_done_callback(self._reap)
        return started

    def _reap(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def backfill_run(
        self,
        project_id: str,
        sheet_id: int,
        column: str,
        row_ids: list[int] | None = None,
        confirmed: bool = False,
        consented_promise_set_hash: str | None = None,
    ) -> dict:
        from frisket.engine.executor import run_action_spec

        p = self.ws.get(project_id)
        action = _backfill_action_payload(
            sheet_id,
            column,
            row_ids,
            confirmed=confirmed,
            consented_promise_set_hash=consented_promise_set_hash,
        )
        # The same executor entry the HTTP route dispatches run.backfill
        # through (ActionRunService.run_action -> run_action_spec), so the
        # source-selection/guard/gate logic has exactly one implementation. Like
        # run_action's ActionRunService path above, its default runner factory
        # builds the canonical attempt authority. The direct backfill executor
        # blocks until the scoped successor finishes, so it runs on a worker thread —
        # Project db handles are per-thread, and the HTTP server runs this same
        # call in FastAPI's threadpool.
        result = await asyncio.to_thread(
            run_action_spec,
            p,
            action,
            project_id=project_id,
            router=self.ws.router_for(p),
        )
        return _backfill_tool_response(result.model_dump(mode="json"))

    def get_run_status(self, project_id: str, run_id: int) -> dict:
        p = self.ws.get(project_id)
        row = p.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id} in project '{project_id}'")
        prog = self.ws.active_runs.get((project_id, run_id))
        return {
            "run_id": run_id,
            "status": row["status"],
            "total": row["total_rows"],
            "completed": row["completed_rows"],
            "failed": row["failed_rows"],
            "cost": row["cost_actual"],
            "live": (row["status"] == "running" and prog is not None and not prog.done),
        }


class HostedBackend:
    """Thin client binding: every tool call is an /api/* request with a PAT."""

    def __init__(
        self,
        base_url: str | None = None,
        pat: str | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        if client is not None:
            self._client = client
            return
        base_url = base_url or os.environ.get("FRISKET_BASE_URL")
        pat = pat or os.environ.get("FRISKET_PAT")
        if not base_url or not pat:
            raise ValueError(
                "hosted mode needs FRISKET_BASE_URL and FRISKET_PAT "
                "(an org-scoped frisket_pat_* token) in the environment"
            )
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {pat}"},
            timeout=30.0,
        )

    async def _json(self, resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise ValueError(f"frisket API error {resp.status_code}: {detail}")
        return resp.json()

    async def list_projects(self) -> list[dict]:
        return await self._json(await self._client.get("/api/projects"))

    async def list_sheets(self, project_id: str) -> list[dict]:
        return await self._json(
            await self._client.get(f"/api/projects/{project_id}/sheets")
        )

    async def read_sheet(
        self, project_id: str, sheet_id: int, offset: int = 0, limit: int = 100
    ) -> dict:
        offset, limit = _clamp_page(offset, limit)
        data = await self._json(
            await self._client.get(
                f"/api/projects/{project_id}/sheets/{sheet_id}/data",
                params={"offset": offset, "limit": limit},
            )
        )
        # the grid endpoint keys cells by column ID; remap to names so both
        # backends hand the model the same shape
        names = {str(c["id"]): c["name"] for c in data["columns"]}
        return {
            "sheet_id": sheet_id,
            "total": data["total"],
            "offset": offset,
            "limit": limit,
            "columns": [
                {
                    "id": c["id"],
                    "name": c["name"],
                    "type": c["type"],
                    "ai_generated": bool(c["ai_generated"]),
                }
                for c in data["columns"]
            ],
            "rows": [
                {
                    "id": r["id"],
                    "cells": {
                        names[cid]: v for cid, v in r["cells"].items() if cid in names
                    },
                }
                for r in data["rows"]
            ],
        }

    async def search(self, project_id: str, query: str, limit: int = 20) -> list[dict]:
        return await self._json(
            await self._client.get(
                f"/api/projects/{project_id}/search",
                params={"q": query, "limit": limit},
            )
        )

    async def run_action(
        self,
        project_id: str,
        action: dict,
        confirmed: bool = False,
        consented_promise_set_hash: str | None = None,
    ) -> dict:
        prepared = _action_payload(
            action,
            confirmed=confirmed,
            consented_promise_set_hash=consented_promise_set_hash,
        )
        _refuse_queued_action_job(prepared)
        resp = await self._client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json=prepared,
        )
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = None
            action_result = _action_result_from_response_body(body)
            if action_result is not None and _action_result_needs_confirmation(
                action_result
            ):
                return _cost_gate_from_action_result(action_result)
            if action_result is not None and _has_terminal_receipt(action_result):
                return _started_from_action_result(action_result)
            if (
                resp.status_code == 402
                and isinstance(body, dict)
                and ("estimate" in body or "message" in body)
            ):
                raw_details = body.get("details")
                return cost_gate_response(
                    body.get("estimate"),
                    body.get("message", ""),
                    details=raw_details if isinstance(raw_details, dict) else body,
                    promise_set_hash=body.get("promise_set_hash"),
                )
            await self._json(resp)
        body = await self._json(resp)
        if body.get("schema_version") == _ACTION_RESULT_SCHEMA_VERSION:
            if _action_result_needs_confirmation(body):
                return _cost_gate_from_action_result(body)
            return _started_from_action_result(body)
        return {
            "status": "started",
            "run_id": body["run_id"],
            "done": bool(body.get("done")),
        }

    async def backfill_run(
        self,
        project_id: str,
        sheet_id: int,
        column: str,
        row_ids: list[int] | None = None,
        confirmed: bool = False,
        consented_promise_set_hash: str | None = None,
    ) -> dict:
        resp = await self._client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json=_backfill_action_payload(
                sheet_id,
                column,
                row_ids,
                confirmed=confirmed,
                consented_promise_set_hash=consented_promise_set_hash,
            ),
        )
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = None
            action_result = _action_result_from_response_body(body)
            if action_result is not None:
                # Both the 402 cost gate and a named refusal (e.g.
                # mixed_origin_column_unsupported) arrive as action
                # results; surface them structurally, exactly like the local
                # binding, instead of as an opaque exception blob.
                return _backfill_tool_response(action_result)
            await self._json(resp)
        return _backfill_tool_response(await self._json(resp))

    async def get_run_status(self, project_id: str, run_id: int) -> dict:
        body = await self._json(
            await self._client.get(
                f"/api/projects/{project_id}/actions/runs/{run_id}/status"
            )
        )
        return body["run"]["public_status"]
