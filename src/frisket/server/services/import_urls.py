"""URL import bridge service for local server routes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from frisket.contracts.action import Receipt as V1Receipt
from fastapi import Request

from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.store.receipts import ReceiptStore
from frisket.server.services.action_runs import v1_action_result_http_status
from frisket.server.workspace import Workspace
from frisket.server.route_errors import RouteError
from frisket.server.services.import_tabular import resolve_import_sheet_name
from frisket.server.services.import_uploads import await_thread_worker


@dataclass(frozen=True)
class ImportUrlsResponse:
    status_code: int
    payload: dict[str, Any]
    force_json_response: bool = False


class ImportUrlsRouteError(RouteError):
    pass


class ImportUrlsService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    async def import_urls(
        self,
        project_id: str,
        *,
        request: Request,
        urls: list[str],
        sheet_name: str | None,
        column: str,
    ) -> ImportUrlsResponse:
        if not urls:
            raise ImportUrlsRouteError(400, "no urls")
        project = self._workspace.get(project_id)
        executor_deps = (
            self._workspace.executor_deps_factory(project_id, request)
            if self._workspace.executor_deps_factory is not None
            else ExecutorDeps()
        )
        executor_deps = executor_deps or ExecutorDeps()
        limits = executor_deps.url_import_limits
        max_urls = limits.max_urls if limits is not None else None
        if max_urls is not None and len(urls) > max_urls:
            raise ImportUrlsRouteError(
                400,
                f"URL count exceeds the deployment limit of {max_urls}",
            )

        name = sheet_name or "downloads"
        normalized_urls = [str(raw_url or "").strip() for raw_url in urls]
        idempotency_basis = json.dumps(
            {
                "urls": normalized_urls,
                "sheet_name": name,
                "column": column,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        idempotency_hash = hashlib.sha256(idempotency_basis.encode("utf-8")).hexdigest()
        request_key = f"http-import-urls@sha256:{idempotency_hash}"
        final_name = resolve_import_sheet_name(
            project,
            request_key=request_key,
            action_kind="import.urls",
            requested_name=name,
        )
        result = await await_thread_worker(
            run_action_spec,
            project,
            {
                "action_id": "import.urls",
                "scope": {"kind": "project"},
                "sheet_name": final_name,
                "output_names": {"media": column},
                "params": {"urls": normalized_urls},
                "idempotency_key": request_key,
            },
            project_id=project_id,
            router=self._workspace.router_for(project),
            deps=executor_deps,
        )
        if result.status != "completed":
            return ImportUrlsResponse(
                status_code=v1_action_result_http_status(result),
                payload=result.model_dump(mode="json"),
                force_json_response=True,
            )

        sheet_output = next(
            (
                output
                for output in result.outputs
                if output.kind == "sheet" and output.sheet_id is not None
            ),
            None,
        )
        rows_output = next(
            (output for output in result.outputs if output.kind == "rows"),
            None,
        )
        if sheet_output is None or rows_output is None:
            raise ImportUrlsRouteError(
                500, "import.urls did not return sheet and row outputs"
            )
        receipt_body = ReceiptStore(project).body_by_id(str(result.receipt_id))
        if receipt_body is None:
            raise ImportUrlsRouteError(500, "import.urls did not write a receipt")
        receipt = V1Receipt.model_validate(json.loads(receipt_body))
        input_ref = next(
            (item.ref for item in receipt.inputs if item.ref.get("kind") == "url_list"),
            {},
        )
        row_count = len(rows_output.row_ids or [])
        downloaded = int(input_ref.get("downloaded") or 0)
        failed = int(input_ref.get("failed") or 0)
        return ImportUrlsResponse(
            status_code=200,
            payload={
                "sheet_id": sheet_output.sheet_id,
                "rows": row_count,
                "downloaded": downloaded,
                "failed": failed,
            },
        )
