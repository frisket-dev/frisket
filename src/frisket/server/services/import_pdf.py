"""PDF upload bridge service for local server routes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from frisket.engine.executor import BoundLocalFile, ExecutorDeps, run_action_spec
from frisket.engine.executor.import_sources import bind_import_sources
from frisket.server.services.action_runs import v1_action_result_http_status
from frisket.server.services.import_uploads import (
    AdmittedUpload,
    await_thread_worker,
    upload_sheet_name,
)
from frisket.server.workspace import Workspace
from frisket.server.route_errors import RouteError


@dataclass(frozen=True)
class ImportPdfUploadResponse:
    status_code: int
    payload: dict[str, Any]
    force_json_response: bool = False


class ImportPdfRouteError(RouteError):
    pass


class ImportPdfUploadService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def executor_deps_for_request(self, project_id: str, request: Any) -> ExecutorDeps:
        """Resolve the deployment composition once at the HTTP boundary."""

        factory = self._workspace.executor_deps_factory
        return (
            factory(project_id, request) if factory is not None else None
        ) or ExecutorDeps()

    async def upload_pdf(
        self,
        project_id: str,
        *,
        upload: AdmittedUpload,
        sheet_name: str | None,
        dpi: int,
        deps: ExecutorDeps | None = None,
    ) -> ImportPdfUploadResponse:
        project = self._workspace.get(project_id)
        src_name = upload.filename
        name = sheet_name or src_name.rsplit(".", 1)[0]
        digest = upload.sha256
        upload_dir = self._workspace.root / ".v1_import_uploads" / project_id / "pdf"
        source_path = upload_dir / f"{digest}.pdf"

        idempotency_basis = json.dumps(
            {
                "filename": src_name,
                "sha256": digest,
                "sheet_name": sheet_name,
                "dpi": dpi,
                "render_pages": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        idempotency_hash = hashlib.sha256(idempotency_basis.encode("utf-8")).hexdigest()
        request_key = f"http-import-pdf@sha256:{idempotency_hash}"
        final_name = upload_sheet_name(
            project, "import.pdf", request_key, name, allocate=sheet_name is None
        )
        executor_deps = deps or ExecutorDeps()
        admitted_path = str(source_path.absolute())
        held_sources = {}
        held_sources[admitted_path] = BoundLocalFile(
            stream=upload.source, sha256=f"sha256:{digest}"
        )
        result = await await_thread_worker(
            run_action_spec,
            project,
            {
                "action_id": "import.pdf",
                "scope": {"kind": "project"},
                "sheet_name": final_name,
                "params": {
                    "source": {
                        "kind": "file",
                        "path": admitted_path,
                        "label": src_name,
                    },
                    "dpi": dpi,
                    "render_pages": True,
                },
                "output_names": {},
                "idempotency_key": request_key,
            },
            project_id=project_id,
            router=self._workspace.router_for(project),
            deps=bind_import_sources(executor_deps, local_files=held_sources),
        )
        if result.status != "completed":
            return ImportPdfUploadResponse(
                status_code=v1_action_result_http_status(result),
                payload=result.model_dump(mode="json"),
                force_json_response=True,
            )

        sheet_output = next(
            (
                output
                for output in result.outputs
                if output.ref.get("kind") == "materialized_sheet"
                and output.sheet_id is not None
            ),
            None,
        )
        if sheet_output is None:
            raise ImportPdfRouteError(500, "import.pdf did not return a sheet output")
        columns = [
            output.name
            for output in result.outputs
            if output.kind == "column" and output.name is not None
        ]
        pages = sheet_output.ref.get("row_count")
        if not isinstance(pages, int) or isinstance(pages, bool) or pages < 0:
            raise ImportPdfRouteError(500, "import.pdf did not return a page count")
        return ImportPdfUploadResponse(
            status_code=200,
            payload={
                "sheet_id": sheet_output.sheet_id,
                "rows": pages,
                "pages": pages,
                "columns": columns,
            },
        )
