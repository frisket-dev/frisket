"""Files upload bridge service for local server routes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from frisket.engine.executor import BoundLocalFile, ExecutorDeps, run_action_spec
from frisket.engine.executor.import_sources import bind_import_sources
from frisket.server.services.action_runs import v1_action_result_http_status
from frisket.server.services.import_uploads import AdmittedUpload, upload_sheet_name
from frisket.server.workspace import Workspace
from frisket.server.route_errors import RouteError


@dataclass(frozen=True)
class ImportFilesUploadResponse:
    status_code: int
    payload: dict[str, Any]
    force_json_response: bool = False


class ImportFilesRouteError(RouteError):
    pass


class ImportFilesUploadService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def executor_deps_for_request(self, project_id: str, request: Any) -> ExecutorDeps:
        """Resolve the deployment composition once at the HTTP boundary."""

        factory = self._workspace.executor_deps_factory
        return (
            factory(project_id, request) if factory is not None else None
        ) or ExecutorDeps()

    def upload_files(
        self,
        project_id: str,
        *,
        files: list[AdmittedUpload],
        sheet_name: str | None,
        deps: ExecutorDeps | None = None,
    ) -> ImportFilesUploadResponse:
        project = self._workspace.get(project_id)
        name = sheet_name or "files"
        executor_deps = deps or ExecutorDeps()
        upload_dir = self._workspace.root / ".v1_import_uploads" / project_id / "files"
        held_sources = {}
        sources: list[dict[str, Any]] = []
        idempotency_files: list[dict[str, Any]] = []
        for index, upload in enumerate(files):
            digest = upload.sha256
            # This stable admission key is not reopened. Repeated upload
            # requests must not incorporate a disposable temporary filename.
            source_path = str((upload_dir / str(index) / digest).absolute())
            held_sources[source_path] = BoundLocalFile(
                stream=upload.source, sha256=f"sha256:{digest}"
            )
            sources.append(
                {
                    "path": source_path,
                    "filename": upload.filename,
                    "mime": upload.mime,
                }
            )
            idempotency_files.append(
                {
                    "filename": upload.filename,
                    "mime": upload.mime,
                    "sha256": digest,
                    "size": upload.size,
                }
            )

        idempotency_basis = json.dumps(
            {
                "files": idempotency_files,
                "sheet_name": sheet_name,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        idempotency_hash = hashlib.sha256(idempotency_basis.encode("utf-8")).hexdigest()
        request_key = f"http-import-files@sha256:{idempotency_hash}"
        final_name = upload_sheet_name(
            project, "import.files", request_key, name, allocate=sheet_name is None
        )
        result = run_action_spec(
            project,
            {
                "action_id": "import.files",
                "scope": {"kind": "project"},
                "sheet_name": final_name,
                "params": {"files": sources},
                "output_names": {},
                "idempotency_key": request_key,
            },
            project_id=project_id,
            router=self._workspace.router_for(project),
            deps=bind_import_sources(executor_deps, local_files=held_sources),
        )
        if result.status != "completed":
            return ImportFilesUploadResponse(
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
            raise ImportFilesRouteError(
                500, "import.files did not return a sheet output"
            )
        return ImportFilesUploadResponse(
            status_code=200,
            payload={"sheet_id": sheet_output.sheet_id, "rows": len(files)},
        )
