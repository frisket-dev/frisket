"""Shared OpenAPI metadata for v1 action HTTP transports."""

from __future__ import annotations

from typing import Any


V1_IMPORT_CSV_UPLOAD_BRIDGE_TASK = "v1-http-import-csv-upload-bridge"
V1_IMPORT_XLSX_UPLOAD_BRIDGE_TASK = "v1-import-xlsx-action-and-http-bridge"
V1_IMPORT_PDF_UPLOAD_BRIDGE_TASK = "v1-import-pdf-action-and-http-bridge"
V1_IMPORT_FILES_UPLOAD_BRIDGE_TASK = "v1-http-import-files-upload-bridge"
V1_IMPORT_URLS_BRIDGE_TASK = "v1-import-urls-action-and-http-bridge"


def v1_action_transport(
    task_id: str, action_kind: str, transport: str
) -> dict[str, Any]:
    return {
        "openapi_extra": {
            "x-frisket-v1-transport": {
                "state": "v1_product_transport",
                "transport": transport,
                "action_kind": action_kind,
                "v1_task": task_id,
                "canonical_action_route": (
                    f"/api/projects/{{pid}}/actions/v1/run#{action_kind}"
                ),
            }
        },
    }
