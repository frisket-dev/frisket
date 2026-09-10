"""Upload admission for the enabled bundled FtM action, not another importer."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from frisket.engine.executor import BoundLocalFile
from frisket.server.services.action_runs import ActionRunResponse, ActionRunService
from frisket.server.services.import_uploads import AdmittedUpload
from frisket.server.workspace import Workspace


class FollowTheMoneyUploadService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def upload(
        self,
        project_id: str,
        *,
        upload: AdmittedUpload,
        dataset_name: str | None,
        request_context: Any = None,
    ) -> ActionRunResponse:
        sha256 = upload.sha256
        # A stable admission label, never created or reopened as a filesystem path.
        source_path = str(
            (
                self._workspace.root
                / ".v1_import_uploads"
                / project_id
                / "followthemoney"
                / sha256
            ).absolute()
        )
        params = {"source_path": source_path, "dataset_name": dataset_name}
        identity = json.dumps(params, sort_keys=True, separators=(",", ":"))
        return ActionRunService(self._workspace).run_action(
            project_id,
            {
                "action_id": "frisket.ftm.ftm_import",
                "scope": {"kind": "project"},
                "params": params,
                "idempotency_key": "http-import-ftm@sha256:"
                + hashlib.sha256(identity.encode()).hexdigest(),
            },
            request_context=request_context,
            admitted_sources={
                source_path: BoundLocalFile(upload.source, "sha256:" + sha256)
            },
        )
